bash

cat > /mnt/user-data/outputs/crypto_sniper_v4.py << 'PYEOF'
"""
🪙 AI Crypto Sniper Pro — v4.0 (Streamlit Cloud Edition)
=========================================================
สิ่งที่เปลี่ยนจาก v3:
  ✅ ตัด TradingView ออกทั้งหมด (ไม่มีประโยชน์บน Cloud + ซ้ำกับ Technical)
  ✅ เพิ่ม Fear & Greed Index (คนละมิติกับ Technical → แม่นจริง)
  ✅ เพิ่ม Bollinger Bands %B + ATR (volatility filter)
  ✅ เพิ่ม Volume Spike Detection (whale activity)
  ✅ สูตรใหม่ 4 แกน: Trend 35% | Momentum 30% | Volatility 15% | Sentiment 20%
  ✅ Yahoo Finance เป็น OHLCV หลัก + CoinGecko เป็น Fundamental
  ✅ ทุก API ทำงานได้บน Streamlit Cloud 100%
"""

import pandas as pd
import yfinance as yf
import time
import logging
import concurrent.futures
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, EMAIndicator
from ta.volatility import BollingerBands, AverageTrueRange
import streamlit as st
from streamlit_autorefresh import st_autorefresh
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =========================================================================
# 0. Logging
# =========================================================================
logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# =========================================================================
# 1. Page Config
# =========================================================================
st.set_page_config(
    page_title="AI Crypto Sniper Pro v4",
    page_icon="🪙",
    layout="wide",
)
st.title("🪙 AI Crypto Sniper Pro (v4.0 — Cloud Edition)")
st.caption(
    "4 แกนวิเคราะห์: Trend · Momentum · Volatility · Sentiment | "
    "ทำงานบน Streamlit Cloud 100% | ไม่พึ่ง TradingView"
)

# Session state
_DEFAULTS = {
    "auto_mode": False,
    "run_once":  False,
    "fg_cache":  None,       # Fear & Greed cache value
    "fg_cache_ts": 0.0,      # timestamp
}
for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# =========================================================================
# 2. HTTP Session (Retry + Browser Header)
# =========================================================================
@st.cache_resource
def get_http_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    })
    return s

# =========================================================================
# 3. Fear & Greed Index  (Alternative.me — ฟรี ไม่ต้อง key)
# =========================================================================
FG_CACHE_TTL = 300  # cache 5 นาที (ค่าอัปเดตทุกวัน ไม่ต้องดึงถี่)

def get_fear_greed() -> dict:
    """
    ดึง Crypto Fear & Greed Index จาก alternative.me
    คืน: {value, label, score}
    score = -20 ถึง +20 (ใช้ในสูตร)
    """
    now = time.time()
    # ใช้ cache ถ้ายังไม่หมดอายุ
    if (st.session_state.fg_cache is not None
            and now - st.session_state.fg_cache_ts < FG_CACHE_TTL):
        return st.session_state.fg_cache

    result = {"value": 50, "label": "Neutral 😐", "score": 0.0, "ok": False}
    try:
        http = get_http_session()
        resp = http.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=6,
        ).json()
        fg_val = int(resp["data"][0]["value"])
        fg_cls = resp["data"][0]["value_classification"]

        # แปลงเป็น score -20 ถึง +20
        # Logic: Extreme Fear = โอกาสซื้อ (contrarian) / Extreme Greed = ระวัง
        if fg_val >= 80:
            score = -20.0   # Extreme Greed → overbought sentiment → ระวัง
            label = f"Extreme Greed 🔥 ({fg_val})"
        elif fg_val >= 60:
            score = +10.0   # Greed → ตลาดบวก
            label = f"Greed 😀 ({fg_val})"
        elif fg_val >= 40:
            score = 0.0     # Neutral
            label = f"Neutral 😐 ({fg_val})"
        elif fg_val >= 20:
            score = -10.0   # Fear → ตลาดลบ
            label = f"Fear 😨 ({fg_val})"
        else:
            score = +20.0   # Extreme Fear → oversold sentiment → โอกาส bounce
            label = f"Extreme Fear 💀 ({fg_val})"

        result = {"value": fg_val, "label": label, "score": score, "ok": True}
        st.session_state.fg_cache    = result
        st.session_state.fg_cache_ts = now

    except Exception as e:
        logger.warning(f"Fear & Greed API failed: {e}")
        # fallback: ใช้ cache เก่าถ้ามี
        if st.session_state.fg_cache is not None:
            result = st.session_state.fg_cache

    return result

