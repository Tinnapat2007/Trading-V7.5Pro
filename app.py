import pandas as pd
import yfinance as yf
import ta
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, EMAIndicator
import streamlit as st
from tradingview_ta import TA_Handler, Interval
from streamlit_autorefresh import st_autorefresh
import time

# =========================================================================
# ⚙️ 1. โครงสร้างพื้นฐานหน้าเว็บ Trading V7.5 Pro (ปรับปรุง Responsive สำหรับมือถือ)
# =========================================================================
st.set_page_config(page_title="Trading V7.5 Pro", page_icon="💎", layout="wide")

st.title("💎 Trading V7.5 Pro")
st.write("ระบบวิเคราะห์มติรวมพหุภาคี (Consensus Engine) รองรับการเข้าดูผ่านมือถือและทุกอุปกรณ์ทั่วโลก")

# ระบบจำสถานะหน้าเว็บ
if 'auto_mode' not in st.session_state:
    st.session_state.auto_mode = False
if 'run_once' not in st.session_state:
    st.session_state.run_once = False

# =========================================================================
# 📖 2. ตารางคลังตัวย่อสินทรัพย์
# =========================================================================
with st.expander("📖 คลังตัวย่อสินทรัพย์ทั้งหมดที่รองรับในระบบ V7.5 Pro"):
    st.markdown("""
    | หมวดหมู่ | สินทรัพย์ | ตัวย่อ (พิมพ์ได้เลย) | แหล่งข้อมูลร่วม |
    | :--- | :--- | :--- | :--- |
    | 🪙 Crypto | โซลานา (Solana) | `SOLUSD` | Binance + Yahoo |
    | 🪙 Crypto | บิตคอยน์ (Bitcoin) | `BTCUSD` | Binance + Yahoo |
    | 🪙 Crypto | อีเธอเรียม (Ethereum) | `ETHUSD` | Binance + Yahoo |
    | 🪙 Crypto | ไบแนนซ์คอยน์ (BNB) | `BNBUSD` | Binance + Yahoo |
    | 🪙 Crypto | โดชคอยน์ (Dogecoin) | `DOGEUSD` | Binance + Yahoo |
    | 🏆 Gold | ทองคำแท่งโลก (Gold) | `XAUUSD` | FX_IDC + Yahoo (GC=F) |
    | 💱 Forex | ยูโร / ดอลลาร์ | `EURUSD` | FX_IDC + Yahoo |
    | 💱 Forex | ปอนด์ / ดอลลาร์ | `GBPUSD` | FX_IDC + Yahoo |
    | 💱 Forex | ดอลลาร์ / เยน | `USDJPY` | FX_IDC + Yahoo |
    | 💱 Forex | ดอลลาร์ / บาทไทย | `USDTHB` | FX_IDC + Yahoo |
    """)

# =========================================================================
# 🛠️ 3. แผงควบคุม (ปรับเป็น Stack แถวบนมือถืออัตโนมัติ)
# =========================================================================
st.subheader("⚙️ แผงควบคุมและเลือกสินทรัพย์")
col_setting1, col_setting2 = st.columns([2, 1])

with col_setting1:
    asset_options = ["SOLUSD", "BTCUSD", "ETHUSD", "BNBUSD", "DOGEUSD", "XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDTHB"]
    selected_assets = st.multiselect(
        "⌨️ เลือกชื่อย่อสินทรัพย์:",
        options=asset_options,
        default=["SOLUSD", "BTCUSD", "XAUUSD", "EURUSD"]
    )

with col_setting2:
    tf_choice = st.selectbox(
        "⏱️ กรอบเวลา (Timeframe):",
        options=["5 นาที (M5) - สัญญาณซิ่งความไวสูง", "15 นาที (M15) - เล่นสั้นฟาสต์แทร็ก", "1 ชั่วโมง (H1) - สัญญาณมาตรฐานสากล", "1 วัน (1D) - แนวโน้มใหญ่ระยะยาว"],
        index=2
    )

if "5 นาที" in tf_choice:
    yf_interval, yf_period, tv_interval = "5m", "1d", Interval.INTERVAL_5_MINUTES
elif "15 นาที" in tf_choice:
    yf_interval, yf_period, tv_interval = "15m", "5d", Interval.INTERVAL_15_MINUTES
elif "1 ชั่วโมง" in tf_choice:
    yf_interval, yf_period, tv_interval = "1h", "1mo", Interval.INTERVAL_1_HOUR
else:
    yf_interval, yf_period, tv_interval = "1d", "3mo", Interval.INTERVAL_1_DAY

