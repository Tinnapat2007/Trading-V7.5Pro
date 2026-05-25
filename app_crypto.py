"""
🪙 AI Crypto Sniper Pro — v4.1 (Mobile-Friendly Edition)
=========================================================
เพิ่มจาก v4.0:
  📱 CSS responsive สำหรับมือถือทั้งแนวตั้งและแนวนอน
  📱 ตาราง Dashboard ปรับขนาดอัตโนมัติ
  📱 Metric cards ไม่แตกบนจอเล็ก
  📱 ปุ่มใหญ่ขึ้น กดง่ายบน touchscreen
  📱 Font size ปรับตามขนาดจอ
"""

import pandas as pd
import yfinance as yf
import time
import logging
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
    page_title="AI Crypto Sniper Pro",
    page_icon="🪙",
    layout="wide",
    initial_sidebar_state="collapsed",   # ซ่อน sidebar บนมือถือ
)

# =========================================================================
# 2. Mobile CSS — รองรับแนวตั้ง + แนวนอน
# =========================================================================
st.markdown("""
<style>
/* ── Global font scale ───────────────────────────────────────────── */
html, body, [class*="css"] {
    font-size: 15px;
}

/* ── ปุ่มใหญ่ขึ้น กดง่ายบน touch ────────────────────────────────── */
.stButton > button {
    height: 3rem;
    font-size: 1rem;
    border-radius: 10px;
    font-weight: 600;
}

/* ── Metric cards: ป้องกันตัวเลขล้นออก ─────────────────────────── */
[data-testid="metric-container"] {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 10px;
    padding: 0.6rem 0.8rem;
    min-width: 0;
    overflow: hidden;
}
[data-testid="metric-container"] label {
    font-size: 0.75rem !important;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
[data-testid="metric-container"] [data-testid="stMetricValue"] {
    font-size: 1rem !important;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
[data-testid="metric-container"] [data-testid="stMetricDelta"] {
    font-size: 0.7rem !important;
}

/* ── DataFrame: scroll แนวนอนบนมือถือ ──────────────────────────── */
[data-testid="stDataFrame"] {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
}

/* ── แนวนอน (landscape) — ขยาย content area ────────────────────── */
@media (max-height: 500px) and (orientation: landscape) {
    .main .block-container {
        padding-top: 0.5rem;
        padding-bottom: 0.5rem;
        max-width: 100%;
    }
    h1 { font-size: 1.3rem !important; }
    h2 { font-size: 1.1rem !important; }
    h3 { font-size: 1rem !important; }
}

/* ── มือถือแนวตั้ง (portrait < 480px) ──────────────────────────── */
@media (max-width: 480px) {
    .main .block-container {
        padding-left: 0.5rem;
        padding-right: 0.5rem;
    }
    h1 { font-size: 1.2rem !important; }
    .stButton > button { font-size: 0.85rem; }
}

/* ── Progress bar text ───────────────────────────────────────────── */
.stProgress > div > div {
    border-radius: 10px;
}

/* ── Expander header ────────────────────────────────────────────── */
details summary {
    font-size: 0.95rem;
    font-weight: 600;
}
</style>
""", unsafe_allow_html=True)

# =========================================================================
# 3. Header
# =========================================================================
st.title("🪙 AI Crypto Sniper Pro")
st.caption("v4.1 · 4 แกนวิเคราะห์: Trend · Momentum · Volatility · Sentiment | Streamlit Cloud ✅ | Mobile ✅")

# Session state
for k, v in [("auto_mode", False), ("run_once", False),
              ("fg_cache", None), ("fg_cache_ts", 0.0)]:
    if k not in st.session_state:
        st.session_state[k] = v

# =========================================================================
# 4. HTTP Session
# =========================================================================
@st.cache_resource
def get_http_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.6,
                  status_forcelist=[429, 500, 502, 503, 504])
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
# 5. Fear & Greed Index
# =========================================================================
FG_TTL = 300  # 5 นาที

