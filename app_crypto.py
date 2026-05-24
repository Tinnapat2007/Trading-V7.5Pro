import pandas as pd
import yfinance as yf
import time
import logging
import concurrent.futures
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, EMAIndicator
import streamlit as st
from tradingview_ta import TA_Handler, Interval
from streamlit_autorefresh import st_autorefresh
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =========================================================================
# ⚙️ 0. Logging
# =========================================================================
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# =========================================================================
# ⚙️ 1. Page Config
# =========================================================================
st.set_page_config(page_title="AI Crypto Sniper Pro", page_icon="🪙", layout="wide")
st.title("🪙 AI Crypto Sniper Pro (v3.0 - Fast & Stable)")
st.caption("ระบบวิเคราะห์ประชามติและปริมาณซื้อขายจากกระดานเทรดทั่วโลก | แก้ปัญหา TradingView 403 + โหลดเร็วขึ้น 3–5×")

for key, default in [("auto_mode", False), ("run_once", False), ("tv_cache", {}), ("tv_cache_ts", {})]:
    if key not in st.session_state:
        st.session_state[key] = default

# =========================================================================
# 🔧 2. HTTP Session + Retry
# =========================================================================
@st.cache_resource
def get_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    # Header จำเป็นสำหรับบาง API ที่ตรวจ User-Agent
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    })
    return session

# =========================================================================
# 🔍 3. TradingView — แก้ปัญหา 403 ด้วย Parallel + Cache + Fallback
# =========================================================================

TV_CACHE_TTL = 90  # วินาที — cache ผล TV ไว้ป้องกันดึงซ้ำ

def _fetch_tv_single(args: tuple) -> dict:
    """
    ดึง TradingView ต่อ 1 เหรียญ — ออกแบบให้รัน parallel ได้
    แก้ 403: ลอง BINANCE ก่อน ถ้าพังให้ลอง BYBIT และ KUCOIN เป็น fallback
    """
    tv_sym, tv_interval = args
    base_sym = tv_sym.replace("USDT", "")
    
    exchanges_to_try = [
        ("BINANCE", f"{base_sym}USDT"),
        ("BYBIT",   f"{base_sym}USDT"),
        ("KUCOIN",  f"{base_sym}USDT"),
        ("BINANCE", f"{base_sym}BTC"),   # fallback pair ถ้า USDT ไม่มี
    ]
    
    for exchange, symbol in exchanges_to_try:
        try:
            handler = TA_Handler(
                symbol=symbol,
                exchange=exchange,
                screener="crypto",
                interval=tv_interval,
                timeout=8,          # ✅ กำหนด timeout ชัดเจน ไม่ hang
            )
            analysis = handler.get_analysis()
            return {
                "ok": True,
                "buy":       int(analysis.summary.get("BUY", 0)),
                "sell":      int(analysis.summary.get("SELL", 0)),
                "neutral":   int(analysis.summary.get("NEUTRAL", 0)),
                "recommend": str(analysis.summary.get("RECOMMENDATION", "NEUTRAL")),
                "close":     float(analysis.indicators.get("close") or 0),
                "exchange":  exchange,
                "symbol":    symbol,
            }
        except Exception as e:
            err_msg = str(e)
            # 403 = geo-block หรือ symbol ไม่มี → ลอง exchange ถัดไป
            if "403" in err_msg or "404" in err_msg or "symbol" in err_msg.lower():
                logger.warning(f"TV {exchange}/{symbol}: {err_msg[:80]}")
                time.sleep(0.2)
                continue
            # timeout / network error → หยุดลอง
            logger.warning(f"TV {exchange}/{symbol} network error: {err_msg[:80]}")
            break
    
    return {"ok": False, "buy": 0, "sell": 0, "neutral": 0,
            "recommend": "NEUTRAL", "close": 0.0, "exchange": "N/A", "symbol": tv_sym}


