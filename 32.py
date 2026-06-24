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
import math
import random
import re
import io
import json
import sqlite3
import plotly.graph_objects as go
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ================= 1. 配置中心 =================
os.environ["no_proxy"] = "*"
DB_FILE = "soul_ma_master.db"
THREAD_COUNT = 20
REQUEST_TIMEOUT = 3.5
REQUEST_RETRIES = 2

DELISTED_CODES = {"600102", "600001", "600002", "600005"}
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

# ================= 2. 数据库管理 =================
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

    def load_db(self) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql("SELECT * FROM stocks", conn)
            return df if not df.empty else pd.DataFrame(columns=["code", "name", "best_ma", "h_floor", "highs_400", "lows_400"])

    def update_db(self, new_rows):
        if not new_rows:
            return
        data = []
        for r in new_rows:
            code = normalize_code(r.get("code", ""))
            if len(code) != 6:
                continue
            data.append((
                code,
                r.get("name", "未知"),
                int(r.get("best_ma", 0)),
                float(r.get("h_floor", 0)),
                json.dumps(r.get("highs_400", [])),
                json.dumps(r.get("lows_400", [])),
                datetime.now().isoformat()
            ))
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany('''
                INSERT OR REPLACE INTO stocks 
                (code, name, best_ma, h_floor, highs_400, lows_400, last_update)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', data)
            conn.commit()

# ================= 3. 工具函数 =================
def normalize_code(code):
    s = re.sub(r"\D", "", str(code).strip())
    return s[-6:].zfill(6) if s else ""

def to_excel(df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="扫描结果")
    return output.getvalue()

# ================= 4. SoulEngine =================
class SoulEngine:
    @staticmethod
    def get_data(code, days=350, retries=None, timeout=None):
        clean_code = normalize_code(code)
        if len(clean_code) != 6:
            return None, "异常"
        if clean_code in DELISTED_CODES:
            return None, "退市"

        symbol = ("sh" if clean_code.startswith("6") else "sz") + clean_code
        url = f"https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get?param={symbol},day,,,{days},qfq"

        for attempt in range(retries or REQUEST_RETRIES):
            try:
                headers = {"User-Agent": random.choice(UA_LIST), "Referer": "https://gu.qq.com/"}
                resp = requests.get(url, headers=headers, timeout=timeout or REQUEST_TIMEOUT, verify=False)
                resp.raise_for_status()
                res_json = resp.json()
                stock_info = res_json.get("data", {}).get(symbol)
                if not stock_info:
                    continue

                name = stock_info.get("qt", {}).get(symbol, ["", "未知"])[1]
                if any(x in name for x in ["指数", "ETF", "ST", "退"]):
                    return None, "过滤"

                data_list = stock_info.get("qfqday") or stock_info.get("day", [])
                if len(data_list) < 30:
                    return None, "不足"

                df = pd.DataFrame(data_list).iloc[:, [0,1,2,3,4,5]]
                df.columns = ["日期", "开盘", "收盘", "最高", "最低", "成交量"]
                df = df.apply(pd.to_numeric, errors='coerce')
                return df.dropna().reset_index(drop=True), name
            except:
                time.sleep(0.2)
        return None, "异常"

    @staticmethod
    def calculate_best_ma(df):
        if df is None or len(df) < 100:
            return 60
        close = df["收盘"]
        returns = close.pct_change()
        best_ma, max_score = 60, -float("inf")
        for p in range(30, 251, 5):
            if len(close) < p:
                continue
            ma = close.rolling(p).mean()
            sig = (close > ma).astype(int)
            trd = sig.diff().abs().sum()
            score = (sig.shift(1) * returns).sum() * (sig.mean() ** 2.5) / (trd + 2)
            if score > max_score:
                max_score, best_ma = score, p
        return best_ma

# ================= 5. Streamlit 主程序 =================
st.set_page_config(page_title="灵魂均线 V27.6 Pro", layout="wide")

try:
    st.title("🚀 灵魂均线 V27.6 Pro（SQLite + 向量化 + 增强版）")
    st.caption("Streamlit Cloud 优化版 | 如有问题请截图反馈")

    db_manager = DatabaseManager()
    db = db_manager.load_db()

    exclude_limit_up = st.sidebar.checkbox("过滤今日涨幅 > 9.3% 的个股", value=True)
    show_w_bottom = st.sidebar.checkbox("显示 W底 快速检测", value=False)

    tabs = st.tabs([
        "🔍 诊断", "🏗️ 基建", "🎯 强势突破", "⛳ 地量回踩",
        "⭐ 三线共振", "🌊 极致缩量", "⚡ 金叉狙击", "🚩 趋势线蓄势"
    ])

    # ================= Tab 0: 诊断 =================
    with tabs[0]:
        c_in = st.text_input("分析并入库", "600376", key="t1_in")
        if st.button("开始单股分析", key="btn_t1"):
            code_key = normalize_code(c_in)
            df_d, name_d = SoulEngine.get_data(code_key, days=600)
            if df_d is not None:
                ma = SoulEngine.calculate_best_ma(df_d)
                h_flr = round(df_d["收盘"].tail(125).value_counts(bins=40).idxmax().mid, 2)
                highs_400 = df_d["最高"].tail(400).tolist()
                lows_400 = df_d["最低"].tail(400).tolist()

                db_manager.update_db([{
                    "code": code_key, "name": name_d, "best_ma": ma,
                    "h_floor": h_flr, "highs_400": highs_400, "lows_400": lows_400
                }])
                st.success(f"**{name_d}** 已入库！灵魂线: MA{ma}")
                st.line_chart(df_d["收盘"].tail(200))

    # ================= Tab 1: 基建 =================
    with tabs[1]:
        st.write(f"当前库已录入：{len(db)} 只。")
        if st.button("开始增量基建普查", key="btn_infra"):
            # 简化版基建（可后续扩展）
            st.info("完整基建功能正在迁移中... 当前版本优先保证核心扫描可用")

    # 其他 Tab 可后续逐步补全
    with tabs[7]:   # 示例：趋势线蓄势 Tab
        st.header("🚩 趋势线蓄势（核心功能）")
        st.info("更多高级扫描功能正在迁移中...")

    st.success("✅ 应用已成功启动！请在左侧选择功能")

except Exception as e:
    st.error("🚨 启动失败")
    st.exception(e)
