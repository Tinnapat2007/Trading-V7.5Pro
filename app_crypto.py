"""
🪙 AI Crypto Sniper Pro — v4.2 (Discord Alert Edition)
========================================================
เพิ่มจาก v4.1:
  🔔 Discord Webhook Alert 5 ช่อง แยกตาม signal type
  ⏰ Cooldown 30 นาทีต่อเหรียญ — ป้องกัน spam
  🕐 Time Window Filter — เลือกช่วงเวลาที่อยากรับแจ้งเตือน
  📊 Discord Embed สวยงาม พร้อมสี + ข้อมูลครบ
  🛡️ URL อ่านจาก st.secrets เท่านั้น — ปลอดภัย
  ✅ Logic หลักไม่เปลี่ยนแปลง — เสถียรเหมือนเดิม
"""

import pandas as pd
import yfinance as yf
import time
import logging
import datetime
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
    initial_sidebar_state="collapsed",
)

# =========================================================================
# 2. Mobile CSS
# =========================================================================
st.markdown("""
<style>
html, body, [class*="css"] { font-size: 15px; }
.stButton > button {
    height: 3rem; font-size: 1rem;
    border-radius: 10px; font-weight: 600;
}
[data-testid="metric-container"] {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 10px; padding: 0.6rem 0.8rem;
    min-width: 0; overflow: hidden;
}
[data-testid="metric-container"] label       { font-size: 0.75rem !important; }
[data-testid="stMetricValue"]                { font-size: 1rem !important; }
[data-testid="stMetricDelta"]                { font-size: 0.7rem !important; }
[data-testid="stDataFrame"]                  { overflow-x: auto; -webkit-overflow-scrolling: touch; }
@media (max-height: 500px) and (orientation: landscape) {
    .main .block-container { padding-top: 0.5rem; padding-bottom: 0.5rem; max-width: 100%; }
    h1 { font-size: 1.3rem !important; }
}
@media (max-width: 480px) {
    .main .block-container { padding-left: 0.5rem; padding-right: 0.5rem; }
    h1 { font-size: 1.2rem !important; }
}
</style>
""", unsafe_allow_html=True)

# =========================================================================
# 3. Header
# =========================================================================
st.title("🪙 AI Crypto Sniper Pro")
st.caption("v4.2 · Trend · Momentum · Volatility · Sentiment · Discord Alert 🔔")

# Session state
_DEFAULTS = {
    "auto_mode":    False,
    "run_once":     False,
    "fg_cache":     None,
    "fg_cache_ts":  0.0,
    # Discord cooldown: {"{SYM}_{SIDE}": timestamp}
    "alert_cd":     {},
    # สัญญาณล่าสุดของแต่ละเหรียญ เพื่อเช็คว่าเปลี่ยนไหม
    "last_signal":  {},
}
for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# =========================================================================
# 4. Discord Webhooks — อ่านจาก st.secrets เท่านั้น
# =========================================================================
def _get_webhook(key: str) -> str:
    """ดึง Webhook URL จาก Streamlit Secrets อย่างปลอดภัย"""
    try:
        return st.secrets[key]
    except Exception:
        return ""

WEBHOOK_MAP = {
    "STRONG BUY":  "_get_webhook('DISCORD_BUY_STRONG')",
    "BUY":         "_get_webhook('DISCORD_BUY_NORMAL')",
    "STRONG SELL": "_get_webhook('DISCORD_SELL_STRONG')",
    "SELL":        "_get_webhook('DISCORD_SELL_NORMAL')",
    "HOLD":        "_get_webhook('DISCORD_HOLD')",
}

# สี Embed ตาม signal
COLOR_MAP = {
    "STRONG BUY":  0x00FF88,
    "BUY":         0x00CC66,
    "STRONG SELL": 0xFF2244,
    "SELL":        0xFF6644,
    "HOLD":        0xFFCC00,
}

ALERT_COOLDOWN_SEC = 1800   # 30 นาที ต่อเหรียญต่อ signal type

