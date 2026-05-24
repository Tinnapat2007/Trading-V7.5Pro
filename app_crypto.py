import pandas as pd
import yfinance as yf
import time
import logging
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, EMAIndicator
import streamlit as st
from tradingview_ta import TA_Handler, Interval
from streamlit_autorefresh import st_autorefresh
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =========================================================================
# ⚙️ 0. ตั้งค่า Logging เพื่อ Debug ได้จริง
# =========================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# =========================================================================
# ⚙️ 1. โครงสร้างพื้นฐานหน้าเว็บ AI Crypto Sniper Pro (v2.0 - Hardened)
# =========================================================================
st.set_page_config(page_title="AI Crypto Sniper Pro", page_icon="🪙", layout="wide")
st.title("🪙 AI Crypto Sniper Pro (v2.0 - Hardened Edition)")
st.write("ระบบวิเคราะห์ประชามติและปริมาณซื้อขายจากกระดานเทรดทั่วโลก ค้นหาเหรียญได้ทุกตัว 24/7")

if 'auto_mode' not in st.session_state:
    st.session_state.auto_mode = False
if 'run_once' not in st.session_state:
    st.session_state.run_once = False

# =========================================================================
# 🔧 2. HTTP Session พร้อม Retry (ป้องกัน Rate Limit crash)
# =========================================================================
def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session

_http = _build_session()

# =========================================================================
# 🔍 3. CoinGecko — แก้ params ให้ถูกต้อง + Cache 60 วินาที
# =========================================================================

# Mapping ชื่อย่อ → CoinGecko ID (เพิ่มได้เรื่อยๆ)
COINGECKO_ID_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "BNB": "binancecoin", "XRP": "ripple", "ADA": "cardano",
    "DOGE": "dogecoin", "AVAX": "avalanche-2", "DOT": "polkadot",
    "MATIC": "matic-network", "LINK": "chainlink", "UNI": "uniswap",
    "LTC": "litecoin", "ATOM": "cosmos", "XLM": "stellar",
    "PEPE": "pepe", "SHIB": "shiba-inu", "TRX": "tron",
    "OP": "optimism", "ARB": "arbitrum", "SUI": "sui",
    "APT": "aptos", "INJ": "injective-protocol", "FIL": "filecoin",
    "NEAR": "near", "ICP": "internet-computer", "FTM": "fantom",
    "SAND": "the-sandbox", "MANA": "decentraland", "AXS": "axie-infinity",
}

@st.cache_data(ttl=60)
def get_coingecko_data(coin_ticker: str) -> dict:
    """ดึงข้อมูล Market Cap / Volume จาก CoinGecko — ใช้ IDs ที่ถูกต้อง"""
    empty = {"market_cap": 0, "volume_24h": 0, "price_change_24h": 0.0, "rank": "-", "cg_price": 0.0}
    sym = coin_ticker.strip().upper()
    cg_id = COINGECKO_ID_MAP.get(sym)

    # ถ้าไม่อยู่ใน map ให้ค้นหาจาก /search ก่อน
    if not cg_id:
        try:
            search_resp = _http.get(
                "https://api.coingecko.com/api/v3/search",
                params={"query": sym}, timeout=6
            ).json()
            coins = search_resp.get("coins", [])
            if coins:
                cg_id = coins[0]["id"]
                logger.info(f"CoinGecko search: {sym} → {cg_id}")
        except Exception as e:
            logger.warning(f"CoinGecko search failed for {sym}: {e}")
            return empty

    if not cg_id:
        return empty

    try:
        resp = _http.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "ids": cg_id, "per_page": 1, "page": 1},
            timeout=7
        ).json()
        if resp and len(resp) > 0:
            d = resp[0]
            return {
                "market_cap": d.get("market_cap") or 0,
                "volume_24h": d.get("total_volume") or 0,
                "price_change_24h": d.get("price_change_percentage_24h") or 0.0,
                "rank": d.get("market_cap_rank") or "-",
                "cg_price": d.get("current_price") or 0.0,
            }
    except Exception as e:
        logger.warning(f"CoinGecko markets failed for {sym} ({cg_id}): {e}")
    return empty


def parse_crypto_meta(symbol_raw: str) -> dict:
    sym = symbol_raw.strip().upper().replace("-", "").replace("/", "").replace("USDT", "").replace("USD", "")
    return {
        "tv_sym": f"{sym}USDT",
        "tv_exch": "BINANCE",
        "tv_screen": "crypto",
        "yf_sym": f"{sym}-USD",
        "clean_ticker": sym,
    }


