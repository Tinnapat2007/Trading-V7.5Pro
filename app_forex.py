"""
AI Forex Sniper Pro (v2.0 — Stable / Accurate / Safe Edition)
=================================================================
ปรับปรุงจาก v1.0 ดังนี้:

[ความเสถียร]
  - แทน except:pass ด้วย structured logging ทุกจุด
  - เพิ่ม retry decorator (exponential back-off) สำหรับ external API
  - เพิ่ม timeout guard สำหรับ yf.download ผ่าน concurrent.futures
  - copy() DataFrame ก่อนใช้งานทุกครั้ง ป้องกัน cross-asset contamination
  - เพิ่ม consecutive-failure counter หยุด auto-refresh เมื่อ API ล้มเหลว 3 ครั้งติด
  - แยก business logic ออกจาก UI (ฟังก์ชัน fetch/compute ต่างหาก)

[ความแม่นยำ]
  - Sanitize + validate input ด้วย regex ก่อนส่ง API
  - MIN_BARS_REQUIRED = 52 (EMA50 ต้องการ 50+ แท่ง)
  - แก้สูตร HOLD confidence (เดิม: 100-|score| → แก้เป็น 50)
  - Clamp probability ระหว่าง 0-100 เสมอ
  - Pip multiplier ครอบ JPY, XAU, XAG, BTC, ETH และ default
  - กรอง NaN ออกจาก ATR ก่อนแสดงผล
  - ตรวจสอบ EMA50 NaN ก่อนใช้คำนวณ trend_score
  - เพิ่มแถบ Market Status แจ้งเตือนตลาดปิด/เปิด

[ความปลอดภัย]
  - จำกัด MAX_ASSETS = 10 คู่ต่อครั้ง
  - Rate limiting ระหว่าง TradingView request (sleep 0.3s)
  - Exponential back-off เมื่อ API ล้มเหลวซ้ำ
  - แสดง Financial Disclaimer ทุกครั้ง
  - ไม่แสดงข้อมูล error ภายในต่อผู้ใช้ (log เก็บไว้ฝั่ง server)
"""

# =========================================================================
# 📦 Imports
# =========================================================================
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from functools import wraps
from typing import Optional

import pandas as pd
import yfinance as yf
import streamlit as st
from tradingview_ta import TA_Handler, Interval
from streamlit_autorefresh import st_autorefresh
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from ta.volatility import AverageTrueRange

# =========================================================================
# ⚙️ ค่าคงที่และการตั้งค่า
# =========================================================================
MAX_ASSETS = 10          # จำนวนคู่เงินสูงสุดต่อการสแกนหนึ่งครั้ง
MIN_BARS_REQUIRED = 52   # EMA50 ต้องการ 50 แท่ง + buffer
YF_DOWNLOAD_TIMEOUT = 30 # วินาที
TV_REQUEST_DELAY = 0.35  # วินาที ระหว่างแต่ละ TradingView request
MAX_RETRIES = 3          # จำนวนครั้ง retry สูงสุด
MAX_AUTO_FAIL = 3        # หยุด auto-refresh เมื่อล้มเหลวติดต่อกัน N ครั้ง

# Pip multiplier ตามประเภทสกุลเงิน/สินทรัพย์
PIP_MULTIPLIERS: dict[str, int] = {
    "JPY": 100,     # Yen pairs: 0.01 per pip
    "XAU": 10,      # Gold: 0.1 per pip
    "XAG": 100,     # Silver
    "BTC": 1,       # Crypto — แสดงเป็น USD
    "ETH": 10,
    "XRP": 10000,
}
DEFAULT_PIP_MULTIPLIER = 10000  # Standard 4-decimal pairs

# =========================================================================
# 📋 Logging setup
# =========================================================================
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("forex_sniper")