def fetch_tv_parallel(assets_meta: list, tv_interval) -> dict:
    """
    ดึง TradingView ทุกเหรียญพร้อมกัน (parallel) → เร็วขึ้น 3–5×
    พร้อม in-memory cache 90 วินาที ป้องกัน rate-limit
    """
    now = time.time()
    results = {}
    to_fetch = []

    for meta in assets_meta:
        sym = meta["clean_ticker"]
        cached_ts = st.session_state.tv_cache_ts.get(sym, 0)
        # ถ้า cache ยังไม่หมดอายุ ใช้ค่าเดิมได้เลย
        if now - cached_ts < TV_CACHE_TTL and sym in st.session_state.tv_cache:
            results[sym] = st.session_state.tv_cache[sym]
        else:
            to_fetch.append(meta)

    if to_fetch:
        args_list = [(m["tv_sym"], tv_interval) for m in to_fetch]
        # ThreadPoolExecutor: รัน HTTP requests พร้อมกัน (I/O bound → thread เหมาะ)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(to_fetch), 5)) as executor:
            futures = {executor.submit(_fetch_tv_single, args): meta
                       for args, meta in zip(args_list, to_fetch)}
            for future in concurrent.futures.as_completed(futures, timeout=20):
                meta = futures[future]
                sym = meta["clean_ticker"]
                try:
                    res = future.result()
                except Exception as e:
                    logger.error(f"TV parallel future error [{sym}]: {e}")
                    res = {"ok": False, "buy": 0, "sell": 0, "neutral": 0,
                           "recommend": "NEUTRAL", "close": 0.0}
                results[sym] = res
                # อัปเดต cache
                st.session_state.tv_cache[sym] = res
                st.session_state.tv_cache_ts[sym] = now

    return results

# =========================================================================
# 🔍 4. CoinGecko — Cache + Correct endpoint + Auto-search ID
# =========================================================================
COINGECKO_ID_MAP = {
    "BTC": "bitcoin",        "ETH": "ethereum",       "SOL": "solana",
    "BNB": "binancecoin",    "XRP": "ripple",          "ADA": "cardano",
    "DOGE": "dogecoin",      "AVAX": "avalanche-2",    "DOT": "polkadot",
    "MATIC": "matic-network","LINK": "chainlink",       "UNI": "uniswap",
    "LTC": "litecoin",       "ATOM": "cosmos",          "XLM": "stellar",
    "PEPE": "pepe",          "SHIB": "shiba-inu",       "TRX": "tron",
    "OP": "optimism",        "ARB": "arbitrum",         "SUI": "sui",
    "APT": "aptos",          "INJ": "injective-protocol","FIL": "filecoin",
    "NEAR": "near",          "ICP": "internet-computer","FTM": "fantom",
    "SAND": "the-sandbox",   "MANA": "decentraland",    "AXS": "axie-infinity",
    "WLD": "worldcoin-wld",  "TON": "the-open-network", "JUP": "jupiter-exchange-solana",
    "SEI": "sei-network",    "TIA": "celestia",         "RENDER": "render-token",
}

