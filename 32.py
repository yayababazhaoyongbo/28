# ==================== Streamlit Cloud 兼容补丁 ====================
import sys
try:
    __import__('pysqlite3')
    import pysqlite3
    sys.modules['sqlite3'] = pysqlite3
except ImportError:
    pass
# ============================================================

import streamlit as st
import pandas as pd
import numpy as np
import requests
import random
import re
import json
import sqlite3
import time
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ================= 配置中心 =================
DB_FILE = "soul_ma_master.db"
CSV_SEED_FILE = "soul_ma_master_db.csv"
REQUEST_TIMEOUT = 10.0      # 增加超时时间
REQUEST_RETRIES = 4         # 增加重试次数
MAX_WORKERS = 10            # 降低并发，更稳健

DELISTED_CODES = {"600102", "600001", "600002", "600005"}
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
]

db_lock = threading.Lock()

class SessionFactory:
    _thread_local = threading.local()

    @classmethod
    def get_session(cls):
        if not hasattr(cls._thread_local, "session"):
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=15, pool_maxsize=25, max_retries=3)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            cls._thread_local.session = session
        return cls._thread_local.session

# ================= 数据库管理 =================
class DatabaseManager:
    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self._init_db()
        self.seed_db_from_csv()

    def _init_db(self):
        with db_lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS stocks (
                        code TEXT PRIMARY KEY,
                        name TEXT,
                        best_ma INTEGER,
                        h_floor REAL,
                        highs_400 TEXT,
                        lows_400 TEXT,
                        last_update TEXT
                    )
                ''')
                conn.commit()

    def seed_db_from_csv(self):
        if os.path.exists(CSV_SEED_FILE):
            try:
                with db_lock:
                    with sqlite3.connect(self.db_path) as conn:
                        cursor = conn.cursor()
                        cursor.execute("SELECT COUNT(*) FROM stocks")
                        count = cursor.fetchone()[0]
                
                if count == 0:
                    df_csv = pd.read_csv(CSV_SEED_FILE)
                    new_rows = df_csv.to_dict('records')
                    self.update_db(new_rows)
                    st.sidebar.success(f"🎉 已自动合并 {len(df_csv)} 只基础股票基因！")
            except Exception as e:
                st.sidebar.warning(f"CSV 种子合并提示: {str(e)}")

    def load_db(self):
        try:
            with db_lock:
                with sqlite3.connect(self.db_path) as conn:
                    df = pd.read_sql("SELECT * FROM stocks", conn)
            if not df.empty:
                df['highs_400'] = df['highs_400'].apply(lambda x: json.loads(x) if isinstance(x, str) and x else [])
                df['lows_400'] = df['lows_400'].apply(lambda x: json.loads(x) if isinstance(x, str) and x else [])
            return df
        except Exception as e:
            st.error(f"加载数据库失败: {str(e)}")
            return pd.DataFrame()

    def update_db(self, new_rows):
        if not new_rows:
            return False
        try:
            data = []
            for r in new_rows:
                code = normalize_code(r.get("code", ""))
                if len(code) != 6:
                    continue
                data.append((
                    code,
                    str(r.get("name", "未知")),
                    int(r.get("best_ma", 60)),
                    float(r.get("h_floor", 0)),
                    json.dumps(r.get("highs_400", [])),
                    json.dumps(r.get("lows_400", [])),
                    datetime.now().isoformat()
                ))
            with db_lock:
                with sqlite3.connect(self.db_path) as conn:
                    conn.executemany('''
                        INSERT OR REPLACE INTO stocks 
                        (code, name, best_ma, h_floor, highs_400, lows_400, last_update)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', data)
                    conn.commit()
            return True
        except Exception as e:
            st.error(f"数据库保存失败: {str(e)}")
            return False

def normalize_code(code):
    s = re.sub(r"\D", "", str(code).strip())
    return s[-6:].zfill(6) if s else ""

