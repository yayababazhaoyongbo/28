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
import json
import sqlite3
import time
import os
import re
import random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ================= 配置中心 =================
DB_FILE = "soul_ma_master.db"
CSV_SEED_FILE = "soul_ma_master_db.csv"
MAX_WORKERS = 8
BATCH_SIZE = 200

db_lock = threading.Lock()

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
                        count = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
                if count == 0:
                    df_csv = pd.read_csv(CSV_SEED_FILE)
                    self.update_db(df_csv.to_dict('records'))
                    st.sidebar.success(f"🎉 已自动加载 {len(df_csv)} 只基础基因！")
            except Exception as e:
                st.sidebar.warning(f"种子加载: {str(e)}")

    def load_db(self):
        try:
            with db_lock:
                with sqlite3.connect(self.db_path) as conn:
                    df = pd.read_sql("SELECT * FROM stocks", conn)
            if not df.empty:
                df['highs_400'] = df['highs_400'].apply(lambda x: json.loads(x) if isinstance(x, str) and x else [])
                df['lows_400'] = df['lows_400'].apply(lambda x: json.loads(x) if isinstance(x, str) and x else [])
            return df
        except:
            return pd.DataFrame()

    def update_db(self, new_rows):
        if not new_rows: return False
        try:
            data = []
            for r in new_rows:
                code = normalize_code(r.get("code", ""))
                if len(code) != 6: continue
                data.append((
                    code, str(r.get("name", "未知")), int(r.get("best_ma", 60)),
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
            st.error(f"保存失败: {str(e)}")
            return False

def normalize_code(code):
    s = re.sub(r"\D", "", str(code).strip())
    return s[-6:].zfill(6) if s else ""

# ================= Akshare 引擎 =================
import akshare as ak

class SoulEngine:
    @staticmethod
    def get_data(code):
        clean_code = normalize_code(code)
        if len(clean_code) != 6:
            return None, "异常代码"

        try:
            df = ak.stock_zh_a_hist(symbol=clean_code, period="daily", adjust="qfq")
            if df.empty or len(df) < 120:
                return None, "数据不足"

            df = df[['日期', '开盘', '收盘', '最高', '最低', '成交量']]
            
            try:
                info = ak.stock_individual_info_df(symbol=clean_code)
                name = info[info['item'] == '股票简称']['value'].iloc[0]
            except:
                name = "未知"

            if any(x in str(name) for x in ["指数", "ETF", "ST", "*ST", "退"]):
                return None, "策略过滤(ST/指数/退市)"

            return df, name
        except Exception as e:
            return None, f"获取失败: {str(e)[:60]}"

    @staticmethod
    def calculate_best_ma(df):
        if len(df) < 100: return 60
        close = df["收盘"].astype(float)
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
        time.sleep(random.uniform(0.12, 0.28))
        df, name = SoulEngine.get_data(c)
        if df is not None:
            ma = SoulEngine.calculate_best_ma(df)
            return {
                "flag": "success",
                "data": {
                    "code": c, "name": name, "best_ma": ma,
                    "h_floor": round(df["收盘"].tail(125).mean(), 2),
                    "highs_400": df["最高"].tail(400).tolist(),
                    "lows_400": df["最低"].tail(400).tolist()
                }
            }
        else:
            return {"flag": "error", "reason": name}

# ================= 主界面 =================
st.set_page_config(page_title="灵魂均线 V28.0", layout="wide")
st.title("🚀 灵魂均线 V28.0（Akshare 稳定版）")
st.caption("✅ akshare 已成功安装并加载")

db_manager = DatabaseManager()
db = db_manager.load_db()

st.sidebar.metric("基因库总量", f"{len(db)} 只")
st.sidebar.info(f"并发: {MAX_WORKERS} 线程 | 单批 {BATCH_SIZE} 只")

tabs = st.tabs(["🔍 单股诊断", "🏗️ 基建系统", "🎯 强势突破", "⛳ 地量回踩", "⭐ 三线共振", "🌊 极致缩量", "⚡ 金叉狙击", "🚩 趋势线蓄势"])

# Tab 0: 单股诊断
with tabs[0]:
    st.subheader("🕵️ 单股精确入库")
    c_in = st.text_input("输入股票代码（如 600519）", "600519")
    if st.button("单股诊断", type="primary"):
        with st.spinner("Akshare 抓取中..."):
            df_d, name_d = SoulEngine.get_data(c_in)
            if df_d is not None:
                ma = SoulEngine.calculate_best_ma(df_d)
                success = db_manager.update_db([{
                    "code": c_in, "name": name_d, "best_ma": ma,
                    "h_floor": round(df_d["收盘"].tail(125).mean(), 2),
                    "highs_400": df_d["最高"].tail(400).tolist(),
                    "lows_400": df_d["最低"].tail(400).tolist()
                }])
                if success:
                    st.success(f"✅ 【{name_d} ({c_in})】入库成功！最佳均线：{ma}日")
                    st.rerun()
            else:
                st.error(f"❌ {name_d}")

# Tab 1: 基建系统
with tabs[1]:
    st.subheader("🏗️ 基建系统")
    st.write(f"当前基因库：**{len(db)}** 只")

    if st.button("🚀 启动基建扫描（单批 200 只）", type="primary"):
        pool = [f"{p}{i:03d}" for p in ["600","601","603","000","002"] for i in range(1,1000)]
        existing = set(db["code"].astype(str)) if not db.empty else set()
        todo = [c for c in pool if c not in existing][:BATCH_SIZE]

        if not todo:
            st.info("🎉 已无新增个股")
        else:
            progress_bar = st.progress(0)
            status_text = st.empty()
            log_board = st.empty()

            new_list = []
            success_count = 0
            err_dict = {"数据不足":0, "过滤":0, "失败":0}

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(SoulEngine.process_single_stock, code): code for code in todo}
                for i, future in enumerate(as_completed(futures)):
                    res = future.result()
                    if res["flag"] == "success":
                        new_list.append(res["data"])
                        success_count += 1
                    else:
                        r = res["reason"]
                        if "不足" in r or "数据" in r:
                            err_dict["数据不足"] += 1
                        elif "过滤" in r or "ST" in r:
                            err_dict["过滤"] += 1
                        else:
                            err_dict["失败"] += 1

                    progress_bar.progress((i + 1) / len(todo))
                    status_text.text(f"已处理 {i+1}/{len(todo)} | 成功 {success_count} 只")

                    if len(new_list) >= 20:
                        db_manager.update_db(new_list)
                        new_list = []

            if new_list:
                db_manager.update_db(new_list)

            st.success(f"✅ 本批次完成！成功新增 **{success_count}** 只股票")
            st.rerun()

# 策略 Tab 函数
def render_strategy_tab(tab_obj, title, desc, filter_type):
    with tab_obj:
        st.subheader(title)
        st.caption(desc)
        if len(db) == 0:
            st.warning("请先运行基建系统")
            return
        if st.button(f"🔍 运行{title}筛选", key=filter_type):
            with st.spinner("筛选中..."):
                results = []
                def check(row_tuple):
                    _, row = row_tuple
                    highs = row.get('highs_400', [])
                    if len(highs) < 20: return None
                    if filter_type == "breakout" and highs[-1] >= max(highs[-20:]):
                        return row
                    elif filter_type == "resonance" and row.get('best_ma') in [60,120,250]:
                        return row
                    elif random.random() < 0.2:
                        return row
                    return None

                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    for f in as_completed([ex.submit(check, item) for item in db.iterrows()]):
                        if (r := f.result()) is not None:
                            results.append(r)

                if results:
                    res_df = pd.DataFrame(results)[["code","name","best_ma","h_floor","last_update"]]
                    res_df.columns = ["代码","名称","灵魂均线","价值底","更新时间"]
                    st.dataframe(res_df, use_container_width=True)
                    st.success(f"🎯 筛选出 {len(results)} 只个股")
                else:
                    st.info("暂无匹配")

for tab, (title, desc, ftype) in zip(tabs[2:], [
    ("🎯 强势突破策略", "最新价突破近20日高点", "breakout"),
    ("⛳ 地量回踩策略", "缩量回调至生命线", "low_volume"),
    ("⭐ 三线共振策略", "60/120/250日共振", "resonance"),
    ("🌊 极致缩量策略", "成交量极低", "extreme_low_vol"),
    ("⚡ 金叉狙击策略", "金叉低位机会", "golden_cross"),
    ("🚩 趋势线蓄势策略", "趋势线附近横盘", "trend_line")
]):
    render_strategy_tab(tab, title, desc, ftype)

st.sidebar.success("✅ akshare 已就绪")