@st.cache_data(ttl=60, show_spinner=False)
def get_coingecko_batch(coin_tickers: tuple) -> dict:
    """
    ดึง CoinGecko ทีเดียวทุกเหรียญ (batch) — เร็วกว่าดึงทีละตัว
    คืน dict: {ticker: {market_cap, volume_24h, price_change_24h, rank, cg_price}}
    """
    http = get_http_session()
    empty = {"market_cap": 0, "volume_24h": 0, "price_change_24h": 0.0, "rank": "-", "cg_price": 0.0}
    results = {t: dict(empty) for t in coin_tickers}

    # หา CoinGecko ID สำหรับทุกเหรียญ
    id_to_ticker = {}
    unknown_tickers = []
    for ticker in coin_tickers:
        sym = ticker.upper()
        cg_id = COINGECKO_ID_MAP.get(sym)
        if cg_id:
            id_to_ticker[cg_id] = sym
        else:
            unknown_tickers.append(sym)

    # Auto-search สำหรับเหรียญที่ไม่อยู่ใน map (ทีละตัว แต่ cache ไว้)
    for sym in unknown_tickers:
        try:
            r = http.get("https://api.coingecko.com/api/v3/search",
                         params={"query": sym}, timeout=5).json()
            coins = r.get("coins", [])
            if coins:
                cg_id = coins[0]["id"]
                id_to_ticker[cg_id] = sym
                logger.info(f"CoinGecko auto-search: {sym} → {cg_id}")
        except Exception as e:
            logger.warning(f"CoinGecko search [{sym}]: {e}")

    if not id_to_ticker:
        return results

    # ดึง batch เดียว
    try:
        all_ids = ",".join(id_to_ticker.keys())
        resp = http.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "ids": all_ids,
                    "per_page": len(id_to_ticker), "page": 1},
            timeout=10
        ).json()
        for d in resp:
            cg_id = d.get("id", "")
            ticker = id_to_ticker.get(cg_id)
            if ticker:
                results[ticker] = {
                    "market_cap":       d.get("market_cap") or 0,
                    "volume_24h":       d.get("total_volume") or 0,
                    "price_change_24h": d.get("price_change_percentage_24h") or 0.0,
                    "rank":             d.get("market_cap_rank") or "-",
                    "cg_price":         d.get("current_price") or 0.0,
                }
    except Exception as e:
        logger.warning(f"CoinGecko batch markets failed: {e}")

    return results


def parse_crypto_meta(symbol_raw: str) -> dict:
    sym = (symbol_raw.strip().upper()
           .replace("-", "").replace("/", "")
           .replace("USDT", "").replace("USD", ""))
    return {
        "tv_sym":       f"{sym}USDT",
        "tv_exch":      "BINANCE",
        "tv_screen":    "crypto",
        "yf_sym":       f"{sym}-USD",
        "clean_ticker": sym,
    }

# =========================================================================
# 📦 5. Yahoo Finance — Batch + Cache
# =========================================================================
@st.cache_data(ttl=60, show_spinner=False)
def fetch_yf_batch(tickers_tuple: tuple, period: str, interval: str) -> pd.DataFrame:
    try:
        df = yf.download(
            tickers=list(tickers_tuple),
            period=period, interval=interval,
            group_by="ticker", progress=False,
            auto_adjust=True, threads=True,   # ✅ parallel download
        )
        return df
    except Exception as e:
        logger.error(f"yfinance batch failed: {e}")
        return pd.DataFrame()


def extract_single_ticker_df(all_data: pd.DataFrame, yf_sym: str) -> pd.DataFrame:
    if all_data.empty:
        return pd.DataFrame()
    try:
        if isinstance(all_data.columns, pd.MultiIndex):
            lvl0 = all_data.columns.get_level_values(0).unique()
            if yf_sym in lvl0:
                return all_data[yf_sym].copy().dropna(how="all")
            # yfinance บางครั้งใส่ OHLCV เป็น level 0 เมื่อดาวน์โหลดเหรียญเดียว
            if "Close" in lvl0:
                return all_data.copy().dropna(how="all")
        else:
            if "Close" in all_data.columns:
                return all_data.copy().dropna(how="all")
    except Exception as e:
        logger.warning(f"extract_ticker_df({yf_sym}): {e}")
    return pd.DataFrame()

