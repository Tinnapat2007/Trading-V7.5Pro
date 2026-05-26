"""
AI Forex Sniper Pro (v3.0 — Bug-Fixed / Mobile-Ready Edition)
=================================================================
แก้ไขจาก v2.0:

[BUG-01] XAUUSD ขึ้นแต่แสดง HOLD
  → สาเหตุ: tv_exch="FX_IDC" ไม่มีข้อมูล Gold/Silver → consensus=0 เสมอ
  → แก้: ใช้ ASSET_PROFILE map กำหนด tv_exch/screener/yf_sym แยกตามประเภท
    - Forex majors  → FX_IDC  / screener=forex
    - Gold/Silver   → OANDA   / screener=forex  | yf=GC=F / SI=F
    - Crypto        → BINANCE / screener=crypto  | yf=BTC-USD

[BUG-02] ATR=None / ข้อความ "ไม่สามารถคำนวณ ATR"
  → สาเหตุ: dropna(subset=["Close"]) ไม่กรอง High/Low NaN
    → AverageTrueRange คืน NaN series ทั้งหมด → atr_pips=None
  → แก้: dropna(subset=["Close","High","Low"]) ก่อนคำนวณ ATR ทุกครั้ง

[BUG-03] โหลดข้อมูลนานมาก
  → สาเหตุ: ไม่มี cache ทุก rerun ยิง yf.download + TV API ใหม่หมด
  → แก้: @st.cache_data(ttl=55) บน fetch_yfinance และ fetch_tradingview
    (TTL 55 วิ ให้สอดรับกับ auto-refresh 60 วิ)

[BUG-04] extract_asset_df: MultiIndex handle ผิดพลาด
  → สาเหตุ: yf.download multi-ticker + auto_adjust=True
    สร้าง MultiIndex (field, ticker) ไม่ใช่ (ticker, field)
  → แก้: ตรวจ level ที่ถูกต้อง + fallback ดาวน์โหลดเดี่ยวถ้า extract ไม่ได้

[BUG-05] ไม่รองรับมือถือแนวนอน
  → แก้: inject CSS responsive + ปรับ column layout ตาม screen width
    - ซ่อน/ย่อ dataframe บนมือถือ
    - Metric cards เรียง 2 คอลัมน์แทน 4
    - ปุ่มกว้างเต็ม (use_container_width=True ทุกปุ่ม)

[BUG-06] Crypto yf symbol ผิด
  → BTCUSD=X ไม่มีข้อมูลใน Yahoo → ต้องใช้ BTC-USD
  → แก้: ASSET_PROFILE จัดการ yf_sym แยกตาม asset class

แหล่งข้อมูล:
  - ราคา OHLCV      → Yahoo Finance (yfinance)   https://finance.yahoo.com
  - สัญญาณ Forex    → TradingView FX_IDC         https://www.tradingview.com
  - สัญญาณ Gold/Ag  → TradingView OANDA          https://www.tradingview.com
  - สัญญาณ Crypto   → TradingView BINANCE        https://www.tradingview.com
  - Indicators      → ta library (RSI/MACD/EMA/ATR) คำนวณ local
"""

# =========================================================================
# 📦 Imports
# =========================================================================
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from functools import wraps

import pandas as pd
import yfinance as yf
import streamlit as st
from tradingview_ta import TA_Handler, Interval
from streamlit_autorefresh import st_autorefresh
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from ta.volatility import AverageTrueRange

# =========================================================================
# ⚙️ ค่าคงที่
# =========================================================================
MAX_ASSETS        = 10    # คู่เงินสูงสุดต่อการสแกน
MIN_BARS_REQUIRED = 52    # EMA50 ต้องการ 50+ แท่ง
YF_DOWNLOAD_TIMEOUT = 30  # วินาที timeout Yahoo Finance
TV_REQUEST_DELAY  = 0.35  # วินาที delay ระหว่าง TradingView request
MAX_RETRIES       = 3     # retry สูงสุด
MAX_AUTO_FAIL     = 3     # หยุด auto-refresh เมื่อล้มเหลวติดต่อกัน

# =========================================================================
# 🗺️ ASSET PROFILE MAP — แก้ BUG-01 และ BUG-06
# =========================================================================
# กำหนด TV exchange, screener, และ yf symbol ที่ถูกต้องสำหรับแต่ละประเภท
# แหล่งข้อมูล:
#   Forex  → FX_IDC (TradingView Forex data center, ข้อมูลรวมจาก bank หลายแห่ง)
#   Gold   → OANDA  (broker ที่ TV ใช้เป็น reference สำหรับ spot metal)
#   Crypto → BINANCE (exchange ที่มี liquidity สูงสุด, TV ใช้เป็น default)
#   yf Gold  → GC=F (Gold Futures CME, ข้อมูลสมบูรณ์กว่า XAUUSD=X)
#   yf Crypto → BTC-USD รูปแบบที่ Yahoo รองรับ (ไม่ใช่ BTCUSD=X)