# =========================================================================
# 📦 4. ดึง Yahoo Finance แบบ Batch + Cache
# =========================================================================
@st.cache_data(ttl=60)
def fetch_yf_batch(tickers_tuple: tuple, period: str, interval: str) -> pd.DataFrame:
    """ดึงข้อมูล yfinance แบบ batch พร้อม cache — คืน DataFrame เดิม"""
    try:
        df = yf.download(
            tickers=list(tickers_tuple),
            period=period,
            interval=interval,
            group_by="ticker",
            progress=False,
            auto_adjust=True,
        )
        return df
    except Exception as e:
        logger.error(f"yfinance batch download failed: {e}")
        return pd.DataFrame()


def extract_single_ticker_df(all_data: pd.DataFrame, yf_sym: str, n_assets: int) -> pd.DataFrame:
    """แยก DataFrame ของเหรียญเดียวออกจาก batch — รองรับทั้ง 1 และหลายเหรียญ"""
    if all_data.empty:
        return pd.DataFrame()
    try:
        if isinstance(all_data.columns, pd.MultiIndex):
            # หลายเหรียญ — level 0 = ticker, level 1 = OHLCV
            lvl0 = all_data.columns.get_level_values(0)
            if yf_sym in lvl0:
                return all_data[yf_sym].copy().dropna(how="all")
        else:
            # เหรียญเดียว — columns ตรงเลย
            return all_data.copy().dropna(how="all")
    except Exception as e:
        logger.warning(f"extract_single_ticker_df({yf_sym}): {e}")
    return pd.DataFrame()