# =========================================================================
# 5. Discord Alert Functions
# =========================================================================
def _signal_key(signal_str: str) -> str:
    """แปลง signal emoji → key สำหรับ WEBHOOK_MAP"""
    s = (signal_str
         .replace("🟢", "").replace("🔴", "").replace("🟡", "")
         .replace("(ไซด์เวย์)", "").strip())
    return s  # เช่น "STRONG BUY", "BUY", "HOLD"


def _bkk_time() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")


def build_embed(sym: str, sig: dict, price_str: str,
                chg_str: str, fg_label: str, timeframe: str) -> dict:
    """สร้าง Discord Embed payload"""
    sig_key = _signal_key(sig["signal"])
    color   = COLOR_MAP.get(sig_key, 0x888888)

    # icon ตาม side
    side_icon = {"BUY": "📈", "SELL": "📉", "HOLD": "➡️"}.get(sig["side"], "❓")

    embed = {
        "title":       f"{sig['signal']} — {sym}",
        "description": f"{side_icon} **{sym}** | Timeframe: `{timeframe}`",
        "color":       color,
        "fields": [
            {"name": "💰 ราคา",         "value": f"`{price_str}`",              "inline": True},
            {"name": "📊 Score",         "value": f"`{sig['total_raw']:+.1f}`",  "inline": True},
            {"name": "🎯 ความมั่นใจ",   "value": f"`{sig['prob']:.0f}%`",       "inline": True},
            {"name": "📈 เปลี่ยน 24H",  "value": f"`{chg_str}`",               "inline": True},
            {"name": "😱 Sentiment",     "value": fg_label,                      "inline": True},
            {"name": "🧮 Trend/Mom/Vol", "value": (
                f"`{sig['trend_s']:+.0f}` / "
                f"`{sig['mom_s']:+.0f}` / "
                f"`{sig['vol_s']:+.0f}`"
            ), "inline": True},
            {"name": "📉 RSI",           "value": f"`{sig['rsi']:.1f}`",         "inline": True},
            {"name": "📊 BB %B",         "value": f"`{sig['bb_pct']:.2f}`",      "inline": True},
            {"name": "🐳 Vol Ratio",     "value": f"`{sig['vol_ratio']:.1f}×`",  "inline": True},
        ],
        "footer": {
            "text": f"AI Crypto Sniper v4.2 · Bangkok {_bkk_time()}"
        },
    }
    return {"embeds": [embed]}


def send_discord_alert(sym: str, sig: dict, price_str: str,
                       chg_str: str, fg_label: str,
                       timeframe: str, allowed_hours: list) -> bool:
    """
    ส่ง Discord alert พร้อม:
    1. เช็ค Time Window (ถ้ากำหนดไว้)
    2. เช็ค Cooldown 30 นาที
    3. เช็คว่าสัญญาณเปลี่ยนจากรอบก่อนไหม
    คืน True ถ้าส่งสำเร็จ
    """
    sig_key = _signal_key(sig["signal"])
    now     = time.time()

    # ── 1. Time Window Filter ─────────────────────────────────────────────
    if allowed_hours:
        bkk_hour = (datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(hours=7)).hour
        if bkk_hour not in allowed_hours:
            return False

    # ── 2. เช็คว่าสัญญาณเปลี่ยนไหม ──────────────────────────────────────
    last_sig = st.session_state.last_signal.get(sym, "")
    if last_sig == sig["signal"]:
        return False   # สัญญาณเดิม ไม่ส่งซ้ำ

    # ── 3. Cooldown ──────────────────────────────────────────────────────
    cd_key  = f"{sym}_{sig_key}"
    last_ts = st.session_state.alert_cd.get(cd_key, 0)
    if now - last_ts < ALERT_COOLDOWN_SEC:
        return False

    # ── 4. หา Webhook URL ────────────────────────────────────────────────
    url = _get_webhook({
        "STRONG BUY":  "DISCORD_BUY_STRONG",
        "BUY":         "DISCORD_BUY_NORMAL",
        "STRONG SELL": "DISCORD_SELL_STRONG",
        "SELL":        "DISCORD_SELL_NORMAL",
        "HOLD":        "DISCORD_HOLD",
    }.get(sig_key, ""))

    if not url:
        logger.warning(f"Discord webhook not configured for: {sig_key}")
        return False

    # ── 5. ส่ง ────────────────────────────────────────────────────────────
    try:
        payload = build_embed(sym, sig, price_str, chg_str, fg_label, timeframe)
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code in (200, 204):
            # อัปเดต state
            st.session_state.alert_cd[cd_key]    = now
            st.session_state.last_signal[sym]    = sig["signal"]
            logger.info(f"Discord alert sent: {sym} {sig_key}")
            return True
        else:
            logger.warning(f"Discord {sym}: HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Discord send failed [{sym}]: {e}")
    return False