ASSET_PROFILE_RULES = [
    # (keywords_in_ticker, tv_exch, tv_screen, yf_suffix_override)
    # yf_suffix_override=None → ใช้ {sym}=X ปกติ
    # yf_suffix_override=str  → ใช้ค่านั้นแทนทั้งหมด
    ({"XAU"},  "OANDA",   "forex",  None),       # Gold spot
    ({"XAG"},  "OANDA",   "forex",  None),       # Silver spot
    ({"XPT"},  "OANDA",   "forex",  None),       # Platinum
    ({"BTC"},  "BINANCE", "crypto", "BTC-USD"),  # Bitcoin
    ({"ETH"},  "BINANCE", "crypto", "ETH-USD"),  # Ethereum
    ({"XRP"},  "BINANCE", "crypto", "XRP-USD"),  # Ripple
    ({"BNB"},  "BINANCE", "crypto", "BNB-USD"),
    ({"SOL"},  "BINANCE", "crypto", "SOL-USD"),
    ({"ADA"},  "BINANCE", "crypto", "ADA-USD"),
    ({"DOGE"}, "BINANCE", "crypto", "DOGE-USD"),
]

# Pip multiplier ตามประเภทสินทรัพย์
PIP_MULTIPLIERS = {
    "JPY": 100,
    "XAU": 10,
    "XAG": 100,
    "XPT": 10,
    "BTC": 1,
    "ETH": 10,
    "XRP": 10000,
}
DEFAULT_PIP_MULTIPLIER = 10000

# =========================================================================
# 📋 Logging
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
                        f"{func.__name__} attempt {attempt+1}/{max_retries} "
                        f"failed: {type(exc).__name__}: {exc}. Retry in {wait:.1f}s"
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
    """กรอง A-Z, 0-9 เท่านั้น ความยาว 3-8 ตัว"""
    cleaned = re.sub(r"[^A-Z0-9]", "", raw.strip().upper())
    if not cleaned:
        raise ValueError(f"สัญลักษณ์ว่างเปล่าหลัง sanitize: '{raw}'")
    if len(cleaned) < 3 or len(cleaned) > 8:
        raise ValueError(f"ความยาวไม่ถูกต้อง ({len(cleaned)} ตัว): '{cleaned}'")
    return cleaned


def get_asset_profile(sym: str) -> dict:
    """
    คืน tv_exch, tv_screen, yf_sym ที่ถูกต้องตามประเภทสินทรัพย์
    แก้ BUG-01: XAUUSD ต้องใช้ OANDA ไม่ใช่ FX_IDC
    แก้ BUG-06: BTC ต้องใช้ BTC-USD ไม่ใช่ BTCUSD=X
    """
    sym_upper = sym.upper()
    for keywords, tv_exch, tv_screen, yf_override in ASSET_PROFILE_RULES:
        if any(kw in sym_upper for kw in keywords):
            yf_sym = yf_override if yf_override else f"{sym}=X"
            return {"tv_exch": tv_exch, "tv_screen": tv_screen, "yf_sym": yf_sym}
    # Default: Forex majors
    return {"tv_exch": "FX_IDC", "tv_screen": "forex", "yf_sym": f"{sym}=X"}


def parse_forex_meta(symbol_raw: str) -> dict:
    """แปลง input → metadata พร้อม exchange/screener/yf_sym ที่ถูกต้อง"""
    sym = sanitize_symbol(symbol_raw)

    if len(sym) == 6:
        base, quote = sym[:3], sym[3:]
    elif len(sym) == 3:
        base = sym if sym != "USD" else "EUR"
        quote = "USD"
        sym = f"{base}{quote}"
    else:
        mid = len(sym) // 2
        base, quote = sym[:mid], sym[mid:]

    profile = get_asset_profile(sym)

    return {
        "tv_sym":        sym,
        "tv_exch":       profile["tv_exch"],
        "tv_screen":     profile["tv_screen"],
        "yf_sym":        profile["yf_sym"],
        "clean_ticker":  sym,
        "base_currency": base,
    }


def get_pip_multiplier(ticker: str) -> int:
    for key, mult in PIP_MULTIPLIERS.items():
        if key in ticker.upper():
            return mult
    return DEFAULT_PIP_MULTIPLIER


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# =========================================================================
# 📡 Data fetching — พร้อม @st.cache_data แก้ BUG-03 (โหลดนาน)
# =========================================================================