def get_fear_greed() -> dict:
    now = time.time()
    if (st.session_state.fg_cache is not None
            and now - st.session_state.fg_cache_ts < FG_TTL):
        return st.session_state.fg_cache

    result = {"value": 50, "label": "Neutral 😐", "score": 0.0, "ok": False}
    try:
        r = get_http_session().get(
            "https://api.alternative.me/fng/?limit=1", timeout=6
        ).json()
        val = int(r["data"][0]["value"])

        if val >= 80:   score, label =  -20.0, f"Extreme Greed 🔥 ({val})"
        elif val >= 60: score, label =  +10.0, f"Greed 😀 ({val})"
        elif val >= 40: score, label =    0.0, f"Neutral 😐 ({val})"
        elif val >= 20: score, label =  -10.0, f"Fear 😨 ({val})"
        else:           score, label =  +20.0, f"Extreme Fear 💀 ({val})"

        result = {"value": val, "label": label, "score": score, "ok": True}
        st.session_state.fg_cache    = result
        st.session_state.fg_cache_ts = now
    except Exception as e:
        logger.warning(f"Fear&Greed failed: {e}")
        if st.session_state.fg_cache:
            result = st.session_state.fg_cache
    return result

# =========================================================================
# 6. CoinGecko Batch
# =========================================================================
COINGECKO_ID_MAP = {
    "BTC":"bitcoin","ETH":"ethereum","SOL":"solana","BNB":"binancecoin",
    "XRP":"ripple","ADA":"cardano","DOGE":"dogecoin","AVAX":"avalanche-2",
    "DOT":"polkadot","MATIC":"matic-network","LINK":"chainlink","UNI":"uniswap",
    "LTC":"litecoin","ATOM":"cosmos","XLM":"stellar","PEPE":"pepe",
    "SHIB":"shiba-inu","TRX":"tron","OP":"optimism","ARB":"arbitrum",
    "SUI":"sui","APT":"aptos","INJ":"injective-protocol","FIL":"filecoin",
    "NEAR":"near","ICP":"internet-computer","FTM":"fantom","SAND":"the-sandbox",
    "MANA":"decentraland","AXS":"axie-infinity","WLD":"worldcoin-wld",
    "TON":"the-open-network","JUP":"jupiter-exchange-solana","SEI":"sei-network",
    "TIA":"celestia","RENDER":"render-token","WIF":"dogwifcoin",
    "BONK":"bonk","FLOKI":"floki","ORDI":"ordinals",
}
_CG_EMPTY = {"market_cap":0,"volume_24h":0,"price_change_24h":0.0,
             "rank":"-","cg_price":0.0}

@st.cache_data(ttl=60, show_spinner=False)
def get_coingecko_batch(coin_tickers: tuple) -> dict:
    http    = get_http_session()
    results = {t: dict(_CG_EMPTY) for t in coin_tickers}
    id_map  = {}
    unknown = []

    for t in coin_tickers:
        cg_id = COINGECKO_ID_MAP.get(t.upper())
        if cg_id: id_map[cg_id] = t
        else:     unknown.append(t)

    for sym in unknown:
        try:
            r = http.get("https://api.coingecko.com/api/v3/search",
                         params={"query": sym}, timeout=5).json()
            coins = r.get("coins", [])
            if coins: id_map[coins[0]["id"]] = sym
        except Exception as e:
            logger.warning(f"CoinGecko search [{sym}]: {e}")

    if not id_map:
        return results

    try:
        resp = http.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency":"usd","ids":",".join(id_map),
                    "per_page":len(id_map),"page":1},
            timeout=10,
        ).json()
        for d in resp:
            t = id_map.get(d.get("id",""))
            if t:
                results[t] = {
                    "market_cap":       d.get("market_cap")                  or 0,
                    "volume_24h":       d.get("total_volume")                or 0,
                    "price_change_24h": d.get("price_change_percentage_24h") or 0.0,
                    "rank":             d.get("market_cap_rank")             or "-",
                    "cg_price":         d.get("current_price")               or 0.0,
                }
    except Exception as e:
        logger.warning(f"CoinGecko batch failed: {e}")

    return results