# =========================================================================
# 4. CoinGecko — Batch (Market Cap, Volume, Price, Rank)
# =========================================================================
COINGECKO_ID_MAP = {
    "BTC":    "bitcoin",              "ETH":    "ethereum",
    "SOL":    "solana",               "BNB":    "binancecoin",
    "XRP":    "ripple",               "ADA":    "cardano",
    "DOGE":   "dogecoin",             "AVAX":   "avalanche-2",
    "DOT":    "polkadot",             "MATIC":  "matic-network",
    "LINK":   "chainlink",            "UNI":    "uniswap",
    "LTC":    "litecoin",             "ATOM":   "cosmos",
    "XLM":    "stellar",              "PEPE":   "pepe",
    "SHIB":   "shiba-inu",            "TRX":    "tron",
    "OP":     "optimism",             "ARB":    "arbitrum",
    "SUI":    "sui",                  "APT":    "aptos",
    "INJ":    "injective-protocol",   "FIL":    "filecoin",
    "NEAR":   "near",                 "ICP":    "internet-computer",
    "FTM":    "fantom",               "SAND":   "the-sandbox",
    "MANA":   "decentraland",         "AXS":    "axie-infinity",
    "WLD":    "worldcoin-wld",        "TON":    "the-open-network",
    "JUP":    "jupiter-exchange-solana","SEI":  "sei-network",
    "TIA":    "celestia",             "RENDER": "render-token",
    "WIF":    "dogwifcoin",           "BONK":   "bonk",
    "FLOKI":  "floki",                "ORDI":   "ordinals",
}

_CG_EMPTY = {
    "market_cap": 0, "volume_24h": 0,
    "price_change_24h": 0.0, "rank": "-", "cg_price": 0.0,
}

@st.cache_data(ttl=60, show_spinner=False)
def get_coingecko_batch(coin_tickers: tuple) -> dict:
    """ดึง CoinGecko ทุกเหรียญใน 1 request"""
    http     = get_http_session()
    results  = {t: dict(_CG_EMPTY) for t in coin_tickers}
    id_map   = {}   # cg_id → ticker
    unknowns = []

    for ticker in coin_tickers:
        cg_id = COINGECKO_ID_MAP.get(ticker.upper())
        if cg_id:
            id_map[cg_id] = ticker
        else:
            unknowns.append(ticker)

    # Auto-search เหรียญที่ไม่อยู่ใน map
    for sym in unknowns:
        try:
            r = http.get(
                "https://api.coingecko.com/api/v3/search",
                params={"query": sym}, timeout=5,
            ).json()
            coins = r.get("coins", [])
            if coins:
                id_map[coins[0]["id"]] = sym
        except Exception as e:
            logger.warning(f"CoinGecko search [{sym}]: {e}")

    if not id_map:
        return results

    try:
        resp = http.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "ids":         ",".join(id_map.keys()),
                "per_page":    len(id_map),
                "page":        1,
            },
            timeout=10,
        ).json()
        for d in resp:
            ticker = id_map.get(d.get("id", ""))
            if ticker:
                results[ticker] = {
                    "market_cap":       d.get("market_cap")                    or 0,
                    "volume_24h":       d.get("total_volume")                  or 0,
                    "price_change_24h": d.get("price_change_percentage_24h")   or 0.0,
                    "rank":             d.get("market_cap_rank")               or "-",
                    "cg_price":         d.get("current_price")                 or 0.0,
                }
    except Exception as e:
        logger.warning(f"CoinGecko batch failed: {e}")

    return results