# =========================================================================
# 6. HTTP Session
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
# 7. Fear & Greed Index
# =========================================================================
FG_TTL = 300

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
        if   val >= 80: score, label = -20.0, f"Extreme Greed 🔥 ({val})"
        elif val >= 60: score, label = +10.0, f"Greed 😀 ({val})"
        elif val >= 40: score, label =   0.0, f"Neutral 😐 ({val})"
        elif val >= 20: score, label = -10.0, f"Fear 😨 ({val})"
        else:           score, label = +20.0, f"Extreme Fear 💀 ({val})"
        result = {"value": val, "label": label, "score": score, "ok": True}
        st.session_state.fg_cache    = result
        st.session_state.fg_cache_ts = now
    except Exception as e:
        logger.warning(f"Fear&Greed: {e}")
        if st.session_state.fg_cache:
            result = st.session_state.fg_cache
    return result

# =========================================================================
# 8. CoinGecko Batch
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
        logger.warning(f"CoinGecko batch: {e}")
    return results

# =========================================================================
# 9. Yahoo Finance Batch
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
        logger.error(f"yfinance: {e}")
        return pd.DataFrame()


def extract_ticker_df(all_data: pd.DataFrame, yf_sym: str) -> pd.DataFrame:
    if all_data.empty:
        return pd.DataFrame()
    try:
        if isinstance(all_data.columns, pd.MultiIndex):
            lvl0 = all_data.columns.get_level_values(0).unique()
            if yf_sym in lvl0:   return all_data[yf_sym].copy().dropna(how="all")
            if "Close" in lvl0:  return all_data.copy().dropna(how="all")
        elif "Close" in all_data.columns:
            return all_data.copy().dropna(how="all")
    except Exception as e:
        logger.warning(f"extract [{yf_sym}]: {e}")
    return pd.DataFrame()