# =========================================================================
# 🧮 5. Signal Engine — สูตรที่แม่นขึ้น + Buffer Zone
# =========================================================================
def compute_signal(
    df_yf: pd.DataFrame,
    tv_recommend: str,
    tv_buy: int,
    tv_sell: int,
) -> dict:
    """
    คำนวณสัญญาณ 3 แกน:
      • Trend   (40%) — EMA20/50 Cross
      • Momentum (30%) — RSI + MACD + Stoch
      • Consensus (30%) — TradingView / Binance
    พร้อม Buffer Zone ลด noise และ RSI Extreme Bonus
    """
    trend_score = 0.0
    momentum_score = 0.0
    rsi_val, macd_val, stoch_k = 50.0, 0.0, 50.0
    latest_price = 0.0

    min_bars = 52  # ต้องการอย่างน้อย 52 แท่งเพื่อให้ EMA50 + MACD มีความหมาย

    if not df_yf.empty and "Close" in df_yf.columns:
        df = df_yf.dropna(subset=["Close"]).copy()
        if len(df) >= min_bars:
            close = df["Close"].astype(float)
            high  = df["High"].astype(float)
            low   = df["Low"].astype(float)
            latest_price = float(close.iloc[-1])

            df["EMA20"] = EMAIndicator(close=close, window=20).ema_indicator()
            df["EMA50"] = EMAIndicator(close=close, window=50).ema_indicator()
            df["RSI"]   = RSIIndicator(close=close, window=14).rsi()
            stoch_obj   = StochasticOscillator(high=high, low=low, close=close, window=14, smooth_window=3)
            df["Stoch_K"]   = stoch_obj.stoch()
            df["Stoch_D"]   = stoch_obj.stoch_signal()
            macd_obj    = MACD(close=close)
            df["MACD_diff"] = macd_obj.macd_diff()
            df["MACD_line"] = macd_obj.macd()
            df["MACD_sig"]  = macd_obj.macd_signal()
            df.ffill(inplace=True)

            last = df.iloc[-1]
            prev = df.iloc[-2]

            rsi_val   = float(last["RSI"])   if not pd.isna(last["RSI"])   else 50.0
            macd_val  = float(last["MACD_diff"]) if not pd.isna(last["MACD_diff"]) else 0.0
            stoch_k   = float(last["Stoch_K"])  if not pd.isna(last["Stoch_K"])  else 50.0

            # --- แกน Trend (40%) ---
            price_above_ema20 = last["Close"] > last["EMA20"]
            ema20_above_ema50 = last["EMA20"] > last["EMA50"]
            price_below_ema20 = last["Close"] < last["EMA20"]
            ema20_below_ema50 = last["EMA20"] < last["EMA50"]

            if price_above_ema20 and ema20_above_ema50:
                trend_score = 40.0
            elif price_below_ema20 and ema20_below_ema50:
                trend_score = -40.0
            elif price_above_ema20:  # ราคาอยู่เหนือ EMA20 แต่ EMA20 ยังต่ำกว่า EMA50
                trend_score = 15.0
            elif price_below_ema20:
                trend_score = -15.0

            # --- แกน Momentum (30%) — ใช้ crossover + ค่าระดับ ---
            # RSI (10 คะแนน)
            if rsi_val > 55:
                momentum_score += 10.0
            elif rsi_val < 45:
                momentum_score -= 10.0
            # RSI Extreme Zones — Bonus/Penalty
            if rsi_val >= 70:
                momentum_score -= 5.0   # overbought → ลดแรง
            elif rsi_val <= 30:
                momentum_score += 5.0   # oversold  → เพิ่มโอกาส bounce

            # MACD Diff + crossover (10 คะแนน)
            if macd_val > 0 and float(prev["MACD_diff"]) < 0:
                momentum_score += 10.0   # กำลัง cross up → แรงสุด
            elif macd_val > 0:
                momentum_score += 7.0
            elif macd_val < 0 and float(prev["MACD_diff"]) > 0:
                momentum_score -= 10.0   # กำลัง cross down → อ่อนสุด
            else:
                momentum_score -= 7.0

            # Stochastic %K vs %D crossover (10 คะแนน)
            stoch_d = float(last["Stoch_D"]) if not pd.isna(last["Stoch_D"]) else 50.0
            prev_k  = float(prev["Stoch_K"]) if not pd.isna(prev["Stoch_K"]) else 50.0
            prev_d  = float(prev["Stoch_D"]) if not pd.isna(prev["Stoch_D"]) else 50.0
            if stoch_k > stoch_d and prev_k <= prev_d:
                momentum_score += 10.0   # %K cross above %D
            elif stoch_k > stoch_d:
                momentum_score += 7.0
            elif stoch_k < stoch_d and prev_k >= prev_d:
                momentum_score -= 10.0
            else:
                momentum_score -= 7.0

    # --- แกน Consensus (30%) — TradingView ---
    consensus_score = 0.0
    total_tv = tv_buy + tv_sell
    if total_tv > 0:
        buy_ratio = tv_buy / total_tv   # 0.0 – 1.0
        # map buy_ratio → -30…+30
        consensus_score = (buy_ratio - 0.5) * 60.0
    elif "BUY" in tv_recommend:
        consensus_score = 30.0 if "STRONG" in tv_recommend else 15.0
    elif "SELL" in tv_recommend:
        consensus_score = -30.0 if "STRONG" in tv_recommend else -15.0

    total_raw = trend_score + momentum_score + consensus_score

    # --- Buffer Zone: ต้องผ่าน ±20 ถึงจะ trigger BUY/SELL (ลด noise) ---
    BUY_THRESHOLD  =  20.0
    SELL_THRESHOLD = -20.0
    STRONG_THRESHOLD = 70.0

    if total_raw >= BUY_THRESHOLD:
        side = "BUY"
        prob = min(abs(total_raw), 100.0)
        signal = "🟢 STRONG BUY" if prob >= STRONG_THRESHOLD else "🟢 BUY"
    elif total_raw <= SELL_THRESHOLD:
        side = "SELL"
        prob = min(abs(total_raw), 100.0)
        signal = "🔴 STRONG SELL" if prob >= STRONG_THRESHOLD else "🔴 SELL"
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
    }


# =========================================================================
# 🛠️ 6. แผงควบคุม
# =========================================================================
st.subheader("⚙️ ตัวเลือกเหรียญและกรอบเวลาคริปโต")
col_s1, col_s2 = st.columns([2, 1])

with col_s1:
    user_input = st.text_input(
        "⌨️ พิมพ์ชื่อย่อเหรียญคริปโตที่ต้องการสแกน (คั่นด้วย `,`):",
        value="BTC, ETH, SOL, DOGE, PEPE",
    )
    selected_assets = [x.strip().upper() for x in user_input.split(",") if x.strip()]

with col_s2:
    tf_choice = st.selectbox(
        "⏱️ กรอบเวลาสแกน (Timeframe):",
        options=[
            "5 นาที (M5) - สายซิ่งสคัลปิ้ง",
            "15 นาที (M15) - เฝ้ากรอบระยะสั้น",
            "1 ชั่วโมง (H1) - เฝ้ารอบใหญ่เดย์เทรด",
            "1 วัน (1D) - ถือรันเทรนด์สปอต",
        ],
        index=2,
    )