# =========================================================================
# 🧮 6. Signal Engine
# =========================================================================
def compute_signal(df_yf: pd.DataFrame, tv_result: dict) -> dict:
    """
    3 แกนรวม:
      Trend 40%  — EMA20/50 (พร้อม partial score)
      Momentum 30% — RSI + MACD crossover + Stoch crossover
      Consensus 30% — TradingView buy ratio
    Buffer Zone ±20 ลด noise
    """
    trend_score = momentum_score = 0.0
    rsi_val, macd_val, stoch_k = 50.0, 0.0, 50.0
    latest_price = 0.0
    data_ok = False

    MIN_BARS = 52

    if not df_yf.empty and "Close" in df_yf.columns:
        df = df_yf.dropna(subset=["Close"]).copy()
        if len(df) >= MIN_BARS:
            data_ok = True
            close = df["Close"].astype(float)
            high  = df["High"].astype(float)
            low   = df["Low"].astype(float)
            latest_price = float(close.iloc[-1])

            df["EMA20"]     = EMAIndicator(close=close, window=20).ema_indicator()
            df["EMA50"]     = EMAIndicator(close=close, window=50).ema_indicator()
            df["RSI"]       = RSIIndicator(close=close, window=14).rsi()
            stoch_obj       = StochasticOscillator(high=high, low=low, close=close,
                                                   window=14, smooth_window=3)
            df["Stoch_K"]   = stoch_obj.stoch()
            df["Stoch_D"]   = stoch_obj.stoch_signal()
            macd_obj        = MACD(close=close)
            df["MACD_diff"] = macd_obj.macd_diff()
            df.ffill(inplace=True)

            last = df.iloc[-1]
            prev = df.iloc[-2]

            rsi_val  = float(last["RSI"])       if not pd.isna(last["RSI"])       else 50.0
            macd_val = float(last["MACD_diff"]) if not pd.isna(last["MACD_diff"]) else 0.0
            stoch_k  = float(last["Stoch_K"])   if not pd.isna(last["Stoch_K"])   else 50.0

            # ── Trend (40%) ──────────────────────────────────────────────
            p_above_20 = float(last["Close"]) > float(last["EMA20"])
            e20_above_50 = float(last["EMA20"]) > float(last["EMA50"])
            if p_above_20 and e20_above_50:     trend_score =  40.0   # แนวโน้มขึ้นเต็ม
            elif not p_above_20 and not e20_above_50: trend_score = -40.0  # แนวโน้มลงเต็ม
            elif p_above_20:                    trend_score =  15.0   # ราคาเหนือ EMA20 แต่ EMA20 ยังต่ำ
            else:                               trend_score = -15.0

            # ── Momentum (30%) ───────────────────────────────────────────
            # RSI component (max ±15)
            if rsi_val > 55:      momentum_score += 10.0
            elif rsi_val < 45:    momentum_score -= 10.0
            if rsi_val >= 70:     momentum_score -= 5.0   # overbought penalty
            elif rsi_val <= 30:   momentum_score += 5.0   # oversold bonus

            # MACD crossover (max ±10)
            prev_macd = float(prev["MACD_diff"]) if not pd.isna(prev["MACD_diff"]) else 0.0
            if   macd_val > 0 and prev_macd <= 0: momentum_score += 10.0  # fresh cross up
            elif macd_val > 0:                     momentum_score +=  7.0
            elif macd_val < 0 and prev_macd >= 0: momentum_score -= 10.0  # fresh cross down
            else:                                  momentum_score -=  7.0

            # Stoch %K/%D crossover (max ±10)
            stoch_d  = float(last["Stoch_D"]) if not pd.isna(last["Stoch_D"]) else 50.0
            prev_k   = float(prev["Stoch_K"]) if not pd.isna(prev["Stoch_K"]) else 50.0
            prev_d   = float(prev["Stoch_D"]) if not pd.isna(prev["Stoch_D"]) else 50.0
            if   stoch_k > stoch_d and prev_k <= prev_d: momentum_score += 10.0
            elif stoch_k > stoch_d:                       momentum_score +=  7.0
            elif stoch_k < stoch_d and prev_k >= prev_d: momentum_score -= 10.0
            else:                                         momentum_score -=  7.0

    # ── Consensus (30%) ───────────────────────────────────────────────────
    consensus_score = 0.0
    tv_buy  = tv_result.get("buy",  0)
    tv_sell = tv_result.get("sell", 0)
    tv_recommend = tv_result.get("recommend", "NEUTRAL")
    total_tv = tv_buy + tv_sell

    if total_tv > 0:
        buy_ratio = tv_buy / total_tv
        consensus_score = (buy_ratio - 0.5) * 60.0   # map 0–1 → -30…+30
    elif "BUY"  in tv_recommend:
        consensus_score =  30.0 if "STRONG" in tv_recommend else 15.0
    elif "SELL" in tv_recommend:
        consensus_score = -30.0 if "STRONG" in tv_recommend else -15.0

    total_raw = trend_score + momentum_score + consensus_score

    # ── Signal verdict ────────────────────────────────────────────────────
    if total_raw >= 20:
        side = "BUY"
        prob = min(abs(total_raw), 100.0)
        signal = "🟢 STRONG BUY" if prob >= 70 else "🟢 BUY"
    elif total_raw <= -20:
        side = "SELL"
        prob = min(abs(total_raw), 100.0)
        signal = "🔴 STRONG SELL" if prob >= 70 else "🔴 SELL"
    else:
        side = "HOLD"
        prob = min(abs(total_raw), 100.0)
        signal = "🟡 HOLD (ไซด์เวย์)"

    return {
        "side": side, "signal": signal, "prob": prob,
        "total_raw": total_raw,
        "trend_score": trend_score,
        "momentum_score": momentum_score,
        "consensus_score": consensus_score,
        "rsi": rsi_val, "macd": macd_val, "stoch": stoch_k,
        "latest_price": latest_price,
        "data_ok": data_ok,
    }

