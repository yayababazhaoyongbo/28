# ==================== Streamlit Cloud 兼容补丁 ====================
import sys
__import__('pysqlite3')
import pysqlite3
sys.modules['sqlite3'] = pysqlite3
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
from datetime import datetime

# ================= 配置 =================
DB_FILE = "soul_ma_master.db"
REQUEST_TIMEOUT = 3.5
REQUEST_RETRIES = 2

DELISTED_CODES = {"600102", "600001", "600002", "600005"}
UA_LIST = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]

# ================= 数据库 =================
class DatabaseManager:
    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
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
            with sqlite3.connect(self.db_path) as conn:
                return pd.read_sql("SELECT * FROM stocks", conn)
        except:
            return pd.DataFrame(columns=["code","name","best_ma","h_floor","highs_400","lows_400"])

    def update_db(self, new_rows):
        if not new_rows:
            return False
        try:
            data = []
            for r in new_rows:
                code = normalize_code(r.get("code", ""))
                if len(code) != 6:
                    continue
                highs_str = json.dumps(r.get("highs_400", []))
                lows_str = json.dumps(r.get("lows_400", []))
                
                data.append((
                    code,
                    str(r.get("name", "未知")),
                    int(r.get("best_ma", 60)),
                    float(r.get("h_floor", 0)),
                    highs_str,
                    lows_str,
                    datetime.now().isoformat()
                ))
            
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany('''
                    INSERT OR REPLACE INTO stocks 
                    VALUES (?,?,?,?,?,?,?)
                ''', data)
                conn.commit()
            return True
        except Exception as e:
            st.error(f"数据库保存失败: {str(e)}")
            return False

def normalize_code(code):
    s = re.sub(r"\D", "", str(code).strip())
    return s[-6:].zfill(6) if s else ""

# ================= SoulEngine =================
class SoulEngine:
    @staticmethod
    def get_data(code, days=400):
        clean_code = normalize_code(code)
        if len(clean_code) != 6 or clean_code in DELISTED_CODES:
            return None, "异常"
        
        symbol = ("sh" if clean_code.startswith("6") else "sz") + clean_code
        url = f"https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get?param={symbol},day,,,{days},qfq"
        
        for _ in range(REQUEST_RETRIES):
            try:
                headers = {"User-Agent": random.choice(UA_LIST)}
                resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, verify=False)
                res_json = resp.json()
                stock_info = res_json.get("data", {}).get(symbol)
                if not stock_info: 
                    continue
                name = stock_info.get("qt", {}).get(symbol, ["", "未知"])[1]
                if any(x in name for x in ["指数", "ETF", "ST", "退"]):
                    return None, "过滤"
                
                data_list = stock_info.get("qfqday") or stock_info.get("day", [])
                df = pd.DataFrame(data_list).iloc[:, [0,1,2,3,4,5]]
                df.columns = ["日期","开盘","收盘","最高","最低","成交量"]
                df = df.apply(pd.to_numeric, errors='coerce').dropna().reset_index(drop=True)
                return df, name
            except:
                time.sleep(0.3)
        return None, "异常"

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

# ================= 主界面 =================
st.set_page_config(page_title="灵魂均线 V27.6 Pro", layout="wide")

st.title("🚀 灵魂均线 V27.6 Pro（完整修复版）")
st.caption("数据库 OperationalError 已修复")

db_manager = DatabaseManager()
db = db_manager.load_db()

st.sidebar.metric("基因库股票数量", len(db))

tabs = st.tabs([
    "🔍 诊断", "🏗️ 基建", "🎯 强势突破", "⛳ 地量回踩",
    "⭐ 三线共振", "🌊 极致缩量", "⚡ 金叉狙击", "🚩 趋势线蓄势"
])

# Tab 0: 诊断
with tabs[0]:
    c_in = st.text_input("分析并入库", "600519", key="t0_code")
    if st.button("开始单股分析", key="btn_t0"):
        with st.spinner("获取数据中..."):
            df_d, name_d = SoulEngine.get_data(c_in, 600)
            if df_d is not None:
                ma = SoulEngine.calculate_best_ma(df_d)
                h_flr = round(df_d["收盘"].tail(125).mean(), 2)
                
                success = db_manager.update_db([{
                    "code": c_in,
                    "name": name_d,
                    "best_ma": ma,
                    "h_floor": h_flr,
                    "highs_400": df_d["最高"].tail(400).tolist(),
                    "lows_400": df_d["最低"].tail(400).tolist()
                }])
                
                if success:
                    st.success(f"✅ **{name_d}** 已成功入库！灵魂线 MA{ma}")
                    st.rerun()
                else:
                    st.error("入库失败")
            else:
                st.error("获取股票数据失败")

# Tab 1: 基建
with tabs[1]:
    st.write(f"当前基因库已有 **{len(db)}** 只股票")
    if st.button("🚀 开始增量基建", type="primary"):
        with st.spinner("基建进行中（请耐心等待）..."):
            pool = [f"{p}{i:03d}" for p in ["600","601","603","000","002"] for i in range(400)]
            existing = set(db["code"].astype(str))
            todo = [c for c in pool if c not in existing][:300]
            
            bar = st.progress(0)
            new_list = []
            for i, c in enumerate(todo):
                df, name = SoulEngine.get_data(c, days=350)
                if df is not None and len(df) >= 120:
                    ma = SoulEngine.calculate_best_ma(df)
                    new_list.append({
                        "code": c,
                        "name": name,
                        "best_ma": ma,
                        "h_floor": round(df["收盘"].tail(125).mean(), 2),
                        "highs_400": df["最高"].tail(400).tolist(),
                        "lows_400": df["最低"].tail(400).tolist()
                    })
                bar.progress((i + 1) / len(todo))
                if len(new_list) >= 30:
                    db_manager.update_db(new_list)
                    new_list = []
            if new_list:
                db_manager.update_db(new_list)
            st.success("✅ 基建完成！")
            st.rerun()

st.sidebar.success("✅ 应用已就绪")
