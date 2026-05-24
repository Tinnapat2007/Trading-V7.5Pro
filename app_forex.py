import pandas as pd
import yfinance as yf
import ta
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from ta.volatility import AverageTrueRange
import streamlit as st
from tradingview_ta import TA_Handler, Interval
from streamlit_autorefresh import st_autorefresh
import requests

# =========================================================================
# ⚙️ 1. โครงสร้างพื้นฐานหน้าเว็บ AI Forex Sniper Pro (v1.0)
# =========================================================================
st.set_page_config(page_title="AI Forex Sniper Pro", page_icon="💱", layout="wide")

st.title("💱 AI Forex Sniper Pro (Dedicated Version)")
st.write("ระบบวิเคราะห์มติรวมพหุภาคีและวัดความผันผวน Pips จากสถาบันการเงินระดับโลก ค้นหาคู่เงินได้ทุกคู่ 24/5")

# ระบบจำสถานะหน้าเว็บ
if 'auto_mode' not in st.session_state:
    st.session_state.auto_mode = False
if 'run_once' not in st.session_state:
    st.session_state.run_once = False

# =========================================================================
# 🔍 2. ฟังก์ชันแปล็กและดึงข้อมูล Macro Currency Strength
# =========================================================================
def parse_forex_meta(symbol_raw):
    """ แปลงคำค้นหาคู่เงินให้เข้าชุดกับ TradingView และ Yahoo Finance ได้แม่นยำทั่วโลก """
    # ทำความสะอาดข้อมูลเบื้องต้น ลบเครื่องหมายคั่นกลางออก
    sym = symbol_raw.strip().upper().replace("-", "").replace("/", "")
    
    # ดักจับกรณีผู้ใช้พิมพ์สลับหรือพิมพ์เฉพาะสกุลเงินหลัก
    if len(sym) == 6:
        base = sym[:3]
        quote = sym[3:]
    else:
        # หากใส่มาไม่ครบ 6 ตัวอักษร ให้ผูกกับดอลลาร์สหรัฐ (USD) เป็นค่าอ้างอิงหลัก
        base = sym if sym != "USD" else "EUR"
        quote = "USD"
        sym = f"{base}{quote}"

    return {
        "tv_sym": sym,
        "tv_exch": "FX_IDC", # แหล่งข้อมูลฟอเร็กซ์มาตรฐานความแม่นยำสูงบน TV
        "tv_screen": "forex",
        "yf_sym": f"{sym}=X", # ฟอร์แมตคู่เงินของ Yahoo Finance
        "clean_ticker": sym,
        "base_currency": base
    }

# =========================================================================
# 🛠️ 3. แผงควบคุมคู่เงินและกรอบเวลา (Responsive Stack)
# =========================================================================
st.subheader("⚙️ ตัวเลือกคู่เงินและกรอบเวลาฟอเร็กซ์")
col_setting1, col_setting2 = st.columns([2, 1])

with col_setting1:
    # ตั้งค่าเริ่มต้นเป็นคู่เงินหลักของโลก (Major Pairs) และคู่เงินบาท
    user_input = st.text_input(
        "⌨️ พิมพ์คู่เงินที่ต้องการสแกน (คั่นด้วยเครื่องหมายจุลภาค `,` เช่น EURUSD, GBPUSD, USDJPY, USDTHB):",
        value="EURUSD, GBPUSD, USDJPY, USDTHB, AUDUSD"
    )
    # แปลงข้อมูลอินพุตเป็น List
    selected_assets = [x.strip().upper() for x in user_input.split(",") if x.strip()]