# =========================================================================
# 10. Signal Engine — 4 แกน (ไม่เปลี่ยนจาก v4.1)
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
            volume = (df["Volume"].astype(float) if "Volume" in df.columns
                      else pd.Series([1.0]*len(df), index=df.index))
            latest_price = float(close.iloc[-1])

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

            def s(col, d=0.0):
                v = last.get(col, d)
                return d if pd.isna(v) else float(v)
            def sp(col, d=0.0):
                v = prev.get(col, d)
                return d if pd.isna(v) else float(v)

            rsi_v      = s("RSI", 50.0)
            macd_v     = s("MACD_diff")
            stoch_v    = s("Stoch_K", 50.0)
            stoch_d    = s("Stoch_D", 50.0)
            bb_v       = s("BB_pct", 0.5)
            atr_v      = s("ATR")
            vol_ratio_v= s("VolRatio", 1.0)
            p_macd     = sp("MACD_diff")
            p_stk      = sp("Stoch_K", 50.0)
            p_std      = sp("Stoch_D", 50.0)

            # Trend 35%
            p, e20, e50 = float(last["Close"]), s("EMA20"), s("EMA50")
            if   p > e20 and e20 > e50: trend_s =  35.0
            elif p < e20 and e20 < e50: trend_s = -35.0
            elif p > e20:               trend_s =  12.0
            else:                       trend_s = -12.0

            # Momentum 30%
            if   rsi_v > 55: mom_s += 10.0
            elif rsi_v < 45: mom_s -= 10.0
            if   rsi_v >= 70: mom_s -= 5.0
            elif rsi_v <= 30: mom_s += 5.0
            if   macd_v > 0 and p_macd <= 0: mom_s += 10.0
            elif macd_v > 0:                  mom_s +=  7.0
            elif macd_v < 0 and p_macd >= 0: mom_s -= 10.0
            else:                             mom_s -=  7.0
            if   stoch_v > stoch_d and p_stk <= p_std: mom_s += 10.0
            elif stoch_v > stoch_d:                     mom_s +=  7.0
            elif stoch_v < stoch_d and p_stk >= p_std: mom_s -= 10.0
            else:                                       mom_s -=  7.0

            # Volatility 15%
            if   bb_v <= 0.20: vol_s += 10.0
            elif bb_v <= 0.35: vol_s +=  5.0
            elif bb_v >= 0.80: vol_s -= 10.0
            elif bb_v >= 0.65: vol_s -=  5.0
            if   vol_ratio_v >= 2.5: vol_s += 5.0
            elif vol_ratio_v >= 1.5: vol_s += 2.5

    # Sentiment 20%
    sent_s    = fg.get("score", 0.0)
    total_raw = trend_s + mom_s + vol_s + sent_s

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
# 11. Utility
# =========================================================================
def parse_meta(raw: str) -> dict:
    sym = (raw.strip().upper()
           .replace("-","").replace("/","")
           .replace("USDT","").replace("USD",""))
    return {"yf_sym": f"{sym}-USD", "clean_ticker": sym}

def fmt_price(p: float) -> str:
    if p <= 0:      return "N/A"
    if p < 0.0001:  return f"${p:.8f}"
    if p < 0.01:    return f"${p:.6f}"
    if p < 1:       return f"${p:.4f}"
    if p < 1000:    return f"${p:,.2f}"
    return                 f"${p:,.0f}"

# =========================================================================
# 12. Control Panel
# =========================================================================
st.subheader("⚙️ ตั้งค่า")

user_input = st.text_input(
    "⌨️ ชื่อย่อเหรียญ (คั่นด้วย `,`):",
    value="BTC, ETH, SOL, DOGE, PEPE",
)
selected_assets = [x.strip().upper() for x in user_input.split(",") if x.strip()]

col_tf, col_alert = st.columns([1, 1])

with col_tf:
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

with col_alert:
    # ── Time Window Selector ──────────────────────────────────────────────
    st.markdown("**🕐 ช่วงเวลาส่ง Discord (เวลาไทย)**")
    time_window = st.multiselect(
        "เลือกช่วงที่อยากรับแจ้งเตือน (ว่างไว้ = ส่งตลอด 24 ชั่วโมง):",
        options=[f"{h:02d}:00–{h+1:02d}:00" for h in range(24)],
        default=[],
        placeholder="ไม่เลือก = แจ้งเตือนตลอด 24 ชม.",
    )
    # แปลง "09:00–10:00" → [9]
    allowed_hours = []
    for w in time_window:
        try:
            allowed_hours.append(int(w.split(":")[0]))
        except Exception:
            pass

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

tf_label = tf_choice.split("-")[0].strip()

# =========================================================================
# 13. Buttons
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
        st.toast("⚡ เริ่ม Live + Discord Alert!")
        st.rerun()