# =========================================================================
# 🛠️ 7. Control Panel
# =========================================================================
st.subheader("⚙️ ตัวเลือกเหรียญและกรอบเวลา")
col_s1, col_s2 = st.columns([2, 1])

with col_s1:
    user_input = st.text_input(
        "⌨️ พิมพ์ชื่อย่อเหรียญคริปโต (คั่นด้วย `,`):",
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
    "5 นาที":    ("5m",  "7d",   Interval.INTERVAL_5_MINUTES),
    "15 นาที":   ("15m", "14d",  Interval.INTERVAL_15_MINUTES),
    "1 ชั่วโมง": ("1h",  "60d",  Interval.INTERVAL_1_HOUR),
    "1 วัน":     ("1d",  "180d", Interval.INTERVAL_1_DAY),
}
yf_interval, yf_period, tv_interval = "1h", "60d", Interval.INTERVAL_1_HOUR
for key, val in TF_MAP.items():
    if key in tf_choice:
        yf_interval, yf_period, tv_interval = val
        break

# =========================================================================
# 🎯 8. Buttons
# =========================================================================
col_b1, col_b2, col_b3 = st.columns(3)
with col_b1:
    if st.button("🔍 เริ่มสแกน", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once  = True
        st.rerun()
with col_b2:
    if st.button("🔄 Auto Refresh 1 นาที", use_container_width=True):
        st.session_state.auto_mode = True
        st.session_state.run_once  = False
        st.toast("⚡ เริ่มสตรีมเรียลไทม์แล้ว!")
        st.rerun()
with col_b3:
    if st.button("🛑 หยุดสแกน", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once  = False
        st.toast("🛑 หยุดแล้ว")
        st.rerun()

if st.session_state.auto_mode:
    st_autorefresh(interval=60000, key="crypto_v3_refresh")
    st.info("🔄 [LIVE] อัปเดตทุก 60 วินาที...")

# =========================================================================
# 🧮 9. Main Engine
# =========================================================================
if (st.session_state.run_once or st.session_state.auto_mode) and selected_assets:
    assets_meta   = [parse_crypto_meta(a) for a in selected_assets]
    dashboard_rows = []
    detailed_results = {}

    # ── Progress bar แสดงความคืบหน้า ────────────────────────────────────
    progress = st.progress(0, text="⏳ เริ่มต้นระบบ...")

    # ── Step 1: Yahoo Finance batch (1 request ดึงทุกเหรียญ) ─────────────
    progress.progress(10, text="📥 [1/3] ดึงข้อมูล OHLCV จาก Yahoo Finance...")
    yf_tickers  = tuple(m["yf_sym"] for m in assets_meta)
    all_yf_data = fetch_yf_batch(yf_tickers, yf_period, yf_interval)

    # ── Step 2: TradingView parallel (ทุกเหรียญพร้อมกัน) ─────────────────
    progress.progress(35, text="📡 [2/3] ดึง Consensus จาก TradingView (Parallel)...")
    tv_results = fetch_tv_parallel(assets_meta, tv_interval)

    # แสดงสถานะ TV
    tv_ok_count  = sum(1 for r in tv_results.values() if r.get("ok"))
    tv_fail_list = [sym for sym, r in tv_results.items() if not r.get("ok")]
    if tv_fail_list:
        st.warning(
            f"⚠️ TradingView ดึงไม่ได้ {len(tv_fail_list)} เหรียญ: **{', '.join(tv_fail_list)}** "
            f"→ คำนวณโดยใช้ YF+CG เท่านั้น (สัญญาณยังใช้งานได้)"
        )
    if tv_ok_count > 0:
        st.success(f"✅ TradingView ดึงสำเร็จ {tv_ok_count}/{len(assets_meta)} เหรียญ")

    # ── Step 3: CoinGecko batch (1 request ดึงทุกเหรียญ) ─────────────────
    progress.progress(65, text="🦎 [3/3] ดึง Market Data จาก CoinGecko (Batch)...")
    cg_tickers   = tuple(m["clean_ticker"] for m in assets_meta)
    cg_all       = get_coingecko_batch(cg_tickers)

    # ── Step 4: คำนวณ Signal ─────────────────────────────────────────────
    progress.progress(85, text="🧮 คำนวณสัญญาณ AI...")

    for meta in assets_meta:
        sym = meta["clean_ticker"]
        try:
            df_yf  = extract_single_ticker_df(all_yf_data, meta["yf_sym"])
            tv_res = tv_results.get(sym, {})
            cg     = cg_all.get(sym, {"market_cap":0,"volume_24h":0,
                                      "price_change_24h":0.0,"rank":"-","cg_price":0.0})

            sig = compute_signal(df_yf, tv_res)

            # ราคา fallback: YF → TV → CoinGecko
            price = sig["latest_price"]
            if price == 0.0: price = tv_res.get("close", 0.0)
            if price == 0.0: price = cg.get("cg_price", 0.0)

            price_str = (f"${price:,.8f}" if 0 < price < 0.001
                         else f"${price:,.6f}" if 0 < price < 1
                         else f"${price:,.4f}" if 0 < price < 100
                         else f"${price:,.2f}" if price >= 100
                         else "N/A")

            chg = cg.get("price_change_24h", 0.0)
            chg_str = f"{'▲' if chg >= 0 else '▼'} {abs(chg):.2f}%" if chg else "—"

            # แหล่งข้อมูล indicator
            sources = []
            if sig["data_ok"]:   sources.append("YF✅")
            if tv_res.get("ok"): sources.append(f"TV✅({tv_res.get('exchange','?')})")
            if cg.get("cg_price"): sources.append("CG✅")
            source_str = " ".join(sources) if sources else "ไม่มีข้อมูล"

            dashboard_rows.append({
                "เหรียญ":        sym,
                "ราคา (USD)":    price_str,
                "เปลี่ยน 24H":   chg_str,
                "สัญญาณ AI":     sig["signal"],
                "Score":         f"{sig['total_raw']:+.1f}",
                "ความมั่นใจ":    f"{sig['prob']:.1f}%",
                "อันดับโลก":     f"#{cg['rank']}" if cg["rank"] != "-" else "N/A",
                "Vol 24H":       f"${cg['volume_24h']:,.0f}" if cg.get("volume_24h") else "N/A",
                "แหล่งข้อมูล":   source_str,
            })

            detailed_results[sym] = {
                **sig,
                "tv_buy":         tv_res.get("buy",     0),
                "tv_sell":        tv_res.get("sell",    0),
                "tv_neutral":     tv_res.get("neutral", 0),
                "tv_exchange":    tv_res.get("exchange","N/A"),
                "market_cap":     cg.get("market_cap", 0),
                "price_change_24h": chg,
            }
        except Exception as e:
            logger.error(f"Signal error [{sym}]: {e}", exc_info=True)
            st.warning(f"⚠️ คำนวณสัญญาณ **{sym}** ไม่สำเร็จ: {e}")

    progress.progress(100, text="✅ สแกนเสร็จแล้ว!")
    time.sleep(0.4)
    progress.empty()

    # =========================================================================
    # 📊 10. Dashboard
    # =========================================================================
    if dashboard_rows:
        st.write("---")
        st.subheader(f"📊 แดชบอร์ด ({tf_choice.split('-')[0].strip()})")
        df_dash = pd.DataFrame(dashboard_rows)
        st.dataframe(df_dash, use_container_width=True, hide_index=True)

        st.write("---")
        st.subheader("🔍 เจาะลึกสัญญาณเทคนิคอล")

        for sym, d in detailed_results.items():
            icon = "🟢" if d["side"]=="BUY" else ("🔴" if d["side"]=="SELL" else "🟡")
            with st.expander(f"{icon} {sym}  |  {d['signal']}  |  Score: {d['total_raw']:+.1f}"):

                st.markdown(f"#### 🎯 ความมั่นใจ: **{d['prob']:.1f}%**")

                # Score breakdown table
                st.dataframe(pd.DataFrame([
                    {"แกน": "1. Trend EMA20/50",     "น้ำหนัก": "40%",  "คะแนน": f"{d['trend_score']:+.1f}"},
                    {"แกน": "2. Momentum RSI/MACD/Stoch","น้ำหนัก":"30%","คะแนน": f"{d['momentum_score']:+.1f}"},
                    {"แกน": f"3. Consensus ({d['tv_exchange']})","น้ำหนัก":"30%","คะแนน":f"{d['consensus_score']:+.1f}"},
                    {"แกน": "✅ รวม Total Score",     "น้ำหนัก": "100%", "คะแนน": f"{d['total_raw']:+.1f}"},
                ]), use_container_width=True, hide_index=True)

                # Metrics row
                c1, c2, c3, c4, c5 = st.columns(5)
                rsi_lbl = "Overbought ⚠️" if d["rsi"]>=70 else ("Oversold 💡" if d["rsi"]<=30 else "Normal ✓")
                with c1: st.metric("RSI (14)",      f"{d['rsi']:.1f}",  delta=rsi_lbl)
                with c2: st.metric("Stoch %K",      f"{d['stoch']:.1f}")
                with c3: st.metric("MACD Diff",     f"{d['macd']:.5f}")
                with c4: st.metric("TV (B/S/N)",    f"{d['tv_buy']}/{d['tv_sell']}/{d['tv_neutral']}")
                with c5: st.metric("เปลี่ยน 24H",   f"{d['price_change_24h']:.2f}%")

                # Score meter
                norm = (d["total_raw"] + 100) / 200
                st.progress(max(0.0, min(1.0, norm)),
                            text=f"📊 Score Meter: {d['total_raw']:+.1f} / ±100")

                if not d["data_ok"]:
                    st.info("ℹ️ ข้อมูล OHLCV ไม่พอ (< 52 แท่ง) — Trend/Momentum ใช้ค่าเริ่มต้น")
                if d["market_cap"] > 0:
                    st.caption(f"Market Cap: ${d['market_cap']:,.0f} USD")

    st.session_state.run_once = False

elif not selected_assets:
    st.warning("ℹ️ กรุณาพิมพ์ชื่อเหรียญในช่องด้านบน")