# =========================================================================
# 5. Yahoo Finance — Batch OHLCV + Cache
# =========================================================================
@st.cache_data(ttl=60, show_spinner=False)
def fetch_yf_batch(tickers_tuple: tuple, period: str, interval: str) -> pd.DataFrame:
    try:
        return yf.download(
            tickers=list(tickers_tuple),
            period=period,
            interval=interval,
            group_by="ticker",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
    except Exception as e:
        logger.error(f"yfinance batch failed: {e}")
        return pd.DataFrame()


def extract_ticker_df(all_data: pd.DataFrame, yf_sym: str) -> pd.DataFrame:
    """แยก DataFrame ของเหรียญเดียวออกจาก batch DataFrame"""
    if all_data.empty:
        return pd.DataFrame()
    try:
        if isinstance(all_data.columns, pd.MultiIndex):
            lvl0 = all_data.columns.get_level_values(0).unique()
            if yf_sym in lvl0:
                return all_data[yf_sym].copy().dropna(how="all")
            if "Close" in lvl0:
                return all_data.copy().dropna(how="all")
        else:
            if "Close" in all_data.columns:
                return all_data.copy().dropna(how="all")
    except Exception as e:
        logger.warning(f"extract_ticker_df({yf_sym}): {e}")
    return pd.DataFrame()

# =========================================================================
# 6. Signal Engine — 4 แกน
# =========================================================================
MIN_BARS = 52  # แท่งเทียนขั้นต่ำให้ indicator มีความหมาย

def _trend_score(last, prev) -> float:
    """
    แกน Trend (35%) — EMA20/50
    ตรวจทิศทางใหญ่ + partial score เมื่อ EMA ยังไม่ cross กัน
    """
    p  = float(last["Close"])
    e20 = float(last["EMA20"])
    e50 = float(last["EMA50"])

    if p > e20 and e20 > e50:   return  35.0   # แนวโน้มขึ้นเต็ม
    if p < e20 and e20 < e50:   return -35.0   # แนวโน้มลงเต็ม
    if p > e20:                 return  12.0   # ราคาเหนือ EMA20 แต่ EMA20 ยังต่ำ
    return                              -12.0  # ราคาต่ำกว่า EMA20


def _momentum_score(last, prev) -> tuple:
    """
    แกน Momentum (30%) — RSI + MACD crossover + Stoch crossover
    คืน (score, rsi, macd_diff, stoch_k)
    """
    score = 0.0

    rsi    = float(last["RSI"])       if not pd.isna(last["RSI"])       else 50.0
    macd   = float(last["MACD_diff"]) if not pd.isna(last["MACD_diff"]) else 0.0
    stoch_k = float(last["Stoch_K"])  if not pd.isna(last["Stoch_K"])   else 50.0
    stoch_d = float(last["Stoch_D"])  if not pd.isna(last["Stoch_D"])   else 50.0

    p_macd  = float(prev["MACD_diff"]) if not pd.isna(prev["MACD_diff"]) else 0.0
    p_stk   = float(prev["Stoch_K"])   if not pd.isna(prev["Stoch_K"])   else 50.0
    p_std   = float(prev["Stoch_D"])   if not pd.isna(prev["Stoch_D"])   else 50.0

    # RSI (max ±10 + bonus ±5)
    if rsi > 55:    score += 10.0
    elif rsi < 45:  score -= 10.0
    if rsi >= 70:   score -=  5.0   # overbought penalty
    elif rsi <= 30: score +=  5.0   # oversold bonus

    # MACD crossover (max ±10)
    if   macd > 0 and p_macd <= 0: score += 10.0   # fresh cross up
    elif macd > 0:                  score +=  7.0
    elif macd < 0 and p_macd >= 0: score -= 10.0   # fresh cross down
    else:                           score -=  7.0

    # Stoch %K/%D crossover (max ±10)
    if   stoch_k > stoch_d and p_stk <= p_std: score += 10.0
    elif stoch_k > stoch_d:                     score +=  7.0
    elif stoch_k < stoch_d and p_stk >= p_std: score -= 10.0
    else:                                       score -=  7.0

    return score, rsi, macd, stoch_k


def _volatility_score(last, close_series) -> tuple:
    """
    แกน Volatility (15%) — Bollinger Bands %B + Volume Spike
    คืน (score, bb_pct, vol_ratio, atr)
    """
    score    = 0.0
    bb_pct   = float(last["BB_pct"])   if not pd.isna(last["BB_pct"])   else 0.5
    atr_val  = float(last["ATR"])      if not pd.isna(last["ATR"])       else 0.0
    vol_ratio = float(last["VolRatio"]) if not pd.isna(last["VolRatio"]) else 1.0

    # Bollinger %B: 0=lower, 1=upper
    # ราคาใกล้ lower band (oversold) → +score / ใกล้ upper (overbought) → -score
    if bb_pct <= 0.20:   score += 10.0   # ใกล้ lower band มาก → โอกาส bounce
    elif bb_pct <= 0.35: score +=  5.0
    elif bb_pct >= 0.80: score -= 10.0   # ใกล้ upper band มาก → ระวัง
    elif bb_pct >= 0.65: score -=  5.0

    # Volume Spike: ปริมาณซื้อขายพุ่ง = มีนัยสำคัญ
    if vol_ratio >= 2.5:  score +=  5.0   # volume spike แรง
    elif vol_ratio >= 1.5: score += 2.5

    return score, bb_pct, vol_ratio, atr_val


def compute_signal(df_yf: pd.DataFrame, fg: dict) -> dict:
    """
    รวม 4 แกน:
      Trend      35% — EMA20/50
      Momentum   30% — RSI + MACD + Stoch
      Volatility 15% — BB%B + Volume Spike
      Sentiment  20% — Fear & Greed Index
    Buffer Zone ±18 ลด noise
    """
    trend_s = mom_s = vol_s = 0.0
    rsi_val = macd_val = stoch_k_val = 50.0
    bb_pct_val = vol_ratio_val = atr_val = 0.0
    latest_price = 0.0
    data_ok = False

    if not df_yf.empty and "Close" in df_yf.columns:
        df = df_yf.dropna(subset=["Close"]).copy()

        if len(df) >= MIN_BARS:
            data_ok = True
            close  = df["Close"].astype(float)
            high   = df["High"].astype(float)
            low    = df["Low"].astype(float)
            volume = df["Volume"].astype(float) if "Volume" in df.columns else pd.Series([1.0]*len(df))
            latest_price = float(close.iloc[-1])

            # ── Indicators ────────────────────────────────────────────────
            df["EMA20"]    = EMAIndicator(close=close, window=20).ema_indicator()
            df["EMA50"]    = EMAIndicator(close=close, window=50).ema_indicator()
            df["RSI"]      = RSIIndicator(close=close, window=14).rsi()

            stoch          = StochasticOscillator(high=high, low=low, close=close,
                                                  window=14, smooth_window=3)
            df["Stoch_K"]  = stoch.stoch()
            df["Stoch_D"]  = stoch.stoch_signal()

            macd_obj       = MACD(close=close)
            df["MACD_diff"]= macd_obj.macd_diff()

            bb             = BollingerBands(close=close, window=20, window_dev=2)
            df["BB_pct"]   = bb.bollinger_pband()   # 0=lower, 1=upper

            atr_obj        = AverageTrueRange(high=high, low=low, close=close, window=14)
            df["ATR"]      = atr_obj.average_true_range()

            # Volume ratio vs 20-bar MA
            vol_ma20       = volume.rolling(20).mean()
            df["VolRatio"] = volume / vol_ma20.replace(0, 1)

            df.ffill(inplace=True)
            last = df.iloc[-1]
            prev = df.iloc[-2]

            trend_s                           = _trend_score(last, prev)
            mom_s, rsi_val, macd_val, stoch_k_val = _momentum_score(last, prev)
            vol_s, bb_pct_val, vol_ratio_val, atr_val = _volatility_score(last, close)

    # ── Sentiment (20%) — Fear & Greed ───────────────────────────────────
    sent_s = fg.get("score", 0.0)   # -20 ถึง +20

    # ── รวม score (normalize ให้ max ~100) ───────────────────────────────
    # Trend max=35, Momentum max=30, Volatility max=15, Sentiment max=20 → รวม max=100
    total_raw = trend_s + mom_s + vol_s + sent_s

    # ── Verdict — Buffer Zone ±18 ────────────────────────────────────────
    STRONG = 65.0
    BUY_T  = 18.0
    SELL_T = -18.0

    if total_raw >= BUY_T:
        side   = "BUY"
        prob   = min(abs(total_raw), 100.0)
        signal = "🟢 STRONG BUY" if prob >= STRONG else "🟢 BUY"
    elif total_raw <= SELL_T:
        side   = "SELL"
        prob   = min(abs(total_raw), 100.0)
        signal = "🔴 STRONG SELL" if prob >= STRONG else "🔴 SELL"
    else:
        side   = "HOLD"
        prob   = min(abs(total_raw), 100.0)
        signal = "🟡 HOLD (ไซด์เวย์)"

    return {
        "side":        side,
        "signal":      signal,
        "prob":        prob,
        "total_raw":   total_raw,
        "trend_s":     trend_s,
        "mom_s":       mom_s,
        "vol_s":       vol_s,
        "sent_s":      sent_s,
        "rsi":         rsi_val,
        "macd":        macd_val,
        "stoch":       stoch_k_val,
        "bb_pct":      bb_pct_val,
        "vol_ratio":   vol_ratio_val,
        "atr":         atr_val,
        "latest_price":latest_price,
        "data_ok":     data_ok,
    }

# =========================================================================
# 7. Utility
# =========================================================================
def parse_meta(symbol_raw: str) -> dict:
    sym = (symbol_raw.strip().upper()
           .replace("-","").replace("/","")
           .replace("USDT","").replace("USD",""))
    return {"yf_sym": f"{sym}-USD", "clean_ticker": sym}


def fmt_price(price: float) -> str:
    if price <= 0:          return "N/A"
    if price < 0.0001:      return f"${price:.8f}"
    if price < 0.01:        return f"${price:.6f}"
    if price < 1:           return f"${price:.4f}"
    if price < 1000:        return f"${price:,.2f}"
    return                         f"${price:,.0f}"

# =========================================================================
# 8. Control Panel
# =========================================================================
st.subheader("⚙️ ตั้งค่าการสแกน")
col_s1, col_s2 = st.columns([2, 1])

with col_s1:
    user_input = st.text_input(
        "⌨️ ชื่อย่อเหรียญ (คั่นด้วย `,`):",
        value="BTC, ETH, SOL, DOGE, PEPE",
    )
    selected_assets = [x.strip().upper() for x in user_input.split(",") if x.strip()]

with col_s2:
    tf_choice = st.selectbox(
        "⏱️ Timeframe:",
        options=[
            "5 นาที (M5) - สคัลปิ้ง",
            "15 นาที (M15) - ระยะสั้น",
            "1 ชั่วโมง (H1) - เดย์เทรด",
            "1 วัน (1D) - สปอต/สวิง",
        ],
        index=2,
    )

TF_MAP = {
    "5 นาที":    ("5m",  "7d"),
    "15 นาที":   ("15m", "14d"),
    "1 ชั่วโมง": ("1h",  "60d"),
    "1 วัน":     ("1d",  "180d"),
}
yf_interval, yf_period = "1h", "60d"
for key, val in TF_MAP.items():
    if key in tf_choice:
        yf_interval, yf_period = val
        break

# =========================================================================
# 9. Buttons
# =========================================================================
cb1, cb2, cb3 = st.columns(3)
with cb1:
    if st.button("🔍 เริ่มสแกน", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once  = True
        st.rerun()
with cb2:
    if st.button("🔄 Auto Refresh 1 นาที", use_container_width=True):
        st.session_state.auto_mode = True
        st.session_state.run_once  = False
        st.toast("⚡ เริ่มสตรีมเรียลไทม์!")
        st.rerun()
with cb3:
    if st.button("🛑 หยุดสแกน", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once  = False
        st.toast("🛑 หยุดแล้ว")
        st.rerun()

if st.session_state.auto_mode:
    st_autorefresh(interval=60000, key="v4_refresh")
    st.info("🔄 [LIVE] อัปเดตทุก 60 วินาที...")

# =========================================================================
# 10. Main Engine
# =========================================================================
if (st.session_state.run_once or st.session_state.auto_mode) and selected_assets:

    assets_meta    = [parse_meta(a) for a in selected_assets]
    dashboard_rows = []
    detailed       = {}

    prog = st.progress(0, text="⏳ เริ่มต้น...")

    # ── Step 1: Fear & Greed (1 request สำหรับทุกเหรียญ) ─────────────────
    prog.progress(10, text="😱 [1/3] ดึง Fear & Greed Index...")
    fg = get_fear_greed()
    fg_status = f"{'✅' if fg['ok'] else '⚠️'} Sentiment: **{fg['label']}**"
    st.info(fg_status)

    # ── Step 2: Yahoo Finance OHLCV (batch) ───────────────────────────────
    prog.progress(30, text="📥 [2/3] ดึง OHLCV จาก Yahoo Finance...")
    yf_tickers  = tuple(m["yf_sym"] for m in assets_meta)
    all_yf_data = fetch_yf_batch(yf_tickers, yf_period, yf_interval)

    # ── Step 3: CoinGecko Fundamental (batch) ────────────────────────────
    prog.progress(60, text="🦎 [3/3] ดึง Fundamental จาก CoinGecko...")
    cg_tickers = tuple(m["clean_ticker"] for m in assets_meta)
    cg_all     = get_coingecko_batch(cg_tickers)

    # ── Step 4: คำนวณ Signal ─────────────────────────────────────────────
    prog.progress(80, text="🧮 คำนวณสัญญาณ AI 4 แกน...")

    for meta in assets_meta:
        sym = meta["clean_ticker"]
        try:
            df_yf = extract_ticker_df(all_yf_data, meta["yf_sym"])
            cg    = cg_all.get(sym, dict(_CG_EMPTY))
            sig   = compute_signal(df_yf, fg)

            # ราคา fallback: YF → CoinGecko
            price = sig["latest_price"]
            if price == 0.0:
                price = cg.get("cg_price", 0.0)

            chg     = cg.get("price_change_24h", 0.0)
            chg_str = f"{'▲' if chg >= 0 else '▼'} {abs(chg):.2f}%" if chg else "—"

            # แหล่งข้อมูล
            src = []
            if sig["data_ok"]:          src.append("YF✅")
            if cg.get("cg_price", 0):   src.append("CG✅")
            if fg["ok"]:                src.append("F&G✅")

            dashboard_rows.append({
                "เหรียญ":      sym,
                "ราคา":        fmt_price(price),
                "เปลี่ยน 24H": chg_str,
                "สัญญาณ AI":   sig["signal"],
                "Score":       f"{sig['total_raw']:+.1f}",
                "ความมั่นใจ":  f"{sig['prob']:.1f}%",
                "อันดับโลก":   f"#{cg['rank']}" if cg["rank"] != "-" else "N/A",
                "Vol 24H":     f"${cg['volume_24h']:,.0f}" if cg.get("volume_24h") else "N/A",
                "แหล่งข้อมูล": " ".join(src) if src else "⚠️ ไม่มีข้อมูล",
            })

            detailed[sym] = {
                **sig,
                "market_cap":       cg.get("market_cap", 0),
                "price_change_24h": chg,
                "fg_label":         fg["label"],
            }

        except Exception as e:
            logger.error(f"Signal error [{sym}]: {e}", exc_info=True)
            st.warning(f"⚠️ คำนวณสัญญาณ **{sym}** ไม่สำเร็จ: {e}")

    prog.progress(100, text="✅ สแกนเสร็จแล้ว!")
    time.sleep(0.3)
    prog.empty()

    # =========================================================================
    # 11. Dashboard
    # =========================================================================
    if dashboard_rows:
        st.write("---")
        st.subheader(f"📊 แดชบอร์ด ({tf_choice.split('-')[0].strip()})")
        st.dataframe(
            pd.DataFrame(dashboard_rows),
            use_container_width=True,
            hide_index=True,
        )

        st.write("---")
        st.subheader("🔍 เจาะลึกสัญญาณ 4 แกน")

        for sym, d in detailed.items():
            icon = "🟢" if d["side"] == "BUY" else ("🔴" if d["side"] == "SELL" else "🟡")
            with st.expander(
                f"{icon} {sym}  |  {d['signal']}  |  Score: {d['total_raw']:+.1f}"
            ):
                st.markdown(f"#### 🎯 ความมั่นใจ: **{d['prob']:.1f}%**")

                # Score breakdown
                st.dataframe(pd.DataFrame([
                    {"แกน": "1. Trend (EMA20/50)",           "น้ำหนัก": "35%", "คะแนน": f"{d['trend_s']:+.1f}"},
                    {"แกน": "2. Momentum (RSI/MACD/Stoch)",  "น้ำหนัก": "30%", "คะแนน": f"{d['mom_s']:+.1f}"},
                    {"แกน": "3. Volatility (BB%B + Volume)",  "น้ำหนัก": "15%", "คะแนน": f"{d['vol_s']:+.1f}"},
                    {"แกน": f"4. Sentiment (Fear & Greed)",  "น้ำหนัก": "20%", "คะแนน": f"{d['sent_s']:+.1f}"},
                    {"แกน": "✅ รวม Total Score",             "น้ำหนัก": "100%","คะแนน": f"{d['total_raw']:+.1f}"},
                ]), use_container_width=True, hide_index=True)

                # Indicator metrics
                c1, c2, c3, c4, c5 = st.columns(5)
                rsi_lbl = ("Overbought ⚠️" if d["rsi"] >= 70
                           else "Oversold 💡" if d["rsi"] <= 30
                           else "Normal ✓")
                bb_lbl  = ("Near Lower 💡" if d["bb_pct"] <= 0.2
                           else "Near Upper ⚠️" if d["bb_pct"] >= 0.8
                           else "Mid Zone")
                with c1: st.metric("RSI (14)",    f"{d['rsi']:.1f}",    delta=rsi_lbl)
                with c2: st.metric("Stoch %K",    f"{d['stoch']:.1f}")
                with c3: st.metric("MACD Diff",   f"{d['macd']:.5f}")
                with c4: st.metric("BB %B",       f"{d['bb_pct']:.2f}", delta=bb_lbl)
                with c5: st.metric("Vol Ratio",   f"{d['vol_ratio']:.1f}x")

                # Fear & Greed + ATR
                fa1, fa2 = st.columns(2)
                with fa1:
                    st.metric("😱 Fear & Greed", d["fg_label"],
                              delta=f"score {d['sent_s']:+.0f}")
                with fa2:
                    st.metric("📏 ATR (14)", f"{d['atr']:.2f}",
                              help="Average True Range — ยิ่งสูง ตลาดยิ่งผันผวน")

                # Score meter
                norm = (d["total_raw"] + 100) / 200
                st.progress(
                    max(0.0, min(1.0, norm)),
                    text=f"📊 Score Meter: {d['total_raw']:+.1f} / ±100",
                )

                if not d["data_ok"]:
                    st.warning(
                        "⚠️ OHLCV ไม่พอ (< 52 แท่ง) — "
                        "Trend/Momentum/Volatility ใช้ค่าเริ่มต้น"
                    )
                if d["market_cap"] > 0:
                    st.caption(f"Market Cap: ${d['market_cap']:,.0f} USD")

    st.session_state.run_once = False

elif not selected_assets:
    st.warning("ℹ️ กรุณาพิมพ์ชื่อเหรียญในช่องด้านบน")
PYEOF
echo "Lines: $(wc -l < /mnt/user-data/outputs/crypto_sniper_v4.py)"
Output

Lines: 678