# =========================================================================
# 7. Yahoo Finance Batch
# =========================================================================
@st.cache_data(ttl=60, show_spinner=False)
def fetch_yf_batch(tickers_tuple: tuple, period: str, interval: str) -> pd.DataFrame:
    try:
        return yf.download(
            tickers=list(tickers_tuple), period=period,
            interval=interval, group_by="ticker",
            progress=False, auto_adjust=True, threads=True,
        )
    except Exception as e:
        logger.error(f"yfinance failed: {e}")
        return pd.DataFrame()


def extract_ticker_df(all_data: pd.DataFrame, yf_sym: str) -> pd.DataFrame:
    if all_data.empty:
        return pd.DataFrame()
    try:
        if isinstance(all_data.columns, pd.MultiIndex):
            lvl0 = all_data.columns.get_level_values(0).unique()
            if yf_sym in lvl0:
                return all_data[yf_sym].copy().dropna(how="all")
            if "Close" in lvl0:
                return all_data.copy().dropna(how="all")
        elif "Close" in all_data.columns:
            return all_data.copy().dropna(how="all")
    except Exception as e:
        logger.warning(f"extract [{yf_sym}]: {e}")
    return pd.DataFrame()

# =========================================================================
# 8. Signal Engine — 4 แกน
# =========================================================================
MIN_BARS = 52

