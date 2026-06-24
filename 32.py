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
import plotly.graph_objects as go
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= 1. 配置中心 =================
os.environ["no_proxy"] = "*"
DB_FILE = "soul_ma_master_db.csv"
THREAD_COUNT = 20
REQUEST_TIMEOUT = 3.5
REQUEST_RETRIES = 1

# 退市股票代码黑名单
DELISTED_CODES = {
    "600102",  # 莱钢股份
    "600001",  # 邯郸钢铁
    "600002",  # 齐鲁石化
    "600005",  # 武钢股份
    # 可根据需要继续添加
}

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


def to_excel(df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="扫描结果")
    return output.getvalue()


def normalize_code(code):
    """统一为 6 位数字代码，避免重复与请求失败。"""
    s = re.sub(r"\D", "", str(code).strip())
    if len(s) >= 6:
        return s[-6:].zfill(6)
    return s.zfill(6) if s else ""


# ================= 2. 墨菲高级引擎 (严格压力线，供 Tab10) =================
class MurphyEnginePro:
    @staticmethod
    def get_strict_trendline(df, window=250):
        """寻找极高合规度的长周期压力线"""
        df_w = df.tail(window).reset_index(drop=True)
        highs = df_w["最高"].values
        closes = df_w["收盘"].values

        p1_idx = int(np.argmax(highs))
        p1_val = highs[p1_idx]
        if p1_idx > window - 60:
            return None, None, None

        others = []
        for i in range(p1_idx + 40, window - 10):
            if highs[i] == max(highs[i - 10 : i + 11]) and highs[i] < p1_val * 0.98:
                others.append((i, highs[i]))
        if not others:
            return None, None, None

        p2_idx, p2_val = others[-1]
        k = (p2_val - p1_val) / (p2_idx - p1_idx)
        if k >= 0:
            return None, None, None

        b = p1_val - k * p1_idx
        line_vals = np.array([k * x + b for x in range(window)])

        check_len = len(line_vals) - 10
        highs_check = highs[p1_idx:check_len]
        closes_check = closes[p1_idx:check_len]
        line_check = line_vals[p1_idx:check_len]

        if np.any((highs_check / line_check - 1) > 0.01):
            return None, None, None

        is_above = closes_check > line_check
        cons_above = 0
        for above in is_above:
            if above:
                cons_above += 1
                if cons_above >= 2:
                    return None, None, None
            else:
                cons_above = 0

        touches = int(np.sum(np.abs(highs[p1_idx:] / line_vals[p1_idx:] - 1) < 0.015))
        if touches < 2:
            return None, None, None

        return line_vals, touches, df_w


# ================= 3. 趋势线引擎 (去噪触碰，供 Tab9 两日突破) =================
class MurphyEngineV26:
    @staticmethod
    def get_tight_line(df, window=250, line_type="resistance"):
        df_w = df.tail(window).reset_index(drop=True)
        src = df_w["最高"].values if line_type == "resistance" else df_w["最低"].values
        p1_idx = int(np.argmax(src) if line_type == "resistance" else np.argmin(src))
        p1_val = src[p1_idx]

        recent_segment = src[-30:]
        p2_rel_idx = int(
            np.argmax(recent_segment) if line_type == "resistance" else np.argmin(recent_segment)
        )
        p2_idx = len(df_w) - 30 + p2_rel_idx
        p2_val = recent_segment[p2_rel_idx]

        if line_type == "resistance":
            if p1_idx >= p2_idx or p1_val <= p2_val:
                return None, None, None
        else:
            if p1_idx >= p2_idx or p1_val >= p2_val:
                return None, None, None

        k = (p2_val - p1_val) / (p2_idx - p1_idx)
        b = p1_val - k * p1_idx
        line_vals = np.array([k * x + b for x in range(window)])

        if line_type == "resistance":
            if np.any(df_w["收盘"].values[p1_idx:] > line_vals[p1_idx:] * 1.02):
                return None, None, None
        else:
            if np.any(df_w["收盘"].values[p1_idx:] < line_vals[p1_idx:] * 0.98):
                return None, None, None

        if line_type == "resistance":
            raw_touch_mask = src[p1_idx:] >= line_vals[p1_idx:] * 0.985
        else:
            raw_touch_mask = src[p1_idx:] <= line_vals[p1_idx:] * 1.015
        touch_indices = np.where(raw_touch_mask)[0]
        if len(touch_indices) == 0:
            return None, None, None

        refined_touches = 0
        last_touch_idx = -999
        for idx in touch_indices:
            if idx - last_touch_idx > 5:
                refined_touches += 1
            last_touch_idx = idx

        if refined_touches < 3:
            return None, None, None

        return line_vals, int(refined_touches), df_w


