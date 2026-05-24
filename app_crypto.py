import pandas as pd
import yfinance as yf
import ta
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, EMAIndicator
import streamlit as st
from tradingview_ta import TA_Handler, Interval
from streamlit_autorefresh import st_autorefresh
import requests

# =========================================================================
# ⚙️ 1. โครงสร้างพื้นฐานหน้าเว็บ AI Trading Crypto Dedicated (v1.2 - Modern Pandas)
# =========================================================================
st.set_page_config(page_title="AI Crypto Sniper Pro", page_icon="🪙", layout="wide")

st.title("🪙 AI Crypto Sniper Pro (Dedicated Version)")
st.write("ระบบวิเคราะห์ประชามติและปริมาณซื้อขายจากกระดานเทรดทั่วโลก ค้นหาเหรียญได้ทุกตัว 24/7")

# ระบบจำสถานะหน้าเว็บ
if 'auto_mode' not in st.session_state:
    st.session_state.auto_mode = False
if 'run_once' not in st.session_state:
    st.session_state.run_once = False

# =========================================================================
# 🔍 2. ฟังก์ชันแปล็กและดึงข้อมูล On-Chain / Market Cap จาก CoinGecko
# =========================================================================
def get_coingecko_data(coin_ticker):
    """ ดึงข้อมูล Fundamental & Volume ล่าสุดจาก CoinGecko public API """
    try:
        ticker_clean = coin_ticker.strip().lower()
        url = f"https://api.coingecko.com/api/v3/coins/markets"
        params = {'vs_currency': 'usd', 'symbols': ticker_clean}
        response = requests.get(url, params=params, timeout=5).json()
        if response and len(response) > 0:
            data = response[0]
            return {
                "market_cap": data.get("market_cap", 0),
                "volume_24h": data.get("total_volume", 0),
                "price_change_24h": data.get("price_change_percentage_24h", 0.0),
                "rank": data.get("market_cap_rank", "-")
            }
    except:
        pass
    return {"market_cap": 0, "volume_24h": 0, "price_change_24h": 0.0, "rank": "-"}

def parse_crypto_meta(symbol_raw):
    """ แปลงคำค้นหาให้เข้าชุดกับ TradingView (Binance) และ Yahoo Finance ได้ทั่วโลก """
    sym = symbol_raw.strip().upper().replace("-", "").replace("/", "").replace("USDT", "").replace("USD", "")
    return {
        "tv_sym": f"{sym}USDT",
        "tv_exch": "BINANCE",
        "tv_screen": "crypto",
        "yf_sym": f"{sym}-USD",
        "clean_ticker": sym
    }

# =========================================================================
# 🛠️ 3. แผงควบคุม (เปิดกว้างให้พิมพ์ชื่อเหรียญได้ทั่วโลก)
# =========================================================================
st.subheader("⚙️ ตัวเลือกเหรียญและกรอบเวลาคริปโต")
col_setting1, col_setting2 = st.columns([2, 1])

with col_setting1:
    user_input = st.text_input(
        "⌨️ พิมพ์ชื่อย่อเหรียญคริปโตที่ต้องการสแกน (คั่นด้วยเครื่องหมายจุลภาค `,`):",
        value="BTC, ETH, SOL, DOGE, PEPE"
    )
    selected_assets = [x.strip().upper() for x in user_input.split(",") if x.strip()]

with col_setting2:
    tf_choice = st.selectbox(
        "⏱️ กรอบเวลาสแกน (Timeframe):",
        options=["5 นาที (M5) - สายซิ่งสคัลปิ้ง", "15 นาที (M15) - เฝ้ากรอบระยะสั้น", "1 ชั่วโมง (H1) - เฝ้ารอบใหญ่เดย์เทรด", "1 วัน (1D) - ถือรันเทรนด์สปอต"],
        index=2
    )

# ขยายระยะเวลาดึงข้อมูลย้อนหลัง (Period) ของไทม์เฟรมย่อย เพื่อให้มีแท่งเทียนพอคำนวณอินดิเคเตอร์
if "5 นาที" in tf_choice:
    yf_interval, yf_period, tv_interval = "5m", "5d", Interval.INTERVAL_5_MINUTES  
