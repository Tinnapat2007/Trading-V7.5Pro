"""
AI Forex Sniper Pro (v4.0 — Forex Only Edition)
================================================
ระบบวิเคราะห์สัญญาณ Forex เท่านั้น (สกุลเงินประเทศ 2 ชนิด)
ไม่รองรับ: ทอง, เงิน, crypto, index, หรือ commodity ใดๆ ทั้งสิ้น

แหล่งข้อมูล:
  ราคา OHLCV  → Yahoo Finance  ({PAIR}=X)  delayed ~15 นาที
  สัญญาณ      → TradingView FX_IDC         สัญญาณ Forex มาตรฐาน
  Indicators   → ta library (RSI/MACD/EMA/ATR) คำนวณ local
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
MAX_ASSETS          = 10    # คู่เงินสูงสุดต่อการสแกน
MIN_BARS_REQUIRED   = 52    # EMA50 ต้องการ 50+ แท่ง
YF_DOWNLOAD_TIMEOUT = 30    # วินาที timeout Yahoo Finance
TV_REQUEST_DELAY    = 0.35  # วินาที ระหว่าง TradingView request
MAX_RETRIES         = 3
MAX_AUTO_FAIL       = 3

# =========================================================================
# 🌐 FOREX-ONLY: รายชื่อสกุลเงินที่ยอมรับ
# =========================================================================
# เฉพาะสกุลเงินประเทศเท่านั้น — ไม่มี XAU, XAG, BTC, ETH ฯลฯ
FOREX_CURRENCIES = {
    # G10
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "NOK", "SEK", "DKK",
    # เอเชีย
    "SGD", "HKD", "CNY", "CNH", "TWD", "KRW", "THB", "MYR",
    "IDR", "PHP", "VND", "INR",
    # ตะวันออกกลาง / แอฟริกา
    "SAR", "AED", "QAR", "KWD", "BHD", "OMR", "EGP",
    "ZAR", "NGN", "KES", "GHS",
    # ยุโรปอื่น
    "PLN", "HUF", "CZK", "RUB", "UAH", "ILS", "TRY",
    # อเมริกาใต้ / อื่น
    "MXN", "BRL", "CLP", "COP", "PEN", "ARS",
}

# keyword ที่ห้ามผ่านอย่างเด็ดขาด (block list)
NON_FOREX_BLOCK = {
    "XAU", "XAG", "XPT", "XPD",                          # metals
    "BTC", "ETH", "XRP", "BNB", "SOL", "ADA", "DOGE",    # crypto
    "LTC", "DOT", "AVAX", "MATIC", "LINK", "UNI",
    "SPX", "NDX", "DJI", "VIX", "DAX", "FTSE", "NKY",   # indices
    "OIL", "WTI", "GAS", "NG", "GC", "SI", "CL",         # commodities/futures
}

# Pip multiplier — เฉพาะ Forex
PIP_MULT_FOREX = {
    "JPY": 100,   # 2 ทศนิยม → 1 pip = 0.01
}
DEFAULT_PIP = 10_000  # 4 ทศนิยม → 1 pip = 0.0001

# =========================================================================
# 📋 Logging
# =========================================================================
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("forex_sniper")


# =========================================================================
# 🔁 Retry decorator
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
            raise last_exc
        return wrapper
    return decorator


# =========================================================================
# 🛡️ Forex-Only Validator
# =========================================================================
def validate_forex_pair(raw: str) -> tuple[bool, str, str]:
    """
    ตรวจสอบว่าเป็นคู่เงิน Forex จริงๆ
    คืน (is_valid, clean_symbol, error_message)

    กฎ:
      1. ต้องมีตัวอักษร A-Z เท่านั้น ความยาว 6 ตัว
      2. base และ quote ต้องอยู่ใน FOREX_CURRENCIES
      3. ห้ามมี keyword ใน NON_FOREX_BLOCK เด็ดขาด
      4. base != quote
    """
    # กรอง A-Z เท่านั้น ไม่รับ 0-9 (Forex ไม่มีตัวเลขใน symbol)
    cleaned = re.sub(r"[^A-Z]", "", raw.strip().upper())

    if len(cleaned) != 6:
        return False, "", f"ต้องเป็น 6 ตัวอักษร เช่น EURUSD (ได้ '{cleaned}' = {len(cleaned)} ตัว)"

    base  = cleaned[:3]
    quote = cleaned[3:]

    # ตรวจ block list ก่อน (เร็วกว่า)
    for bk in NON_FOREX_BLOCK:
        if bk in cleaned:
            return False, "", f"'{bk}' ไม่ใช่สกุลเงิน Forex — ระบบรองรับเฉพาะ Forex เท่านั้น"

    if base == quote:
        return False, "", f"base และ quote เป็นสกุลเดียวกัน ({base})"

    if base not in FOREX_CURRENCIES:
        return False, "", f"'{base}' ไม่อยู่ในรายการสกุลเงิน Forex ที่รองรับ"

    if quote not in FOREX_CURRENCIES:
        return False, "", f"'{quote}' ไม่อยู่ในรายการสกุลเงิน Forex ที่รองรับ"

    return True, cleaned, "OK"


def get_pip_multiplier(ticker: str) -> int:
    for key, mult in PIP_MULT_FOREX.items():
        if key in ticker.upper():
            return mult
    return DEFAULT_PIP


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# =========================================================================
# 📡 Data fetching — cached
# =========================================================================
@st.cache_data(ttl=55, show_spinner=False)
def fetch_tradingview_cached(
    tv_sym: str,
    tv_interval_str: str,
) -> dict:
    """
    แหล่งข้อมูล: TradingView FX_IDC
    FX_IDC = Forex Interbank Data Center — รวมสัญญาณจาก broker หลายร้อยแห่ง
    screener="forex" เสมอ (Forex only)
    cache 55 วิ ป้องกันยิง API ซ้ำ
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
            exchange="FX_IDC",    # Forex เท่านั้น — ไม่เปลี่ยน
            screener="forex",     # Forex เท่านั้น — ไม่เปลี่ยน
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
    tickers_tuple: tuple,
    period: str,
    interval: str,
) -> pd.DataFrame:
    """
    แหล่งข้อมูล: Yahoo Finance
    Forex symbol format: EURUSD=X, USDJPY=X ฯลฯ
    delayed ~15 นาทีสำหรับ Forex (ข้อมูลฟรี)
    cache 55 วิ ลดเวลาโหลด
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
        with ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_download).result(timeout=YF_DOWNLOAD_TIMEOUT)
    except FuturesTimeoutError:
        logger.error("yf.download timed out")
        return pd.DataFrame()
    except Exception as exc:
        logger.error(f"yf.download failed: {exc}")
        return pd.DataFrame()


def extract_df(all_yf: pd.DataFrame, yf_sym: str, is_single: bool) -> pd.DataFrame:
    """
    แยก DataFrame ของ asset หนึ่งออกจาก batch download
    จัดการ MultiIndex (field, ticker) อย่างถูกต้อง
    กรอง NaN ทั้ง Close, High, Low ก่อนคืนค่า
    """
    try:
        if all_yf is None or all_yf.empty:
            return pd.DataFrame()

        FIELD_NAMES = {"Open", "High", "Low", "Close", "Volume", "Adj Close"}

        if is_single:
            df = all_yf.copy()
            if isinstance(df.columns, pd.MultiIndex):
                lvl0 = set(df.columns.get_level_values(0))
                df.columns = (
                    df.columns.get_level_values(0)
                    if lvl0 & FIELD_NAMES
                    else df.columns.get_level_values(1)
                )
        else:
            if not isinstance(all_yf.columns, pd.MultiIndex):
                return pd.DataFrame()
            lvl0 = set(all_yf.columns.get_level_values(0))
            if lvl0 & FIELD_NAMES:
                # (field, ticker) → swap ก่อนดึง
                swapped = all_yf.swaplevel(axis=1)
                if yf_sym not in swapped.columns.get_level_values(0):
                    return pd.DataFrame()
                df = swapped[yf_sym].copy()
            else:
                if yf_sym not in lvl0:
                    return pd.DataFrame()
                df = all_yf[yf_sym].copy()

        # กรอง NaN ทั้ง 3 คอลัมน์ (สำคัญสำหรับ ATR)
        needed = [c for c in ["Close", "High", "Low"] if c in df.columns]
        df = df.dropna(subset=needed)
        return df

    except Exception as exc:
        logger.error(f"extract_df {yf_sym}: {exc}")
        return pd.DataFrame()


def fallback_single_download(yf_sym: str, period: str, interval: str) -> pd.DataFrame:
    """ดาวน์โหลดเดี่ยว เมื่อ batch extract ไม่ได้"""
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
        needed = [c for c in ["Close", "High", "Low"] if c in df.columns]
        return df.dropna(subset=needed)
    except Exception as exc:
        logger.error(f"fallback_single_download {yf_sym}: {exc}")
        return pd.DataFrame()


# =========================================================================
# 🧮 Technical computation
# =========================================================================
def compute_indicators(df: pd.DataFrame) -> dict:
    """
    คำนวณ RSI, MACD, EMA20/50, ATR จาก Yahoo Finance OHLCV
    คืน dict พร้อม is_valid flag
    """
    result = {
        "is_valid":       False,
        "latest_price":   0.0,
        "trend_score":    0.0,
        "momentum_score": 0.0,
        "rsi":            50.0,
        "macd":           0.0,
        "atr":            0.0,
        "bar_count":      len(df),
        "warnings":       [],
    }

    if df.empty or len(df) < 15:
        result["warnings"].append(f"ข้อมูลน้อยเกินไป ({len(df)} แท่ง ต้องการ ≥ 15)")
        return result

    try:
        close = df["Close"].astype(float).squeeze()
        high  = df["High"].astype(float).squeeze()
        low   = df["Low"].astype(float).squeeze()

        result["latest_price"] = float(close.iloc[-1])

        # RSI (14) — วัดความแข็งแกร่งราคา
        rsi_s = RSIIndicator(close=close, window=14).rsi().dropna()
        result["rsi"] = float(rsi_s.iloc[-1]) if not rsi_s.empty else 50.0

        # MACD histogram — วัด momentum
        macd_s = MACD(close=close).macd_diff().dropna()
        result["macd"] = float(macd_s.iloc[-1]) if not macd_s.empty else 0.0

        # ATR (14) — วัดความผันผวน (High/Low ถูก dropna แล้วตั้งแต่ extract)
        atr_s = AverageTrueRange(
            high=high, low=low, close=close, window=14
        ).average_true_range().dropna()
        result["atr"] = float(atr_s.iloc[-1]) if not atr_s.empty else 0.0

        # Momentum score ±30
        result["momentum_score"] += 15.0 if result["macd"] > 0 else -15.0
        result["momentum_score"] += 15.0 if result["rsi"] > 50 else -15.0

        # Trend score ±40 (ต้องการ 52+ แท่ง)
        if len(df) >= MIN_BARS_REQUIRED:
            ema20 = EMAIndicator(close=close, window=20).ema_indicator().dropna()
            ema50 = EMAIndicator(close=close, window=50).ema_indicator().dropna()

            if not ema20.empty and not ema50.empty:
                c  = float(close.iloc[-1])
                e20 = float(ema20.iloc[-1])
                e50 = float(ema50.iloc[-1])

                if   c > e20 and e20 > e50:   result["trend_score"] =  40.0  # Bullish full
                elif c > e20 and e20 <= e50:  result["trend_score"] =  15.0  # Bullish partial
                elif c < e20 and e20 < e50:   result["trend_score"] = -40.0  # Bearish full
                elif c < e20 and e20 >= e50:  result["trend_score"] = -15.0  # Bearish partial
                # else mixed → 0
            else:
                result["warnings"].append("EMA มี NaN — ข้าม trend score")
        else:
            result["warnings"].append(
                f"ข้อมูล {len(df)} แท่ง < {MIN_BARS_REQUIRED} — ข้าม EMA50"
            )

        result["is_valid"] = True

    except Exception as exc:
        logger.error(f"compute_indicators: {type(exc).__name__}: {exc}")
        result["warnings"].append("คำนวณ indicators ไม่สำเร็จ")

    return result


def build_signal(tech: dict, tv: dict, ticker: str) -> dict:
    """รวม technical + TradingView → สัญญาณสุดท้าย"""
    trend    = tech.get("trend_score", 0.0)
    momentum = tech.get("momentum_score", 0.0)

    rec = tv.get("recommend", "NEUTRAL")
    if   "STRONG_BUY"  in rec or "STRONG BUY"  in rec: consensus =  30.0
    elif "BUY"         in rec:                          consensus =  15.0
    elif "STRONG_SELL" in rec or "STRONG SELL" in rec:  consensus = -30.0
    elif "SELL"        in rec:                          consensus = -15.0
    else:                                               consensus =   0.0

    total    = trend + momentum + consensus
    abs_total = abs(total)

    if total > 15:
        side = "BUY"
        prob = clamp(abs_total, 0.0, 100.0)
        sig  = "🟢 STRONG BUY" if prob >= 75 else "🟢 BUY"
    elif total < -15:
        side = "SELL"
        prob = clamp(abs_total, 0.0, 100.0)
        sig  = "🔴 STRONG SELL" if prob >= 75 else "🔴 SELL"
    else:
        side = "HOLD"
        prob = 50.0   # ไม่มีทิศ → 50 คงที่
        sig  = "🟡 HOLD (ไซด์เวย์)"

    pip_mult = get_pip_multiplier(ticker)
    atr_val  = tech.get("atr", 0.0)
    atr_pips = (atr_val * pip_mult) if (atr_val > 0 and pd.notna(atr_val)) else None

    price = tech.get("latest_price", 0.0)
    price_fmt = f"{price:,.3f}" if "JPY" in ticker else f"{price:,.5f}"

    return {
        "side":          side,
        "prob":          prob,
        "signal":        sig,
        "total_score":   total,
        "trend":         trend,
        "momentum":      momentum,
        "consensus":     consensus,
        "rsi":           tech.get("rsi", 50.0),
        "macd":          tech.get("macd", 0.0),
        "atr":           atr_val,
        "atr_pips":      atr_pips,
        "tv_buy":        tv.get("buy", 0),
        "tv_sell":       tv.get("sell", 0),
        "tv_neutral":    tv.get("neutral", 0),
        "price":         price,
        "price_fmt":     price_fmt,
        "bar_count":     tech.get("bar_count", 0),
        "warnings":      tech.get("warnings", []),
    }


# =========================================================================
# 🎨 Page config + Mobile CSS
# =========================================================================
st.set_page_config(
    page_title="AI Forex Sniper Pro",
    page_icon="💱",
    layout="wide",
)

st.markdown("""
<style>
/* Mobile แนวตั้งและแนวนอน */
@media (max-width: 768px) {
    h1 { font-size: 1.2rem !important; }
    h2, h3 { font-size: 1rem !important; }
    .stButton > button {
        width: 100% !important;
        font-size: 0.82rem !important;
        padding: 0.4rem !important;
    }
    .stAlert { font-size: 0.78rem !important; padding: 0.45rem !important; }
    [data-testid="metric-container"] label { font-size: 0.68rem !important; }
    [data-testid="metric-container"] [data-testid="stMetricValue"] {
        font-size: 0.95rem !important;
    }
    .streamlit-expanderHeader { font-size: 0.82rem !important; }
}
@media (max-width: 926px) and (orientation: landscape) {
    .block-container { padding-top: 0.4rem !important; padding-bottom: 0.4rem !important; }
    h1 { font-size: 1rem !important; }
}
/* Dataframe scroll แนวนอนทุก device */
.stDataFrame { overflow-x: auto !important; -webkit-overflow-scrolling: touch; }
</style>
""", unsafe_allow_html=True)


# =========================================================================
# 🏷️ Header
# =========================================================================
st.title("💱 AI Forex Sniper Pro (v4.0 — Forex Only)")
st.caption(
    "วิเคราะห์สัญญาณ Forex เท่านั้น | "
    "แหล่งข้อมูล: Yahoo Finance (ราคา) + TradingView FX_IDC (สัญญาณ)"
)

st.warning(
    "**⚠️ คำเตือน:** ข้อมูลทั้งหมดเป็นการวิเคราะห์เชิงเทคนิคอัตโนมัติ "
    "**ไม่ใช่คำแนะนำการลงทุน** การเทรด Forex มีความเสี่ยงสูง "
    "อาจสูญเสียเงินต้นทั้งหมด ผู้ใช้รับผิดชอบการตัดสินใจเองทุกกรณี"
)

# =========================================================================
# 🗄️ Session state
# =========================================================================
for k, v in {"auto_mode": False, "run_once": False, "fail_count": 0}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# =========================================================================
# ⚙️ Control Panel
# =========================================================================
st.subheader("⚙️ ตัวเลือก")
col_s1, col_s2 = st.columns([2, 1])

with col_s1:
    user_input = st.text_input(
        f"⌨️ คู่เงิน Forex (คั่นด้วย `,` สูงสุด {MAX_ASSETS} คู่):",
        value="EURUSD, GBPUSD, USDJPY, USDTHB, AUDUSD",
        max_chars=200,
        help=(
            "รับเฉพาะคู่เงิน Forex เท่านั้น เช่น EURUSD, USDJPY, GBPUSD\n"
            "ไม่รองรับ: ทอง (XAUUSD), Crypto (BTCUSD), Index หรือ Commodity"
        ),
    )

with col_s2:
    tf_choice = st.selectbox(
        "⏱️ Timeframe:",
        options=["5m — สคัลปิ้ง", "15m — เล่นสั้น", "1h — เดย์เทรด", "1d — สวิงเทรด"],
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
# ✅ Validate — Forex Only
# =========================================================================
raw_assets = [x.strip() for x in user_input.split(",") if x.strip()]
selected: list  = []   # (clean_sym, yf_sym)
rejected: list  = []   # (raw, reason)

for raw in raw_assets:
    valid, clean_sym, err = validate_forex_pair(raw)
    if valid:
        selected.append((clean_sym, f"{clean_sym}=X"))
    else:
        rejected.append((raw, err))

if rejected:
    msgs = "\n".join(f"❌ `{r}` — {e}" for r, e in rejected)
    st.error(f"สัญลักษณ์ต่อไปนี้ไม่ใช่ Forex หรือไม่ถูกต้อง:\n{msgs}")

if len(selected) > MAX_ASSETS:
    cut = [s[0] for s in selected[MAX_ASSETS:]]
    st.warning(f"⚠️ จำกัด {MAX_ASSETS} คู่ — ตัดทิ้ง: {', '.join(cut)}")
    selected = selected[:MAX_ASSETS]

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
    st_autorefresh(interval=60_000, key="forex_only_refresh")
    fc = st.session_state.fail_count
    st.info(
        "🔄 Auto Refresh ทุก 60 วิ | 📦 Cache 55 วิ (ประหยัด API calls)"
        + (f" | ⚠️ ล้มเหลว {fc}/{MAX_AUTO_FAIL} ครั้งติด" if fc > 0 else "")
    )

# =========================================================================
# 🚀 Main Engine
# =========================================================================
if (st.session_state.run_once or st.session_state.auto_mode) and selected:

    rows:    list = []
    details: dict = {}
    success_count = 0

    prog = st.progress(0, text="⏳ เตรียมข้อมูล...")

    # Step 1: batch download (Yahoo Finance)
    yf_tuple = tuple(yf_sym for _, yf_sym in selected)
    is_single = len(selected) == 1

    with st.spinner("📡 Yahoo Finance — ดาวน์โหลด OHLCV..."):
        all_yf = fetch_yfinance_cached(
            tickers_tuple=yf_tuple,
            period=yf_period,
            interval=yf_interval,
        )

    total = len(selected)

    for idx, (sym, yf_sym) in enumerate(selected):
        prog.progress(
            (idx + 1) / total,
            text=f"⏳ วิเคราะห์ {sym} ({idx+1}/{total})...",
        )

        try:
            # --- TradingView FX_IDC (Forex เท่านั้น)
            tv = {"buy": 0, "sell": 0, "neutral": 0, "recommend": "NEUTRAL"}
            try:
                tv = fetch_tradingview_cached(
                    tv_sym=sym,
                    tv_interval_str=tv_interval_str,
                )
            except Exception as exc:
                logger.warning(f"TV FX_IDC failed for {sym}: {exc}")

            if idx < total - 1:
                time.sleep(TV_REQUEST_DELAY)

            # --- Yahoo Finance: extract → fallback
            df = extract_df(all_yf, yf_sym, is_single)
            if df.empty:
                df = fallback_single_download(yf_sym, yf_period, yf_interval)

            # --- Indicators
            tech   = compute_indicators(df)
            signal = build_signal(tech, tv, sym)

            # --- Dashboard row
            atr_str   = f"{signal['atr_pips']:.1f}" if signal["atr_pips"] else "N/A"
            trend_str = ("📈 Bullish" if signal["side"] == "BUY"
                         else "📉 Bearish" if signal["side"] == "SELL"
                         else "➡️ ไซด์เวย์")

            rows.append({
                "คู่เงิน":        sym,
                "ราคา":           signal["price_fmt"],
                "สัญญาณ AI":      signal["signal"],
                "มั่นใจ":         f"{signal['prob']:.0f}%",
                "ATR (Pips)":    atr_str,
                "แนวโน้ม":        trend_str,
                "TV B/S/N":      f"{signal['tv_buy']}/{signal['tv_sell']}/{signal['tv_neutral']}",
                "แท่งข้อมูล":     str(signal["bar_count"]),
            })

            details[sym] = signal
            success_count += 1

        except Exception as exc:
            logger.error(f"Error {sym}: {type(exc).__name__}: {exc}")
            st.warning(f"⚠️ วิเคราะห์ `{sym}` ไม่สำเร็จชั่วคราว")

    prog.empty()

    # Failure tracking
    if success_count == 0:
        st.session_state.fail_count += 1
        if st.session_state.fail_count >= MAX_AUTO_FAIL:
            st.session_state.auto_mode = False
            st.error(f"🛑 API ล้มเหลว {MAX_AUTO_FAIL} ครั้งติด — หยุด Auto Refresh")
    else:
        st.session_state.fail_count = 0

    # =========================================================================
    # 📊 Dashboard
    # =========================================================================
    if rows:
        st.divider()
        st.subheader(f"📊 ผลสแกน Forex {len(rows)} คู่ — {tf_choice}")
        st.caption(
            "📡 **แหล่งข้อมูล:** "
            "ราคา → Yahoo Finance (`PAIR=X`, delayed ~15 min) · "
            "สัญญาณ → TradingView **FX_IDC** (forex screener) · "
            "Indicators → ta-lib คำนวณ local"
        )

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # Detail panels
        st.divider()
        st.subheader("🔍 วิเคราะห์เจาะลึกรายคู่เงิน")

        for sym, d in details.items():
            emoji = "🟢" if d["side"] == "BUY" else ("🔴" if d["side"] == "SELL" else "🟡")

            with st.expander(
                f"{emoji} {sym} · {d['signal']} · มั่นใจ {d['prob']:.0f}% "
                f"· ราคา {d['price_fmt']}"
            ):
                # Warnings
                for w in d.get("warnings", []):
                    st.caption(f"⚠️ {w}")

                # Score breakdown
                st.markdown(f"**คะแนนรวม: {d['total_score']:+.1f} / ±100**")

                bdf = pd.DataFrame([
                    {
                        "แกนชี้วัด":   "EMA 20/50 Trend (แนวโน้มหลัก)",
                        "น้ำหนัก":     "±40",
                        "คะแนน":       f"{d['trend']:+.1f}",
                        "แหล่งข้อมูล": "Yahoo Finance → ta-lib EMA",
                    },
                    {
                        "แกนชี้วัด":   "RSI + MACD Momentum",
                        "น้ำหนัก":     "±30",
                        "คะแนน":       f"{d['momentum']:+.1f}",
                        "แหล่งข้อมูล": "Yahoo Finance → ta-lib RSI/MACD",
                    },
                    {
                        "แกนชี้วัด":   "TradingView FX_IDC Consensus",
                        "น้ำหนัก":     "±30",
                        "คะแนน":       f"{d['consensus']:+.1f}",
                        "แหล่งข้อมูล": "TradingView FX_IDC (forex screener)",
                    },
                ])
                st.dataframe(bdf, use_container_width=True, hide_index=True)

                # Metrics 2 คอลัมน์ (mobile friendly)
                mc1, mc2 = st.columns(2)
                with mc1:
                    st.metric(
                        "RSI (14)",
                        f"{d['rsi']:.1f}",
                        help="< 30 = Oversold (ซื้อมากเกิน) · > 70 = Overbought (ขายมากเกิน)",
                    )
                    atr_disp = f"{d['atr_pips']:.1f} Pips" if d["atr_pips"] else "N/A"
                    st.metric(
                        "ATR (กรอบผันผวน)",
                        atr_disp,
                        help="Average True Range — ระยะวิ่งเฉลี่ยต่อแท่งเทียน คำนวณจาก Yahoo Finance",
                    )
                with mc2:
                    st.metric(
                        "MACD Histogram",
                        f"{d['macd']:.6f}",
                        help="บวก = momentum ขาขึ้น · ลบ = momentum ขาลง",
                    )
                    st.metric(
                        "TradingView Vote (B/S/N)",
                        f"{d['tv_buy']} / {d['tv_sell']} / {d['tv_neutral']}",
                        help="จำนวน indicators ที่โหวต BUY / SELL / NEUTRAL ใน FX_IDC",
                    )

                # TP/SL
                if d["atr_pips"] and d["atr_pips"] > 0:
                    a = d["atr_pips"]
                    st.info(
                        f"💡 **แนวทาง TP/SL อ้างอิง ATR = {a:.1f} Pips**\n\n"
                        f"🎯 TP1 ≈ **{a:.1f} Pips** &nbsp;·&nbsp; "
                        f"TP2 ≈ **{a*2:.1f} Pips** &nbsp;·&nbsp; "
                        f"SL ≈ **{a*0.5:.1f} Pips** (Risk 0.5×ATR)\n\n"
                        f"*ค่าอ้างอิงเท่านั้น ไม่ใช่คำแนะนำการลงทุน*"
                    )
                else:
                    st.caption(
                        "⚠️ ATR ยังไม่พร้อม — ข้อมูล High/Low อาจไม่เพียงพอใน timeframe นี้"
                    )

    st.divider()
    st.caption(
        "⚠️ สัญญาณทั้งหมดเป็นการวิเคราะห์เชิงเทคนิค ไม่ใช่คำแนะนำการลงทุน "
        "| AI Forex Sniper Pro v4.0 Forex Only "
        "| Yahoo Finance + TradingView FX_IDC"
    )

    st.session_state.run_once = False

elif not selected and not rejected:
    st.info("ℹ️ กรุณากรอกคู่เงิน Forex ด้านบน แล้วกด 'สแกนทันที'")