# ================= 3b. 下降通道：反弹高点连线 + 穿刺合规 =================
class MurphyReboundChannelEngine:
    """
    在下降通道中，用「每次反弹的高点」尝试连成一条向下压力线。
    - 理想：所有反弹高点均贴近同一直线，且盘中上穿压力线的「穿刺」次数 <= 3，
      每次穿刺幅度 < 1% → 重点推荐。
    - 否则：用「前期窗口内最高点 + 最近 1～2 个反弹高点」拟合备选压力线，
      以贴近/触碰该线的反弹点越多为稳定性越高。
    """

    @staticmethod
    def _swing_high_indices(highs, look=2, min_rise_pct=10.0, min_days_between=20):
        """
        识别反弹高点，要求：
        1. 反弹幅度 > min_rise_pct%
        2. 相邻反弹高点间隔 > min_days_between 交易日
        """
        n = len(highs)
        out = []
        for i in range(look, n - look):
            seg = highs[i - look : i + look + 1]
            if highs[i] >= np.max(seg):
                # 检查反弹幅度：从最近的一个低点到这个高点的涨幅
                # 找到最近的一个低点
                low_idx = i - look
                while low_idx > look and highs[low_idx] >= highs[low_idx - 1]:
                    low_idx -= 1
                if low_idx < i:
                    low_price = min(highs[low_idx:i])
                    rise_pct = (highs[i] - low_price) / low_price * 100 if low_price > 0 else 0
                    if rise_pct >= min_rise_pct:
                        out.append(i)
        # 过滤相邻高点间隔
        filtered = []
        for idx in out:
            if not filtered:
                filtered.append(idx)
            else:
                if idx - filtered[-1] >= min_days_between:
                    filtered.append(idx)
        return filtered

    @staticmethod
    def _pierce_stats(highs, line_vals, pierce_max_pct=0.01):
        """将连续「最高价高于线价」的区间合并为一次穿刺；记录每次最大上穿比例。"""
        n = len(highs)
        line_safe = np.maximum(line_vals, 1e-9)
        over = highs > line_safe * 1.0000001
        events_max = []
        i = 0
        while i < n:
            if not over[i]:
                i += 1
                continue
            j = i
            while j < n and over[j]:
                j += 1
            excess = np.max(highs[i:j] / line_safe[i:j] - 1.0)
            events_max.append(float(excess))
            i = j
        n_events = len(events_max)
        pierce_ok = n_events <= 3 and all(e < pierce_max_pct for e in events_max)
        return n_events, pierce_ok, events_max

    @staticmethod
    def _touch_score(swing_idx, highs, line_vals, near_pct=0.015):
        line_safe = np.maximum(line_vals, 1e-9)
        cnt = 0
        for i in swing_idx:
            if abs(highs[i] / line_safe[i] - 1.0) <= near_pct:
                cnt += 1
        return cnt

    @staticmethod
    def _all_swings_near_line(swing_idx, highs, line_vals, tol=0.012):
        line_safe = np.maximum(line_vals, 1e-9)
        for i in swing_idx:
            if abs(highs[i] / line_safe[i] - 1.0) > tol:
                return False
        return True

    @staticmethod
    def _line_from_two(i0, h0, i1, h1, n):
        if i1 == i0:
            return None, None
        k = (h1 - h0) / (i1 - i0)
        b = h0 - k * i0
        return k, b

    @staticmethod
    def build_channel_pressure(
        df,
        window=250,
        swing_look=2,
        min_rise_pct=10.0,
        min_days_between=20,
        touch_tol=0.015,
        pierce_max_pct=0.01,
        max_pierce_events=3,
        full_fit_tol=0.012,
        exclude_tail_days=25,
    ):
        if df is None or len(df) < window:
            return None
        df_w = df.tail(window).reset_index(drop=True)
        highs = df_w["最高"].values.astype(float)
        closes = df_w["收盘"].values.astype(float)
        n = len(highs)

        # 下降通道：用「窗口前段」对比「排除末端大涨区后的 60 日」均价。
        # 若仍用「最后 60 日」，突破/涨停会把后段均价整体抬高，容易把东风科技这类票误判为非通道。
        et = int(np.clip(exclude_tail_days, 8, max(8, n // 3)))
        if n >= 80 + et:
            early_m = float(np.mean(closes[:60]))
            late_seg = closes[-(60 + et) : -et] if et > 0 else closes[-60:]
            if len(late_seg) < 30:
                late_seg = closes[-(90 + et) : -et] if et > 0 else closes[-90:]
            late_m = float(np.mean(late_seg)) if len(late_seg) else early_m
            if late_m >= early_m * 0.998:
                return None
            
            # 增加MA斜率判断：MA20在最近20天内应该持续下跌
            ma20 = pd.Series(closes).rolling(20).mean().tail(20).dropna()
            if len(ma20) >= 10:
                ma20_slope = (ma20.iloc[-1] - ma20.iloc[0]) / len(ma20) if ma20.iloc[0] > 0 else 0
                # MA20斜率应该为负，且下跌幅度超过0.1%
                if ma20_slope >= -0.001:
                    return None
            
            # 多时间周期验证：检查周线趋势一致性
            # 从日线数据聚合周线数据
            df_weekly = df_w.copy()
            df_weekly['week'] = pd.to_datetime(df_weekly['日期']).dt.isocalendar().week
            df_weekly['year'] = pd.to_datetime(df_weekly['日期']).dt.year
            weekly_data = df_weekly.groupby(['year', 'week']).agg({
                '开盘': 'first',
                '收盘': 'last',
                '最高': 'max',
                '最低': 'min',
                '成交量': 'sum'
            }).reset_index(drop=True)
            if len(weekly_data) >= 10:
                weekly_ma20 = weekly_data['收盘'].rolling(20).mean().tail(10).dropna()
                if len(weekly_ma20) >= 5:
                    weekly_slope = (weekly_ma20.iloc[-1] - weekly_ma20.iloc[0]) / len(weekly_ma20) if weekly_ma20.iloc[0] > 0 else 0
                    # 周线MA20也应该下跌
                    if weekly_slope >= -0.0005:
                        return None

        swing_idx = MurphyReboundChannelEngine._swing_high_indices(highs, look=swing_look, min_rise_pct=min_rise_pct, min_days_between=min_days_between)
        if len(swing_idx) < 3:
            return None

        # 计算波动率，动态调整穿刺容忍度
        returns = pd.Series(closes).pct_change().dropna()
        volatility = float(returns.std()) if len(returns) > 0 else 0.02
        # 波动率越大，穿刺容忍度越高
        dynamic_pierce_max_pct = pierce_max_pct * (1 + volatility * 10)

        candidates = []

        def add_candidate(k, b, mode):
            if k >= 0:
                return
            line_vals = k * np.arange(n, dtype=float) + b
            if np.any(line_vals <= 0):
                return
            n_pierce, pierce_ok_strict, events_max = MurphyReboundChannelEngine._pierce_stats(
                highs, line_vals, pierce_max_pct=dynamic_pierce_max_pct
            )
            loose_ok = n_pierce <= 8 and all(e < 0.025 for e in events_max)
            if not loose_ok:
                return
            tscore = MurphyReboundChannelEngine._touch_score(swing_idx, highs, line_vals, near_pct=touch_tol)
            if tscore < 2:
                return
            all_on = MurphyReboundChannelEngine._all_swings_near_line(swing_idx, highs, line_vals, tol=full_fit_tol)
            featured = bool(
                all_on and pierce_ok_strict and n_pierce <= max_pierce_events
            )
            candidates.append(
                {
                    "line_vals": line_vals,
                    "df_w": df_w,
                    "swing_indices": list(swing_idx),
                    "mode": mode,
                    "touch_score": int(tscore),
                    "pierce_events": int(n_pierce),
                    "pierce_ok_strict": pierce_ok_strict,
                    "pierce_events_max_pct": max(events_max) * 100 if events_max else 0.0,
                    "all_swings_on_line": all_on,
                    "featured": featured,
                    "stability": float(tscore + 0.15 * min(n_pierce, 5)),
                    "k": float(k),
                    "volatility": volatility,
                }
            )

        xs = np.array(swing_idx, dtype=float)
        ys = highs[swing_idx]
        k_ls, b_ls = np.polyfit(xs, ys, 1)
        add_candidate(k_ls, b_ls, "全反弹高点拟合")

        early_end = max(swing_look + 5, int(n * 0.45))
        early_peak = int(np.argmax(highs[:early_end]))
        tail_swings = [i for i in swing_idx if i > early_peak]
        if len(tail_swings) >= 1:
            last1 = tail_swings[-1]
            k, b = MurphyReboundChannelEngine._line_from_two(early_peak, highs[early_peak], last1, highs[last1], n)
            if k is not None:
                add_candidate(k, b, "主峰+最近反弹高点")
        if len(tail_swings) >= 2:
            last2, last1 = tail_swings[-2], tail_swings[-1]
            k, b = MurphyReboundChannelEngine._line_from_two(last2, highs[last2], last1, highs[last1], n)
            if k is not None:
                add_candidate(k, b, "近两次反弹高点连线")
            k, b = MurphyReboundChannelEngine._line_from_two(early_peak, highs[early_peak], last2, highs[last2], n)
            if k is not None:
                add_candidate(k, b, "主峰+次近反弹高点")

        if not candidates:
            return None

        def sort_key(c):
            return (c["featured"], c["touch_score"], -c["pierce_events"], c["stability"])

        best = max(candidates, key=sort_key)
        return best

    @staticmethod
    def detect_close_breakout(
        rec,
        min_break_pct=0.2,
        lookback=5,
        require_cross=True,
        require_still_above=True,
        min_vol_ratio=None,
        require_above_ma200_on_break=False,
    ):
        """
        在 build_channel_pressure 得到的压力线基础上，寻找「收盘突破压力线」。
        min_break_pct: 突破幅度，单位为「百分点」如 0.2 表示 0.2%。
        require_cross: 突破日昨收在压力线（含微小容差）之下。
        require_still_above: 最新收盘仍在线上方。
        min_vol_ratio: 若给定，突破日成交量 >= 该倍数 × 突破日前5日均量。
        require_above_ma200_on_break: 突破日收盘须站上当日 MA200。
        """
        if rec is None:
            return None
        df_w = rec["df_w"]
        line_vals = rec["line_vals"]
        closes = df_w["收盘"].values.astype(float)
        vols = df_w["成交量"].values.astype(float)
        n = len(closes)
        if n < 3:
            return None
        line_safe = np.maximum(line_vals, 1e-9)
        ma200 = None
        if require_above_ma200_on_break:
            ma200 = df_w["收盘"].rolling(200).mean().values
        if require_still_above and closes[-1] <= line_safe[-1]:
            return None
        for ago in range(int(lookback)):
            i = n - 1 - ago
            if i < 1:
                continue
            l_t = line_safe[i]
            c_t = closes[i]
            pct = (c_t / l_t - 1.0) * 100.0
            if pct < min_break_pct:
                continue
            if require_cross:
                l_y = line_safe[i - 1]
                c_y = closes[i - 1]
                if c_y > l_y * 1.001:
                    continue
            if min_vol_ratio is not None and min_vol_ratio > 0:
                if i < 6:
                    continue
                v_avg = float(np.mean(vols[i - 6 : i - 1]))
                if v_avg <= 0 or vols[i] < v_avg * min_vol_ratio:
                    continue
            if require_above_ma200_on_break:
                if i < 199 or ma200 is None or np.isnan(ma200[i]) or closes[i] <= ma200[i]:
                    continue
            return {
                "day_idx": int(i),
                "days_ago": int(ago),
                "break_pct": float(pct),
                "break_date": df_w["日期"].iloc[i],
            }
        return None

    @staticmethod
    def backtest_breakout_performance(df, window=250, min_break_pct=0.2, hold_days=10):
        """
        回测历史突破后的胜率
        返回：突破次数、成功次数（持有期间涨幅>0）、胜率、平均涨幅
        """
        if df is None or len(df) < window + hold_days:
            return None
        
        df_w = df.tail(window + hold_days).reset_index(drop=True)
        closes = df_w["收盘"].values.astype(float)
        highs = df_w["最高"].values.astype(float)
        n = len(closes)
        
        # 构建压力线
        rec = MurphyReboundChannelEngine.build_channel_pressure(df_w.head(window))
        if rec is None:
            return None
        
        line_vals = rec["line_vals"]
        line_safe = np.maximum(line_vals, 1e-9)
        
        # 寻找历史突破
        breakouts = []
        for i in range(1, window):
            if i >= n - hold_days:
                continue
            prev_close = closes[i - 1]
            prev_line = line_safe[i - 1]
            cur_close = closes[i]
            cur_line = line_safe[i]
            pct = (cur_close / cur_line - 1.0) * 100.0
            
            if prev_close <= prev_line and pct >= min_break_pct:
                # 计算持有hold_days天后的涨幅
                future_close = closes[min(i + hold_days, n - 1)]
                future_pct = (future_close / cur_close - 1.0) * 100.0
                breakouts.append(future_pct)
        
        if not breakouts:
            return None
        
        success_count = sum(1 for pct in breakouts if pct > 0)
        win_rate = success_count / len(breakouts) * 100
        avg_return = sum(breakouts) / len(breakouts)
        
        return {
            "突破次数": len(breakouts),
            "成功次数": success_count,
            "胜率%": round(win_rate, 2),
            "平均涨幅%": round(avg_return, 2),
        }


# ================= 4. 技术指标计算 =================
class TechnicalIndicators:
    @staticmethod
    def calculate_rsi(df, period=14):
        """计算RSI相对强弱指标"""
        if df is None or len(df) < period + 1:
            return None
        closes = df["收盘"].values
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.zeros_like(closes)
        avg_loss = np.zeros_like(closes)
        
        avg_gain[period] = np.mean(gains[:period])
        avg_loss[period] = np.mean(losses[:period])
        
        for i in range(period + 1, len(closes)):
            avg_gain[i] = (avg_gain[i-1] * (period - 1) + gains[i-1]) / period
            avg_loss[i] = (avg_loss[i-1] * (period - 1) + losses[i-1]) / period
        
        rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100)
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    @staticmethod
    def calculate_kdj(df, n=9, m1=3, m2=3):
        """计算KDJ指标"""
        if df is None or len(df) < n:
            return None
        highs = df["最高"].values
        lows = df["最低"].values
        closes = df["收盘"].values
        
        rsv = np.zeros_like(closes)
        for i in range(n - 1, len(closes)):
            high_n = np.max(highs[i - n + 1:i + 1])
            low_n = np.min(lows[i - n + 1:i + 1])
            if high_n != low_n:
                rsv[i] = (closes[i] - low_n) / (high_n - low_n) * 100
            else:
                rsv[i] = 50
        
        k = np.zeros_like(closes)
        d = np.zeros_like(closes)
        j = np.zeros_like(closes)
        
        k[n - 1] = 50
        d[n - 1] = 50
        
        for i in range(n, len(closes)):
            k[i] = (2 / 3) * k[i - 1] + (1 / 3) * rsv[i]
            d[i] = (2 / 3) * d[i - 1] + (1 / 3) * k[i]
            j[i] = 3 * k[i] - 2 * d[i]
        
        return k, d, j
    
    @staticmethod
    def calculate_macd(df, fast=12, slow=26, signal=9):
        """计算MACD指标"""
        if df is None or len(df) < slow + signal:
            return None
        closes = df["收盘"].values
        
        ema_fast = pd.Series(closes).ewm(span=fast, adjust=False).mean().values
        ema_slow = pd.Series(closes).ewm(span=slow, adjust=False).mean().values
        dif = ema_fast - ema_slow
        dea = pd.Series(dif).ewm(span=signal, adjust=False).mean().values
        macd = (dif - dea) * 2
        
        return dif, dea, macd


# ================= 5. 形态识别 =================
class PatternRecognition:
    @staticmethod
    def detect_head_and_shoulders(df, window=100):
        """识别头肩顶/底形态"""
        if df is None or len(df) < window:
            return None
        
        highs = df["最高"].values
        lows = df["最低"].values
        closes = df["收盘"].values
        
        # 识别局部高点
        peaks = []
        for i in range(5, len(highs) - 5):
            if highs[i] == np.max(highs[i - 5:i + 6]):
                peaks.append((i, highs[i]))
        
        if len(peaks) < 3:
            return None
        
        # 寻找头肩顶：左肩 < 头 > 右肩，且头在中间
        for i in range(len(peaks) - 2):
            left_idx, left_val = peaks[i]
            head_idx, head_val = peaks[i + 1]
            right_idx, right_val = peaks[i + 2]
            
            # 头肩顶条件：头最高，左右肩接近
            if head_val > left_val and head_val > right_val:
                shoulder_diff = abs(left_val - right_val) / head_val
                if shoulder_diff < 0.05:  # 左右肩高度差小于5%
                    # 检查颈线
                    neckline = (left_val + right_val) / 2
                    # 检查是否突破颈线
                    if len(closes) > right_idx + 10:
                        recent_close = closes[right_idx + 10]
                        if recent_close < neckline:
                            return {
                                "类型": "头肩顶",
                                "左肩位置": left_idx,
                                "头位置": head_idx,
                                "右肩位置": right_idx,
                                "颈线": round(neckline, 2),
                                "突破": True
                            }
        
        # 寻找头肩底（类似逻辑，但反转）
        troughs = []
        for i in range(5, len(lows) - 5):
            if lows[i] == np.min(lows[i - 5:i + 6]):
                troughs.append((i, lows[i]))
        
        if len(troughs) < 3:
            return None
        
        for i in range(len(troughs) - 2):
            left_idx, left_val = troughs[i]
            head_idx, head_val = troughs[i + 1]
            right_idx, right_val = troughs[i + 2]
            
            if head_val < left_val and head_val < right_val:
                shoulder_diff = abs(left_val - right_val) / head_val
                if shoulder_diff < 0.05:
                    neckline = (left_val + right_val) / 2
                    if len(closes) > right_idx + 10:
                        recent_close = closes[right_idx + 10]
                        if recent_close > neckline:
                            return {
                                "类型": "头肩底",
                                "左肩位置": left_idx,
                                "头位置": head_idx,
                                "右肩位置": right_idx,
                                "颈线": round(neckline, 2),
                                "突破": True
                            }
        
        return None
    
    @staticmethod
    def detect_double_top_bottom(df, window=100):
        """识别双顶/双底形态"""
        if df is None or len(df) < window:
            return None
        
        highs = df["最高"].values
        lows = df["最低"].values
        closes = df["收盘"].values
        
        # 识别局部高点
        peaks = []
        for i in range(5, len(highs) - 5):
            if highs[i] == np.max(highs[i - 5:i + 6]):
                peaks.append((i, highs[i]))
        
        # 寻找双顶
        for i in range(len(peaks) - 1):
            idx1, val1 = peaks[i]
            idx2, val2 = peaks[i + 1]
            
            # 双顶条件：两个高点高度接近，间隔合理
            if abs(val1 - val2) / val1 < 0.03 and 10 < idx2 - idx1 < 60:
                # 检查中间是否有明显的回调
                valley = np.min(highs[idx1:idx2])
                if valley < val1 * 0.97:
                    # 检查是否突破颈线
                    neckline = valley
                    if len(closes) > idx2 + 10:
                        recent_close = closes[idx2 + 10]
                        if recent_close < neckline:
                            return {
                                "类型": "双顶",
                                "第一顶位置": idx1,
                                "第二顶位置": idx2,
                                "颈线": round(neckline, 2),
                                "突破": True
                            }
        
        # 识别局部低点
        troughs = []
        for i in range(5, len(lows) - 5):
            if lows[i] == np.min(lows[i - 5:i + 6]):
                troughs.append((i, lows[i]))
        
        # 寻找双底
        for i in range(len(troughs) - 1):
            idx1, val1 = troughs[i]
            idx2, val2 = troughs[i + 1]
            
            if abs(val1 - val2) / val1 < 0.03 and 10 < idx2 - idx1 < 60:
                peak = np.max(lows[idx1:idx2])
                if peak > val1 * 1.03:
                    neckline = peak
                    if len(closes) > idx2 + 10:
                        recent_close = closes[idx2 + 10]
                        if recent_close > neckline:
                            return {
                                "类型": "双底",
                                "第一底位置": idx1,
                                "第二底位置": idx2,
                                "颈线": round(neckline, 2),
                                "突破": True
                            }
        
        return None
    
    @staticmethod
    def detect_triangle(df, window=60):
        """识别三角形形态（上升、下降、对称）"""
        if df is None or len(df) < window:
            return None
        
        highs = df["最高"].values
        lows = df["最低"].values
        closes = df["收盘"].values
        
        # 取最近window天的数据
        recent_highs = highs[-window:]
        recent_lows = lows[-window:]
        
        # 拟合上趋势线（高点连线）
        x = np.arange(len(recent_highs))
        z_high = np.polyfit(x, recent_highs, 1)
        p_high = np.poly1d(z_high)
        
        # 拟合下趋势线（低点连线）
        z_low = np.polyfit(x, recent_lows, 1)
        p_low = np.poly1d(z_low)
        
        slope_high = z_high[0]
        slope_low = z_low[0]
        
        # 上升三角形：上边水平，下边上升
        if abs(slope_high) < 0.001 and slope_low > 0:
            return {
                "类型": "上升三角形",
                "上边斜率": round(slope_high, 6),
                "下边斜率": round(slope_low, 6),
            }
        
        # 下降三角形：下边水平，上边下降
        if abs(slope_low) < 0.001 and slope_high < 0:
            return {
                "类型": "下降三角形",
                "上边斜率": round(slope_high, 6),
                "下边斜率": round(slope_low, 6),
            }
        
        # 对称三角形：上边下降，下边上升
        if slope_high < 0 and slope_low > 0:
            return {
                "类型": "对称三角形",
                "上边斜率": round(slope_high, 6),
                "下边斜率": round(slope_low, 6),
            }
        
        return None


# ================= 6. 艾略特波浪分析 =================
class ElliottWaveAnalysis:
    @staticmethod
    def detect_wave_structure(df, window=200):
        """识别艾略特波浪结构（简化版）"""
        if df is None or len(df) < window:
            return None
        
        closes = df["收盘"].values
        highs = df["最高"].values
        lows = df["最低"].values
        
        # 识别局部极值点
        peaks = []
        troughs = []
        
        for i in range(5, len(closes) - 5):
            if highs[i] == np.max(highs[i - 5:i + 6]):
                peaks.append((i, highs[i]))
            if lows[i] == np.min(lows[i - 5:i + 6]):
                troughs.append((i, lows[i]))
        
        if len(peaks) < 3 or len(troughs) < 3:
            return None
        
        # 寻找5浪上升结构
        # 1浪：从低点开始上升
        # 2浪：回调不超过1浪起点
        # 3浪：最长，突破1浪高点
        # 4浪：回调不超过3浪高点
        # 5浪：突破3浪高点但动能减弱
        
        recent_data = closes[-window:]
        wave_info = {
            "当前浪型": "未知",
            "波浪计数": [],
            "斐波那契回撤": []
        }
        
        # 简化判断：检查是否处于上升5浪的某个阶段
        if len(peaks) >= 3 and len(troughs) >= 3:
            # 检查最近3个波峰是否呈上升趋势
            recent_peaks = peaks[-3:]
            if recent_peaks[0][1] < recent_peaks[1][1] < recent_peaks[2][1]:
                wave_info["当前浪型"] = "上升5浪"
                wave_info["波浪计数"] = [f"波峰{i+1}: {val:.2f}" for i, (idx, val) in enumerate(recent_peaks)]
        
        # 计算斐波那契回撤位
        if len(recent_data) > 50:
            high_50 = np.max(recent_data[-50:])
            low_50 = np.min(recent_data[-50:])
            current = recent_data[-1]
            
            fib_levels = {
                "0.382": high_50 - (high_50 - low_50) * 0.382,
                "0.5": high_50 - (high_50 - low_50) * 0.5,
                "0.618": high_50 - (high_50 - low_50) * 0.618,
            }
            
            wave_info["斐波那契回撤"] = [
                {k: round(v, 2) for k, v in fib_levels.items()},
                {"当前价格": round(current, 2)},
                {"最近50日高点": round(high_50, 2)},
                {"最近50日低点": round(low_50, 2)}
            ]
        
        return wave_info


# ================= 7. 时间周期分析 =================
class TimeCycleAnalysis:
    @staticmethod
    def detect_time_cycles(df, window=250):
        """识别价格时间周期"""
        if df is None or len(df) < window:
            return None
        
        closes = df["收盘"].values
        dates = pd.to_datetime(df["日期"])
        
        # 计算价格波动的周期性
        returns = pd.Series(closes).pct_change().dropna()
        
        # 使用FFT检测周期
        if len(returns) < 50:
            return None
        
        fft_result = np.fft.fft(returns)
        freq = np.fft.fftfreq(len(returns))
        
        # 找到主要频率成分
        power = np.abs(fft_result) ** 2
        positive_freq_idx = np.where(freq > 0)[0]
        
        if len(positive_freq_idx) == 0:
            return None
        
        # 找到功率最大的频率
        main_freq_idx = positive_freq_idx[np.argmax(power[positive_freq_idx])]
        main_freq = freq[main_freq_idx]
        
        if main_freq > 0:
            cycle_length = int(1 / main_freq)
            # 限制周期长度在合理范围内（5-60个交易日）
            cycle_length = max(5, min(60, cycle_length))
        else:
            cycle_length = 20  # 默认20天周期
        
        # 计算当前在周期中的位置
        current_phase = len(returns) % cycle_length
        
        cycle_info = {
            "检测周期(交易日)": cycle_length,
            "当前相位": current_phase,
            "周期阶段": "上升" if current_phase < cycle_length / 2 else "下降",
            "周期强度": round(np.max(power[positive_freq_idx]), 4)
        }
        
        return cycle_info


# ================= 8. 数据与基因库 =================
class SoulEngine:
    @staticmethod
    def get_data(code, days=350, retries=None, timeout=None):
        clean_code = normalize_code(code)
        if len(clean_code) != 6:
            return None, "异常"
        # 过滤退市股票
        if clean_code in DELISTED_CODES:
            return None, "退市"
        symbol = ("sh" if clean_code.startswith("6") else "sz") + clean_code
        url = f"https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get?param={symbol},day,,,{days},qfq"
        blacklist = ["指数", "ETF", "LOF", "基金", "期权", "债", "转", "ST", "退", "平"]
        retries = REQUEST_RETRIES if retries is None else retries
        timeout = REQUEST_TIMEOUT if timeout is None else timeout

        for attempt in range(retries + 1):
            headers = {"User-Agent": random.choice(UA_LIST), "Referer": "https://gu.qq.com/"}
            try:
                resp = requests.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    verify=False,
                    proxies={"http": None, "https": None},
                )
                resp.raise_for_status()
                res_json = resp.json()
                stock_info = res_json.get("data", {}).get(symbol)
                if not stock_info:
                    return None, "异常"

                qt_info = stock_info.get("qt", {}).get(symbol, [])
                if isinstance(qt_info, list) and len(qt_info) > 1 and qt_info[1]:
                    name = qt_info[1]
                else:
                    name = "未知"
                if any(x in name for x in blacklist):
                    return None, "过滤"

                data_list = stock_info.get("qfqday", stock_info.get("day", []))
                if not data_list or len(data_list) < 20:
                    return None, "不足"

                base_df = pd.DataFrame(data_list)
                if base_df.shape[1] < 6:
                    return None, "不足"
                df = base_df.iloc[:, [0, 1, 2, 3, 4, 5]]
                df.columns = ["日期", "开盘", "收盘", "最高", "最低", "成交量"]
                df[["开盘", "收盘", "最高", "最低", "成交量"]] = df[
                    ["开盘", "收盘", "最高", "最低", "成交量"]
                ].apply(pd.to_numeric)
                return df.dropna(), name
            except Exception:
                if attempt < retries:
                    time.sleep(0.15)
                else:
                    return None, "异常"
        return None, "异常"

    @staticmethod
    def calculate_best_ma(df):
        close, returns = df["收盘"], df["收盘"].pct_change()
        best_ma, max_score = 0, -float("inf")
        for p in range(30, 251, 5):
            if len(close) < p:
                continue
            ma = close.rolling(window=p).mean()
            sig = np.where(close > ma, 1, 0)
            trd = np.diff(sig).astype(bool).sum()
            score = (
                (pd.Series(sig, index=close.index).shift(1) * returns).sum()
                * math.pow((sig.sum() / (trd + 2)), 2.5)
            )
            if score > max_score:
                max_score, best_ma = score, p
        return best_ma


def load_db():
    if os.path.exists(DB_FILE):
        df = pd.read_csv(DB_FILE, encoding="utf-8-sig", dtype={"code": str})
        df["code"] = df["code"].map(normalize_code)
        df = df[df["code"].str.len() == 6].drop_duplicates(subset=["code"], keep="last").reset_index(drop=True)
        # 过滤退市股票
        df = df[~df["code"].isin(DELISTED_CODES)].reset_index(drop=True)
        if "h_floor" not in df.columns:
            df["h_floor"] = 0.0
        if "highs_400" not in df.columns:
            df["highs_400"] = ""
        if "lows_400" not in df.columns:
            df["lows_400"] = ""
        return df
    return pd.DataFrame(columns=["code", "name", "best_ma", "h_floor", "highs_400", "lows_400"])


def update_db(new_rows):
    rows = []
    for r in new_rows:
        r = dict(r)
        r["code"] = normalize_code(r.get("code", ""))
        if len(r["code"]) == 6:
            rows.append(r)
    if not rows:
        return
    db = load_db()
    db = pd.concat([db, pd.DataFrame(rows)], ignore_index=True).drop_duplicates("code", keep="last")
    db.to_csv(DB_FILE, index=False, encoding="utf-8-sig")


def ensure_db_ready(db_df):
    if db_df.empty:
        st.warning("基因库为空，请先在「基建」或「诊断」中入库。")
        return False
    return True


def is_limit_move(df, code):
    if df is None or len(df) < 2:
        return False
    clean_code = normalize_code(code)
    prev_close = df["收盘"].iloc[-2]
    close_now = df["收盘"].iloc[-1]
    if prev_close <= 0:
        return False
    change_pct = (close_now / prev_close - 1) * 100
    limit_pct = 20.0 if clean_code.startswith(("300", "301", "688")) else 10.0
    return abs(change_pct) >= (limit_pct - 0.2)


def find_w_bottom_signal(df, min_gap=15, max_gap=80, bottom_tol_pct=3.0, breakout_pct=1.0, vol_ratio_min=1.5):
    if df is None or len(df) < 80:
        return None
    w = df.tail(220).reset_index(drop=True).copy()
    lows = w["最低"]
    highs = w["最高"]
    closes = w["收盘"]
    vols = w["成交量"]
    right_search_start = max(10, len(w) - 120)
    c_idx = int(lows.iloc[right_search_start:].idxmin())
    c_low = lows.iloc[c_idx]
    left_start = max(0, c_idx - max_gap)
    left_end = c_idx - min_gap
    if left_end <= left_start:
        return None
    a_idx = int(lows.iloc[left_start : left_end + 1].idxmin())
    a_low = lows.iloc[a_idx]
    bottom_diff_pct = abs(c_low / a_low - 1) * 100 if a_low > 0 else 99
    if bottom_diff_pct > bottom_tol_pct:
        return None
    if c_idx - a_idx < min_gap:
        return None
    b_idx = int(highs.iloc[a_idx : c_idx + 1].idxmax())
    b_high = highs.iloc[b_idx]
    if b_high <= 0:
        return None
    close_t = closes.iloc[-1]
    breakout_real_pct = (close_t / b_high - 1) * 100
    if breakout_real_pct < breakout_pct:
        return None
    v_avg5 = vols.iloc[-6:-1].mean() if len(vols) >= 6 else 0
    if v_avg5 <= 0:
        return None
    vol_ratio = vols.iloc[-1] / v_avg5
    if vol_ratio < vol_ratio_min:
        return None
    shape_score = max(0, 35 - bottom_diff_pct * 8)
    breakout_score = min(35, breakout_real_pct * 10)
    volume_score = min(30, (vol_ratio - vol_ratio_min) * 20 + 15)
    total_score = round(shape_score + breakout_score + volume_score, 1)
    return {
        "A日期": w["日期"].iloc[a_idx],
        "A价": round(float(a_low), 2),
        "B日期": w["日期"].iloc[b_idx],
        "颈线": round(float(b_high), 2),
        "C日期": w["日期"].iloc[c_idx],
        "C价": round(float(c_low), 2),
        "底部价差": round(bottom_diff_pct, 2),
        "突破": f"{breakout_real_pct:.2f}%",
        "放量": f"{vol_ratio:.2f}倍",
        "综合分": total_score,
    }


# ================= 5. 页面 =================
st.set_page_config(page_title="灵魂均线 V27.5 Pro", layout="wide")
st.title("🚀 灵魂均线 V27.5 Pro（全功能 + 破阵蓄力 + 400日数据预存）")

db = load_db()
exclude_limit_up = st.sidebar.checkbox("过滤今日涨幅 > 9.3% 的个股", value=True)
show_w_bottom = st.sidebar.checkbox("在诊断页显示 W底 快速检测", value=False)

tabs = st.tabs(
    [
        "🔍 诊断",
        "🏗️ 基建",
        "🎯 强势突破",
        "⛳ 地量回踩",
        "⭐ 三线共振",
        "🧊 绝对地量",
        "🏗️ 墨菲平台",
        "🌊 极致缩量",
        "⚡ 金叉狙击",
        "🚩 趋势线蓄势",
    ]
)

# --- Tab 0: 诊断 ---
with tabs[0]:
    c_in = st.text_input("分析并入库", "600376", key="t1_in")
    if st.button("开始单股分析", key="btn_t1"):
        code_key = normalize_code(c_in)
        if len(code_key) != 6:
            st.error("请输入 6 位股票代码。")
        else:
            df_d, name_d = SoulEngine.get_data(code_key, days=600)
            if df_d is not None:
                if is_limit_move(df_d, code_key):
                    st.warning(f"**{name_d}** 当日触及涨跌停，已跳过入库。")
                    st.stop()
                ma = SoulEngine.calculate_best_ma(df_d)
                counts, price_bins = np.histogram(df_d["收盘"].tail(125), bins=40)
                h_flr = round(price_bins[np.argmax(counts)], 2)
                
                # 提取前400天的高低价数据
                df_400 = df_d.tail(400).reset_index(drop=True)
                highs_400 = df_400["最高"].tolist()
                lows_400 = df_400["最低"].tolist()
                
                update_db([{
                    "code": code_key, 
                    "name": name_d, 
                    "best_ma": ma, 
                    "h_floor": h_flr,
                    "highs_400": json.dumps(highs_400),
                    "lows_400": json.dumps(lows_400)
                }])
                st.success(f"**{name_d}** 已入库！灵魂线: MA{ma}")
                st.line_chart(df_d["收盘"].tail(200))
                if show_w_bottom:
                    sig = find_w_bottom_signal(df_d)
                    if sig:
                        st.info(f"W底形态参考（非入库条件）: 综合分 {sig['综合分']}，颈线 {sig['颈线']}")
                    else:
                        st.caption("当前参数下未检出典型 W底 结构。")

# --- Tab 1: 基建（不过滤涨跌停，全市场收录）---
with tabs[1]:
    st.write(f"当前库已录入：{len(db)} 只。")
    if st.button("开始增量基建普查", key="btn_infra"):
        pool = [
            f"{p}{i:03d}"
            for p in ["600", "601", "603", "605", "000", "001", "002", "003"]
            for i in range(1000)
        ]
        codes_in_db = set(db["code"].tolist())
        todo = [c for c in pool if c not in codes_in_db]
        if not todo:
            st.success("库已最新！")
        else:
            bar1 = st.progress(0.0)
            st_txt = st.empty()
            new_list = []

            def task_infra(c):
                d, n = SoulEngine.get_data(c, days=650, retries=0, timeout=2.8)
                if d is not None and len(d) >= 150:
                    ck = normalize_code(c)
                    ma = SoulEngine.calculate_best_ma(d)
                    counts, price_bins = np.histogram(d["收盘"].tail(125), bins=40)
                    
                    # 提取前400天的高低价数据
                    df_400 = d.tail(400).reset_index(drop=True)
                    highs_400 = df_400["最高"].tolist()
                    lows_400 = df_400["最低"].tolist()
                    
                    return {
                        "code": ck,
                        "name": n,
                        "best_ma": ma,
                        "h_floor": round(price_bins[np.argmax(counts)], 2),
                        "highs_400": json.dumps(highs_400),
                        "lows_400": json.dumps(lows_400),
                    }
                return None

            with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
                futures = {executor.submit(task_infra, c): c for c in todo}
                for i, f in enumerate(as_completed(futures)):
                    res = f.result()
                    if res:
                        new_list.append(res)
                    if i % 20 == 0:
                        bar1.progress((i + 1) / len(todo))
                        st_txt.text(f"已处理: {i} / {len(todo)}")
                    if i % 100 == 0 and new_list:
                        update_db(new_list)
                        new_list = []
            if new_list:
                update_db(new_list)
            st.success("基建完成")
            st.caption("提示：基建阶段不过滤涨跌停，便于全市场建库；其他扫描 Tab 会过滤。")

# --- Tab 2: 强势突破 ---
with tabs[2]:
    st.header("🎯 强势突破狙击")
    vol_m3 = st.slider("成交量放大倍数", 1.0, 3.0, 1.5, key="t3_vol")
    min_score_t3 = st.slider("最低综合评分", 40, 95, 60, key="t3_min_score")
    if st.button("开始全市场突破扫描", key="btn_t3"):
        if not ensure_db_ready(db):
            st.stop()
        bar3, hits3 = st.progress(0.0), []

        def task3(row):
            ck = normalize_code(row["code"])
            df, n = SoulEngine.get_data(ck, days=60)
            if is_limit_move(df, ck):
                return None
            if df is not None and len(df) >= 30:
                b_ma = int(row["best_ma"])
                ma_l = df["收盘"].rolling(window=b_ma).mean()
                if df["收盘"].iloc[-1] > ma_l.iloc[-1] and df["收盘"].iloc[-2] <= ma_l.iloc[-2]:
                    v_avg5 = df["成交量"].iloc[-6:-1].mean()
                    if v_avg5 <= 0:
                        return None
                    vol_ratio = df["成交量"].iloc[-1] / v_avg5
                    if vol_ratio >= vol_m3:
                        ma_now = ma_l.iloc[-1]
                        ma_prev_5 = ma_l.iloc[-6] if len(ma_l.dropna()) >= 6 else ma_l.iloc[-2]
                        slope_pct = (ma_now / ma_prev_5 - 1) * 100 if ma_prev_5 > 0 else 0
                        trend_score = min(40, max(0, slope_pct * 40))
                        volume_score = min(30, max(0, (vol_ratio - vol_m3) * 25 + 18))
                        dist_pct = abs(df["收盘"].iloc[-1] / ma_now - 1) * 100 if ma_now > 0 else 99
                        position_score = min(30, max(0, (2.5 - dist_pct) * 12))
                        total_score = round(trend_score + volume_score + position_score, 1)
                        if total_score >= min_score_t3:
                            return {
                                "名称": n,
                                "代码": ck,
                                "综合分": total_score,
                                "趋势分": round(trend_score, 1),
                                "量能分": round(volume_score, 1),
                                "位置分": round(position_score, 1),
                                "灵魂线": f"MA{b_ma}",
                                "放量": f"{vol_ratio:.2f}倍",
                                "离线": f"{dist_pct:.2f}%",
                            }
            return None

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            futures3 = [executor.submit(task3, r) for _, r in db.iterrows()]
            for i, f in enumerate(as_completed(futures3)):
                if i % 30 == 0:
                    bar3.progress((i + 1) / len(db))
                res = f.result()
                if res:
                    hits3.append(res)
        if hits3:
            df3 = pd.DataFrame(hits3).sort_values(by="综合分", ascending=False).reset_index(drop=True)
            st.table(df3)
            st.download_button("💾 下载 Excel", to_excel(df3), "强势突破.xlsx")
        else:
            st.info("本次无满足评分阈值的标的，可尝试降低最低综合评分。")

# --- Tab 3: 地量回踩 ---
with tabs[3]:
    if st.button("开始地量回踩扫描", key="btn_t4"):
        if not ensure_db_ready(db):
            st.stop()
        bar4, hits4 = st.progress(0.0), []

        def task4(row):
            ck = normalize_code(row["code"])
            df, n = SoulEngine.get_data(ck, days=100)
            if is_limit_move(df, ck):
                return None
            if df is not None and len(df) >= 60:
                b_ma = int(row["best_ma"])
                max_v = df["成交量"].tail(20).max()
                shrink = df["成交量"].iloc[-1] / max_v
                ma_v = df["收盘"].rolling(window=b_ma).mean().iloc[-1]
                bias = (df["收盘"].iloc[-1] / ma_v - 1) * 100
                if shrink <= 0.35 and df["收盘"].iloc[-1] > ma_v and bias <= 2.0:
                    return {
                        "名称": n,
                        "代码": ck,
                        "地量比": f"{shrink*100:.1f}%",
                        "偏离": f"{bias:.2f}%",
                        "灵魂线": f"MA{b_ma}",
                    }
            return None

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            futures4 = [executor.submit(task4, r) for _, r in db.iterrows()]
            for i, f in enumerate(as_completed(futures4)):
                if i % 30 == 0:
                    bar4.progress((i + 1) / len(db))
                res = f.result()
                if res:
                    hits4.append(res)
        if hits4:
            st.table(pd.DataFrame(hits4))
            st.download_button("💾 下载 Excel", to_excel(pd.DataFrame(hits4)), "地量回踩.xlsx")

# --- Tab 4: 三线共振 ---
with tabs[4]:
    max_a_t5 = st.slider("今日最大振幅 (%)", 0.5, 3.0, 1.5, key="t5_a")
    if st.button("开始三线共振扫描", key="btn_t5"):
        if not ensure_db_ready(db):
            st.stop()
        bar5, hits5 = st.progress(0.0), []

        def task5(row):
            ck = normalize_code(row["code"])
            df, n = SoulEngine.get_data(ck, days=350)
            if is_limit_move(df, ck):
                return None
            if df is not None and len(df) >= 200:
                amp = (df["最高"].iloc[-1] - df["最低"].iloc[-1]) / df["收盘"].iloc[-2] * 100
                if amp <= max_a_t5:
                    p_c, b_ma = df["收盘"].iloc[-1], int(row["best_ma"])
                    m_s = df["收盘"].rolling(window=b_ma).mean().iloc[-1]
                    m6 = df["收盘"].rolling(window=60).mean().iloc[-1]
                    m2 = df["收盘"].rolling(window=200).mean().iloc[-1]
                    d1, d2, d3 = abs(p_c / m_s - 1) * 100, abs(p_c / m6 - 1) * 100, abs(p_c / m2 - 1) * 100
                    min_d = min(d1, d2, d3)
                    if min_d <= 2.0:
                        l_n = "灵魂线" if min_d == d1 else ("MA60" if min_d == d2 else "MA200")
                        return {
                            "名称": n,
                            "代码": ck,
                            "支撑": l_n,
                            "距离": f"{min_d:.2f}%",
                            "振幅": f"{amp:.2f}%",
                        }
            return None

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            futures5 = [executor.submit(task5, r) for _, r in db.iterrows()]
            for i, f in enumerate(as_completed(futures5)):
                if i % 30 == 0:
                    bar5.progress((i + 1) / len(db))
                res = f.result()
                if res:
                    hits5.append(res)
        if hits5:
            st.table(pd.DataFrame(hits5))
            st.download_button("💾 下载 Excel", to_excel(pd.DataFrame(hits5)), "三线共振.xlsx")

# --- Tab 5: 绝对地量 ---
with tabs[5]:
    min_d_t6 = st.slider("地量天数纪录", 6, 40, 10, key="t6_d")
    if st.button("启动极速地量扫描", key="btn_t6"):
        if not ensure_db_ready(db):
            st.stop()
        bar6, hits6 = st.progress(0.0), []

        def task6(row):
            ck = normalize_code(row["code"])
            df, n = SoulEngine.get_data(ck, days=50)
            if is_limit_move(df, ck):
                return None
            if df is not None and len(df) >= 30:
                today_v = df["成交量"].iloc[-1]
                cnt = 0
                for d in range(1, len(df["成交量"])):
                    if today_v < df["成交量"].iloc[-(d + 1) : -1].min():
                        cnt = d
                    else:
                        break
                if cnt >= min_d_t6:
                    return {"名称": n, "代码": ck, "地量纪录": f"近 {cnt} 日最低", "价格": df["收盘"].iloc[-1]}
            return None

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            futures6 = [executor.submit(task6, r) for _, r in db.iterrows()]
            for i, f in enumerate(as_completed(futures6)):
                if i % 30 == 0:
                    bar6.progress((i + 1) / len(db))
                res = f.result()
                if res:
                    hits6.append(res)
        if hits6:
            st.table(pd.DataFrame(hits6))
            st.download_button("💾 下载 Excel", to_excel(pd.DataFrame(hits6)), "绝对地量.xlsx")

# --- Tab 6: 墨菲平台（综合扫描）---
with tabs[6]:
    st.header("🏗️ 墨菲平台综合扫描")
    st.caption("基于《期货市场技术分析》的综合选股系统，可组合多个条件筛选")
    
    st.divider()
    st.subheader("📋 选择筛选条件")
    
    # 条件选择
    col1, col2 = st.columns(2)
    with col1:
        use_floor = st.checkbox("半年平台铁底", value=False, help="现价接近半年地基")
        use_rsi = st.checkbox("RSI指标", value=False, help="RSI超买超卖信号")
        use_kdj = st.checkbox("KDJ指标", value=False, help="KDJ金叉死叉信号")
        use_macd = st.checkbox("MACD指标", value=False, help="MACD多空信号")
    with col2:
        use_pattern = st.checkbox("形态识别", value=False, help="头肩、双顶、三角形形态")
        use_elliott = st.checkbox("艾略特波浪", value=False, help="波浪结构分析")
        use_cycle = st.checkbox("时间周期", value=False, help="FFT周期检测")
    
    st.divider()
    st.subheader("⚙️ 参数配置")
    
    # 参数配置
    col3, col4 = st.columns(2)
    with col3:
        if use_floor:
            floor_bias_max = st.slider("铁底偏离上限(%)", 0.5, 5.0, 2.0, key="floor_bias")
        if use_rsi:
            rsi_signal = st.selectbox("RSI信号", ["超卖(<30)", "超买(>70)", "中性(30-70)"], key="rsi_sig")
        if use_kdj:
            kdj_signal = st.selectbox("KDJ信号", ["超卖", "超买", "金叉", "死叉", "中性"], key="kdj_sig")
    with col4:
        if use_macd:
            macd_signal = st.selectbox("MACD信号", ["多头", "空头", "金叉", "死叉", "中性"], key="macd_sig")
        if use_pattern:
            pattern_type = st.selectbox("形态类型", ["头肩顶/底", "双顶/双底", "三角形"], key="pat_type")
    
    if st.button("🚀 开始综合扫描", key="btn_comprehensive"):
        if not ensure_db_ready(db):
            st.stop()
        
        # 检查是否至少选择一个条件
        if not any([use_floor, use_rsi, use_kdj, use_macd, use_pattern, use_elliott, use_cycle]):
            st.warning("请至少选择一个筛选条件")
            st.stop()
        
        bar_comp, hits_comp = st.progress(0.0), []
        
        def task_comprehensive(row):
            ck = normalize_code(row["code"])
            df, name = SoulEngine.get_data(ck, days=300)
            if df is None or len(df) < 100:
                return None
            
            result = {"名称": name, "代码": ck}
            conditions_met = []
            
            # 半年平台铁底
            if use_floor:
                if "h_floor" not in db.columns or pd.isna(row.get("h_floor")):
                    return None
                bias = (df["收盘"].iloc[-1] / row["h_floor"] - 1) * 100
                if 0 <= bias <= floor_bias_max:
                    result["半年地基"] = row["h_floor"]
                    result["偏离%"] = f"{bias:.2f}"
                    conditions_met.append("铁底")
                else:
                    return None
            
            # RSI指标
            if use_rsi:
                rsi = TechnicalIndicators.calculate_rsi(df, period=14)
                if rsi is None:
                    return None
                current_rsi = rsi[-1]
                result["RSI"] = round(current_rsi, 2)
                
                if rsi_signal == "超卖(<30)" and current_rsi >= 30:
                    return None
                elif rsi_signal == "超买(>70)" and current_rsi <= 70:
                    return None
                elif rsi_signal == "中性(30-70)" and (current_rsi < 30 or current_rsi > 70):
                    return None
                conditions_met.append("RSI")
            
            # KDJ指标
            if use_kdj:
                k, d, j = TechnicalIndicators.calculate_kdj(df, n=9, m1=3, m2=3)
                if k is None:
                    return None
                current_k = k[-1]
                current_d = d[-1]
                result["K"] = round(current_k, 2)
                result["D"] = round(current_d, 2)
                
                if kdj_signal == "超卖" and not (current_k < 20 and current_d < 20):
                    return None
                elif kdj_signal == "超买" and not (current_k > 80 and current_d > 80):
                    return None
                elif kdj_signal == "金叉" and not (current_k > current_d and k[-2] <= d[-2]):
                    return None
                elif kdj_signal == "死叉" and not (current_k < current_d and k[-2] >= d[-2]):
                    return None
                conditions_met.append("KDJ")
            
            # MACD指标
            if use_macd:
                dif, dea, macd = TechnicalIndicators.calculate_macd(df, fast=12, slow=26, signal=9)
                if dif is None:
                    return None
                current_dif = dif[-1]
                current_dea = dea[-1]
                current_macd = macd[-1]
                result["DIF"] = round(current_dif, 4)
                result["DEA"] = round(current_dea, 4)
                
                if macd_signal == "多头" and not (current_dif > current_dea and current_macd > 0):
                    return None
                elif macd_signal == "空头" and not (current_dif < current_dea and current_macd < 0):
                    return None
                elif macd_signal == "金叉" and not (current_dif > current_dea and dif[-2] <= dea[-2]):
                    return None
                elif macd_signal == "死叉" and not (current_dif < current_dea and dif[-2] >= dea[-2]):
                    return None
                conditions_met.append("MACD")
            
            # 形态识别
            if use_pattern:
                pattern_result = None
                if pattern_type == "头肩顶/底":
                    pattern_result = PatternRecognition.detect_head_and_shoulders(df, window=150)
                elif pattern_type == "双顶/双底":
                    pattern_result = PatternRecognition.detect_double_top_bottom(df, window=150)
                elif pattern_type == "三角形":
                    pattern_result = PatternRecognition.detect_triangle(df, window=60)
                
                if pattern_result:
                    result["形态"] = pattern_result["类型"]
                    conditions_met.append("形态")
                else:
                    return None
            
            # 艾略特波浪
            if use_elliott:
                wave_result = ElliottWaveAnalysis.detect_wave_structure(df, window=200)
                if wave_result:
                    result["浪型"] = wave_result["当前浪型"]
                    conditions_met.append("波浪")
                else:
                    return None
            
            # 时间周期
            if use_cycle:
                cycle_result = TimeCycleAnalysis.detect_time_cycles(df, window=250)
                if cycle_result:
                    result["周期(日)"] = cycle_result["检测周期(交易日)"]
                    result["周期阶段"] = cycle_result["周期阶段"]
                    conditions_met.append("周期")
                else:
                    return None
            
            result["满足条件"] = ", ".join(conditions_met)
            result["条件数"] = len(conditions_met)
            return result
        
        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            futs = [executor.submit(task_comprehensive, r) for _, r in db.iterrows()]
            for i, f in enumerate(as_completed(futs)):
                if i % 25 == 0:
                    bar_comp.progress((i + 1) / len(db))
                res = f.result()
                if res:
                    hits_comp.append(res)
        
        if hits_comp:
            df_comp = pd.DataFrame(hits_comp)
            df_comp = df_comp.sort_values("条件数", ascending=False)
            st.success(f"找到 {len(hits_comp)} 只满足条件的股票")
            st.table(df_comp)
            st.download_button("💾 综合扫描 Excel", to_excel(df_comp), "Murphy_Comprehensive.xlsx")
        else:
            st.info("无结果，请尝试放宽条件或减少筛选条件")

# --- Tab 7: 极致缩量 ---
with tabs[7]:
    st.header("🌊 极致缩量")
    st.caption("今日成交量相对近20日均量显著萎缩，且收盘在灵魂线上方（偏蓄势）。")
    vol_ratio_max = st.slider("今日量 / 20日均量 上限", 0.15, 0.6, 0.35, key="t7_shrink")
    if st.button("开始极致缩量扫描", key="btn_t7_shrink"):
        if not ensure_db_ready(db):
            st.stop()
        bar7b, hits7b = st.progress(0.0), []

        def task7b(row):
            ck = normalize_code(row["code"])
            df, n = SoulEngine.get_data(ck, days=80)
            if is_limit_move(df, ck):
                return None
            if df is None or len(df) < 30:
                return None
            b_ma = int(row["best_ma"])
            ma_s = df["收盘"].rolling(window=b_ma).mean().iloc[-1]
            v20 = df["成交量"].tail(20).mean()
            if v20 <= 0:
                return None
            r = df["成交量"].iloc[-1] / v20
            if r <= vol_ratio_max and df["收盘"].iloc[-1] > ma_s:
                return {"名称": n, "代码": ck, "量比20日": f"{r:.2f}", "灵魂线": f"MA{b_ma}"}
            return None

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            futs = [executor.submit(task7b, r) for _, r in db.iterrows()]
            for i, f in enumerate(as_completed(futs)):
                if i % 30 == 0:
                    bar7b.progress((i + 1) / len(db))
                res = f.result()
                if res:
                    hits7b.append(res)
        if hits7b:
            st.table(pd.DataFrame(hits7b))
            st.download_button("💾 下载 Excel", to_excel(pd.DataFrame(hits7b)), "极致缩量.xlsx")
        else:
            st.info("无命中，可放宽量比上限。")

# --- Tab 8: 金叉狙击 ---
with tabs[8]:
    st.header("⚡ 金叉狙击（MA5 上穿 MA10）")
    need_vol = st.checkbox("要求放量确认", value=True, key="t8_gc_vol")
    vol_mult = st.slider("放量：相对5日均量倍数", 1.0, 2.5, 1.2, key="t8_gc_vm")
    if st.button("开始金叉扫描", key="btn_gc"):
        if not ensure_db_ready(db):
            st.stop()
        bar_gc, hits_gc = st.progress(0.0), []

        def task_gc(row):
            ck = normalize_code(row["code"])
            df, n = SoulEngine.get_data(ck, days=80)
            if is_limit_move(df, ck):
                return None
            if df is None or len(df) < 30:
                return None
            c = df["收盘"]
            ma5 = c.rolling(5).mean()
            ma10 = c.rolling(10).mean()
            ma20 = c.rolling(20).mean()
            
            # 检测当前是否金叉
            if ma5.iloc[-1] <= ma10.iloc[-1] or ma5.iloc[-2] > ma10.iloc[-2]:
                return None
            
            # 检测下降趋势：MA20在最近20天内持续下跌
            ma20_recent = ma20.tail(20).dropna()
            if len(ma20_recent) < 15:
                return None
            is_downtrend = all(ma20_recent.iloc[i] > ma20_recent.iloc[i+1] for i in range(len(ma20_recent)-1))
            if not is_downtrend:
                return None
            
            # 统计下降趋势以来的金叉次数
            cross_count = 0
            for i in range(len(ma5) - 1, max(0, len(ma5) - 40), -1):
                if i < 1 or pd.isna(ma5.iloc[i]) or pd.isna(ma10.iloc[i]):
                    continue
                if ma5.iloc[i] > ma10.iloc[i] and ma5.iloc[i-1] <= ma10.iloc[i-1]:
                    cross_count += 1
                    if cross_count >= 2:
                        break
            
            # 只选择第二次金叉
            if cross_count != 2:
                return None
            
            if need_vol:
                v5 = df["成交量"].iloc[-6:-1].mean()
                if v5 <= 0 or df["成交量"].iloc[-1] < v5 * vol_mult:
                    return None
            return {"名称": n, "代码": ck, "MA5": round(ma5.iloc[-1], 2), "MA10": round(ma10.iloc[-1], 2), "金叉序数": f"第{cross_count}次"}

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            futs = [executor.submit(task_gc, r) for _, r in db.iterrows()]
            for i, f in enumerate(as_completed(futs)):
                if i % 30 == 0:
                    bar_gc.progress((i + 1) / len(db))
                res = f.result()
                if res:
                    hits_gc.append(res)
        if hits_gc:
            st.table(pd.DataFrame(hits_gc))
            st.download_button("💾 下载 Excel", to_excel(pd.DataFrame(hits_gc)), "金叉狙击.xlsx")
        else:
            st.info("无命中，可关闭放量或降低倍数。")

# --- Tab 9: 趋势线蓄势（墨菲 Pro 破阵蓄力）---
with tabs[9]:
    st.header("🚩 墨菲趋势线：突破后的蓄势盘整")
    st.markdown(
        """
**逻辑说明：**
1. 不一定非得是今天突破的。
2. 在过去 N 天内带量突破严格压力线，且突破后最低价未跌破突破日「线上价位」。
"""
    )
    col_t1, col_t2 = st.columns(2)
    with col_t1:
        search_window = st.slider("突破可追溯天数", 1, 10, 5, help="在最近若干交易日内寻找突破日")
    with col_t2:
        vol_req = st.slider("突破当天的放量倍数", 1.0, 3.0, 1.5, help="相对突破前5日均量")

    if st.button("开始【破阵蓄力】全市场扫描", key="btn_t10"):
        if db.empty:
            st.error("请先做基建！")
        else:
            bar_10, hits_10 = st.progress(0.0), []

            def task_t10(row):
                code_key = normalize_code(row["code"])
                if len(code_key) != 6:
                    return None
                
                # 优先使用预存的400天数据
                highs_400_str = row.get("highs_400", "")
                lows_400_str = row.get("lows_400", "")
                
                if highs_400_str and lows_400_str:
                    try:
                        highs_400 = json.loads(highs_400_str)
                        lows_400 = json.loads(lows_400_str)
                        # 使用预存数据构建DataFrame
                        df, name = SoulEngine.get_data(code_key, days=400)
                        if df is not None and len(df) >= 250:
                            df_w = df.tail(250).reset_index(drop=True)
                            # 使用预存的高低价数据
                            # 这里可以进一步优化，直接使用数组进行计算
                        else:
                            return None
                    except:
                        # 如果JSON解析失败，回退到实时获取
                        df, name = SoulEngine.get_data(code_key, days=400)
                else:
                    # 如果没有预存数据，实时获取
                    df, name = SoulEngine.get_data(code_key, days=400)
                
                if df is not None and len(df) >= 250:
                    l_vals, touches, df_w = MurphyEnginePro.get_strict_trendline(df, window=250)
                    if l_vals is not None:
                        closes = df_w["收盘"].values
                        lows = df_w["最低"].values
                        vols = df_w["成交量"].values
                        break_idx = -1
                        for i in range(1, search_window + 1):
                            idx = len(df_w) - i
                            idx_prev = idx - 1
                            p_c = closes[idx]
                            p_y = closes[idx_prev]
                            l_c = l_vals[idx]
                            l_y = l_vals[idx_prev]
                            if p_c > l_c and p_y <= l_y:
                                v_c = vols[idx]
                                lo = max(0, idx_prev - 4)
                                v_a5 = df_w["成交量"].iloc[lo : idx_prev + 1].mean()
                                if v_c >= v_a5 * vol_req:
                                    break_idx = idx
                                    break
                        if break_idx != -1:
                            break_line_price = l_vals[break_idx]
                            is_safe = True
                            for j in range(break_idx + 1, len(df_w)):
                                if lows[j] < break_line_price:
                                    is_safe = False
                                    break
                            if is_safe:
                                days_ago = len(df_w) - 1 - break_idx
                                p_today = closes[-1]
                                if exclude_limit_up and (p_today / closes[-2] - 1) * 100 > 9.3:
                                    return None
                                dist_pct = (
                                    (p_today / break_line_price - 1) * 100 if break_line_price > 0 else 0.0
                                )
                                return {
                                    "名称": name,
                                    "代码": code_key,
                                    "压制次数": f"{touches}次",
                                    "状态": "🚀今日刚突破" if days_ago == 0 else f"🔥突破蓄势 ({days_ago}天前)",
                                    "突破防守价": round(break_line_price, 2),
                                    "今日现价": p_today,
                                    "距防守线": f"{dist_pct:.2f}%",
                                    "_距防守线数值": round(float(dist_pct), 4),
                                    "df": df_w,
                                    "line": l_vals,
                                }
                return None

            with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
                futs = [executor.submit(task_t10, r) for _, r in db.iterrows()]
                for i, f in enumerate(as_completed(futs)):
                    if i % 20 == 0:
                        bar_10.progress((i + 1) / len(db))
                    res = f.result()
                    if res:
                        hits_10.append(res)

            if hits_10:
                seen = set()
                deduped = []
                for h in hits_10:
                    c = normalize_code(h.get("代码", ""))
                    if c in seen:
                        continue
                    seen.add(c)
                    deduped.append(h)
                hits_10 = deduped
                st.success(f"发现 {len(hits_10)} 只「破阵后踩稳防线」标的。")
                clean_hits = [
                    {k: v for k, v in h.items() if k not in ["df", "line", "_距防守线数值"]} for h in hits_10
                ]
                df_res_10 = pd.DataFrame(clean_hits)
                order = [h["_距防守线数值"] for h in hits_10]
                df_res_10 = df_res_10.assign(_sort=order).sort_values("_sort").drop(columns=["_sort"])
                st.table(df_res_10)
                st.download_button("💾 下载名单 Excel", to_excel(df_res_10), "Trendline_Hold.xlsx")
                st.subheader("🖼️ 突破蓄势视觉确认（前10）")
                hits_sorted = sorted(hits_10, key=lambda x: x.get("_距防守线数值", 0.0))[:10]
                for h in hits_sorted:
                    with st.expander(f"🚩 {h['名称']} ({h['代码']}) - {h['状态']}"):
                        fig = go.Figure()
                        fig.add_trace(
                            go.Candlestick(
                                x=h["df"]["日期"],
                                open=h["df"]["开盘"],
                                high=h["df"]["最高"],
                                low=h["df"]["最低"],
                                close=h["df"]["收盘"],
                                name="K线",
                            )
                        )
                        fig.add_trace(
                            go.Scatter(
                                x=h["df"]["日期"],
                                y=h["line"],
                                name="绝对压制线",
                                line=dict(color="red", width=2),
                            )
                        )
                        fig.add_hline(
                            y=h["突破防守价"],
                            line_dash="dot",
                            line_color="green",
                            annotation_text="防守参考价",
                        )
                        fig.update_layout(template="plotly_dark", height=450, xaxis_rangeslider_visible=False)
                        st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("全市场暂无满足条件的标的，可放宽「突破可追溯天数」或放量倍数。")

    st.divider()
    st.subheader("📊 反弹高点连线（使用预存400天数据）")
    st.caption("直接使用基建时预存的前400天高低价数据，识别反弹高点并连线，无需重复请求API。")
    
    col_s1, col_s2, col_s3 = st.columns(3)
    with col_s1:
        swing_look_400 = st.slider("反弹高点识别半径", 1, 4, 2, key="swing_400")
    with col_s2:
        min_rise_pct = st.slider("最小反弹幅度(%)", 5.0, 20.0, 10.0, key="rise_pct")
    with col_s3:
        min_days_between = st.slider("相邻高点最小间隔(交易日)", 10, 40, 20, key="days_between")
    
    touch_tol_400 = st.slider("触碰容差(%)", 0.5, 3.0, 1.5, key="touch_400") / 100.0
    
    if st.button("开始【反弹高点连线】扫描（预存数据）", key="btn_swing_400"):
        if db.empty:
            st.error("请先做基建！")
        else:
            # 检查是否有预存数据
            has_400_data = db["highs_400"].str.len() > 0
            if has_400_data.sum() == 0:
                st.warning("数据库中没有预存的400天数据，请先重新运行基建。")
            else:
                bar_swing, hits_swing = st.progress(0.0), []
                
                def task_swing_400(row):
                    ck = normalize_code(row["code"])
                    highs_str = row.get("highs_400", "")
                    lows_str = row.get("lows_400", "")
                    name = row.get("name", "未知")
                    
                    if not highs_str or not lows_str:
                        return None
                    
                    try:
                        highs = np.array(json.loads(highs_str))
                        lows = np.array(json.loads(lows_str))
                        
                        if len(highs) < 50:
                            return None
                        
                        # 识别反弹高点
                        swing_idx = MurphyReboundChannelEngine._swing_high_indices(highs, look=int(swing_look_400))
                        
                        if len(swing_idx) < 3:
                            return None
                        
                        # 尝试多种连接方式，选择最佳
                        candidates = []
                        n = len(highs)
                        
                        # 方式1: 全部反弹高点的最小二乘拟合
                        xs = np.array(swing_idx, dtype=float)
                        ys = highs[swing_idx]
                        k_ls, b_ls = np.polyfit(xs, ys, 1)
                        if k_ls < 0:
                            line_vals = k_ls * np.arange(n, dtype=float) + b_ls
                            tscore = MurphyReboundChannelEngine._touch_score(swing_idx, highs, line_vals, near_pct=touch_tol_400)
                            n_pierce, pierce_ok, events_max = MurphyReboundChannelEngine._pierce_stats(highs, line_vals, pierce_max_pct=0.01)
                            all_on = MurphyReboundChannelEngine._all_swings_near_line(swing_idx, highs, line_vals, tol=0.012)
                            if tscore >= 2:
                                candidates.append({
                                    "line_vals": line_vals,
                                    "k": k_ls,
                                    "b": b_ls,
                                    "tscore": tscore,
                                    "n_pierce": n_pierce,
                                    "pierce_ok": pierce_ok,
                                    "all_on": all_on,
                                    "mode": "全反弹高点拟合"
                                })
                        
                        # 方式2: 尝试连接任意两个反弹高点
                        swing_idx_sorted = sorted(swing_idx)
                        for i in range(len(swing_idx_sorted)):
                            for j in range(i + 1, len(swing_idx_sorted)):
                                idx1, idx2 = swing_idx_sorted[i], swing_idx_sorted[j]
                                if idx2 - idx1 < 20:  # 两个高点之间至少间隔20天
                                    continue
                                h1, h2 = highs[idx1], highs[idx2]
                                k = (h2 - h1) / (idx2 - idx1)
                                if k >= 0:
                                    continue
                                b = h1 - k * idx1
                                line_vals = k * np.arange(n, dtype=float) + b
                                tscore = MurphyReboundChannelEngine._touch_score(swing_idx, highs, line_vals, near_pct=touch_tol_400)
                                if tscore >= 2:
                                    n_pierce, pierce_ok, events_max = MurphyReboundChannelEngine._pierce_stats(highs, line_vals, pierce_max_pct=0.01)
                                    all_on = MurphyReboundChannelEngine._all_swings_near_line(swing_idx, highs, line_vals, tol=0.012)
                                    candidates.append({
                                        "line_vals": line_vals,
                                        "k": k,
                                        "b": b,
                                        "tscore": tscore,
                                        "n_pierce": n_pierce,
                                        "pierce_ok": pierce_ok,
                                        "all_on": all_on,
                                        "mode": f"连接高点{idx1}-{idx2}"
                                    })
                        
                        # 方式3: 连接最近的几个反弹高点
                        if len(swing_idx_sorted) >= 3:
                            # 最近3个
                            idx1, idx2, idx3 = swing_idx_sorted[-3], swing_idx_sorted[-2], swing_idx_sorted[-1]
                            k1, b1 = MurphyReboundChannelEngine._line_from_two(idx1, highs[idx1], idx3, highs[idx3], n)
                            if k1 is not None and k1 < 0:
                                line_vals = k1 * np.arange(n, dtype=float) + b1
                                tscore = MurphyReboundChannelEngine._touch_score(swing_idx, highs, line_vals, near_pct=touch_tol_400)
                                if tscore >= 2:
                                    n_pierce, pierce_ok, events_max = MurphyReboundChannelEngine._pierce_stats(highs, line_vals, pierce_max_pct=0.01)
                                    all_on = MurphyReboundChannelEngine._all_swings_near_line(swing_idx, highs, line_vals, tol=0.012)
                                    candidates.append({
                                        "line_vals": line_vals,
                                        "k": k1,
                                        "b": b1,
                                        "tscore": tscore,
                                        "n_pierce": n_pierce,
                                        "pierce_ok": pierce_ok,
                                        "all_on": all_on,
                                        "mode": "最近3个高点连线"
                                    })
                            # 最近2个
                            k2, b2 = MurphyReboundChannelEngine._line_from_two(idx2, highs[idx2], idx3, highs[idx3], n)
                            if k2 is not None and k2 < 0:
                                line_vals = k2 * np.arange(n, dtype=float) + b2
                                tscore = MurphyReboundChannelEngine._touch_score(swing_idx, highs, line_vals, near_pct=touch_tol_400)
                                if tscore >= 2:
                                    n_pierce, pierce_ok, events_max = MurphyReboundChannelEngine._pierce_stats(highs, line_vals, pierce_max_pct=0.01)
                                    all_on = MurphyReboundChannelEngine._all_swings_near_line(swing_idx, highs, line_vals, tol=0.012)
                                    candidates.append({
                                        "line_vals": line_vals,
                                        "k": k2,
                                        "b": b2,
                                        "tscore": tscore,
                                        "n_pierce": n_pierce,
                                        "pierce_ok": pierce_ok,
                                        "all_on": all_on,
                                        "mode": "最近2个高点连线"
                                    })
                        
                        if not candidates:
                            return None
                        
                        # 选择最佳候选：优先考虑触碰分，其次考虑穿刺次数，最后考虑是否全贴合
                        def sort_key(c):
                            return (c["tscore"], -c["n_pierce"], c["all_on"], c["pierce_ok"])
                        
                        best = max(candidates, key=sort_key)
                        
                        featured = bool(best["all_on"] and best["pierce_ok"] and best["n_pierce"] <= 3)
                        
                        return {
                            "名称": name,
                            "代码": ck,
                            "评级": "⭐重点推荐" if featured else "备选",
                            "反弹高点数": len(swing_idx),
                            "触碰分": best["tscore"],
                            "穿刺次数": best["n_pierce"],
                            "最大穿刺%": "0%",  # 简化，不再计算
                            "全高点贴合": "是" if best["all_on"] else "否",
                            "斜率k": round(best["k"], 6),
                            "拟合方式": best["mode"],
                            "_sort_feat": 1 if featured else 0,
                            "_sort_touch": best["tscore"],
                            "highs": highs,
                            "line": best["line_vals"],
                            "swing_indices": list(swing_idx),
                        }
                    except Exception as e:
                        return None
                
                # 只处理有预存数据的行
                db_with_data = db[db["highs_400"].str.len() > 0]
                
                with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
                    futs = [executor.submit(task_swing_400, r) for _, r in db_with_data.iterrows()]
                    for i, f in enumerate(as_completed(futs)):
                        if i % 25 == 0:
                            bar_swing.progress((i + 1) / len(db_with_data))
                        res = f.result()
                        if res:
                            hits_swing.append(res)
                
                if hits_swing:
                    seen = set()
                    dedup = []
                    for h in hits_swing:
                        c = h["代码"]
                        if c in seen:
                            continue
                        seen.add(c)
                        dedup.append(h)
                    hits_swing = sorted(dedup, key=lambda x: (x["_sort_feat"], x["_sort_touch"]), reverse=True)
                    clean = [
                        {k: v for k, v in h.items() if k not in ["highs", "line", "swing_indices", "_sort_feat", "_sort_touch"]}
                        for h in hits_swing
                    ]
                    # 确保显示拟合方式
                    for h in clean:
                        if "拟合方式" not in h:
                            h["拟合方式"] = "未知"
                    st.success(f"命中 {len(hits_swing)} 只（按重点推荐 + 触碰分排序）。")
                    st.table(pd.DataFrame(clean))
                    st.download_button("💾 反弹高点连线 Excel", to_excel(pd.DataFrame(clean)), "Swing_High_Line_400.xlsx")
                    st.subheader("图例确认（前12）")
                    for h in hits_swing[:12]:
                        with st.expander(f"{h['评级']} {h['名称']} ({h['代码']})"):
                            fig = go.Figure()
                            # 绘制价格线（使用索引作为x轴）
                            fig.add_trace(
                                go.Scatter(
                                    x=list(range(len(h["highs"]))),
                                    y=h["highs"],
                                    name="最高价",
                                    line=dict(color="blue", width=1),
                                )
                            )
                            fig.add_trace(
                                go.Scatter(
                                    x=list(range(len(h["line"]))),
                                    y=h["line"],
                                    name="反弹高点连线",
                                    line=dict(color="orange", width=2),
                                )
                            )
                            # 标记反弹高点
                            sx = h["swing_indices"]
                            sy = h["highs"][sx].tolist()
                            fig.add_trace(
                                go.Scatter(
                                    x=sx,
                                    y=sy,
                                    mode="markers",
                                    name="反弹高点",
                                    marker=dict(size=8, color="cyan"),
                                )
                            )
                            fig.update_layout(template="plotly_dark", height=420, xaxis_title="交易日索引（0=最旧，399=最新）", yaxis_title="价格")
                            st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("无命中：可放宽触碰容差或降低识别半径。")
    
    st.divider()
    st.subheader("附加：去噪压力线 · 近两日收盘突破（V26）")
    breakout_min_v26 = st.slider("有效突破幅度(%)", 0.1, 3.0, 0.5, key="t8_break_min_v26")
    if st.button("开始两日突破扫描", key="btn_t8_v26"):
        if not ensure_db_ready(db):
            st.stop()
        bar_v26, hits_v26 = st.progress(0.0), []

        def task_v26(row):
            ck = normalize_code(row["code"])
            df, n = SoulEngine.get_data(ck, days=400)
            if is_limit_move(df, ck):
                return None
            if df is not None and len(df) >= 250:
                l_vals, touches, df_w = MurphyEngineV26.get_tight_line(df, line_type="resistance")
                if l_vals is None or len(df_w) < 5:
                    return None
                highs = df_w["最高"].values
                closes = df_w["收盘"].values
                if np.any(highs[:-2] > l_vals[:-2] * 1.001):
                    return None
                for i in (-2, -1):
                    if i - 1 < -len(df_w):
                        continue
                    prev_close = closes[i - 1]
                    prev_line = l_vals[i - 1]
                    cur_close = closes[i]
                    cur_line = l_vals[i]
                    cur_break_pct = (cur_close / cur_line - 1) * 100
                    if prev_close <= prev_line and cur_break_pct >= breakout_min_v26:
                        return {
                            "名称": n,
                            "代码": ck,
                            "独立触碰": f"{touches}次",
                            "突破日": df_w["日期"].iloc[i],
                            "突破幅度": f"{cur_break_pct:.2f}%",
                            "突破幅度值": round(float(cur_break_pct), 4),
                            "df": df_w,
                            "line": l_vals,
                        }
            return None

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            futs = [executor.submit(task_v26, r) for _, r in db.iterrows()]
            for i, f in enumerate(as_completed(futs)):
                if i % 20 == 0:
                    bar_v26.progress((i + 1) / len(db))
                res = f.result()
                if res:
                    hits_v26.append(res)
        if hits_v26:
            for h in hits_v26:
                with st.expander(f"🚩 {h['名称']} ({h['代码']}) - 突破日 {h['突破日']}"):
                    fig = go.Figure()
                    fig.add_trace(
                        go.Candlestick(
                            x=h["df"]["日期"],
                            open=h["df"]["开盘"],
                            high=h["df"]["最高"],
                            low=h["df"]["最低"],
                            close=h["df"]["收盘"],
                            name="K线",
                        )
                    )
                    fig.add_trace(
                        go.Scatter(
                            x=h["df"]["日期"],
                            y=h["line"],
                            name="压力线",
                            line=dict(color="red", width=2, dash="dash"),
                        )
                    )
                    fig.update_layout(template="plotly_dark", height=450, xaxis_rangeslider_visible=False)
                    st.plotly_chart(fig, use_container_width=True)
            out_v = (
                pd.DataFrame(hits_v26)
                .drop(columns=["df", "line"], errors="ignore")
                .sort_values(by="突破幅度值", ascending=False)
                .drop(columns=["突破幅度值"], errors="ignore")
            )
            st.download_button("💾 两日突破 Excel", to_excel(out_v), "压力线两日突破.xlsx")
        else:
            st.caption("两日突破扫描无命中。")

    st.divider()
    st.subheader("📉 下降通道 · 反弹高点连线（穿刺合规 + 稳定性）")
    st.caption(
        "识别局部反弹高点 → 优先用「全反弹高点」最小二乘拟合；若穿刺/贴合不佳则在候选中选「主峰+近端反弹」等。"
        "穿刺：连续几日最高价在线上方合并为一次，且每次最大上穿 < 设定阈值；≤3 次且全高点贴近 → 重点推荐。"
    )
    
    col_ch1, col_ch2 = st.columns(2)
    with col_ch1:
        ch_window = st.slider("分析窗口(日)", 180, 300, 250, key="ch_win")
        ch_swing = st.slider("反弹高点识别半径", 1, 4, 2, key="ch_swing")
        ch_min_rise = st.slider("最小反弹幅度(%)", 5.0, 20.0, 10.0, key="ch_min_rise")
        ch_min_days = st.slider("相邻高点最小间隔(日)", 10, 40, 20, key="ch_min_days")
    with col_ch2:
        ch_pierce_pct = st.slider("单次穿刺最大上穿(%)", 0.3, 2.0, 1.0, key="ch_pierce") / 100.0
        ch_touch_tol = st.slider("触碰/接近容差(%)", 0.5, 3.0, 1.5, key="ch_touch") / 100.0
        ch_full_tol = st.slider("「全高点连线」贴合容差(%)", 0.5, 2.0, 1.2, key="ch_full") / 100.0
        ch_max_pierce = st.slider("最大穿刺次数", 2, 6, 3, key="ch_max_pierce")
    
    ch_exclude_tail = st.slider(
        "通道判定：排除近端交易日",
        8,
        45,
        25,
        key="ch_ex_tail",
        help="末端大涨会抬高「最后60日均价」，易误判为非下降通道；排除近端若干日后再比前段与中段均价（类似东风科技突破前形态）。",
    )
    only_featured_ch = st.checkbox("仅显示重点推荐", value=False, key="ch_only_feat")
    
    st.divider()
    st.subheader("📊 回测功能 - 验证历史突破胜率")
    st.caption("对当前参数设置进行历史回测，验证突破后的胜率和平均涨幅")
    backtest_hold_days = st.slider("回测持有天数", 5, 30, 10, key="bt_hold")
    if st.button("开始回测（仅前50只股票）", key="btn_backtest"):
        if not ensure_db_ready(db):
            st.stop()
        bar_bt, results_bt = st.progress(0.0), []
        
        def task_bt(row):
            ck = normalize_code(row["code"])
            df, name = SoulEngine.get_data(ck, days=400)
            if df is None or len(df) < 300:
                return None
            bt_result = MurphyReboundChannelEngine.backtest_breakout_performance(
                df, 
                window=ch_window, 
                min_break_pct=br_min_pct, 
                hold_days=backtest_hold_days
            )
            if bt_result:
                return {"名称": name, "代码": ck, **bt_result}
            return None
        
        # 只回测前50只股票以节省时间
        sample_db = db.head(50)
        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            futs = [executor.submit(task_bt, r) for _, r in sample_db.iterrows()]
            for i, f in enumerate(as_completed(futs)):
                if i % 5 == 0:
                    bar_bt.progress((i + 1) / len(sample_db))
                res = f.result()
                if res:
                    results_bt.append(res)
        
        if results_bt:
            df_bt = pd.DataFrame(results_bt)
            avg_win_rate = df_bt["胜率%"].mean()
            avg_return = df_bt["平均涨幅%"].mean()
            st.success(f"回测完成！平均胜率: {avg_win_rate:.2f}%, 平均涨幅: {avg_return:.2f}%")
            st.table(df_bt)
        else:
            st.info("回测无结果，可能参数设置过于严格")

    st.markdown(
        "**突破扫描（压力线仍用上方同一套算法）**  "
        "形态参考：长期橙色下降压力线（多次反弹高点构成）→ 近若干日内**放量长阳**向上突破，并常伴随站上 **MA200**（如东风科技类走势）。"
        "突破日不必是今天：在「回溯交易日数」内从近到远找**最近一次**有效突破。"
    )
    br_min_pct = st.slider("收盘突破压力线幅度(%)", 0.05, 2.0, 0.2, key="ch_br_min")
    br_lookback = st.slider(
        "突破日回溯（交易日，含今日）",
        1,
        20,
        10,
        key="ch_br_lb",
        help="例如 10：在最近 10 个交易日内任意一天出现有效突破均可命中（不必是今天）。",
    )
    br_require_cross = st.checkbox("要求昨收在压力线下（干净上穿）", value=True, key="ch_br_x")
    br_still_above = st.checkbox("要求今日仍收盘在压力线上方", value=True, key="ch_br_up")
    br_keep_limit = st.checkbox(
        "保留涨停/跌停日突破（典型强突破常为涨停，默认开启）",
        value=True,
        key="ch_br_lim",
    )
    br_req_vol = st.checkbox("要求突破日放量（对前5日均量）", value=True, key="ch_br_vreq")
    br_vol_mult = st.slider("突破日量比下限", 1.0, 3.0, 1.2, key="ch_br_vm", disabled=not br_req_vol)
    br_ma200 = st.checkbox("要求突破日收盘站上 MA200", value=False, key="ch_br_m200")
    br_min_touch = st.slider("压力线触碰分下限（线越稳分越高）", 2, 8, 2, key="ch_br_mtouch")

    st.caption(
        "全市场扫描仅遍历「基因库」里的代码；若某只（如 600081）未入库，请先在 **诊断** Tab 分析入库后再扫。"
        "若仍为 0，可增大「排除近端交易日」或降低「触碰分下限」/关闭放量。"
    )
    if st.button("开始【下降通道压力线】扫描", key="btn_channel"):
        if not ensure_db_ready(db):
            st.stop()
        bar_ch, hits_ch = st.progress(0.0), []

        def task_ch(row):
            ck = normalize_code(row["code"])
            df, name = SoulEngine.get_data(ck, days=max(400, ch_window + 30))
            if is_limit_move(df, ck):
                return None
            if df is None or len(df) < ch_window:
                return None
            rec = MurphyReboundChannelEngine.build_channel_pressure(
                df,
                window=ch_window,
                swing_look=int(ch_swing),
                min_rise_pct=ch_min_rise,
                min_days_between=int(ch_min_days),
                touch_tol=ch_touch_tol,
                pierce_max_pct=ch_pierce_pct,
                max_pierce_events=int(ch_max_pierce),
                full_fit_tol=ch_full_tol,
                exclude_tail_days=int(ch_exclude_tail),
            )
            if rec is None:
                return None
            if only_featured_ch and not rec["featured"]:
                return None
            return {
                "名称": name,
                "代码": ck,
                "评级": "⭐重点推荐" if rec["featured"] else "备选",
                "拟合方式": rec["mode"],
                "触碰分": rec["touch_score"],
                "穿刺次数": rec["pierce_events"],
                "最大穿刺%": f"{rec['pierce_events_max_pct']:.2f}%",
                "全高点贴合": "是" if rec["all_swings_on_line"] else "否",
                "斜率k": round(rec["k"], 6),
                "_sort_feat": 1 if rec["featured"] else 0,
                "_sort_touch": rec["touch_score"],
                "df": rec["df_w"],
                "line": rec["line_vals"],
                "swing": rec["swing_indices"],
            }

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            futs = [executor.submit(task_ch, r) for _, r in db.iterrows()]
            for i, f in enumerate(as_completed(futs)):
                if i % 25 == 0:
                    bar_ch.progress((i + 1) / len(db))
                res = f.result()
                if res:
                    hits_ch.append(res)

        if hits_ch:
            seen = set()
            dedup = []
            for h in hits_ch:
                c = h["代码"]
                if c in seen:
                    continue
                seen.add(c)
                dedup.append(h)
            hits_ch = sorted(dedup, key=lambda x: (x["_sort_feat"], x["_sort_touch"]), reverse=True)
            clean = [
                {k: v for k, v in h.items() if k not in ["df", "line", "swing", "_sort_feat", "_sort_touch"]}
                for h in hits_ch
            ]
            st.success(f"命中 {len(hits_ch)} 只（按重点推荐 + 触碰分排序）。")
            st.table(pd.DataFrame(clean))
            st.download_button("💾 下降通道压力线 Excel", to_excel(pd.DataFrame(clean)), "Channel_Rebound_Line.xlsx")
            st.subheader("图例确认（前12）")
            for h in hits_ch[:12]:
                with st.expander(f"{h['评级']} {h['名称']} ({h['代码']}) · {h['拟合方式']}"):
                    fig = go.Figure()
                    fig.add_trace(
                        go.Candlestick(
                            x=h["df"]["日期"],
                            open=h["df"]["开盘"],
                            high=h["df"]["最高"],
                            low=h["df"]["最低"],
                            close=h["df"]["收盘"],
                            name="K线",
                        )
                    )
                    fig.add_trace(
                        go.Scatter(
                            x=h["df"]["日期"],
                            y=h["line"],
                            name="压力线",
                            line=dict(color="orange", width=2),
                        )
                    )
                    sx = h["df"]["日期"].iloc[list(h["swing"])].tolist()
                    sy = h["df"]["最高"].iloc[list(h["swing"])].tolist()
                    fig.add_trace(
                        go.Scatter(
                            x=sx,
                            y=sy,
                            mode="markers",
                            name="反弹高点",
                            marker=dict(size=8, color="cyan"),
                        )
                    )
                    fig.update_layout(template="plotly_dark", height=420, xaxis_rangeslider_visible=False)
                    st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("无命中：可放宽触碰容差、穿刺阈值，或取消「仅重点推荐」。")

    if st.button("开始【压力线收盘突破】扫描", key="btn_channel_break"):
        if not ensure_db_ready(db):
            st.stop()
        bar_br, hits_br = st.progress(0.0), []

        def task_br(row):
            ck = normalize_code(row["code"])
            df, name = SoulEngine.get_data(ck, days=max(400, ch_window + 30))
            if not br_keep_limit and is_limit_move(df, ck):
                return None
            if df is None or len(df) < ch_window:
                return None
            rec = MurphyReboundChannelEngine.build_channel_pressure(
                df,
                window=ch_window,
                swing_look=int(ch_swing),
                min_rise_pct=ch_min_rise,
                min_days_between=int(ch_min_days),
                touch_tol=ch_touch_tol,
                pierce_max_pct=ch_pierce_pct,
                max_pierce_events=int(ch_max_pierce),
                full_fit_tol=ch_full_tol,
                exclude_tail_days=int(ch_exclude_tail),
            )
            if rec is None:
                return None
            if rec["touch_score"] < br_min_touch:
                return None
            if only_featured_ch and not rec["featured"]:
                return None
            br = MurphyReboundChannelEngine.detect_close_breakout(
                rec,
                min_break_pct=br_min_pct,
                lookback=br_lookback,
                require_cross=br_require_cross,
                require_still_above=br_still_above,
                min_vol_ratio=(br_vol_mult if br_req_vol else None),
                require_above_ma200_on_break=br_ma200,
            )
            if br is None:
                return None
            df_w = rec["df_w"]
            bi = br["day_idx"]
            v5 = float(df_w["成交量"].iloc[bi - 5 : bi].mean()) if bi >= 5 else 0.0
            v_break = float(df_w["成交量"].iloc[bi])
            vol_ratio_str = f"{(v_break / v5):.2f}倍" if v5 > 0 else "-"
            # 计算止损位：基于突破日压力线价格，向下2%作为止损位
            break_line_price = rec["line_vals"][br["day_idx"]]
            stop_loss_price = break_line_price * 0.98
            
            # 计算预警：当前价格距离压力线的距离
            current_price = df_w["收盘"].iloc[-1]
            current_line_price = rec["line_vals"][-1]
            distance_to_line_pct = (current_price / current_line_price - 1) * 100 if current_line_price > 0 else 0
            warning_level = ""
            if -3 <= distance_to_line_pct < 0:
                warning_level = "⚠️ 接近"
            elif -5 <= distance_to_line_pct < -3:
                warning_level = "🔥 非常接近"
            
            return {
                "名称": name,
                "代码": ck,
                "评级": "⭐重点推荐" if rec["featured"] else "备选",
                "拟合方式": rec["mode"],
                "触碰分": rec["touch_score"],
                "穿刺次数": rec["pierce_events"],
                "突破日": br["break_date"],
                "距今天数": br["days_ago"],
                "突破幅度%": f"{br['break_pct']:.2f}%",
                "突破日量比": vol_ratio_str,
                "止损位": round(stop_loss_price, 2),
                "距压力线": f"{distance_to_line_pct:.2f}%",
                "预警": warning_level,
                "_br_pct": br["break_pct"],
                "df": rec["df_w"],
                "line": rec["line_vals"],
                "swing": rec["swing_indices"],
            }

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            futs = [executor.submit(task_br, r) for _, r in db.iterrows()]
            for i, f in enumerate(as_completed(futs)):
                if i % 25 == 0:
                    bar_br.progress((i + 1) / len(db))
                res = f.result()
                if res:
                    hits_br.append(res)

        if hits_br:
            seen = set()
            dedup = []
            for h in hits_br:
                c = h["代码"]
                if c in seen:
                    continue
                seen.add(c)
                dedup.append(h)
            hits_br = sorted(
                dedup,
                key=lambda x: (x["评级"].startswith("⭐"), x["_br_pct"]),
                reverse=True,
            )
            clean_br = [
                {k: v for k, v in h.items() if k not in ["df", "line", "swing", "_br_pct"]}
                for h in hits_br
            ]
            st.success(f"找到 {len(hits_br)} 只「下降通道压力线 + 收盘突破」标的。")
            st.table(pd.DataFrame(clean_br))
            st.download_button(
                "💾 压力线突破 Excel",
                to_excel(pd.DataFrame(clean_br)),
                "Channel_Pressure_Breakout.xlsx",
            )
            st.subheader("突破确认图（前12）")
            for h in hits_br[:12]:
                with st.expander(f"{h['名称']} ({h['代码']}) · 突破日 {h['突破日']}"):
                    fig = go.Figure()
                    fig.add_trace(
                        go.Candlestick(
                            x=h["df"]["日期"],
                            open=h["df"]["开盘"],
                            high=h["df"]["最高"],
                            low=h["df"]["最低"],
                            close=h["df"]["收盘"],
                            name="K线",
                        )
                    )
                    fig.add_trace(
                        go.Scatter(
                            x=h["df"]["日期"],
                            y=h["line"],
                            name="压力线(反弹高点)",
                            line=dict(color="orange", width=2),
                        )
                    )
                    sx = h["df"]["日期"].iloc[list(h["swing"])].tolist()
                    sy = h["df"]["最高"].iloc[list(h["swing"])].tolist()
                    fig.add_trace(
                        go.Scatter(
                            x=sx,
                            y=sy,
                            mode="markers",
                            name="反弹高点",
                            marker=dict(size=7, color="cyan"),
                        )
                    )
                    if len(h["df"]) >= 200:
                        ma200_s = h["df"]["收盘"].rolling(200).mean()
                        fig.add_trace(
                            go.Scatter(
                                x=h["df"]["日期"],
                                y=ma200_s,
                                name="MA200",
                                line=dict(color="white", width=1, dash="dot"),
                            )
                        )
                    fig.update_layout(template="plotly_dark", height=420, xaxis_rangeslider_visible=False)
                    st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("无突破命中：可放宽突破幅度、加大回溯天数，或取消「昨收在线下/今日仍在线上」。")