TF_MAP = {
    "5 นาที":   ("5m",  "7d",   Interval.INTERVAL_5_MINUTES),
    "15 นาที":  ("15m", "14d",  Interval.INTERVAL_15_MINUTES),
    "1 ชั่วโมง": ("1h",  "60d",  Interval.INTERVAL_1_HOUR),
    "1 วัน":    ("1d",  "180d", Interval.INTERVAL_1_DAY),
}
for key, val in TF_MAP.items():
    if key in tf_choice:
        yf_interval, yf_period, tv_interval = val
        break

# =========================================================================
# 🎯 7. ปุ่มควบคุม
# =========================================================================
col_b1, col_b2, col_b3 = st.columns(3)
with col_b1:
    if st.button("🔍 เริ่มสแกนคริปโตทันที", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once = True
        st.rerun()
with col_b2:
    if st.button("🔄 Auto Refresh 1 นาที", use_container_width=True):
        st.session_state.auto_mode = True
        st.session_state.run_once = False
        st.toast("⚡ เริ่มระบบสตรีมข้อมูลคริปโตเรียลไทม์")
        st.rerun()
with col_b3:
    if st.button("🛑 หยุดสแกน", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once = False
        st.toast("🛑 หยุดระบบเรียบร้อย")
        st.rerun()

if st.session_state.auto_mode:
    st_autorefresh(interval=60000, key="crypto_v2_refresh")
    st.info("🔄 [LIVE] กำลังอัปเดตทุก 60 วินาที...")

# =========================================================================
# 🧮 8. Main Engine Loop
# =========================================================================
if (st.session_state.run_once or st.session_state.auto_mode) and selected_assets:
    dashboard_summary = []
    detailed_results  = {}

    with st.spinner("⏳ กำลังควบรวมข้อมูลจาก Binance, Yahoo Finance และ CoinGecko..."):

        # --- ดึง Yahoo Finance แบบ Batch (1 ครั้ง) ---
        yf_tickers = tuple(parse_crypto_meta(a)["yf_sym"] for a in selected_assets)
        all_yf_data = fetch_yf_batch(yf_tickers, yf_period, yf_interval)

        for asset_raw in selected_assets:
            try:
                meta = parse_crypto_meta(asset_raw)

                # --- แหล่ง 1: TradingView / Binance ---
                tv_buy, tv_sell, tv_neutral, tv_recommend = 0, 0, 0, "NEUTRAL"
                tv_close_price = 0.0
                try:
                    handler  = TA_Handler(
                        symbol=meta["tv_sym"], exchange=meta["tv_exch"],
                        screener=meta["tv_screen"], interval=tv_interval,
                    )
                    analysis = handler.get_analysis()
                    tv_buy       = int(analysis.summary.get("BUY", 0))
                    tv_sell      = int(analysis.summary.get("SELL", 0))
                    tv_neutral   = int(analysis.summary.get("NEUTRAL", 0))
                    tv_recommend = str(analysis.summary.get("RECOMMENDATION", "NEUTRAL"))
                    tv_close_price = float(analysis.indicators.get("close") or 0)
                except Exception as e:
                    logger.warning(f"TradingView failed [{meta['tv_sym']}]: {e}")
                    st.caption(f"⚠️ TradingView ดึงไม่ได้สำหรับ {meta['clean_ticker']} — ใช้เฉพาะ YF+CG")

                # --- แหล่ง 2: Yahoo Finance ---
                df_yf = extract_single_ticker_df(all_yf_data, meta["yf_sym"], len(selected_assets))

                # เพิ่ม sleep เล็กน้อยเพื่อไม่ spam API
                time.sleep(0.3)

                # --- แหล่ง 3: CoinGecko ---
                cg = get_coingecko_data(meta["clean_ticker"])

                # --- คำนวณ Signal ---
                sig = compute_signal(df_yf, tv_recommend, tv_buy, tv_sell)

                # ราคา fallback: YF → TV → CoinGecko
                latest_price = sig["latest_price"]
                if latest_price == 0.0 and tv_close_price > 0:
                    latest_price = tv_close_price
                if latest_price == 0.0 and cg["cg_price"] > 0:
                    latest_price = cg["cg_price"]

                # Format ราคา
                if latest_price > 0:
                    price_str = f"${latest_price:,.6f}" if latest_price < 1 else f"${latest_price:,.4f}" if latest_price < 100 else f"${latest_price:,.2f}"
                else:
                    price_str = "N/A"

                # % เปลี่ยนแปลง 24H จาก CoinGecko
                chg = cg["price_change_24h"]
                chg_str = f"{'▲' if chg >= 0 else '▼'} {abs(chg):.2f}%" if chg != 0.0 else "—"

                dashboard_summary.append({
                    "เหรียญ":          meta["clean_ticker"],
                    "ราคา (USD)":      price_str,
                    "เปลี่ยน 24H":     chg_str,
                    "สัญญาณ AI":       sig["signal"],
                    "Score":           f"{sig['total_raw']:.1f}",
                    "ความมั่นใจ":      f"{sig['prob']:.1f}%",
                    "อันดับโลก":       f"#{cg['rank']}" if cg["rank"] != "-" else "N/A",
                    "Vol 24H (USD)":   f"${cg['volume_24h']:,.0f}" if cg["volume_24h"] > 0 else "N/A",
                })

                detailed_results[meta["clean_ticker"]] = {
                    **sig, "tv_buy": tv_buy, "tv_sell": tv_sell,
                    "tv_neutral": tv_neutral, "market_cap": cg["market_cap"],
                    "price_change_24h": chg,
                }

            except Exception as e:
                logger.error(f"Main loop error [{asset_raw}]: {e}", exc_info=True)
                st.warning(f"⚠️ เกิดข้อผิดพลาดกับเหรียญ **{asset_raw}**: {e}")

        # =========================================================================
        # 📊 9. Dashboard
        # =========================================================================
        if dashboard_summary:
            st.write("---")
            st.subheader(f"📊 แดชบอร์ดคริปโต ({tf_choice.split('-')[0].strip()})")

            df_dash = pd.DataFrame(dashboard_summary)
            st.dataframe(df_dash, use_container_width=True, hide_index=True)

            st.write("---")
            st.subheader("🔍 เจาะลึกสัญญาณเทคนิคอล")

            for name, d in detailed_results.items():
                with st.expander(f"🔎 {name}  |  {d['signal']}  |  Score: {d['total_raw']:.1f}"):
                    st.markdown(f"### 🎯 ความมั่นใจสัญญาณ: **{d['prob']:.1f}%**")

                    # Score Breakdown
                    breakdown = pd.DataFrame([
                        {"แกน": "1. แนวโน้ม EMA 20/50",        "น้ำหนัก": "40%", "คะแนน (raw)": f"{d['trend_score']:.1f}"},
                        {"แกน": "2. โมเมนตัม RSI/MACD/Stoch",  "น้ำหนัก": "30%", "คะแนน (raw)": f"{d['momentum_score']:.1f}"},
                        {"แกน": "3. Binance Consensus",          "น้ำหนัก": "30%", "คะแนน (raw)": f"{d['consensus_score']:.1f}"},
                        {"แกน": "✅ รวม (Total Score)",           "น้ำหนัก": "100%","คะแนน (raw)": f"{d['total_raw']:.1f}"},
                    ])
                    st.dataframe(breakdown, use_container_width=True, hide_index=True)

                    # Indicator Metrics
                    m1, m2, m3, m4, m5 = st.columns(5)
                    rsi_delta = "Overbought ⚠️" if d["rsi"] >= 70 else ("Oversold 💡" if d["rsi"] <= 30 else "Normal")
                    with m1: st.metric("RSI (14)", f"{d['rsi']:.1f}", delta=rsi_delta)
                    with m2: st.metric("Stochastic %K", f"{d['stoch']:.1f}")
                    with m3: st.metric("MACD Diff", f"{d['macd']:.5f}")
                    with m4: st.metric("TV (B/S/N)", f"{d['tv_buy']}/{d['tv_sell']}/{d['tv_neutral']}")
                    with m5: st.metric("เปลี่ยน 24H", f"{d['price_change_24h']:.2f}%")

                    # Progress Bar (visual)
                    norm = (d["total_raw"] + 100) / 200  # normalize 0–1
                    st.progress(max(0.0, min(1.0, norm)), text=f"Score meter: {d['total_raw']:.1f}")

                    if d["market_cap"] > 0:
                        st.info(f"💡 Market Cap: ${d['market_cap']:,.0f} USD")

    st.session_state.run_once = False

elif not selected_assets:
    st.warning("ℹ️ กรุณาพิมพ์ชื่อย่อเหรียญในช่องด้านบนเพื่อเริ่มต้น")