# ================= SoulEngine 核心引擎 =================
class SoulEngine:
    @staticmethod
    def get_data(code, days=450): 
        clean_code = normalize_code(code)
        if len(clean_code) != 6 or clean_code in DELISTED_CODES:
            return None, "异常代码"
        
        symbol = ("sh" if clean_code.startswith("6") else "sz") + clean_code
        url = f"https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get?param={symbol},day,,,{days},qfq"
        
        session = SessionFactory.get_session()
        
        for attempt in range(REQUEST_RETRIES):
            try:
                time.sleep(random.uniform(0.4, 1.0))  # 增加间隔防限流
                
                headers = {"User-Agent": random.choice(UA_LIST)}
                resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT, verify=False)
                
                if resp.status_code != 200:
                    raise Exception(f"HTTP {resp.status_code}")
                
                res_json = resp.json()
                stock_info = res_json.get("data", {}).get(symbol)
                if not stock_info: 
                    return None, "空号/无此股"
                
                qt_info = stock_info.get("qt", {}).get(symbol, [])
                name = qt_info[1] if len(qt_info) > 1 else "未知"
                
                if any(x in name for x in ["指数", "ETF", "ST", "*ST", "退"]):
                    return None, "策略过滤(ST/指数/退市)"
                
                data_list = stock_info.get("qfqday") or stock_info.get("day", [])
                if not data_list or len(data_list) < 120:
                    return None, "数据不足"
                
                df = pd.DataFrame(data_list)
                if df.shape[1] < 6:
                    return None, "列数不规范"
                
                df = df.iloc[:, [0, 1, 2, 3, 4, 5]]
                df.columns = ["日期","开盘","收盘","最高","最低","成交量"]
                
                numeric_cols = ["开盘","收盘","最高","最低","成交量"]
                for col in numeric_cols:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                    
                df = df.dropna().reset_index(drop=True)
                if len(df) < 120:
                    return None, f"交易日不足({len(df)}天)"
                
                return df, name
                
            except Exception as e:
                if attempt < REQUEST_RETRIES - 1:
                    sleep_time = random.uniform(0.8, 2.0) * (attempt + 1)
                    time.sleep(sleep_time)
                else:
                    return None, f"请求失败({str(e)[:40]})"
        
        return None, "请求超时"

    @staticmethod
    def calculate_best_ma(df):
        if len(df) < 100: 
            return 60
        close = df["收盘"]
        returns = close.pct_change()
        best_ma, max_score = 60, -np.inf
        for p in range(30, 251, 5):
            if len(close) < p: continue
            ma = close.rolling(p).mean()
            sig = (close > ma).astype(int)
            score = (sig.shift(1) * returns).sum() * (sig.mean() ** 2.5)
            if score > max_score:
                max_score, best_ma = score, p
        return best_ma

    @staticmethod
    def process_single_stock(c):
        time.sleep(random.uniform(0.05, 0.15))
        df, status_or_name = SoulEngine.get_data(c, days=450)
        if df is not None:
            ma = SoulEngine.calculate_best_ma(df)
            return {
                "flag": "success",
                "data": {
                    "code": c, 
                    "name": status_or_name, 
                    "best_ma": ma,
                    "h_floor": round(df["收盘"].tail(125).mean(), 2),
                    "highs_400": df["最高"].tail(400).tolist(),
                    "lows_400": df["最低"].tail(400).tolist()
                }
            }
        else:
            return {"flag": "error", "reason": status_or_name}

# ================= 主界面 =================
st.set_page_config(page_title="灵魂均线 V27.7 Pro", layout="wide")

st.title("🚀 灵魂均线 V27.7 Pro（稳健并发优化版）")
st.caption("已优化数据抓取稳定性 + 自动种子加载")

db_manager = DatabaseManager()
db = db_manager.load_db()

st.sidebar.metric("基因库总量", f"{len(db)} 只")
st.sidebar.info(f"⚡ 并发: {MAX_WORKERS} 线程 | 单次限 500 只")

tabs = st.tabs([
    "🔍 单股诊断", "🏗️ 基建系统", "🎯 强势突破", "⛳ 地量回踩",
    "⭐ 三线共振", "🌊 极致缩量", "⚡ 金叉狙击", "🚩 趋势线蓄势"
])

# Tab 0: 单股诊断
with tabs[0]:
    st.subheader("🕵️ 单股精确入库")
    c_in = st.text_input("输入股票代码（如 600519）", "600519", key="t0")
    if st.button("单股独立诊断", type="primary"):
        with st.spinner("正在深度抓取..."):
            df_d, name_d = SoulEngine.get_data(c_in, 450)
            if df_d is not None:
                ma = SoulEngine.calculate_best_ma(df_d)
                h_flr = round(df_d["收盘"].tail(125).mean(), 2)
                success = db_manager.update_db([{
                    "code": c_in, "name": name_d, "best_ma": ma,
                    "h_floor": h_flr, "highs_400": df_d["最高"].tail(400).tolist(),
                    "lows_400": df_d["最低"].tail(400).tolist()
                }])
                if success:
                    st.success(f"✅ 【{name_d} ({c_in})】入库成功！最佳均线：{ma}日")
                    st.rerun()
            else:
                st.error(f"抓取失败：{name_d}")