with col_setting2:
    tf_choice = st.selectbox(
        "⏱️ กรอบเวลาสแกน (Timeframe):",
        options=["5 นาที (M5) - สัญญาณสคัลปิ้งเก็งกำไรไว", "15 นาที (M15) - เล่นสั้นฟาสต์แทร็ก", "1 ชั่วโมง (H1) - สัญญาณมาตรฐานเดย์เทรด", "1 วัน (1D) - รันเทรนด์ใหญ่ระยะยาว"],
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
# 🎯 4. ปุ่มควบคุมสแกน
# =========================================================================
col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1])
with col_btn1:
    if st.button("🔍 เริ่มสแกนฟอเร็กซ์ทันที", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once = True
        st.rerun()
with col_btn2:
    if st.button("🔄 Auto Refresh 1 นาที", use_container_width=True):
        st.session_state.auto_mode = True
        st.session_state.run_once = False
        st.toast("⚡ เริ่มระบบเฝ้าอัตราแลกเปลี่ยนเรียลไทม์")
        st.rerun()
with col_btn3:
    if st.button("🛑 หยุดสแกน", use_container_width=True):
        st.session_state.auto_mode = False
        st.session_state.run_once = False
        st.toast("🛑 หยุดระบบเรียบร้อย")
        st.rerun()

if st.session_state.auto_mode:
    st_autorefresh(interval=60000, key="forex_dedicated_refresh")
    st.info("🔄 [FOREX STATUS] บอทกำลังดึงข้อมูลด่วนจากศูนย์กลางการเงินโลกและอัปเดตทุกๆ 60 วินาที...")

# =========================================================================
# 🧮 5. กลไก Consensus Engine สำหรับ Forex (คำนวณทศนิยม Pips + Volatility)
# =========================================================================
if (st.session_state.run_once or st.session_state.auto_mode) and selected_assets:
    dashboard_summary = []
    detailed_results = {}

    with st.spinner('⏳ [FOREX ENGINE] กำลังควบรวมข้อมูลจากสถาบันการเงิน FX_IDC และ Yahoo Finance...'):
        cleaned_yf_tickers = [parse_forex_meta(a)["yf_sym"] for a in selected_assets]
        try:
            all_yf_data = yf.download(tickers=cleaned_yf_tickers, period=yf_period, interval=yf_interval, group_by='ticker', progress=False)
        except:
            all_yf_data = pd.DataFrame()

        for asset_raw in selected_assets:
            try:
                meta = parse_forex_meta(asset_raw)
                
                # 1. แหล่งที่ 1: ดึงมติประชามติบอทโลก (TradingView FX_IDC API)
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

                # 2. แหล่งที่ 2: ประมวลผลเทคนิคอลเชิงลึกและค่าความผันผวน (Yahoo Finance)
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
                rsi_val, macd_val, atr_val = 50.0, 0.0, 0.0
                
                if not df_yf.empty and len(df_yf) >= 20:
                    df_yf.dropna(subset=['Close'], inplace=True)
                    latest_price = float(df_yf['Close'].iloc[-1])
                    
                    close_series = df_yf['Close'].astype(float)
                    high_series = df_yf['High'].astype(float)
                    low_series = df_yf['Low'].astype(float)
                    
                    df_yf['EMA20'] = EMAIndicator(close=close_series, window=20).ema_indicator()
                    df_yf['EMA50'] = EMAIndicator(close=close_series, window=50).ema_indicator()
                    df_yf['RSI'] = RSIIndicator(close=close_series, window=14).rsi()
                    
                    # คำนวณความผันผวนและระยะวิ่งปลอดภัยด้วย ATR (Average True Range) เจาะลึกสาย Forex
                    df_yf['ATR'] = AverageTrueRange(high=high_series, low=low_series, close=close_series, window=14).average_true_range()
                    
                    macd_obj = MACD(close=close_series)
                    df_yf['MACD_diff'] = macd_obj.macd_diff()

                    latest_calc = df_yf.iloc[-1]
                    rsi_val = latest_calc['RSI']
                    macd_val = latest_calc['MACD_diff']
                    atr_val = latest_calc['ATR']

                    # แกนที่ 1: Trend Score (40%) -> วิเคราะห์การยืนระยะเหนือโครงสร้างราคา
                    if latest_calc['Close'] > latest_calc['EMA20'] and latest_calc['EMA20'] > latest_calc['EMA50']:
                        trend_score = 40.0  
                    elif latest_calc['Close'] < latest_calc['EMA20'] and latest_calc['EMA20'] < latest_calc['EMA50']:
                        trend_score = -40.0 

                    # แกนที่ 2: Momentum Score (30%) -> วัดความแรงอินดิเคเตอร์เชิงปริมาณ
                    if macd_val > 0: momentum_score += 15.0
                    else: momentum_score -= 15.0
                    if rsi_val > 50: momentum_score += 15.0
                    else: momentum_score -= 15.0

                # แกนที่ 3: TradingView Consensus (30%) -> สัญญาณสรุปคลังข้อมูลใหญ่
                if "BUY" in tv_recommend:
                    consensus_score = 30.0 if "STRONG" in tv_recommend else 15.0
                elif "SELL" in tv_recommend:
                    consensus_score = -30.0 if "STRONG" in tv_recommend else -15.0

                # ประมวลผลคะแนนสุทธิเพื่อแปลงเป็นเปอร์เซ็นต์ความแม่นยำ
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

                # คำนวณระยะสเปรดและจุดกรอบผันผวนโดยประมาณตามทศนิยมของคู่นั้นๆ
                pip_multiplier = 100 if "JPY" in meta["clean_ticker"] else 10000
                atr_pips = atr_val * pip_multiplier

                dashboard_summary.append({
                    "คู่เงิน": meta["clean_ticker"],
                    "ราคาล่าสุด": f"{latest_price:,.4f}" if "JPY" not in meta["clean_ticker"] else f"{latest_price:,.2f}",
                    "ทิศทาง AI": final_signal,
                    "น้ำหนักความมั่นใจ": f"{probability_pct:.1f}%",
                    "ระยะแกว่งตัว (Pips)": f"{atr_pips:.1f} Pips",
                    "แนวโน้มฝั่ง": "ขาขึ้น (Bullish)" if side_verdict == "BUY" else ("ขาลง (Bearish)" if side_verdict == "SELL" else "ไร้ทิศทาง")
                })

                detailed_results[meta["clean_ticker"]] = {
                    "verdict": side_verdict, "prob": probability_pct,
                    "trend_part": abs(trend_score), "momentum_part": abs(momentum_score), "consensus_part": abs(consensus_score),
                    "rsi": rsi_val, "macd": macd_val, "atr": atr_val, "atr_pips": atr_pips,
                    "tv_buy": tv_buy, "tv_sell": tv_sell, "tv_neutral": tv_neutral
                }
            except Exception as e:
                st.warning(f"⚠️ ดึงข้อมูลคู่เงิน {asset_raw} ขัดข้องชั่วคราว: {e}")

        # =========================================================================
        # 📊 6. ส่วนการแสดงผลแดชบอร์ดสรุป (UI Rendering)
        # =========================================================================
        if dashboard_summary:
            st.write("---")
            st.subheader(f"📊 แดชบอร์ดมติพหุภาคีตลาดอัตราแลกเปลี่ยน ({tf_choice.split('-')[0].strip()})")
            
            df_dash = pd.DataFrame(dashboard_summary)
            st.dataframe(df_dash, use_container_width=True, hide_index=True)
            
            st.write("---")
            st.subheader("🔍 ถอดสูตรคณิตศาสตร์และระยะทำกำไรสากล")
            
            for name, data in detailed_results.items():
                with st.expander(f"🔎 เจาะลึกโครงสร้างสัญญาณและวิเคราะห์ระยะวิ่ง: {name}"):
                    st.markdown(f"### 🎯 น้ำหนักมติฝั่งระบบอัตโนมัติ: **{data['prob']:.1f}%**")
                    
                    breakdown_table_data = [
                        {"แกนชี้วัดความแม่นยำ": "1. แนวโน้มโครงสร้างหลัก (EMA 20/50)", "น้ำหนักสัดส่วน": "40%", "คะแนนดิบที่คำนวณได้": f"{data['trend_part']:.1f}%"},
                        {"แกนชี้วัดความแม่นยำ": "2. โมเมนตัมกำลังซื้อขาย (RSI/MACD)", "น้ำหนักสัดส่วน": "30%", "คะแนนดิบที่คำนวณได้": f"{data['momentum_part']:.1f}%"},
                        {"แกนชี้วัดความแม่นยำ": "3. สัญญาณจากกลุ่มบอทสถาบัน (FX_IDC)", "น้ำหนักสัดส่วน": "30%", "คะแนนดิบที่คำนวณได้": f"{data['consensus_part']:.1f}%"}
                    ]
                    st.dataframe(pd.DataFrame(breakdown_table_data), use_container_width=True, hide_index=True)
                    
                    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
                    with m_col1: st.metric("RSI (14)", f"{data['rsi']:.1f}")
                    with m_col2: st.metric("MACD Diff", f"{data['macd']:.5f}")
                    with m_col3: st.metric("กรอบผันผวนบาร์นี้ (Pips)", f"{data['atr_pips']:.1f}")
                    with m_col4: st.metric("เสียงโหวตบอท (B/S/N)", f"{data['tv_buy']}/{data['tv_sell']}/{data['tv_neutral']}")
                    
                    st.caption(f"💡 คำแนะนำในการตั้งระยะทำกำไร (TP/SL): อ้างอิงจากค่าความผันผวนปัจจุบัน คู่เงิน {name} มีกรอบการวิ่งเฉลี่ยในไทม์เฟรมนี้อยู่ที่ประมาณ {data['atr_pips']:.1f} Pips")
                    
    st.session_state.run_once = False
elif not selected_assets:
    st.warning("ℹ️ สัญญาณว่างเปล่า: กรุณากรอกชื่อย่อสกุลเงินคู่ที่ต้องการตรวจสอบในกล่องระบุข้อความด้านบน")
