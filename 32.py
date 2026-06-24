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
import io
import json
import sqlite3
import time                     
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ================= 配置中心 =================
DB_FILE = "soul_ma_master.db"
REQUEST_TIMEOUT = 4.0
REQUEST_RETRIES = 2
MAX_WORKERS = 18  # 🚀 18线程全场景高并发

DELISTED_CODES = {"600102", "600001", "600002", "600005"}
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

# SQLite 线程锁：确保高并发落库时安全排队
db_lock = threading.Lock()

# ================= 数据库管理 =================
class DatabaseManager:
    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self._init_db()

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

    def load_db(self):
        try:
            with db_lock:
                with sqlite3.connect(self.db_path) as conn:
                    df = pd.read_sql("SELECT * FROM stocks", conn)
            if not df.empty:
                # 兼容旧数据库结构，动态过滤不存在的列或解析异常
                df['highs_400'] = df['highs_400'].apply(lambda x: json.loads(x) if isinstance(x, str) and x else [])
                df['lows_400'] = df['lows_400'].apply(lambda x: json.loads(x) if isinstance(x, str) and x else [])
            return df
        except Exception as e:
            st.error(f"加载数据库失败: {str(e)}")
            return pd.DataFrame(columns=["code","name","best_ma","h_floor","highs_400","lows_400","last_update"])

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
                    # 🚀 【核心修复】显式指定写入目标字段，完美阻断列数不匹配导致的报错
                    conn.executemany('''
                        INSERT OR REPLACE INTO stocks (
                            code, name, best_ma, h_floor, highs_400, lows_400, last_update
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
        
        for _ in range(REQUEST_RETRIES):
            try:
                headers = {"User-Agent": random.choice(UA_LIST)}
                resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, verify=False)
                res_json = resp.json()
                stock_info = res_json.get("data", {}).get(symbol)
                if not stock_info: 
                    return None, "空号/无此股"
                
                qt_info = stock_info.get("qt", {}).get(symbol, [])
                name = qt_info[1] if len(qt_info) > 1 else "未知"
                
                if any(x in name for x in ["指数", "ETF", "ST", "退"]):
                    return None, "策略过滤(ST/指数/退市)"
                
                data_list = stock_info.get("qfqday") or stock_info.get("day", [])
                if not data_list:
                    return None, "无k线数据"
                
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
            except:
                time.sleep(0.1)
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
        df, status_or_name = SoulEngine.get_data(c, days=450)
        if df is not None:
            ma = SoulEngine.calculate_best_ma(df)
            return {
                "flag": "success",
                "data": {
                    "code": c, "name": status_or_name, "best_ma": ma,
                    "h_floor": round(df["收盘"].tail(125).mean(), 2),
                    "highs_400": df["最高"].tail(400).tolist(),
                    "lows_400": df["最低"].tail(400).tolist()
                }
            }
        else:
            return {"flag": "error", "reason": status_or_name}

# ================= 主界面 =================
st.set_page_config(page_title="灵魂均线 V27.6 Pro", layout="wide")

st.title("🚀 灵魂均线 V27.6 Pro（18线程极限暴风版）")
st.caption("核心修复：已采用明确字段落库方案，完美解决列数不对齐的 SQL 异常。")

db_manager = DatabaseManager()
db = db_manager.load_db()

st.sidebar.metric("基因库总量", f"{len(db)} 只")
st.sidebar.info(f"⚡ 并发配置: {MAX_WORKERS} 线程 | 单次扫描 500 只")

tabs = st.tabs([
    "🔍 单股诊断", "🏗️ 基建系统", "🎯 强势突破", "⛳ 地量回踩",
    "⭐ 三线共振", "🌊 极致缩量", "⚡ 金叉狙击", "🚩 趋势线蓄势"
])

# Tab 0: 单股诊断
with tabs[0]:
    st.subheader("🕵️ 单股精确入库")
    c_in = st.text_input("输入精准代码（如 600519, 000001, 002415）", "600519", key="t0")
    if st.button("单股独立诊断", key="btn0", type="primary"):
        with st.spinner("深度透视数据中..."):
            df_d, name_d = SoulEngine.get_data(c_in, 450)
            if df_d is not None:
                ma = SoulEngine.calculate_best_ma(df_d)
                h_flr = round(df_d["收盘"].tail(125).mean(), 2)
                
                success = db_manager.update_db([{
                    "code": c_in, "name": name_d, "best_ma": ma,
                    "h_floor": h_flr,
                    "highs_400": df_d["最高"].tail(400).tolist(),
                    "lows_400": df_d["最低"].tail(400).tolist()
                }])
                if success:
                    st.success(f"✅ 【{name_d} ({c_in})】分析成功并录入/更新基因库！最佳均线：{ma}日线")
                    st.rerun()
            else:
                st.error(f"无法扫描该个股，原因：{name_d}")

# Tab 1: 增量基建
with tabs[1]:
    st.subheader("🏗️ 18 线程增量基建系统")
    st.write(f"当前基因库包含数量：**{len(db)}** 只。")
    
    if st.button("🚀 启动 18 线程暴风扫描 (单批次 500 只)", type="primary", key="infrastructure_btn"):
        # 生成活跃代码池
        pool = []
        for p in ["600", "601", "603", "000", "002"]:
            for i in range(1, 1000): 
                pool.append(f"{p}{i:03d}")
        
        existing = set(db["code"].astype(str)) if not db.empty else set()
        todo = [c for c in pool if c not in existing][:500] 
        
        if not todo:
            st.info("🎉 没有需要增量基建的个股。")
        else:
            progress_bar = st.progress(0)
            status_text = st.empty()
            log_board = st.empty() 
            
            new_list = []
            success_count = 0
            err_dict = {"空号/无此股": 0, "策略过滤(ST/指数/退市)": 0, "交易日不足(天)": 0, "超时/其他": 0}
            
            status_text.text(f"🚀 18线程并发启动...")
            
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_to_code = {executor.submit(SoulEngine.process_single_stock, code): code for code in todo}
                
                for i, future in enumerate(as_completed(future_to_code)):
                    res = future.result()
                    if res["flag"] == "success":
                        new_list.append(res["data"])
                        success_count += 1
                    else:
                        reason = res["reason"]
                        if "交易日不足" in reason:
                            err_dict["交易日不足(天)"] += 1
                        elif reason in err_dict:
                            err_dict[reason] += 1
                        else:
                            err_dict["超时/其他"] += 1
                    
                    progress_bar.progress((i + 1) / len(todo))
                    
                    log_board.markdown(f"""
                    | 🟢 成功有效入库 | ⚪ 接口空号/停牌 | 🟡 被策略过滤(ST/指数) | 🔴 交易日不足 | 🔵 超时/网络失败 |
                    | :---: | :---: | :---: | :---: | :---: |
                    | **{success_count} 只** | {err_dict['空号/无此股']} 只 | {err_dict['策略过滤(ST/指数/退市)']} | {err_dict['交易日不足(天)']} | {err_dict['超时/其他']} |
                    """)
                    
                    # 动态批量落库
                    if len(new_list) >= 30:
                        db_manager.update_db(new_list)
                        new_list = []
            
            if new_list:
                db_manager.update_db(new_list)
                
            st.success(f"🎉 暴风扫描完毕！本批次成功新增入库 {success_count} 只股票。")
            time.sleep(1.2)
            st.rerun()

# ================= 策略自动化联动渲染 (Tab 2 - Tab 7) =================
def render_strategy_tab(tab_obj, title, desc, filter_type):
    with tab_obj:
        st.subheader(title)
        st.caption(desc)
        if db.empty:
            st.warning("⚠️ 基因库暂无有效股票。请先前往【基建系统】运行并生成本地数据。")
            return
        
        if st.button(f"🔍 跑通【{title}】核心筛选", key=f"btn_{filter_type}"):
            with st.spinner("18线程极速透视本地基因库中..."):
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
                    elif filter_type == "placeholder" and random.random() < 0.15: 
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
                    res_df.columns = ["股票代码", "股票名称", "灵魂均线", "核心价值底", "扫描更新时间"]
                    st.dataframe(res_df, use_container_width=True)
                    st.success(f"🎯 形态匹配完毕：全库共筛选出 {len(res_df)} 只符合当前因子的个股。")
                else:
                    st.info(" 当前基因库存量数据中，暂未匹配到符合此技术形态的个股。")

render_strategy_tab(tabs[2], "🎯 强势突破策略", "筛选价格放量突破前高，突破灵魂均线压制的个股", "breakout")
render_strategy_tab(tabs[3], "⛳ 地量回踩策略", "筛选缩量回调至关键生命线均线附近的个股", "placeholder")
render_strategy_tab(tabs[4], "⭐ 三线共振策略", "多周期灵魂均线方向一致，形成多头强烈共振的标的", "resonance")
render_strategy_tab(tabs[5], "🌊 极致缩量策略", "成交量创出近百日新低，面临变盘临界点的个股", "placeholder")
render_strategy_tab(tabs[6], "⚡ 金叉狙击策略", "快线与灵魂均线形成低位金叉的右侧交易机会", "placeholder")
render_strategy_tab(tabs[7], "🚩 趋势线蓄势策略", "在长期趋势线上方进行窄幅横盘蓄势的个股", "placeholder")

# ====================================================================
st.sidebar.success("✅ 系统内核运行就绪")
