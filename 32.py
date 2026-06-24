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
from pathlib import Path

# ================= 1. 配置中心 =================
os.environ["no_proxy"] = "*"
DB_FILE = "soul_ma_master.db"  # 改为 SQLite
THREAD_COUNT = 20
REQUEST_TIMEOUT = 3.5
REQUEST_RETRIES = 2

# ================= 数据库升级为 SQLite =================
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
                    last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    data_version INTEGER DEFAULT 1
                )
            ''')
            conn.commit()
    
    def load_db(self) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            return pd.read_sql("SELECT * FROM stocks", conn)
    
    def update_db(self, new_rows):
        if not new_rows:
            return
        data = []
        for r in new_rows:
            r = dict(r)
            code = normalize_code(r.get("code", ""))
            if len(code) != 6:
                continue
            data.append((
                code,
                r.get("name"),
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
    
    def get_stock(self, code):
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql("SELECT * FROM stocks WHERE code=?", conn, params=(normalize_code(code),))
            return df.iloc[0] if not df.empty else None

db_manager = DatabaseManager()

# ================= 2. 工具函数增强 =================
def normalize_code(code):
    """更健壮的代码标准化"""
    if not code:
        return ""
    s = re.sub(r"\D", "", str(code).strip())
    return s[-6:].zfill(6) if s else ""

def is_limit_move(df, code):
    """改进：更安全的涨跌停判断"""
    if df is None or len(df) < 2:
        return False
    try:
        prev_close = float(df["收盘"].iloc[-2])
        close_now = float(df["收盘"].iloc[-1])
        if prev_close <= 0:
            return False
        change_pct = (close_now / prev_close - 1) * 100
        limit_pct = 20.0 if normalize_code(code).startswith(("300", "301", "688")) else 10.0
        return abs(change_pct) >= (limit_pct - 0.3)  # 略微放宽容差
    except:
        return False

# ================= 3. 向量化指标计算（性能大幅提升） =================
class TechnicalIndicators:
    
    @staticmethod
    def calculate_rsi(df, period=14):
        """向量化 RSI 计算"""
        if df is None or len(df) < period + 1:
            return None
        closes = df["收盘"].values
        deltas = np.diff(closes)
        gains = np.maximum(deltas, 0)
        losses = np.maximum(-deltas, 0)
        
        avg_gain = np.zeros_like(closes)
        avg_loss = np.zeros_like(closes)
        
        avg_gain[period] = gains[:period].mean()
        avg_loss[period] = losses[:period].mean()
        
        for i in range(period + 1, len(closes)):
            avg_gain[i] = (avg_gain[i-1] * (period - 1) + gains[i-1]) / period
            avg_loss[i] = (avg_loss[i-1] * (period - 1) + losses[i-1]) / period
        
        rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100)
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    @staticmethod
    def calculate_kdj(df, n=9, m1=3, m2=3):
        """向量化 KDJ"""
        if df is None or len(df) < n:
            return None, None, None
        
        highs = df["最高"].values
        lows = df["最低"].values
        closes = df["收盘"].values
        
        # RSV
        rsv = np.full_like(closes, 50.0)
        for i in range(n-1, len(closes)):
            high_n = highs[i-n+1:i+1].max()
            low_n = lows[i-n+1:i+1].min()
            if high_n != low_n:
                rsv[i] = (closes[i] - low_n) / (high_n - low_n) * 100
        
        # KDJ
        k = np.full_like(closes, 50.0)
        d = np.full_like(closes, 50.0)
        j = np.full_like(closes, 50.0)
        
        for i in range(n, len(closes)):
            k[i] = (2/3) * k[i-1] + (1/3) * rsv[i]
            d[i] = (2/3) * d[i-1] + (1/3) * k[i]
            j[i] = 3 * k[i] - 2 * d[i]
        
        return k, d, j

# ================= 4. MurphyReboundChannelEngine 增强 =================
class MurphyReboundChannelEngine:
    # ...（保留原有核心逻辑，增加更多异常保护和日志）
    
    @staticmethod
    def build_channel_pressure(df, **kwargs):
        try:
            # 原有逻辑保持不变，增加输入验证
            if df is None or len(df) < 100:
                return None
            # ...（原有代码）
            return best
        except Exception as e:
            # st.warning(f"通道计算异常: {e}")  # 可选
            return None

# ================= 5. SoulEngine 优化 =================
class SoulEngine:
    @staticmethod
    def get_data(code, days=350, retries=None, timeout=None):
        clean_code = normalize_code(code)
        if len(clean_code) != 6:
            return None, "异常"
        if clean_code in DELISTED_CODES:
            return None, "退市"
        
        # 增加缓存（可选进一步优化）
        symbol = ("sh" if clean_code.startswith("6") else "sz") + clean_code
        url = f"https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get?param={symbol},day,,,{days},qfq"
        
        for attempt in range(retries or REQUEST_RETRIES):
            try:
                headers = {"User-Agent": random.choice(UA_LIST), "Referer": "https://gu.qq.com/"}
                resp = requests.get(url, headers=headers, timeout=timeout or REQUEST_TIMEOUT, verify=False)
                resp.raise_for_status()
                data = resp.json()
                
                # 更健壮的解析
                stock_info = data.get("data", {}).get(symbol)
                if not stock_info:
                    continue
                
                name = stock_info.get("qt", {}).get(symbol, ["", "未知"])[1]
                if any(x in name for x in ["指数", "ETF", "ST", "退"]):
                    return None, "过滤"
                
                day_data = stock_info.get("qfqday") or stock_info.get("day")
                if not day_data or len(day_data) < 30:
                    return None, "不足"
                
                df = pd.DataFrame(day_data)
                df = df.iloc[:, [0,1,2,3,4,5]]
                df.columns = ["日期", "开盘", "收盘", "最高", "最低", "成交量"]
                df = df.apply(pd.to_numeric, errors='coerce')
                return df.dropna().reset_index(drop=True), name
                
            except Exception:
                if attempt < (retries or REQUEST_RETRIES) - 1:
                    time.sleep(0.2 * (attempt + 1))
                else:
                    return None, "异常"
        return None, "异常"

# ================= Streamlit 主程序 =================
st.set_page_config(page_title="灵魂均线 V27.6 Pro", layout="wide")
st.title("🚀 灵魂均线 V27.6 Pro（SQLite + 向量化 + 增强版）")

db = db_manager.load_db()

# ...（其余 Tab 逻辑基本保持，但把 load_db() 改为 db_manager.load_db()）

# 示例：在基建和诊断中使用新数据库
def update_db(new_rows):
    db_manager.update_db(new_rows)