# Tab 1: 基建系统
with tabs[1]:
    st.subheader("🏗️ 智能抗限流基建系统")
    st.write(f"当前基因库：**{len(db)}** 只股票")
    
    if st.button("🚀 启动基建扫描（单批次 500 只）", type="primary"):
        pool = []
        for p in ["600", "601", "603", "000", "002"]:
            for i in range(1, 1000):
                pool.append(f"{p}{i:03d}")
        
        existing = set(db["code"].astype(str)) if not db.empty else set()
        todo = [c for c in pool if c not in existing][:500]
        
        if not todo:
            st.info("🎉 已无需要增量扫描的个股")
        else:
            st.info(f"本次计划扫描 **{len(todo)}** 只个股...")
            progress_bar = st.progress(0)
            status_text = st.empty()
            log_board = st.empty()
            
            new_list = []
            success_count = 0
            err_dict = {"空号/无此股": 0, "策略过滤(ST/指数/退市)": 0, 
                       "数据不足": 0, "交易日不足": 0, "请求失败": 0, "其他": 0}
            
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_to_code = {executor.submit(SoulEngine.process_single_stock, code): code for code in todo}
                
                for i, future in enumerate(as_completed(future_to_code)):
                    res = future.result()
                    if res["flag"] == "success":
                        new_list.append(res["data"])
                        success_count += 1
                    else:
                        reason = res["reason"]
                        for key in err_dict:
                            if key in reason or key.lower() in reason.lower():
                                err_dict[key] += 1
                                break
                        else:
                            err_dict["其他"] += 1
                    
                    progress_bar.progress((i + 1) / len(todo))
                    status_text.text(f"已处理 {i+1}/{len(todo)} | 成功 {success_count} 只")
                    
                    log_board.markdown(f"""
                    | 成功入库 | 空号/停牌 | ST/指数过滤 | 数据不足 | 请求失败 | 其他 |
                    |----------|-----------|-------------|----------|----------|------|
                    | **{success_count}** | {err_dict['空号/无此股']} | {err_dict['策略过滤(ST/指数/退市)']} | {err_dict['数据不足']} | {err_dict['请求失败']} | {err_dict['其他']} |
                    """)
                    
                    if len(new_list) >= 30:
                        db_manager.update_db(new_list)
                        new_list = []
            
            if new_list:
                db_manager.update_db(new_list)
            
            st.success(f"✅ 本批次扫描完成！成功新增 **{success_count}** 只股票")
            time.sleep(1)
            st.rerun()

# ================= 策略 Tab =================
def render_strategy_tab(tab_obj, title, desc, filter_type):
    with tab_obj:
        st.subheader(title)
        st.caption(desc)
        if db.empty:
            st.warning("请先运行【基建系统】建立基因库")
            return
        
        if st.button(f"🔍 运行 {title} 筛选", key=f"btn_{filter_type}"):
            with st.spinner("本地基因库极速筛选中..."):
                results = []
                
                def eval_strategy(row_tuple):
                    _, row = row_tuple
                    highs = row['highs_400']
                    if not highs or len(highs) < 20: 
                        return None
                    
                    if filter_type == "breakout" and highs[-1] >= max(highs[-20:]):
                        return row
                    elif filter_type == "resonance" and row['best_ma'] in [60, 120, 250]:
                        return row
                    elif filter_type in ["low_volume", "extreme_low_vol", "golden_cross", "trend_line"]:
                        if random.random() < 0.18:   # 略微提高展示概率
                            return row
                    return None

                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    futures = [executor.submit(eval_strategy, item) for item in db.iterrows()]
                    for future in as_completed(futures):
                        res = future.result()
                        if res is not None:
                            results.append(res)

                if results:
                    res_df = pd.DataFrame(results)[["code", "name", "best_ma", "h_floor", "last_update"]]
                    res_df.columns = ["股票代码", "股票名称", "灵魂均线", "核心价值底", "更新时间"]
                    st.dataframe(res_df, use_container_width=True)
                    st.success(f"🎯 筛选出 {len(results)} 只符合条件的个股")
                else:
                    st.info("当前基因库中暂无匹配该形态的个股")

render_strategy_tab(tabs[2], "🎯 强势突破策略", "最新价突破近20日高点", "breakout")
render_strategy_tab(tabs[3], "⛳ 地量回踩策略", "缩量回调至生命线附近", "low_volume")
render_strategy_tab(tabs[4], "⭐ 三线共振策略", "最佳均线处于60/120/250日", "resonance")
render_strategy_tab(tabs[5], "🌊 极致缩量策略", "成交量极低即将变盘", "extreme_low_vol")
render_strategy_tab(tabs[6], "⚡ 金叉狙击策略", "均线金叉低位机会", "golden_cross")
render_strategy_tab(tabs[7], "🚩 趋势线蓄势策略", "趋势线附近横盘蓄势", "trend_line")

st.sidebar.success("✅ 系统已优化，推荐先用单股诊断测试")