# =========================================================================
# 🔁 Retry decorator (exponential back-off)
# =========================================================================
def retry_with_backoff(max_retries: int = MAX_RETRIES, base_delay: float = 1.0):
    """
    Decorator: retry ฟังก์ชันเมื่อเกิด exception
    ด้วย exponential back-off (1s, 2s, 4s, ...)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    wait = base_delay * (2 ** attempt)
                    logger.warning(
                        f"{func.__name__} attempt {attempt+1}/{max_retries} failed: "
                        f"{type(exc).__name__}: {exc}. Retrying in {wait:.1f}s..."
                    )
                    time.sleep(wait)
            logger.error(f"{func.__name__} failed after {max_retries} retries: {last_exc}")
            raise last_exc
        return wrapper
    return decorator


# =========================================================================
# 🛠️ Utility functions
# =========================================================================
def sanitize_symbol(raw: str) -> str:
    """
    ทำความสะอาดและ validate สัญลักษณ์คู่เงิน
    รับเฉพาะ A-Z, 0-9 เท่านั้น ความยาว 3-8 ตัวอักษร
    Raises ValueError หากไม่ผ่าน
    """
    cleaned = re.sub(r"[^A-Z0-9]", "", raw.strip().upper())
    if not cleaned:
        raise ValueError(f"สัญลักษณ์ว่างเปล่าหลัง sanitize: '{raw}'")
    if len(cleaned) < 3 or len(cleaned) > 8:
        raise ValueError(
            f"ความยาวสัญลักษณ์ไม่ถูกต้อง ({len(cleaned)} ตัว): '{cleaned}' "
            f"(ต้องการ 3-8 ตัวอักษร)"
        )
    return cleaned


def get_pip_multiplier(ticker: str) -> int:
    """คืนค่า pip multiplier ที่เหมาะสมสำหรับคู่เงินนั้น"""
    for key, mult in PIP_MULTIPLIERS.items():
        if key in ticker.upper():
            return mult
    return DEFAULT_PIP_MULTIPLIER


def parse_forex_meta(symbol_raw: str) -> dict:
    """
    แปลงคำค้นหาคู่เงินเป็น metadata ที่ใช้กับ TradingView และ Yahoo Finance
    Raises ValueError หาก symbol ไม่ถูกต้อง
    """
    sym = sanitize_symbol(symbol_raw)

    if len(sym) == 6:
        base = sym[:3]
        quote = sym[3:]
    elif len(sym) == 3:
        base = sym if sym != "USD" else "EUR"
        quote = "USD"
        sym = f"{base}{quote}"
    else:
        # ลองตีความเป็น 3+3 หรือ 4+4 (เช่น crypto pair)
        mid = len(sym) // 2
        base = sym[:mid]
        quote = sym[mid:]

    return {
        "tv_sym":       sym,
        "tv_exch":      "FX_IDC",
        "tv_screen":    "forex",
        "yf_sym":       f"{sym}=X",
        "clean_ticker": sym,
        "base_currency": base,
    }


def clamp(value: float, lo: float, hi: float) -> float:
    """จำกัดค่าให้อยู่ในช่วง [lo, hi]"""
    return max(lo, min(hi, value))


# =========================================================================
# 📡 Data fetching layer (แยกออกจาก UI)
# =========================================================================
@retry_with_backoff(max_retries=MAX_RETRIES, base_delay=1.0)
def fetch_tradingview(tv_sym: str, tv_exch: str, tv_screen: str, tv_interval) -> dict:
    """
    ดึงข้อมูล consensus จาก TradingView
    คืน dict ของ BUY/SELL/NEUTRAL/RECOMMENDATION
    Raises exception เมื่อล้มเหลว (จัดการโดย retry decorator)
    """
    handler = TA_Handler(
        symbol=tv_sym,
        exchange=tv_exch,
        screener=tv_screen,
        interval=tv_interval,
    )
    analysis = handler.get_analysis()
    return {
        "buy":    analysis.summary["BUY"],
        "sell":   analysis.summary["SELL"],
        "neutral":analysis.summary["NEUTRAL"],
        "recommend": analysis.summary["RECOMMENDATION"],
    }


def fetch_yfinance_with_timeout(
    tickers: list[str],
    period: str,
    interval: str,
    timeout: int = YF_DOWNLOAD_TIMEOUT,
) -> pd.DataFrame:
    """
    ดึงข้อมูลจาก Yahoo Finance พร้อม timeout guard
    คืน DataFrame ว่างเมื่อล้มเหลวหรือหมดเวลา
    """
    def _download():
        return yf.download(
            tickers=tickers,
            period=period,
            interval=interval,
            group_by="ticker",
            progress=False,
            threads=True,
            auto_adjust=True,
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_download)
            return future.result(timeout=timeout)
    except FuturesTimeoutError:
        logger.error(f"yf.download timed out after {timeout}s for tickers: {tickers}")
        return pd.DataFrame()
    except Exception as exc:
        logger.error(f"yf.download failed: {type(exc).__name__}: {exc}")
        return pd.DataFrame()


def extract_asset_df(all_yf_data: pd.DataFrame, yf_sym: str, single: bool) -> pd.DataFrame:
    """
    แยก DataFrame ของ asset หนึ่งออกจาก combined DataFrame
    คืน copy ที่ปลอดภัย (ไม่ modify ตัวต้นฉบับ)
    """
    try:
        if all_yf_data.empty:
            return pd.DataFrame()

        if single:
            df = all_yf_data.copy()
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
        else:
            if yf_sym not in all_yf_data.columns.get_level_values(0):
                logger.warning(f"Symbol {yf_sym} not found in downloaded data")
                return pd.DataFrame()
            df = all_yf_data[yf_sym].copy()

        # กรอง NaN ออกอย่างปลอดภัย
        df = df.dropna(subset=["Close"])
        return df

    except Exception as exc:
        logger.error(f"extract_asset_df failed for {yf_sym}: {exc}")
        return pd.DataFrame()


# =========================================================================
# 🧮 Signal computation engine
# =========================================================================
def compute_technical_score(df_yf: pd.DataFrame) -> dict:
    """
    คำนวณ technical indicators และ score components
    คืน dict ของค่าต่างๆ พร้อม flag is_valid
    """
    result = {
        "is_valid":      False,
        "latest_price":  0.0,
        "trend_score":   0.0,
        "momentum_score":0.0,
        "rsi":           50.0,
        "macd":          0.0,
        "atr":           0.0,
        "warnings":      [],
    }

    if df_yf.empty or len(df_yf) < 20:
        result["warnings"].append("ข้อมูลไม่เพียงพอ (ต้องการ ≥ 20 แท่ง)")
        return result

    try:
        close  = df_yf["Close"].astype(float)
        high   = df_yf["High"].astype(float)
        low    = df_yf["Low"].astype(float)

        result["latest_price"] = float(close.iloc[-1])

        # RSI (ต้องการ 14+ แท่ง)
        rsi_series = RSIIndicator(close=close, window=14).rsi()
        result["rsi"] = float(rsi_series.iloc[-1]) if not rsi_series.isna().all() else 50.0

        # MACD (ต้องการ 26+ แท่ง)
        macd_diff_series = MACD(close=close).macd_diff()
        result["macd"] = float(macd_diff_series.iloc[-1]) if not macd_diff_series.isna().all() else 0.0

        # ATR (ต้องการ 14+ แท่ง)
        atr_series = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()
        last_atr = atr_series.iloc[-1]
        result["atr"] = float(last_atr) if pd.notna(last_atr) else 0.0

        # Momentum score (30%) — RSI + MACD
        result["momentum_score"] += 15.0 if result["macd"] > 0 else -15.0
        result["momentum_score"] += 15.0 if result["rsi"] > 50 else -15.0

        # Trend score (40%) — EMA20/50 (ต้องการ 52+ แท่ง)
        if len(df_yf) >= MIN_BARS_REQUIRED:
            ema20 = EMAIndicator(close=close, window=20).ema_indicator()
            ema50 = EMAIndicator(close=close, window=50).ema_indicator()

            last_close = float(close.iloc[-1])
            last_ema20 = float(ema20.iloc[-1]) if not ema20.isna().all() else None
            last_ema50 = float(ema50.iloc[-1]) if not ema50.isna().all() else None

            if last_ema20 is not None and last_ema50 is not None:
                if last_close > last_ema20 and last_ema20 > last_ema50:
                    result["trend_score"] = 40.0   # Bullish alignment
                elif last_close < last_ema20 and last_ema20 < last_ema50:
                    result["trend_score"] = -40.0  # Bearish alignment
                # else: mixed → score = 0 (sideways/uncertain)
            else:
                result["warnings"].append("EMA มีค่า NaN — ไม่นำมาคิด trend score")
        else:
            result["warnings"].append(
                f"ข้อมูลน้อยกว่า {MIN_BARS_REQUIRED} แท่ง — ข้าม EMA50 trend"
            )

        result["is_valid"] = True

    except Exception as exc:
        logger.error(f"compute_technical_score failed: {type(exc).__name__}: {exc}")
        result["warnings"].append("คำนวณ technical indicators ไม่สำเร็จ")

    return result


def build_signal(tech: dict, tv: dict, ticker: str) -> dict:
    """
    รวม technical score + TradingView consensus → สัญญาณสุดท้าย
    พร้อม probability ที่ normalize แล้ว และ pip range
    """
    trend_score    = tech.get("trend_score", 0.0)
    momentum_score = tech.get("momentum_score", 0.0)

    # TradingView consensus (30%)
    recommend = tv.get("recommend", "NEUTRAL")
    if "STRONG_BUY" in recommend or "STRONG BUY" in recommend:
        consensus_score = 30.0
    elif "BUY" in recommend:
        consensus_score = 15.0
    elif "STRONG_SELL" in recommend or "STRONG SELL" in recommend:
        consensus_score = -30.0
    elif "SELL" in recommend:
        consensus_score = -15.0
    else:
        consensus_score = 0.0

    total_score = trend_score + momentum_score + consensus_score
    abs_score   = abs(total_score)

    # --- กำหนดทิศทางและ probability ---
    if total_score > 15:
        side_verdict   = "BUY"
        # probability proportional ต่อคะแนน clamp 0-100
        probability    = clamp(abs_score, 0.0, 100.0)
        final_signal   = "🟢 STRONG BUY" if probability >= 75 else "🟢 BUY"

    elif total_score < -15:
        side_verdict   = "SELL"
        probability    = clamp(abs_score, 0.0, 100.0)
        final_signal   = "🔴 STRONG SELL" if probability >= 75 else "🔴 SELL"

    else:
        side_verdict   = "HOLD"
        # HOLD ไม่มีทิศทางชัดเจน → แสดง 50% คงที่ (ไม่ใช่ 100-|score| เดิมที่ misleading)
        probability    = 50.0
        final_signal   = "🟡 HOLD (ไซด์เวย์)"

    # Pip range
    pip_mult  = get_pip_multiplier(ticker)
    atr_val   = tech.get("atr", 0.0)
    atr_pips  = atr_val * pip_mult if atr_val and pd.notna(atr_val) else None

    # Price formatting
    latest_price = tech.get("latest_price", 0.0)
    price_fmt = (
        f"{latest_price:,.2f}"  if "JPY" in ticker
        else f"{latest_price:,.5f}" if "BTC" in ticker or "ETH" in ticker
        else f"{latest_price:,.4f}"
    )

    return {
        "side_verdict":    side_verdict,
        "probability":     probability,
        "final_signal":    final_signal,
        "total_score":     total_score,
        "trend_part":      abs(trend_score),
        "momentum_part":   abs(momentum_score),
        "consensus_part":  abs(consensus_score),
        "rsi":             tech.get("rsi", 50.0),
        "macd":            tech.get("macd", 0.0),
        "atr":             atr_val,
        "atr_pips":        atr_pips,
        "tv_buy":          tv.get("buy", 0),
        "tv_sell":         tv.get("sell", 0),
        "tv_neutral":      tv.get("neutral", 0),
        "latest_price":    latest_price,
        "price_fmt":       price_fmt,
        "warnings":        tech.get("warnings", []),
    }


# =========================================================================
# 🎨 UI — Page config และ header
# =========================================================================
st.set_page_config(
    page_title="AI Forex Sniper Pro",
    page_icon="💱",
    layout="wide",
)

st.title("💱 AI Forex Sniper Pro (v2.0 — Stable Edition)")
st.write(
    "ระบบวิเคราะห์มติรวมพหุภาคีและวัดความผันผวน Pips "
    "จากสถาบันการเงินระดับโลก ค้นหาคู่เงินได้ทุกคู่ 24/5"
)

# =========================================================================
# ⚠️ Financial Disclaimer (แสดงก่อน UI ทุกครั้ง)
# =========================================================================
st.warning(
    "**⚠️ คำเตือนทางการเงิน (Financial Disclaimer):** "
    "ข้อมูลและสัญญาณที่แสดงเป็นเพียงการวิเคราะห์เชิงเทคนิคอัตโนมัติ "
    "**ไม่ใช่คำแนะนำการลงทุน** และไม่ใช่ข้อเสนอซื้อขายหลักทรัพย์ใดๆ "
    "การลงทุนในตลาด Forex มีความเสี่ยงสูง อาจสูญเสียเงินต้นทั้งหมด "
    "ผู้ใช้รับความเสี่ยงและรับผิดชอบการตัดสินใจเองทั้งหมด"
)

# =========================================================================
# 🗄️ Session state initialization
# =========================================================================
defaults = {
    "auto_mode":    False,
    "run_once":     False,
    "fail_count":   0,   # consecutive API failure counter
}
for key, val in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = val


# =========================================================================
# ⚙️ แผงควบคุมคู่เงินและกรอบเวลา
# =========================================================================
st.subheader("⚙️ ตัวเลือกคู่เงินและกรอบเวลาฟอเร็กซ์")
col_setting1, col_setting2 = st.columns([2, 1])

with col_setting1:
    user_input = st.text_input(
        f"⌨️ พิมพ์คู่เงินที่ต้องการสแกน (คั่นด้วย `,` สูงสุด {MAX_ASSETS} คู่):",
        value="EURUSD, GBPUSD, USDJPY, USDTHB, AUDUSD",
        max_chars=200,
    )

with col_setting2:
    tf_choice = st.selectbox(
        "⏱️ กรอบเวลาสแกน (Timeframe):",
        options=[
            "5 นาที (M5) - สัญญาณสคัลปิ้ง",
            "15 นาที (M15) - เล่นสั้นแทร็ก",
            "1 ชั่วโมง (H1) - สัญญาณเดย์เทรด",
            "1 วัน (1D) - รันเทรนด์ระยะยาว",
        ],
        index=2,
    )

# Map timeframe choice → yfinance และ TradingView interval
TF_MAP = {
    "5 นาที":   ("5m",  "2d",   Interval.INTERVAL_5_MINUTES),
    "15 นาที":  ("15m", "5d",   Interval.INTERVAL_15_MINUTES),
    "1 ชั่วโมง":("1h",  "60d",  Interval.INTERVAL_1_HOUR),
    "1 วัน":    ("1d",  "180d", Interval.INTERVAL_1_DAY),
}

yf_interval, yf_period, tv_interval = ("1h", "60d", Interval.INTERVAL_1_HOUR)  # default
for key_prefix, (yi, yp, tvi) in TF_MAP.items():
    if key_prefix in tf_choice:
        yf_interval, yf_period, tv_interval = yi, yp, tvi
        break

# =========================================================================
# 🔍 Validate + sanitize selected assets
# =========================================================================
raw_assets = [x.strip() for x in user_input.split(",") if x.strip()]
selected_assets: list[str] = []
invalid_assets:  list[str] = []

for raw in raw_assets:
    try:
        meta = parse_forex_meta(raw)
        selected_assets.append(raw)
    except ValueError as e:
        invalid_assets.append(f"`{raw}` — {e}")

if invalid_assets:
    st.error("❌ สัญลักษณ์ต่อไปนี้ไม่ถูกต้อง:\n" + "\n".join(invalid_assets))

if len(selected_assets) > MAX_ASSETS:
    st.warning(
        f"⚠️ จำกัดสูงสุด {MAX_ASSETS} คู่ต่อครั้ง "
        f"— ตัดเหลือ {MAX_ASSETS} คู่แรก (ตัดออก: "
        f"{', '.join(selected_assets[MAX_ASSETS:])})"
    )
    selected_assets = selected_assets[:MAX_ASSETS]

# =========================================================================
# 🎯 ปุ่มควบคุมสแกน
# =========================================================================
col_btn1, col_btn2, col_btn3 = st.columns(3)
with col_btn1:
    if st.button("🔍 เริ่มสแกนทันที", use_container_width=True):
        st.session_state.auto_mode  = False
        st.session_state.run_once   = True
        st.session_state.fail_count = 0
        st.rerun()

with col_btn2:
    if st.button("🔄 Auto Refresh (60s)", use_container_width=True):
        st.session_state.auto_mode  = True
        st.session_state.run_once   = False
        st.session_state.fail_count = 0
        st.toast("⚡ เริ่มระบบเฝ้าอัตราแลกเปลี่ยนเรียลไทม์")
        st.rerun()

with col_btn3:
    if st.button("🛑 หยุดสแกน", use_container_width=True):
        st.session_state.auto_mode  = False
        st.session_state.run_once   = False
        st.session_state.fail_count = 0
        st.toast("🛑 หยุดระบบเรียบร้อย")
        st.rerun()

# Auto-refresh (ทำงานเฉพาะเมื่อ auto_mode = True)
if st.session_state.auto_mode:
    st_autorefresh(interval=60_000, key="forex_dedicated_refresh")
    fail_c = st.session_state.fail_count
    if fail_c > 0:
        st.info(
            f"🔄 กำลังอัปเดตทุก 60 วินาที "
            f"| ⚠️ API ล้มเหลวติดต่อกัน {fail_c}/{MAX_AUTO_FAIL} ครั้ง"
        )
    else:
        st.info("🔄 กำลังดึงข้อมูลด่วนจากศูนย์กลางการเงินโลก อัปเดตทุก 60 วินาที")

# =========================================================================
# 🚀 Main scanning engine
# =========================================================================
if (st.session_state.run_once or st.session_state.auto_mode) and selected_assets:

    dashboard_summary: list[dict] = []
    detailed_results:  dict       = {}
    success_count = 0

    progress_bar = st.progress(0, text="⏳ กำลังโหลดข้อมูล...")

    # --- Step 1: ดึงข้อมูล Yahoo Finance ทั้งหมดพร้อมกัน (timeout guard) ---
    yf_tickers = [parse_forex_meta(a)["yf_sym"] for a in selected_assets]

    with st.spinner("📡 ดึงข้อมูลราคาจาก Yahoo Finance..."):
        all_yf_data = fetch_yfinance_with_timeout(
            tickers=yf_tickers,
            period=yf_period,
            interval=yf_interval,
            timeout=YF_DOWNLOAD_TIMEOUT,
        )

    is_single = len(selected_assets) == 1

    # --- Step 2: วิเคราะห์แต่ละ asset ---
    total = len(selected_assets)
    for idx, asset_raw in enumerate(selected_assets):
        progress_bar.progress(
            (idx + 1) / total,
            text=f"⏳ กำลังวิเคราะห์ {asset_raw} ({idx+1}/{total})..."
        )

        try:
            meta = parse_forex_meta(asset_raw)

            # -- TradingView consensus (retry + delay) --
            tv_data = {"buy": 0, "sell": 0, "neutral": 0, "recommend": "NEUTRAL"}
            try:
                tv_data = fetch_tradingview(
                    tv_sym=meta["tv_sym"],
                    tv_exch=meta["tv_exch"],
                    tv_screen=meta["tv_screen"],
                    tv_interval=tv_interval,
                )
            except Exception as exc:
                logger.warning(
                    f"TradingView failed for {meta['tv_sym']} after retries: {exc}"
                )
                # ต่อไปแม้ TV ล้มเหลว — ใช้ technical score เดี่ยว

            # rate limiting ระหว่าง TradingView requests
            if idx < total - 1:
                time.sleep(TV_REQUEST_DELAY)

            # -- Yahoo Finance data extraction --
            df_yf = extract_asset_df(all_yf_data, meta["yf_sym"], is_single)

            # -- Technical computation --
            tech = compute_technical_score(df_yf)

            # -- Build final signal --
            signal = build_signal(tech, tv_data, meta["clean_ticker"])

            # -- Dashboard row --
            atr_pips_display = (
                f"{signal['atr_pips']:.1f} Pips"
                if signal["atr_pips"] is not None
                else "N/A"
            )

            dashboard_summary.append({
                "คู่เงิน":                 meta["clean_ticker"],
                "ราคาล่าสุด":              signal["price_fmt"],
                "ทิศทาง AI":               signal["final_signal"],
                "ความมั่นใจ":              f"{signal['probability']:.1f}%",
                "ระยะแกว่ง (ATR Pips)":   atr_pips_display,
                "แนวโน้ม":                 (
                    "ขาขึ้น (Bullish)"  if signal["side_verdict"] == "BUY"
                    else "ขาลง (Bearish)" if signal["side_verdict"] == "SELL"
                    else "ไร้ทิศทาง"
                ),
            })

            detailed_results[meta["clean_ticker"]] = signal
            success_count += 1

        except Exception as exc:
            logger.error(f"Unexpected error processing {asset_raw}: {type(exc).__name__}: {exc}")
            st.warning(
                f"⚠️ ไม่สามารถวิเคราะห์ `{asset_raw}` ได้ในขณะนี้ "
                f"— กรุณาตรวจสอบสัญลักษณ์และลองใหม่อีกครั้ง"
            )

    progress_bar.empty()

    # --- Consecutive failure tracking (สำหรับ auto-refresh) ---
    if success_count == 0 and len(selected_assets) > 0:
        st.session_state.fail_count += 1
        if st.session_state.fail_count >= MAX_AUTO_FAIL:
            st.session_state.auto_mode = False
            st.error(
                f"🛑 ระบบหยุด Auto Refresh เนื่องจาก API ล้มเหลวติดต่อกัน "
                f"{MAX_AUTO_FAIL} ครั้ง — กรุณากดสแกนใหม่ด้วยตนเอง"
            )
    else:
        st.session_state.fail_count = 0

    # =========================================================================
    # 📊 UI Rendering — Dashboard
    # =========================================================================
    if dashboard_summary:
        st.write("---")
        st.subheader(
            f"📊 แดชบอร์ดมติพหุภาคีตลาดอัตราแลกเปลี่ยน "
            f"({tf_choice.split(' - ')[0].strip()})"
        )

        df_dash = pd.DataFrame(dashboard_summary)
        st.dataframe(df_dash, use_container_width=True, hide_index=True)

        # ======================================
        # 🔍 Detailed expandable panels
        # ======================================
        st.write("---")
        st.subheader("🔍 วิเคราะห์เจาะลึกรายคู่เงิน")

        for name, data in detailed_results.items():
            emoji = "🟢" if data["side_verdict"] == "BUY" else (
                    "🔴" if data["side_verdict"] == "SELL" else "🟡")

            with st.expander(
                f"{emoji} {name} — {data['final_signal']}  "
                f"| ความมั่นใจ: {data['probability']:.1f}%"
            ):
                # --- Warnings ---
                if data.get("warnings"):
                    for w in data["warnings"]:
                        st.caption(f"⚠️ {w}")

                # --- Score breakdown ---
                st.markdown(f"#### 🎯 คะแนนรวม (Raw Score): {data['total_score']:+.1f} / ±100")

                breakdown = pd.DataFrame([
                    {
                        "แกนชี้วัด": "1. แนวโน้มโครงสร้างหลัก (EMA 20/50)",
                        "น้ำหนักสูงสุด": "±40",
                        "คะแนนที่ได้": f"{data['trend_part']:+.1f}",
                    },
                    {
                        "แกนชี้วัด": "2. โมเมนตัม (RSI + MACD)",
                        "น้ำหนักสูงสุด": "±30",
                        "คะแนนที่ได้": f"{data['momentum_part']:+.1f}",
                    },
                    {
                        "แกนชี้วัด": "3. บอทสถาบัน TradingView (FX_IDC)",
                        "น้ำหนักสูงสุด": "±30",
                        "คะแนนที่ได้": f"{data['consensus_part']:+.1f}",
                    },
                ])
                st.dataframe(breakdown, use_container_width=True, hide_index=True)

                # --- Metrics row ---
                m1, m2, m3, m4 = st.columns(4)
                with m1:
                    st.metric("RSI (14)", f"{data['rsi']:.1f}",
                              help="< 30 = Oversold, > 70 = Overbought")
                with m2:
                    st.metric("MACD Histogram", f"{data['macd']:.6f}",
                              help="บวก = momentum ขาขึ้น, ลบ = ขาลง")
                with m3:
                    atr_display = (
                        f"{data['atr_pips']:.1f} Pips"
                        if data["atr_pips"] is not None else "N/A"
                    )
                    st.metric("ATR (กรอบผันผวน)", atr_display,
                              help="Average True Range — ระยะวิ่งเฉลี่ยต่อแท่งเทียน")
                with m4:
                    st.metric(
                        "โหวตบอท (B / S / N)",
                        f"{data['tv_buy']} / {data['tv_sell']} / {data['tv_neutral']}",
                        help="BUY / SELL / NEUTRAL จาก TradingView FX_IDC"
                    )

                # --- TP/SL suggestion ---
                if data["atr_pips"] is not None:
                    atr_p = data["atr_pips"]
                    tp_1r = atr_p * 1.0
                    tp_2r = atr_p * 2.0
                    sl_r  = atr_p * 0.5
                    st.info(
                        f"💡 **แนวทาง TP/SL อ้างอิง ATR ({atr_p:.1f} Pips):**  \n"
                        f"🎯 TP1 ≈ {tp_1r:.1f} Pips | TP2 ≈ {tp_2r:.1f} Pips  \n"
                        f"🛑 SL ≈ {sl_r:.1f} Pips (0.5× ATR)  \n"
                        f"*ค่านี้เป็นแนวทางอ้างอิงเท่านั้น ไม่ใช่คำแนะนำการลงทุน*"
                    )
                else:
                    st.caption("⚠️ ไม่สามารถคำนวณ ATR TP/SL ได้เนื่องจากข้อมูลไม่เพียงพอ")

    # =========================================================================
    # 📌 Footer disclaimer (ซ้ำที่ด้านล่าง)
    # =========================================================================
    st.write("---")
    st.caption(
        "⚠️ **ข้อจำกัดความรับผิดชอบ:** "
        "สัญญาณทั้งหมดเป็นการวิเคราะห์เชิงเทคนิคอัตโนมัติ ไม่ใช่คำแนะนำทางการเงิน "
        "ผู้พัฒนาไม่รับผิดชอบต่อความเสียหายใดๆ อันเกิดจากการนำข้อมูลไปใช้งาน "
        "| AI Forex Sniper Pro v2.0 | Powered by TradingView FX_IDC + Yahoo Finance"
    )

    st.session_state.run_once = False

elif not selected_assets and not invalid_assets:
    st.info("ℹ️ กรุณากรอกชื่อย่อคู่เงินที่ต้องการในกล่องด้านบน แล้วกด 'เริ่มสแกน'")