def compute_signal(df_yf: pd.DataFrame, fg: dict) -> dict:
    trend_s = mom_s = vol_s = 0.0
    rsi_v = macd_v = stoch_v = bb_v = vol_ratio_v = atr_v = 0.0
    latest_price = 0.0
    data_ok = False

    if not df_yf.empty and "Close" in df_yf.columns:
        df = df_yf.dropna(subset=["Close"]).copy()
        if len(df) >= MIN_BARS:
            data_ok = True
            close  = df["Close"].astype(float)
            high   = df["High"].astype(float)
            low    = df["Low"].astype(float)
            volume = (df["Volume"].astype(float)
                      if "Volume" in df.columns
                      else pd.Series([1.0]*len(df), index=df.index))
            latest_price = float(close.iloc[-1])

            # Indicators
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
            df["BB_pct"]   = bb.bollinger_pband()
            atr_obj        = AverageTrueRange(high=high, low=low, close=close, window=14)
            df["ATR"]      = atr_obj.average_true_range()
            vol_ma         = volume.rolling(20).mean()
            df["VolRatio"] = volume / vol_ma.replace(0, 1)
            df.ffill(inplace=True)

            last = df.iloc[-1]
            prev = df.iloc[-2]

            def safe(col, default=0.0):
                v = last.get(col, default)
                return default if pd.isna(v) else float(v)

            def safe_prev(col, default=0.0):
                v = prev.get(col, default)
                return default if pd.isna(v) else float(v)

            rsi_v    = safe("RSI", 50.0)
            macd_v   = safe("MACD_diff")
            stoch_v  = safe("Stoch_K", 50.0)
            stoch_d  = safe("Stoch_D", 50.0)
            bb_v     = safe("BB_pct", 0.5)
            atr_v    = safe("ATR")
            vol_ratio_v = safe("VolRatio", 1.0)
            p_macd   = safe_prev("MACD_diff")
            p_stk    = safe_prev("Stoch_K", 50.0)
            p_std    = safe_prev("Stoch_D", 50.0)

            # ── Trend 35% ─────────────────────────────────────────────
            p    = float(last["Close"])
            e20  = safe("EMA20")
            e50  = safe("EMA50")
            if   p > e20 and e20 > e50:  trend_s =  35.0
            elif p < e20 and e20 < e50:  trend_s = -35.0
            elif p > e20:                trend_s =  12.0
            else:                        trend_s = -12.0

            # ── Momentum 30% ──────────────────────────────────────────
            # RSI (max ±15)
            if   rsi_v > 55: mom_s += 10.0
            elif rsi_v < 45: mom_s -= 10.0
            if   rsi_v >= 70: mom_s -= 5.0   # overbought
            elif rsi_v <= 30: mom_s += 5.0   # oversold

            # MACD crossover (max ±10)
            if   macd_v > 0 and p_macd <= 0: mom_s += 10.0
            elif macd_v > 0:                  mom_s +=  7.0
            elif macd_v < 0 and p_macd >= 0: mom_s -= 10.0
            else:                             mom_s -=  7.0

            # Stoch crossover (max ±10)
            if   stoch_v > stoch_d and p_stk <= p_std: mom_s += 10.0
            elif stoch_v > stoch_d:                     mom_s +=  7.0
            elif stoch_v < stoch_d and p_stk >= p_std: mom_s -= 10.0
            else:                                       mom_s -=  7.0

            # ── Volatility 15% ────────────────────────────────────────
            # BB %B
            if   bb_v <= 0.20: vol_s += 10.0   # ใกล้ lower → โอกาส bounce
            elif bb_v <= 0.35: vol_s +=  5.0
            elif bb_v >= 0.80: vol_s -= 10.0   # ใกล้ upper → ระวัง
            elif bb_v >= 0.65: vol_s -=  5.0
            # Volume spike
            if   vol_ratio_v >= 2.5: vol_s += 5.0
            elif vol_ratio_v >= 1.5: vol_s += 2.5

    # ── Sentiment 20% ─────────────────────────────────────────────────────
    sent_s = fg.get("score", 0.0)

    total_raw = trend_s + mom_s + vol_s + sent_s

    # ── Verdict Buffer Zone ±18 ───────────────────────────────────────────
    if total_raw >= 18:
        side = "BUY"
        prob = min(abs(total_raw), 100.0)
        signal = "🟢 STRONG BUY" if prob >= 65 else "🟢 BUY"
    elif total_raw <= -18:
        side = "SELL"
        prob = min(abs(total_raw), 100.0)
        signal = "🔴 STRONG SELL" if prob >= 65 else "🔴 SELL"
    else:
        side = "HOLD"
        prob = min(abs(total_raw), 100.0)
        signal = "🟡 HOLD"

    return {
        "side":side,"signal":signal,"prob":prob,"total_raw":total_raw,
        "trend_s":trend_s,"mom_s":mom_s,"vol_s":vol_s,"sent_s":sent_s,
        "rsi":rsi_v,"macd":macd_v,"stoch":stoch_v,
        "bb_pct":bb_v,"vol_ratio":vol_ratio_v,"atr":atr_v,
        "latest_price":latest_price,"data_ok":data_ok,
    }

# =========================================================================
# 9. Utility
# =========================================================================
def parse_meta(raw: str) -> dict:
    sym = (raw.strip().upper()
           .replace("-","").replace("/","")
           .replace("USDT","").replace("USD",""))
    return {"yf_sym": f"{sym}-USD", "clean_ticker": sym}

def fmt_price(p: float) -> str:
    if p <= 0:       return "N/A"
    if p < 0.0001:   return f"${p:.8f}"
    if p < 0.01:     return f"${p:.6f}"
    if p < 1:        return f"${p:.4f}"
    if p < 1000:     return f"${p:,.2f}"
    return                  f"${p:,.0f}"

# =========================================================================
# 10. Control Panel — Mobile Friendly Layout
# =========================================================================
st.subheader("⚙️ ตั้งค่า")

# Input เต็มความกว้าง (ดีบนมือถือ)
user_input = st.text_input(
    "⌨️ ชื่อย่อเหรียญ (คั่นด้วย `,`):",
    value="BTC, ETH, SOL, DOGE, PEPE",
)
selected_assets = [x.strip().upper() for x in user_input.split(",") if x.strip()]

