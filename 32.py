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
import os
import time
import random
import re
import io
import json
import sqlite3
from datetime import datetime

# ================= 配置 =================
DB_FILE = "soul_ma_master.db"
THREAD_COUNT = 18
REQUEST_TIMEOUT = 3.5
REQUEST_RETRIES = 2

DELISTED_CODES = {"600102", "600001", "600002", "600005"}
UA_LIST = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]

# ================= 数据库管理（加强版） =================
class DatabaseManager:
    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        try:
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
                st.success("✅ 数据库初始化成功") if "initialized" not in st.session_state else None
        except Exception as e:
            st.error(f"数据库初始化失败: {e}")

    def load_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql("SELECT * FROM stocks ORDER BY code", conn)
                return df
        except:
            return pd.DataFrame(columns=["code","name","best_ma","h_floor","highs_400","lows_400"])

    def update_db(self, new_rows):
        if not new_rows: return False
        try:
            data = []
            for r in new_rows:
                code = normalize_code(r.get("code", ""))
                if len(code) != 6: continue
                data.append((
                    code, 
                    r.get("name","未知"), 
                    int(r.get("best_ma",60)),
                    float(r.get("h_floor",0)),
                    json.dumps(r.get("highs_400",[])),
                    json.dumps(r.get("lows_400",[])),
                    datetime.now().isoformat()
                ))
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany('INSERT OR REPLACE INTO stocks VALUES (?,?,?,?,?,?,?)', data)
                conn.commit()
            return True
        except Exception as e:
            st.error(f"保存失败: {e}")
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
                resp = requests.get(url, headers={"User-Agent": random.choice(UA_LIST)}, 
                                  timeout=REQUEST_TIMEOUT, verify=False)
                data = resp.json()["data"][symbol]
                name = data["qt"][symbol][1]
                if any(x in name for x in ["指数","ETF","ST","退"]):
                    return None, "过滤"
                
                df_list = data.get("qfqday") or data.get("day")
                df = pd.DataFrame(df_list).iloc[:, [0,1,2,3,4,5]]
                df.columns = ["日期","开盘","收盘","最高","最低","成交量"]
                df = df.apply(pd.to_numeric, errors='coerce').dropna().reset_index(drop=True)
                return df, name
            except:
                time.sleep(0.3)
        return None, "异常"

# ================= 主界面 =================
st.set_page_config(page_title="灵魂均线 V27.6 Pro", layout="wide")

st.title("🚀 灵魂均线 V27.6 Pro（完整版）")
st.caption("数据库诊断模式已开启")

db_manager = DatabaseManager()
db = db_manager.load_db()

st.sidebar.info(f"当前基因库：**{len(db)}** 只股票")

# 调试信息
if len(db) == 0:
    st.warning("⚠️ 基因库为空，请先执行【基建】")
    if st.button("🧪 测试数据库写入"):
        test_data = [{"code": "600519", "name": "贵州茅台", "best_ma": 120, "h_floor": 1500,
                     "highs_400": [1600]*400, "lows_400": [1400]*400}]
        if db_manager.update_db(test_data):
            st.success("测试数据写入成功！请刷新页面")
            st.rerun()

# ================= Tabs =================
tabs = st.tabs(["🔍 诊断", "🏗️ 基建", "🎯 强势突破", "🚩 趋势线蓄势"])

with tabs[0]:
    code_in = st.text_input("股票代码", "600519")
    if st.button("分析并入库"):
        df, name = SoulEngine.get_data(code_in)
        if df is not None:
            # 简化计算
            best_ma = 60
            db_manager.update_db([{
                "code": code_in,
                "name": name,
                "best_ma": best_ma,
                "h_floor": float(df["收盘"].tail(100).mean()),
                "highs_400": df["最高"].tail(400).tolist(),
                "lows_400": df["最低"].tail(400).tolist()
            }])
            st.success(f"✅ {name} 入库成功")
            st.rerun()

with tabs[1]:
    st.subheader("🏗️ 全市场基建")
    st.write(f"当前库已有 **{len(db)}** 只")
    
    if st.button("🚀 开始增量基建（推荐）", type="primary"):
        with st.spinner("正在进行基建..."):
            pool = [f"{p}{i:03d}" for p in ["600","601","603","000","002"] for i in range(200)]
            todo = [c for c in pool if c not in set(db["code"])]
            todo = todo[:300]   # 限制数量，避免超时
            
            progress_bar = st.progress(0)
            new_list = []
            
            for idx, c in enumerate(todo):
                df, name = SoulEngine.get_data(c, days=350)
                if df is not None and len(df) > 100:
                    new_list.append({
                        "code": c,
                        "name": name,
                        "best_ma": 60,
                        "h_floor": float(df["收盘"].mean()),
                        "highs_400": df["最高"].tail(400).tolist(),
                        "lows_400": df["最低"].tail(400).tolist()
                    })
                progress_bar.progress((idx + 1) / len(todo))
                if len(new_list) >= 20:
                    db_manager.update_db(new_list)
                    new_list = []
            
            if new_list:
                db_manager.update_db(new_list)
            
            st.success(f"基建完成！新增 {len(new_list) + (idx+1 - len(new_list)//20*20)} 只")
            st.rerun()

with tabs[3]:
    st.header("🚩 趋势线蓄势")
    st.info("请先完成基建后再使用此功能")

st.sidebar.success("诊断模式已开启")