# =========================================================================
# 🎯 4. ปุ่มควบคุมสแกน (ปรับขนาดพอดีจอสัมผัสมือถือ)
# =========================================================================
col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1])
with col_btn1:
    if st.button("🔍 สแกน 1 ครั้ง", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once = True
        st.rerun()
with col_btn2:
    if st.button("🔄 Auto 1 นาที", use_container_width=True):
        st.session_state.auto_mode = True
        st.session_state.run_once = False
        st.toast("⚡ เริ่มโหมดเฝ้าจอ AI ทุกๆ 60 วินาที")
        st.rerun()
with col_btn3:
    if st.button("🛑 หยุดทำงาน", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once = False
        st.toast("🛑 หยุดระบบเรียบร้อย")
        st.rerun()

if st.session_state.auto_mode:
    st_autorefresh(interval=60000, key="trading_v7_5_pro_refresh")
    st.info("🔄 [PRO STATUS] บอทกำลังเฝ้าจอและดึงข้อมูลเรียลไทม์ให้คุณทุกๆ 60 วินาที...")

# =========================================================================
# 🧠 5. อัลกอริทึมแปลงฟอร์แมตโบรกเกอร์
# =========================================================================
def parse_asset_meta(symbol_raw):
    sym = symbol_raw.strip().upper().replace("-", "").replace("/", "")
    if sym in ["XAUUSD", "GOLD"]:
        return {"tv_sym": "XAUUSD", "tv_exch": "FX_IDC", "tv_screen": "forex", "yf_sym": "GC=F", "type": "🏆 ทองคำโลก"}
    elif any(f in sym for f in ["EURUSD", "GBPUSD", "USDJPY", "USDTHB"]):
        return {"tv_sym": sym, "tv_exch": "FX_IDC", "tv_screen": "forex", "yf_sym": f"{sym}=X", "type": "💱 Forex สากล"}
    else:
        base_crypto = sym.replace("USD", "").replace("USDT", "")
        return {"tv_sym": f"{base_crypto}USD", "tv_exch": "BINANCE", "tv_screen": "crypto", "yf_sym": f"{base_crypto}-USD", "type": "🪙 คริปโต"}

# =========================================================================
# 🧮 6. กลไกคำนวณและตรวจสอบความน่าจะเป็น (Core Engine V7.5)
# =========================================================================
if (st.session_state.run_once or st.session_state.auto_mode) and selected_assets:
    dashboard_summary = []
    detailed_results = {}

    with st.spinner('⏳ [PRO ENGINE] กำลังระดมสูตรคณิตศาสตร์และดึงข้อมูลข้ามแพลตฟอร์ม...'):
        cleaned_yf_tickers = [parse_asset_meta(a)["yf_sym"] for a in selected_assets]
        try:
            all_yf_data = yf.download(tickers=cleaned_yf_tickers, period=yf_period, interval=yf_interval, group_by='ticker', progress=False)
        except:
            all_yf_data = pd.DataFrame()

        for asset_raw in selected_assets:
            try:
                meta = parse_asset_meta(asset_raw)
                
                # ข้อมูลจาก TradingView
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

                # ข้อมูลจาก Yahoo Finance (ส่วนที่อัปเดตเพื่อแก้บั๊กกรณีเลือกตัวเดียว)
                df_yf = pd.DataFrame()
                if not all_yf_data.empty:
                    if len(selected_assets) == 1:
                        df_yf = all_yf_data.copy()
                        # แก้ไขปัญหา MultiIndex กรณีดึงข้อมูลตัวเดียวจาก yfinance
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
                
                if not df_yf.empty and len(df_yf) >= 20:
                    df_yf.dropna(subset=['Close'], inplace=True)
                    latest = df_yf.iloc[-1]
                    latest_price = float(latest['Close'])
                    
                    close_series = df_yf['Close'].astype(float)
                    df_yf['EMA20'] = EMAIndicator(close=close_series, window=20).ema_indicator()
                    df_yf['EMA50'] = EMAIndicator(close=close_series, window=50).ema_indicator()
                    df_yf['RSI'] = RSIIndicator(close=close_series, window=14).rsi()
                    
                    macd_obj = MACD(close=close_series)
                    df_yf['MACD_diff'] = macd_obj.macd_diff()

                    latest_calc = df_yf.iloc[-1]
                    rsi_val = latest_calc['RSI']
                    macd_val = latest_calc['MACD_diff']

                    # ส่วนที่ 1: Trend Score (40%)
                    if latest_calc['Close'] > latest_calc['EMA20'] and latest_calc['EMA20'] > latest_calc['EMA50']:
                        trend_score = 40.0  
                    elif latest_calc['Close'] < latest_calc['EMA20'] and latest_calc['EMA20'] < latest_calc['EMA50']:
                        trend_score = -40.0 

                    # ส่วนที่ 2: Momentum Score (30%)
                    if macd_val > 0: momentum_score += 15.0
                    else: momentum_score -= 15.0
                    if rsi_val > 50: momentum_score += 15.0
                    else: momentum_score -= 15.0

                # ส่วนที่ 3: TradingView Consensus (30%)
                if "BUY" in tv_recommend:
                    consensus_score = 30.0 if "STRONG" in tv_recommend else 15.0
                elif "SELL" in tv_recommend:
                    consensus_score = -30.0 if "STRONG" in tv_recommend else -15.0

                # สรุปผลลัพธ์สุทธิ
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
                    final_signal = "🟡 HOLD (รอ)"

                dashboard_summary.append({
                    "สินทรัพย์": meta["tv_sym"],
                    "ราคาล่าสุด": f"{latest_price:,.4f}" if latest_price < 2 else f"{latest_price:,.2f}",
                    "คำแนะนำ AI": final_signal,
                    "ความน่าจะเป็น (%)": f"{probability_pct:.1f}%",
                    "ความได้เปรียบ": "ซื้อ (Bull)" if side_verdict == "BUY" else ("ขาย (Bear)" if side_verdict == "SELL" else "ไซด์เวย์")
                })

                detailed_results[meta["tv_sym"]] = {
                    "type": meta["type"], "verdict": side_verdict, "prob": probability_pct,
                    "trend_part": abs(trend_score), "momentum_part": abs(momentum_score), "consensus_part": abs(consensus_score),
                    "rsi": rsi_val, "macd": macd_val, "tv_buy": tv_buy, "tv_sell": tv_sell, "tv_neutral": tv_neutral
                }
            except Exception as e:
                st.warning(f"⚠️ ข้าม {asset_raw} ชั่วคราวเนื่องจากสัญญาณขัดข้อง: {e}")

        # =========================================================================
        # 📊 7. ส่วนการแสดงผลตารางเรียลไทม์ (UI Rendering สำหรับหน้าจอมือถือ)
        # =========================================================================
        if dashboard_summary:
            st.write("---")
            st.subheader(f"📊 แดชบอร์ดสรุปผลความน่าจะเป็น ({tf_choice.split('-')[0].strip()})")
            
            df_dash = pd.DataFrame(dashboard_summary)
            st.dataframe(df_dash, use_container_width=True, hide_index=True)
            
            st.write("---")
            st.subheader("🔍 ตารางถอดรหัสเจาะลึกที่มาเปอร์เซ็นต์")
            
            for name, data in detailed_results.items():
                with st.expander(f"🔎 เจาะลึกสัญญาณของ: {name}"):
                    st.markdown(f"### 🎯 มติรวมให้น้ำหนักไปทางฝั่ง: **{data['prob']:.1f}%**")
                    
                    breakdown_table_data = [
                        {"แกนชี้วัดหลัก": "1. โครงสร้างเทรนด์ (EMA20/50)", "น้ำหนัก": "40%", "คะแนนที่ได้": f"{data['trend_part']:.1f}%"},
                        {"แกนชี้วัดหลัก": "2. โมเมนตัมสถิติ (RSI/MACD)", "น้ำหนัก": "30%", "คะแนนที่ได้": f"{data['momentum_part']:.1f}%"},
                        {"แกนชี้วัดหลัก": "3. มติบอทโลก (TradingView)", "น้ำหนัก": "30%", "คะแนนที่ได้": f"{data['consensus_part']:.1f}%"}
                    ]
                    st.dataframe(pd.DataFrame(breakdown_table_data), use_container_width=True, hide_index=True)
                    
                    m_col1, m_col2, m_col3 = st.columns(3)
                    with m_col1: st.metric("RSI (14)", f"{data['rsi']:.1f}")
                    with m_col2: st.metric("MACD Diff", f"{data['macd']:.4f}")
                    with m_col3: st.metric("บอทโลก (B/S/H)", f"{data['tv_buy']}/{data['tv_sell']}/{data['tv_neutral']}")
                        
    st.session_state.run_once = False
elif not selected_assets:
    st.warning("ℹ️ สัญญาณว่างเปล่า: กรุณาเลือกตัวย่อจากกล่องด้านบนอย่างน้อย 1 ตัว เพื่อประมวลผล")