# Timeframe selector
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
# 11. Buttons — 3 คอลัมน์ (ปุ่มใหญ่ กดง่ายบน touch)
# =========================================================================
cb1, cb2, cb3 = st.columns(3)
with cb1:
    if st.button("🔍 สแกน", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once  = True
        st.rerun()
with cb2:
    if st.button("🔄 Auto 1 นาที", use_container_width=True):
        st.session_state.auto_mode = True
        st.session_state.run_once  = False
        st.toast("⚡ เริ่ม Live!")
        st.rerun()
with cb3:
    if st.button("🛑 หยุด", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once  = False
        st.toast("🛑 หยุดแล้ว")
        st.rerun()

if st.session_state.auto_mode:
    st_autorefresh(interval=60000, key="v41_refresh")
    st.info("🔄 Live — อัปเดตทุก 60 วินาที")

# =========================================================================
# 12. Main Engine
# =========================================================================
if (st.session_state.run_once or st.session_state.auto_mode) and selected_assets:
    assets_meta    = [parse_meta(a) for a in selected_assets]
    dashboard_rows = []
    detailed       = {}

    prog = st.progress(0, text="⏳ เริ่มต้น...")

    # Step 1: Fear & Greed
    prog.progress(10, text="😱 [1/3] Fear & Greed Index...")
    fg = get_fear_greed()
    fg_color = ("🔴" if fg["value"] >= 75 else
                "🟢" if fg["value"] <= 25 else "🟡")
    st.info(f"{fg_color} Sentiment ตลาดรวม: **{fg['label']}**"
            + (" (cache)" if not fg["ok"] else ""))

    # Step 2: Yahoo Finance
    prog.progress(30, text="📥 [2/3] OHLCV จาก Yahoo Finance...")
    yf_tickers  = tuple(m["yf_sym"] for m in assets_meta)
    all_yf_data = fetch_yf_batch(yf_tickers, yf_period, yf_interval)

    # Step 3: CoinGecko
    prog.progress(60, text="🦎 [3/3] Fundamental จาก CoinGecko...")
    cg_tickers = tuple(m["clean_ticker"] for m in assets_meta)
    cg_all     = get_coingecko_batch(cg_tickers)

    # Step 4: Signal
    prog.progress(82, text="🧮 คำนวณสัญญาณ 4 แกน...")

    for meta in assets_meta:
        sym = meta["clean_ticker"]
        try:
            df_yf = extract_ticker_df(all_yf_data, meta["yf_sym"])
            cg    = cg_all.get(sym, dict(_CG_EMPTY))
            sig   = compute_signal(df_yf, fg)

            price = sig["latest_price"] or cg.get("cg_price", 0.0)
            chg   = cg.get("price_change_24h", 0.0)

            src = (["YF✅"] if sig["data_ok"] else []) + \
                  (["CG✅"] if cg.get("cg_price") else []) + \
                  (["F&G✅"] if fg["ok"] else [])

            dashboard_rows.append({
                "เหรียญ":      sym,
                "ราคา":        fmt_price(price),
                "24H %":       f"{'▲' if chg>=0 else '▼'}{abs(chg):.2f}%" if chg else "—",
                "สัญญาณ":      sig["signal"],
                "Score":       f"{sig['total_raw']:+.1f}",
                "มั่นใจ":      f"{sig['prob']:.0f}%",
                "Rank":        f"#{cg['rank']}" if cg["rank"] != "-" else "N/A",
                "Vol 24H":     f"${cg['volume_24h']/1e6:.1f}M" if cg.get("volume_24h",0)>1e6 else "N/A",
                "แหล่ง":       " ".join(src) if src else "⚠️",
            })

            detailed[sym] = {
                **sig,
                "market_cap":       cg.get("market_cap", 0),
                "price_change_24h": chg,
                "fg_label":         fg["label"],
            }
        except Exception as e:
            logger.error(f"[{sym}]: {e}", exc_info=True)
            st.warning(f"⚠️ **{sym}**: {e}")

    prog.progress(100, text="✅ เสร็จแล้ว!")
    time.sleep(0.3)
    prog.empty()

    # =========================================================================
    # 13. Dashboard — Mobile Optimized
    # =========================================================================
    if dashboard_rows:
        st.write("---")
        tf_label = tf_choice.split("-")[0].strip()
        st.subheader(f"📊 แดชบอร์ด · {tf_label}")

        # ตาราง scroll ได้บนมือถือ
        st.dataframe(
            pd.DataFrame(dashboard_rows),
            use_container_width=True,
            hide_index=True,
            height=min(200 + len(dashboard_rows) * 40, 500),
        )

        st.write("---")
        st.subheader("🔍 เจาะลึกสัญญาณ")

        for sym, d in detailed.items():
            icon = "🟢" if d["side"]=="BUY" else ("🔴" if d["side"]=="SELL" else "🟡")
            with st.expander(f"{icon} {sym} · {d['signal']} · {d['total_raw']:+.1f}"):

                st.markdown(f"#### 🎯 ความมั่นใจ: **{d['prob']:.0f}%**")

                # Score breakdown
                st.dataframe(pd.DataFrame([
                    {"แกน": "1. Trend EMA20/50",        "น้ำหนัก":"35%","คะแนน":f"{d['trend_s']:+.1f}"},
                    {"แกน": "2. Momentum RSI/MACD/Stoch","น้ำหนัก":"30%","คะแนน":f"{d['mom_s']:+.1f}"},
                    {"แกน": "3. Volatility BB+Volume",   "น้ำหนัก":"15%","คะแนน":f"{d['vol_s']:+.1f}"},
                    {"แกน": "4. Sentiment Fear&Greed",   "น้ำหนัก":"20%","คะแนน":f"{d['sent_s']:+.1f}"},
                    {"แกน": "✅ รวม",                    "น้ำหนัก":"100%","คะแนน":f"{d['total_raw']:+.1f}"},
                ]), use_container_width=True, hide_index=True)

                # Metrics — 2 แถว ป้องกันล้นบนมือถือแนวตั้ง
                r1c1, r1c2, r1c3 = st.columns(3)
                with r1c1:
                    rsi_lbl = ("Overbought ⚠️" if d["rsi"]>=70
                               else "Oversold 💡" if d["rsi"]<=30 else "Normal ✓")
                    st.metric("RSI", f"{d['rsi']:.1f}", delta=rsi_lbl)
                with r1c2:
                    st.metric("Stoch %K", f"{d['stoch']:.1f}")
                with r1c3:
                    st.metric("MACD", f"{d['macd']:.4f}")

                r2c1, r2c2, r2c3 = st.columns(3)
                with r2c1:
                    bb_lbl = ("Lower 💡" if d["bb_pct"]<=0.2
                              else "Upper ⚠️" if d["bb_pct"]>=0.8 else "Mid ✓")
                    st.metric("BB %B", f"{d['bb_pct']:.2f}", delta=bb_lbl)
                with r2c2:
                    st.metric("Vol Ratio", f"{d['vol_ratio']:.1f}×",
                              delta="Spike! 🐳" if d["vol_ratio"]>=2 else None)
                with r2c3:
                    st.metric("ATR", f"{d['atr']:.2f}")

                # Fear & Greed highlight
                st.metric("😱 Fear & Greed",
                          d["fg_label"],
                          delta=f"Sentiment score {d['sent_s']:+.0f}")

                # Score meter
                norm = (d["total_raw"] + 100) / 200
                st.progress(max(0.0, min(1.0, norm)),
                            text=f"Score: {d['total_raw']:+.1f} / ±100")

                if not d["data_ok"]:
                    st.warning("⚠️ OHLCV < 52 แท่ง — Trend/Momentum ใช้ค่าเริ่มต้น")
                if d["market_cap"] > 0:
                    mcap = d["market_cap"]
                    mcap_str = (f"${mcap/1e9:.2f}B" if mcap>=1e9
                                else f"${mcap/1e6:.1f}M")
                    st.caption(f"Market Cap: {mcap_str}")

    st.session_state.run_once = False

elif not selected_assets:
    st.warning("ℹ️ กรุณาพิมพ์ชื่อเหรียญด้านบน")