elif "15 นาที" in tf_choice:
    yf_interval, yf_period, tv_interval = "15m", "7d", Interval.INTERVAL_15_MINUTES 
elif "1 ชั่วโมง" in tf_choice:
    yf_interval, yf_period, tv_interval = "1h", "1mo", Interval.INTERVAL_1_HOUR
else:
    yf_interval, yf_period, tv_interval = "1d", "3mo", Interval.INTERVAL_1_DAY

# =========================================================================
# 🎯 4. ปุ่มควบคุมสแกน
# =========================================================================
col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1])
with col_btn1:
    if st.button("🔍 เริ่มสแกนคริปโตทันที", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once = True
        st.rerun()
with col_btn2:
    if st.button("🔄 Auto Refresh 1 นาที", use_container_width=True):
        st.session_state.auto_mode = True
        st.session_state.run_once = False
        st.toast("⚡ เริ่มระบบสตรีมข้อมูลคริปโตเรียลไทม์")
        st.rerun()
with col_btn3:
    if st.button("🛑 หยุดสแกน", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once = False
        st.toast("🛑 หยุดระบบเรียบร้อย")
        st.rerun()

if st.session_state.auto_mode:
    st_autorefresh(interval=60000, key="crypto_dedicated_refresh")
    st.info("🔄 [CRYPTO STATUS] บอทกำลังดึงข้อมูลด่วนจากบอร์ดสากลและอัปเดตทุกๆ 60 วินาที...")

# =========================================================================
# 🧮 5. กลไก Consensus Engine สำหรับ Crypto
# =========================================================================
if (st.session_state.run_once or st.session_state.auto_mode) and selected_assets:
    dashboard_summary = []
    detailed_results = {}

    with st.spinner('⏳ [CRYPTO ENGINE] กำลังควบรวมข้อมูลจาก Binance, Yahoo Finance และ CoinGecko...'):
        cleaned_yf_tickers = [parse_crypto_meta(a)["yf_sym"] for a in selected_assets]
        try:
            all_yf_data = yf.download(tickers=cleaned_yf_tickers, period=yf_period, interval=yf_interval, group_by='ticker', progress=False)
        except:
            all_yf_data = pd.DataFrame()

        for asset_raw in selected_assets:
            try:
                meta = parse_crypto_meta(asset_raw)
                
                # 1. แหล่งที่ 1: TradingView Binance API
                tv_buy, tv_sell, tv_neutral, tv_recommend = 0, 0, 0, "NEUTRAL"
                try:
                    handler = TA_Handler(symbol=meta["tv_sym"], exchange=meta["tv_exch"], screener=meta["tv_screen"], interval=tv_interval)
                    analysis = handler.get_analysis()
                    tv_buy = analysis.summary["BUY"]
                    tv_sell = analysis.summary["SELL"]
                    tv_neutral = analysis.summary["NEUTRAL"]
                    tv_recommend = analysis.summary["RECOMMENDATION"]
                except:
                    pass 

                # 2. แหล่งที่ 2: Yahoo Finance
                df_yf = pd.DataFrame()
                if not all_yf_data.empty:
                    if len(selected_assets) == 1:
                        df_yf = all_yf_data.copy()
                        if isinstance(df_yf.columns, pd.MultiIndex):
                            df_yf.columns = df_yf.columns.get_level_values(1)
                    else:
                        if meta["yf_sym"] in all_yf_data.columns.get_level_values(0):
                            df_yf = all_yf_data[meta["yf_sym"]].copy()

                trend_score = 0.0     
                momentum_score = 0.0  
                consensus_score = 0.0 
                latest_price = 0.0
                rsi_val, macd_val, stoch_k = 50.0, 0.0, 50.0
                
                # ตรวจสอบความพร้อมของข้อมูลและจำนวนแถวป้องกัน out-of-bounds
                if not df_yf.empty and 'Close' in df_yf.columns and len(df_yf) >= 20:
                    df_yf.dropna(subset=['Close'], inplace=True)
                    
                    if len(df_yf) >= 20: # ตรวจสอบซ้ำหลังตัดแถวว่างออกแล้ว
                        close_series = df_yf['Close'].astype(float)
                        high_series = df_yf['High'].astype(float)
                        low_series = df_yf['Low'].astype(float)
                        
                        latest_price = float(close_series.iloc[-1])
                        
                        df_yf['EMA20'] = EMAIndicator(close=close_series, window=20).ema_indicator()
                        df_yf['EMA50'] = EMAIndicator(close=close_series, window=50).ema_indicator()
                        df_yf['RSI'] = RSIIndicator(close=close_series, window=14).rsi()
                        
                        stoch = StochasticOscillator(high=high_series, low=low_series, close=close_series, window=14, smooth_window=3)
                        df_yf['Stoch_K'] = stoch.stoch()
                        
                        macd_obj = MACD(close=close_series)
                        df_yf['MACD_diff'] = macd_obj.macd_diff()

                        # 🔥 [FIXED] เปลี่ยนจาก .fillna(method='ffill') เป็น .ffill() เพื่อให้รองรับ Pandas เวอร์ชันใหม่
                        df_yf.ffill(inplace=True)
                        latest_calc = df_yf.iloc[-1]
                        
                        rsi_val = latest_calc['RSI'] if not pd.isna(latest_calc['RSI']) else 50.0
                        macd_val = latest_calc['MACD_diff'] if not pd.isna(latest_calc['MACD_diff']) else 0.0
                        stoch_k = latest_calc['Stoch_K'] if not pd.isna(latest_calc['Stoch_K']) else 50.0

                        # แกนที่ 1: Trend Score (40%)
                        if latest_calc['Close'] > latest_calc['EMA20'] and latest_calc['EMA20'] > latest_calc['EMA50']:
                            trend_score = 40.0  
                        elif latest_calc['Close'] < latest_calc['EMA20'] and latest_calc['EMA20'] < latest_calc['EMA50']:
                            trend_score = -40.0 

                        # แกนที่ 2: Momentum Score (30%)
                        if macd_val > 0: momentum_score += 10.0
                        else: momentum_score -= 10.0
                        if rsi_val > 50: momentum_score += 10.0
                        else: momentum_score -= 10.0
                        if stoch_k > 50: momentum_score += 10.0
                        else: momentum_score -= 10.0
                else:
                    # เคสรองรับเมื่อไม่มีข้อมูลจาก Yahoo Finance เลย (เช่น เหรียญ PEPE ในบางเวลา)
                    latest_price = 0.0

                # แกนที่ 3: TradingView Consensus (30%)
                if "BUY" in tv_recommend:
                    consensus_score = 30.0 if "STRONG" in tv_recommend else 15.0
                elif "SELL" in tv_recommend:
                    consensus_score = -30.0 if "STRONG" in tv_recommend else -15.0

                total_raw_score = trend_score + momentum_score + consensus_score 
                
                if total_raw_score > 15:
                    side_verdict = "BUY"
                    probability_pct = abs(total_raw_score)
                    final_signal = "🟢 STRONG BUY" if probability_pct >= 75 else "🟢 BUY"
                elif total_raw_score < -15:
                    side_verdict = "SELL"
                    probability_pct = abs(total_raw_score)
                    final_signal = "🔴 STRONG SELL" if probability_pct >= 75 else "🔴 SELL"
                else:
                    side_verdict = "HOLD"
                    probability_pct = 100.0 - abs(total_raw_score) 
                    final_signal = "🟡 HOLD (ไซด์เวย์)"

                # 3. แหล่งที่ 3: CoinGecko
                cg_meta = get_coingecko_data(meta["clean_ticker"])

                # หากดึงราคาจาก Yahoo ไม่ได้ ให้ลองดึงราคารองรับจาก TradingView แทน
                if latest_price == 0.0 and 'analysis' in locals() and analysis.indicators.get("close"):
                    latest_price = float(analysis.indicators["close"])

                dashboard_summary.append({
                    "เหรียญ": meta["clean_ticker"],
                    "ราคา (USD)": f"${latest_price:,.6f}" if latest_price > 0 and latest_price < 1 else (f"${latest_price:,.2f}" if latest_price > 0 else "N/A"),
                    "สัญญาณ AI": final_signal,
                    "ความแม่นยำ/มั่นใจ": f"{probability_pct:.1f}%",
                    "อันดับโลก": f"#{cg_meta['rank']}" if cg_meta['rank'] != "-" else "N/A",
                    "Vol 24H (USD)": f"${cg_meta['volume_24h']:,.0f}" if cg_meta['volume_24h'] > 0 else "N/A"
                })

                detailed_results[meta["clean_ticker"]] = {
                    "verdict": side_verdict, "prob": probability_pct,
                    "trend_part": abs(trend_score), "momentum_part": abs(momentum_score), "consensus_part": abs(consensus_score),
                    "rsi": rsi_val, "macd": macd_val, "stoch": stoch_k,
                    "tv_buy": tv_buy, "tv_sell": tv_sell, "tv_neutral": tv_neutral,
                    "market_cap": cg_meta['market_cap']
                }
            except Exception as e:
                st.warning(f"⚠️ ตรวจสอบขัดข้องชั่วคราวสำหรับเหรียญ {asset_raw}: {e}")

        # =========================================================================
        # 📊 6. การแสดงผล Dashboard เรียลไทม์
        # =========================================================================
        if dashboard_summary:
            st.write("---")
            st.subheader(f"📊 แดชบอร์ดคริปโตทริปเปิลดาต้า ({tf_choice.split('-')[0].strip()})")
            
            df_dash = pd.DataFrame(dashboard_summary)
            st.dataframe(df_dash, use_container_width=True, hide_index=True)
            
            st.write("---")
            st.subheader("🔍 ถอดรหัสคณิตศาสตร์และจุดเทคนิคอลเจาะลึก")
            
            for name, data in detailed_results.items():
                with st.expander(f"🔎 เจาะลึกสัญญาณ & สภาพคล่องของ: {name}"):
                    st.markdown(f"### 🎯 มติตลาดคริปโตให้น้ำหนักความมั่นใจ: **{data['prob']:.1f}%**")
                    
                    breakdown_table_data = [
                        {"โครงสร้างแกนคำนวณ": "1. แนวโน้มใหญ่รากฐาน (EMA 20/50)", "สัดส่วน": "40%", "คะแนนที่ทำได้": f"{data['trend_part']:.1f}%"},
                        {"โครงสร้างแกนคำนวณ": "2. ดัชนีโมเมนตัมซิ่ง (RSI/MACD/Stoch)", "สัดส่วน": "30%", "คะแนนที่ทำได้": f"{data['momentum_part']:.1f}%"},
                        {"โครงสร้างแกนคำนวณ": "3. แรงซื้อขายสะสม (Binance API)", "สัดส่วน": "30%", "คะแนนที่ทำได้": f"{data['consensus_part']:.1f}%"}
                    ]
                    st.dataframe(pd.DataFrame(breakdown_table_data), use_container_width=True, hide_index=True)
                    
                    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
                    with m_col1: st.metric("RSI (14)", f"{data['rsi']:.1f}")
                    with m_col2: st.metric("Stochastic %K", f"{data['stoch']:.1f}")
                    with m_col3: st.metric("MACD Diff", f"{data['macd']:.4f}")
                    with m_col4: st.metric("Binance (B/S/N)", f"{data['tv_buy']}/{data['tv_sell']}/{data['tv_neutral']}")
                    
                    if data['market_cap'] > 0:
                        st.info(f"💡 มูลค่าตลาดปัจจุบัน (Market Cap): ${data['market_cap']:,.0f} USD")
                        
    st.session_state.run_once = False
elif not selected_assets:
    st.warning("ℹ️ ไม่พบรายชื่อเหรียญ: กรุณาพิมพ์ตัวย่อเหรียญคริปโตในช่องค้นหาด้านบนเพื่อเริ่มต้นทำงาน")