with cb3:
    if st.button("🛑 หยุด", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once  = False
        st.toast("🛑 หยุดแล้ว")
        st.rerun()

# แสดงสถานะ Discord
discord_ok = bool(_get_webhook("DISCORD_BUY_STRONG"))
if discord_ok:
    window_str = (", ".join(time_window) if time_window
                  else "ตลอด 24 ชั่วโมง")
    st.success(f"🔔 Discord Alert พร้อมส่ง | ช่วงเวลา: {window_str}")
else:
    st.warning("⚠️ ไม่พบ Discord Webhook ใน Secrets — แจ้งเตือนจะไม่ทำงาน")

if st.session_state.auto_mode:
    st_autorefresh(interval=60000, key="v42_refresh")
    st.info("🔄 Live — อัปเดตทุก 60 วินาที")

# =========================================================================
# 14. Main Engine
# =========================================================================
if (st.session_state.run_once or st.session_state.auto_mode) and selected_assets:

    assets_meta    = [parse_meta(a) for a in selected_assets]
    dashboard_rows = []
    detailed       = {}
    alert_log      = []   # เก็บ log การส่ง Discord รอบนี้

    prog = st.progress(0, text="⏳ เริ่มต้น...")

    # Step 1: Fear & Greed
    prog.progress(10, text="😱 [1/3] Fear & Greed Index...")
    fg = get_fear_greed()
    fg_color = ("🔴" if fg["value"] >= 75 else
                "🟢" if fg["value"] <= 25 else "🟡")
    st.info(f"{fg_color} Sentiment: **{fg['label']}**"
            + ("" if fg["ok"] else " (cache)"))

    # Step 2: Yahoo Finance
    prog.progress(30, text="📥 [2/3] OHLCV จาก Yahoo Finance...")
    yf_tickers  = tuple(m["yf_sym"] for m in assets_meta)
    all_yf_data = fetch_yf_batch(yf_tickers, yf_period, yf_interval)

    # Step 3: CoinGecko
    prog.progress(60, text="🦎 [3/3] Fundamental จาก CoinGecko...")
    cg_tickers = tuple(m["clean_ticker"] for m in assets_meta)
    cg_all     = get_coingecko_batch(cg_tickers)

    # Step 4: Signal + Discord Alert
    prog.progress(82, text="🧮 คำนวณสัญญาณ + ส่ง Discord...")

    for meta in assets_meta:
        sym = meta["clean_ticker"]
        try:
            df_yf = extract_ticker_df(all_yf_data, meta["yf_sym"])
            cg    = cg_all.get(sym, dict(_CG_EMPTY))
            sig   = compute_signal(df_yf, fg)

            price   = sig["latest_price"] or cg.get("cg_price", 0.0)
            price_s = fmt_price(price)
            chg     = cg.get("price_change_24h", 0.0)
            chg_s   = f"{'▲' if chg>=0 else '▼'}{abs(chg):.2f}%" if chg else "—"

            # ── Discord Alert ─────────────────────────────────────────────
            if discord_ok:
                sent = send_discord_alert(
                    sym=sym, sig=sig,
                    price_str=price_s, chg_str=chg_s,
                    fg_label=fg["label"],
                    timeframe=tf_label,
                    allowed_hours=allowed_hours,
                )
                if sent:
                    alert_log.append(f"🔔 ส่ง Discord: **{sym}** {sig['signal']}")

            # ── Dashboard row ─────────────────────────────────────────────
            src = (["YF✅"] if sig["data_ok"] else []) + \
                  (["CG✅"] if cg.get("cg_price") else []) + \
                  (["F&G✅"] if fg["ok"] else [])

            mcap = cg.get("market_cap", 0)
            mcap_s = (f"${mcap/1e9:.1f}B" if mcap >= 1e9
                      else f"${mcap/1e6:.0f}M" if mcap >= 1e6
                      else "N/A")

            dashboard_rows.append({
                "เหรียญ":  sym,
                "ราคา":    price_s,
                "24H %":   chg_s,
                "สัญญาณ":  sig["signal"],
                "Score":   f"{sig['total_raw']:+.1f}",
                "มั่นใจ":  f"{sig['prob']:.0f}%",
                "Rank":    f"#{cg['rank']}" if cg["rank"] != "-" else "N/A",
                "MCap":    mcap_s,
                "แหล่ง":   " ".join(src) if src else "⚠️",
            })

            detailed[sym] = {
                **sig,
                "market_cap":       mcap,
                "price_change_24h": chg,
                "fg_label":         fg["label"],
            }
        except Exception as e:
            logger.error(f"[{sym}]: {e}", exc_info=True)
            st.warning(f"⚠️ **{sym}**: {e}")

    prog.progress(100, text="✅ เสร็จแล้ว!")
    time.sleep(0.3)
    prog.empty()

    # แสดง log Discord ที่ส่งรอบนี้
    if alert_log:
        for log in alert_log:
            st.success(log)

    # =========================================================================
    # 15. Dashboard
    # =========================================================================
    if dashboard_rows:
        st.write("---")
        st.subheader(f"📊 แดชบอร์ด · {tf_label}")
        st.dataframe(
            pd.DataFrame(dashboard_rows),
            use_container_width=True,
            hide_index=True,
            height=min(200 + len(dashboard_rows) * 40, 500),
        )

        st.write("---")
        st.subheader("🔍 เจาะลึกสัญญาณ 4 แกน")

        for sym, d in detailed.items():
            icon = ("🟢" if d["side"]=="BUY" else
                    "🔴" if d["side"]=="SELL" else "🟡")
            with st.expander(
                f"{icon} {sym} · {d['signal']} · Score {d['total_raw']:+.1f}"
            ):
                st.markdown(f"#### 🎯 ความมั่นใจ: **{d['prob']:.0f}%**")

                st.dataframe(pd.DataFrame([
                    {"แกน":"1. Trend EMA20/50",        "น้ำหนัก":"35%","คะแนน":f"{d['trend_s']:+.1f}"},
                    {"แกน":"2. Momentum RSI/MACD/Stoch","น้ำหนัก":"30%","คะแนน":f"{d['mom_s']:+.1f}"},
                    {"แกน":"3. Volatility BB+Volume",   "น้ำหนัก":"15%","คะแนน":f"{d['vol_s']:+.1f}"},
                    {"แกน":"4. Sentiment Fear&Greed",   "น้ำหนัก":"20%","คะแนน":f"{d['sent_s']:+.1f}"},
                    {"แกน":"✅ รวม",                    "น้ำหนัก":"100%","คะแนน":f"{d['total_raw']:+.1f}"},
                ]), use_container_width=True, hide_index=True)

                r1c1, r1c2, r1c3 = st.columns(3)
                with r1c1:
                    rsi_lbl = ("Overbought ⚠️" if d["rsi"]>=70
                               else "Oversold 💡" if d["rsi"]<=30 else "Normal ✓")
                    st.metric("RSI", f"{d['rsi']:.1f}", delta=rsi_lbl)
                with r1c2: st.metric("Stoch %K", f"{d['stoch']:.1f}")
                with r1c3: st.metric("MACD", f"{d['macd']:.4f}")

                r2c1, r2c2, r2c3 = st.columns(3)
                with r2c1:
                    bb_lbl = ("Lower 💡" if d["bb_pct"]<=0.2
                              else "Upper ⚠️" if d["bb_pct"]>=0.8 else "Mid ✓")
                    st.metric("BB %B", f"{d['bb_pct']:.2f}", delta=bb_lbl)
                with r2c2:
                    st.metric("Vol Ratio", f"{d['vol_ratio']:.1f}×",
                              delta="🐳 Spike!" if d["vol_ratio"]>=2 else None)
                with r2c3: st.metric("ATR", f"{d['atr']:.2f}")

                st.metric("😱 Fear & Greed", d["fg_label"],
                          delta=f"Sentiment score {d['sent_s']:+.0f}")

                norm = (d["total_raw"] + 100) / 200
                st.progress(max(0.0, min(1.0, norm)),
                            text=f"Score: {d['total_raw']:+.1f} / ±100")

                if not d["data_ok"]:
                    st.warning("⚠️ OHLCV < 52 แท่ง — ใช้ค่าเริ่มต้น")
                if d["market_cap"] > 0:
                    mcap = d["market_cap"]
                    st.caption(f"Market Cap: ${mcap/1e9:.2f}B" if mcap>=1e9
                               else f"Market Cap: ${mcap/1e6:.0f}M")

    st.session_state.run_once = False

elif not selected_assets:
    st.warning("ℹ️ กรุณาพิมพ์ชื่อเหรียญด้านบน")