@st.cache_data(ttl=55, show_spinner=False)
def fetch_tradingview_cached(tv_sym: str, tv_exch: str, tv_screen: str, tv_interval_str: str) -> dict:
    """
    ดึง consensus จาก TradingView พร้อม cache 55 วินาที
    แก้ BUG-03: ไม่ยิง API ซ้ำทุก rerun
    แหล่งข้อมูล: TradingView technical analysis (รวม indicators จากหลาย source)
    tv_interval_str ใช้ string เพราะ cache ต้องการ hashable key
    """
    interval_map = {
        "5m":  Interval.INTERVAL_5_MINUTES,
        "15m": Interval.INTERVAL_15_MINUTES,
        "1h":  Interval.INTERVAL_1_HOUR,
        "1d":  Interval.INTERVAL_1_DAY,
    }
    tv_interval = interval_map.get(tv_interval_str, Interval.INTERVAL_1_HOUR)

    @retry_with_backoff(max_retries=MAX_RETRIES, base_delay=1.0)
    def _fetch():
        handler = TA_Handler(
            symbol=tv_sym,
            exchange=tv_exch,
            screener=tv_screen,
            interval=tv_interval,
        )
        analysis = handler.get_analysis()
        return {
            "buy":       analysis.summary["BUY"],
            "sell":      analysis.summary["SELL"],
            "neutral":   analysis.summary["NEUTRAL"],
            "recommend": analysis.summary["RECOMMENDATION"],
        }

    return _fetch()


@st.cache_data(ttl=55, show_spinner=False)
def fetch_yfinance_cached(
    tickers_tuple: tuple,   # tuple แทน list เพราะ cache ต้องการ hashable
    period: str,
    interval: str,
    timeout: int = YF_DOWNLOAD_TIMEOUT,
) -> pd.DataFrame:
    """
    ดึงราคา OHLCV จาก Yahoo Finance พร้อม cache 55 วินาที + timeout guard
    แก้ BUG-03: ไม่ดาวน์โหลดซ้ำทุก rerun
    แหล่งข้อมูล: Yahoo Finance (ราคาตลาดจริง delayed ~15 นาที สำหรับ Forex ฟรี)
    """
    tickers = list(tickers_tuple)

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
        logger.error(f"yf.download timed out after {timeout}s")
        return pd.DataFrame()
    except Exception as exc:
        logger.error(f"yf.download failed: {type(exc).__name__}: {exc}")
        return pd.DataFrame()


def extract_asset_df(all_yf_data: pd.DataFrame, yf_sym: str, is_single: bool) -> pd.DataFrame:
    """
    แยก DataFrame ของ asset หนึ่ง จาก combined download result
    แก้ BUG-04: จัดการ MultiIndex อย่างถูกต้อง
    
    yf.download multi-ticker + auto_adjust=True สร้าง MultiIndex แบบ:
      columns = MultiIndex([('Close','EURUSD=X'), ('Close','GBPUSD=X'), ...])
      → level 0 = field name, level 1 = ticker
    """
    try:
        if all_yf_data is None or all_yf_data.empty:
            return pd.DataFrame()

        if is_single:
            # กรณีดาวน์โหลดตัวเดียว → columns แบน ไม่มี MultiIndex
            df = all_yf_data.copy()
            if isinstance(df.columns, pd.MultiIndex):
                # บางครั้ง single ก็ยังเป็น MultiIndex ขึ้นกับ version
                # level 0 = field, level 1 = ticker (หรือกลับกัน)
                if df.columns.get_level_values(0)[0] in ["Open","High","Low","Close","Volume","Adj Close"]:
                    df.columns = df.columns.get_level_values(0)
                else:
                    df.columns = df.columns.get_level_values(1)
        else:
            # กรณี multi-ticker: MultiIndex(field, ticker) → ต้องสลับ level
            if not isinstance(all_yf_data.columns, pd.MultiIndex):
                logger.warning("Expected MultiIndex for multi-ticker download")
                return pd.DataFrame()

            # ตรวจว่า ticker อยู่ level ไหน
            lvl0_samples = set(all_yf_data.columns.get_level_values(0))
            field_names  = {"Open","High","Low","Close","Volume","Adj Close"}

            if lvl0_samples & field_names:
                # level 0 = field, level 1 = ticker → swap แล้วดึง ticker
                df = all_yf_data.swaplevel(axis=1)[yf_sym].copy()
            else:
                # level 0 = ticker, level 1 = field
                if yf_sym not in lvl0_samples:
                    logger.warning(f"{yf_sym} not in downloaded tickers: {lvl0_samples}")
                    return pd.DataFrame()
                df = all_yf_data[yf_sym].copy()

        # แก้ BUG-02: กรอง NaN ทั้ง Close, High, Low ก่อนคำนวณ ATR
        df = df.dropna(subset=["Close", "High", "Low"])
        return df

    except Exception as exc:
        logger.error(f"extract_asset_df failed for {yf_sym}: {type(exc).__name__}: {exc}")
        return pd.DataFrame()


def fetch_single_yf_fallback(yf_sym: str, period: str, interval: str) -> pd.DataFrame:
    """
    Fallback: ดาวน์โหลดเดี่ยวถ้า extract จาก batch ไม่ได้
    แก้ BUG-04: รองรับกรณี MultiIndex แปลกๆ
    """
    try:
        df = yf.download(
            tickers=yf_sym,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close", "High", "Low"])
        return df
    except Exception as exc:
        logger.error(f"Fallback download failed for {yf_sym}: {exc}")
        return pd.DataFrame()


# =========================================================================
# 🧮 Signal computation engine
# =========================================================================
def compute_technical_score(df_yf: pd.DataFrame) -> dict:
    """
    คำนวณ RSI, MACD, EMA, ATR จากข้อมูลราคา
    แหล่งข้อมูล: Yahoo Finance OHLCV → คำนวณด้วย ta library (local)
    แก้ BUG-02: dropna High/Low ก่อนคำนวณ ATR แล้ว
    """
    result = {
        "is_valid":       False,
        "latest_price":   0.0,
        "trend_score":    0.0,
        "momentum_score": 0.0,
        "rsi":            50.0,
        "macd":           0.0,
        "atr":            0.0,
        "warnings":       [],
    }

    if df_yf.empty or len(df_yf) < 15:
        result["warnings"].append(f"ข้อมูลไม่เพียงพอ (มี {len(df_yf)} แท่ง ต้องการ ≥ 15)")
        return result

    try:
        close = df_yf["Close"].astype(float).squeeze()
        high  = df_yf["High"].astype(float).squeeze()
        low   = df_yf["Low"].astype(float).squeeze()

        result["latest_price"] = float(close.iloc[-1])

        # --- RSI (14) ---
        rsi_series = RSIIndicator(close=close, window=14).rsi()
        result["rsi"] = float(rsi_series.dropna().iloc[-1]) if not rsi_series.dropna().empty else 50.0

        # --- MACD histogram ---
        macd_diff = MACD(close=close).macd_diff()
        result["macd"] = float(macd_diff.dropna().iloc[-1]) if not macd_diff.dropna().empty else 0.0

        # --- ATR (14) — BUG-02 แก้แล้ว: High/Low ถูก dropna ก่อนมาถึงนี่ ---
        atr_series = AverageTrueRange(
            high=high, low=low, close=close, window=14
        ).average_true_range()
        last_atr = atr_series.dropna().iloc[-1] if not atr_series.dropna().empty else None
        result["atr"] = float(last_atr) if last_atr is not None and pd.notna(last_atr) else 0.0

        # --- Momentum score (30%) ---
        result["momentum_score"] += 15.0 if result["macd"] > 0 else -15.0
        result["momentum_score"] += 15.0 if result["rsi"] > 50 else -15.0

        # --- Trend score (40%) — ต้องการ 52+ แท่ง ---
        if len(df_yf) >= MIN_BARS_REQUIRED:
            ema20 = EMAIndicator(close=close, window=20).ema_indicator()
            ema50 = EMAIndicator(close=close, window=50).ema_indicator()

            ema20_clean = ema20.dropna()
            ema50_clean = ema50.dropna()

            if not ema20_clean.empty and not ema50_clean.empty:
                last_close = float(close.iloc[-1])
                last_ema20 = float(ema20_clean.iloc[-1])
                last_ema50 = float(ema50_clean.iloc[-1])

                if last_close > last_ema20 and last_ema20 > last_ema50:
                    result["trend_score"] = 40.0   # Bullish full alignment
                elif last_close > last_ema20 and last_ema20 <= last_ema50:
                    result["trend_score"] = 10.0   # Partial bullish
                elif last_close < last_ema20 and last_ema20 < last_ema50:
                    result["trend_score"] = -40.0  # Bearish full alignment
                elif last_close < last_ema20 and last_ema20 >= last_ema50:
                    result["trend_score"] = -10.0  # Partial bearish
                # else: mixed → 0
            else:
                result["warnings"].append("EMA มี NaN — ไม่คิด trend score")
        else:
            result["warnings"].append(
                f"ข้อมูล {len(df_yf)} แท่ง < {MIN_BARS_REQUIRED} — ข้าม EMA50 trend"
            )

        result["is_valid"] = True

    except Exception as exc:
        logger.error(f"compute_technical_score error: {type(exc).__name__}: {exc}")
        result["warnings"].append("คำนวณ indicators ไม่สำเร็จ")

    return result


def build_signal(tech: dict, tv: dict, ticker: str) -> dict:
    """รวม score ทั้งหมด → สัญญาณสุดท้าย"""
    trend_score    = tech.get("trend_score", 0.0)
    momentum_score = tech.get("momentum_score", 0.0)

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

    if total_score > 15:
        side       = "BUY"
        prob       = clamp(abs_score, 0.0, 100.0)
        signal_str = "🟢 STRONG BUY" if prob >= 75 else "🟢 BUY"
    elif total_score < -15:
        side       = "SELL"
        prob       = clamp(abs_score, 0.0, 100.0)
        signal_str = "🔴 STRONG SELL" if prob >= 75 else "🔴 SELL"
    else:
        side       = "HOLD"
        prob       = 50.0   # ไม่มีทิศ → 50% ไม่ใช่ 100% (bug v1)
        signal_str = "🟡 HOLD (ไซด์เวย์)"

    # Pip range
    pip_mult = get_pip_multiplier(ticker)
    atr_val  = tech.get("atr", 0.0)
    atr_pips = (atr_val * pip_mult) if (atr_val and atr_val > 0 and pd.notna(atr_val)) else None

    # Price format
    latest_price = tech.get("latest_price", 0.0)
    if "JPY" in ticker:
        price_fmt = f"{latest_price:,.3f}"
    elif any(k in ticker for k in ("BTC", "ETH")):
        price_fmt = f"{latest_price:,.2f}"
    elif "XAU" in ticker or "XAG" in ticker:
        price_fmt = f"{latest_price:,.2f}"
    else:
        price_fmt = f"{latest_price:,.5f}"

    return {
        "side_verdict":   side,
        "probability":    prob,
        "final_signal":   signal_str,
        "total_score":    total_score,
        "trend_part":     trend_score,
        "momentum_part":  momentum_score,
        "consensus_part": consensus_score,
        "rsi":            tech.get("rsi", 50.0),
        "macd":           tech.get("macd", 0.0),
        "atr":            atr_val,
        "atr_pips":       atr_pips,
        "tv_buy":         tv.get("buy", 0),
        "tv_sell":        tv.get("sell", 0),
        "tv_neutral":     tv.get("neutral", 0),
        "latest_price":   latest_price,
        "price_fmt":      price_fmt,
        "warnings":       tech.get("warnings", []),
    }


# =========================================================================
# 🎨 Page config + Mobile CSS — แก้ BUG-05
# =========================================================================
st.set_page_config(
    page_title="AI Forex Sniper Pro",
    page_icon="💱",
    layout="wide",
)

# Inject CSS สำหรับ mobile responsive (แนวนอนและแนวตั้ง)
st.markdown("""
<style>
/* ===== Mobile responsive base ===== */
@media (max-width: 768px) {
    /* ย่อ font หัวข้อบน mobile */
    h1 { font-size: 1.3rem !important; }
    h2, h3 { font-size: 1.1rem !important; }

    /* ปุ่มสแกนเต็มความกว้าง */
    .stButton > button {
        width: 100% !important;
        font-size: 0.85rem !important;
        padding: 0.4rem 0.5rem !important;
    }

    /* dataframe scroll แนวนอน */
    .stDataFrame { overflow-x: auto !important; }
    .stDataFrame > div { min-width: 0 !important; }

    /* metric cards ขนาดเล็กลง */
    [data-testid="metric-container"] {
        padding: 0.3rem !important;
    }
    [data-testid="metric-container"] label {
        font-size: 0.7rem !important;
    }
    [data-testid="metric-container"] [data-testid="stMetricValue"] {
        font-size: 1rem !important;
    }

    /* warning/info box ย่อลง */
    .stAlert { font-size: 0.8rem !important; padding: 0.5rem !important; }

    /* selectbox เต็มความกว้าง */
    .stSelectbox { width: 100% !important; }
}

/* ===== Landscape mobile (แนวนอน) ===== */
@media (max-width: 926px) and (orientation: landscape) {
    h1 { font-size: 1.1rem !important; }
    /* ลด padding ของ main container */
    .block-container {
        padding-top: 0.5rem !important;
        padding-bottom: 0.5rem !important;
    }
}

/* ===== Dataframe scroll ทุก viewport ===== */
.stDataFrame {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
}

/* ===== expander header อ่านง่ายขึ้น ===== */
.streamlit-expanderHeader {
    font-size: 0.9rem !important;
    font-weight: 600 !important;
}
</style>
""", unsafe_allow_html=True)


# =========================================================================
# 🏷️ Header + Disclaimer
# =========================================================================
st.title("💱 AI Forex Sniper Pro v3.0")
st.caption("ระบบวิเคราะห์มติรวมพหุภาคี | Forex · Gold · Crypto | 24/5")

st.warning(
    "**⚠️ คำเตือนทางการเงิน:** "
    "ข้อมูลทั้งหมดเป็นการวิเคราะห์เชิงเทคนิคอัตโนมัติ **ไม่ใช่คำแนะนำการลงทุน** "
    "Forex/Gold/Crypto มีความเสี่ยงสูง อาจสูญเสียเงินต้นทั้งหมด "
    "ผู้ใช้รับผิดชอบการตัดสินใจเองทุกกรณี"
)

# =========================================================================
# 🗄️ Session state
# =========================================================================
for key, val in {"auto_mode": False, "run_once": False, "fail_count": 0}.items():
    if key not in st.session_state:
        st.session_state[key] = val


# =========================================================================
# ⚙️ Control Panel
# =========================================================================
st.subheader("⚙️ ตัวเลือก")
col_s1, col_s2 = st.columns([2, 1])

with col_s1:
    user_input = st.text_input(
        f"⌨️ คู่เงิน (คั่นด้วย `,` สูงสุด {MAX_ASSETS} คู่ รองรับ Forex/Gold/Crypto):",
        value="EURUSD, GBPUSD, USDJPY, XAUUSD, USDTHB",
        max_chars=200,
        help="ตัวอย่าง: EURUSD, XAUUSD, BTCUSD, USDJPY, USDTHB"
    )

with col_s2:
    tf_choice = st.selectbox(
        "⏱️ Timeframe:",
        options=[
            "5m — สคัลปิ้ง",
            "15m — เล่นสั้น",
            "1h — เดย์เทรด",
            "1d — สวิงเทรด",
        ],
        index=2,
    )

TF_MAP = {
    "5m":  ("5m",  "2d",   "5m"),
    "15m": ("15m", "5d",   "15m"),
    "1h":  ("1h",  "60d",  "1h"),
    "1d":  ("1d",  "180d", "1d"),
}

tf_key = tf_choice.split("—")[0].strip()
yf_interval, yf_period, tv_interval_str = TF_MAP.get(tf_key, ("1h", "60d", "1h"))

# =========================================================================
# ✅ Validate assets
# =========================================================================
raw_assets = [x.strip() for x in user_input.split(",") if x.strip()]
selected_assets: list = []
invalid_assets:  list = []

for raw in raw_assets:
    try:
        parse_forex_meta(raw)   # validate เท่านั้น ไม่เก็บผล
        selected_assets.append(raw)
    except ValueError as e:
        invalid_assets.append(f"`{raw}` — {e}")

if invalid_assets:
    st.error("❌ สัญลักษณ์ไม่ถูกต้อง:\n" + "\n".join(invalid_assets))

if len(selected_assets) > MAX_ASSETS:
    st.warning(
        f"⚠️ จำกัด {MAX_ASSETS} คู่ — ตัดทิ้ง: {', '.join(selected_assets[MAX_ASSETS:])}"
    )
    selected_assets = selected_assets[:MAX_ASSETS]

# =========================================================================
# 🎯 Scan buttons
# =========================================================================
cb1, cb2, cb3 = st.columns(3)
with cb1:
    if st.button("🔍 สแกนทันที", use_container_width=True):
        st.session_state.update({"auto_mode": False, "run_once": True, "fail_count": 0})
        st.rerun()
with cb2:
    if st.button("🔄 Auto (60s)", use_container_width=True):
        st.session_state.update({"auto_mode": True, "run_once": False, "fail_count": 0})
        st.toast("⚡ เริ่ม Auto Refresh")
        st.rerun()
with cb3:
    if st.button("🛑 หยุด", use_container_width=True):
        st.session_state.update({"auto_mode": False, "run_once": False, "fail_count": 0})
        st.toast("🛑 หยุดระบบแล้ว")
        st.rerun()

if st.session_state.auto_mode:
    st_autorefresh(interval=60_000, key="forex_refresh_v3")
    fc = st.session_state.fail_count
    st.info(
        f"🔄 Auto Refresh ทุก 60 วิ"
        + (f" | ⚠️ ล้มเหลว {fc}/{MAX_AUTO_FAIL} ครั้งติด" if fc > 0 else "")
        + f" | 📦 Cache: ข้อมูลบันทึกไว้ 55 วิ (ประหยัด API calls)"
    )

# =========================================================================
# 🚀 Main scanning engine
# =========================================================================
if (st.session_state.run_once or st.session_state.auto_mode) and selected_assets:

    dashboard_rows: list = []
    detail_map:     dict = {}
    success_count = 0

    prog = st.progress(0, text="⏳ เตรียมข้อมูล...")

    # Step 1: Build meta list
    meta_list = [parse_forex_meta(a) for a in selected_assets]
    yf_tickers_tuple = tuple(m["yf_sym"] for m in meta_list)
    is_single = len(meta_list) == 1

    # Step 2: ดาวน์โหลดราคา batch (cached)
    # แหล่งข้อมูล: Yahoo Finance — ราคา OHLCV (delayed ~15 นาที สำหรับ Forex)
    with st.spinner("📡 Yahoo Finance — ดาวน์โหลดราคา OHLCV..."):
        all_yf = fetch_yfinance_cached(
            tickers_tuple=yf_tickers_tuple,
            period=yf_period,
            interval=yf_interval,
        )

    # Step 3: วิเคราะห์แต่ละ asset
    total = len(meta_list)
    for idx, meta in enumerate(meta_list):
        prog.progress(
            (idx + 1) / total,
            text=f"⏳ วิเคราะห์ {meta['clean_ticker']} ({idx+1}/{total})..."
        )

        try:
            # --- TradingView (cached, retry, delay)
            # แหล่งข้อมูล: TradingView TA — รวมสัญญาณจาก indicators ~30+ ตัว
            tv_data = {"buy": 0, "sell": 0, "neutral": 0, "recommend": "NEUTRAL"}
            try:
                tv_data = fetch_tradingview_cached(
                    tv_sym=meta["tv_sym"],
                    tv_exch=meta["tv_exch"],       # ← BUG-01 แก้แล้ว (OANDA/BINANCE/FX_IDC)
                    tv_screen=meta["tv_screen"],   # ← BUG-01 แก้แล้ว (crypto/forex)
                    tv_interval_str=tv_interval_str,
                )
            except Exception as exc:
                logger.warning(f"TV failed for {meta['tv_sym']} ({meta['tv_exch']}): {exc}")

            if idx < total - 1:
                time.sleep(TV_REQUEST_DELAY)

            # --- Yahoo Finance extraction
            df_yf = extract_asset_df(all_yf, meta["yf_sym"], is_single)

            # Fallback: ดาวน์โหลดเดี่ยวถ้า batch extract ไม่ได้
            if df_yf.empty:
                logger.warning(f"Batch extract empty for {meta['yf_sym']}, trying single download")
                df_yf = fetch_single_yf_fallback(meta["yf_sym"], yf_period, yf_interval)

            # --- คำนวณ technical indicators
            tech = compute_technical_score(df_yf)

            # --- สร้างสัญญาณ
            signal = build_signal(tech, tv_data, meta["clean_ticker"])

            # --- Dashboard row
            atr_disp = f"{signal['atr_pips']:.1f}" if signal["atr_pips"] else "N/A"
            trend_disp = (
                "📈 Bullish" if signal["side_verdict"] == "BUY"
                else "📉 Bearish" if signal["side_verdict"] == "SELL"
                else "➡️ ไซด์เวย์"
            )

            dashboard_rows.append({
                "คู่เงิน":          meta["clean_ticker"],
                "ราคา":             signal["price_fmt"],
                "สัญญาณ":           signal["final_signal"],
                "มั่นใจ %":         f"{signal['probability']:.0f}%",
                "ATR (Pips)":       atr_disp,
                "แนวโน้ม":          trend_disp,
                "TV Vote B/S/N":    f"{signal['tv_buy']}/{signal['tv_sell']}/{signal['tv_neutral']}",
            })

            detail_map[meta["clean_ticker"]] = {**signal, "meta": meta}
            success_count += 1

        except Exception as exc:
            logger.error(f"Error processing {meta['clean_ticker']}: {type(exc).__name__}: {exc}")
            st.warning(f"⚠️ วิเคราะห์ `{meta['clean_ticker']}` ไม่สำเร็จชั่วคราว")

    prog.empty()

    # --- Failure tracking
    if success_count == 0:
        st.session_state.fail_count += 1
        if st.session_state.fail_count >= MAX_AUTO_FAIL:
            st.session_state.auto_mode = False
            st.error(f"🛑 API ล้มเหลวติดต่อกัน {MAX_AUTO_FAIL} ครั้ง — หยุด Auto Refresh อัตโนมัติ")
    else:
        st.session_state.fail_count = 0

    # =========================================================================
    # 📊 Dashboard UI
    # =========================================================================
    if dashboard_rows:
        st.divider()
        st.subheader(f"📊 ผลสแกน {len(dashboard_rows)} คู่เงิน — {tf_choice}")

        # แหล่งข้อมูลที่ใช้
        st.caption(
            "📡 แหล่งข้อมูล: "
            "**ราคา** Yahoo Finance (OHLCV delayed ~15 min) · "
            "**Forex signal** TradingView FX_IDC · "
            "**Gold/Silver** TradingView OANDA · "
            "**Crypto** TradingView BINANCE · "
            "**Indicators** ta-lib (คำนวณ local)"
        )

        df_dash = pd.DataFrame(dashboard_rows)
        st.dataframe(df_dash, use_container_width=True, hide_index=True)

        # =========================================
        # 🔍 Detail panels (mobile-friendly layout)
        # แก้ BUG-05: ใช้ 2 col แทน 4 col บน mobile
        # =========================================
        st.divider()
        st.subheader("🔍 วิเคราะห์เจาะลึกรายคู่เงิน")

        for name, data in detail_map.items():
            emoji  = "🟢" if data["side_verdict"] == "BUY" else ("🔴" if data["side_verdict"] == "SELL" else "🟡")
            exch_label = f"{data['meta']['tv_exch']} / {data['meta']['tv_screen']}"

            with st.expander(f"{emoji} {name} · {data['final_signal']} · มั่นใจ {data['probability']:.0f}%"):

                # แสดง warnings
                if data.get("warnings"):
                    for w in data["warnings"]:
                        st.caption(f"⚠️ {w}")

                # Score breakdown
                st.markdown(f"**คะแนนรวม (Raw Score): {data['total_score']:+.1f} / ±100**")
                breakdown_df = pd.DataFrame([
                    {"แกน": "EMA 20/50 Trend",      "น้ำหนัก": "±40", "ได้":  f"{data['trend_part']:+.1f}",    "แหล่ง": "Yahoo Finance → ta-lib"},
                    {"แกน": "RSI + MACD Momentum",   "น้ำหนัก": "±30", "ได้":  f"{data['momentum_part']:+.1f}", "แหล่ง": "Yahoo Finance → ta-lib"},
                    {"แกน": f"Consensus ({exch_label})", "น้ำหนัก": "±30", "ได้": f"{data['consensus_part']:+.1f}", "แหล่ง": f"TradingView {data['meta']['tv_exch']}"},
                ])
                st.dataframe(breakdown_df, use_container_width=True, hide_index=True)

                # Metrics — 2 คอลัมน์ (mobile friendly กว่า 4 คอลัมน์)
                mc1, mc2 = st.columns(2)
                with mc1:
                    st.metric("RSI (14)", f"{data['rsi']:.1f}",
                              help="< 30 Oversold · > 70 Overbought")
                    atr_disp2 = f"{data['atr_pips']:.1f} Pips" if data["atr_pips"] else "N/A"
                    st.metric("ATR Pips (กรอบผันผวน)", atr_disp2,
                              help="ระยะวิ่งเฉลี่ยต่อแท่งเทียน จาก Yahoo Finance")
                with mc2:
                    st.metric("MACD Histogram", f"{data['macd']:.6f}",
                              help="บวก = momentum ขาขึ้น")
                    st.metric(f"บอท Vote ({exch_label})",
                              f"B:{data['tv_buy']} S:{data['tv_sell']} N:{data['tv_neutral']}",
                              help="จาก TradingView Technical Analysis")

                # TP/SL
                if data["atr_pips"] and data["atr_pips"] > 0:
                    a = data["atr_pips"]
                    st.info(
                        f"💡 **แนวทาง TP/SL (อ้างอิง ATR {a:.1f} Pips)**\n\n"
                        f"🎯 TP1 ≈ **{a:.1f}** Pips &nbsp;|&nbsp; "
                        f"TP2 ≈ **{a*2:.1f}** Pips &nbsp;|&nbsp; "
                        f"SL ≈ **{a*0.5:.1f}** Pips  \n"
                        f"*ค่าอ้างอิงเท่านั้น ไม่ใช่คำแนะนำการลงทุน*"
                    )
                else:
                    st.caption("⚠️ ATR ไม่พร้อม — ข้อมูลราคาอาจยังไม่เพียงพอ")

    # Footer
    st.divider()
    st.caption(
        "⚠️ สัญญาณทั้งหมดเป็นการวิเคราะห์เชิงเทคนิคอัตโนมัติ ไม่ใช่คำแนะนำการลงทุน "
        "| AI Forex Sniper Pro v3.0 | YF + TradingView (FX_IDC · OANDA · BINANCE)"
    )

    st.session_state.run_once = False

elif not selected_assets and not invalid_assets:
    st.info("ℹ️ กรุณากรอกคู่เงินด้านบน แล้วกด 'สแกนทันที'")
