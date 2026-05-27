        st.warning("ติดตั้ง `streamlit-autorefresh` เพื่อให้ Auto 60s ทำงาน")
    st.info(f"Auto Refresh ทุก 60 วินาที | ล้มเหลว {st.session_state.fail_count}/{MAX_AUTO_FAIL}")

allowed_markets = set(market_choices)
raw_symbols = [x.strip() for x in re.split(r"[,|\n]", user_input) if x.strip()]
assets: list[Asset] = []
rejected: list[str] = []

for raw_symbol in raw_symbols:
    asset, error = normalize_asset(raw_symbol, allowed_markets)
    if asset:
        assets.append(asset)
    elif error:
        rejected.append(error)

assets = dedupe_assets(assets)
if len(assets) > MAX_ASSETS:
    st.warning(f"จำกัด {MAX_ASSETS} symbols ต่อรอบ ระบบตัดส่วนเกินออก")
    assets = assets[:MAX_ASSETS]

if rejected:
    st.error("Symbols บางตัวใช้ไม่ได้:\n" + "\n".join(f"- {item}" for item in rejected))

tf = TF_MAP[tf_key]

if (st.session_state.run_once or st.session_state.auto_mode) and assets:
    calls: list[dict] = []
    success_count = 0

    tickers = tuple(asset.yf_symbol for asset in assets)
    is_single = len(assets) == 1

    progress = st.progress(0, text="กำลังโหลดข้อมูลจาก Yahoo Finance...")
    with st.spinner("กำลังดาวน์โหลด OHLCV..."):
        all_yf = fetch_yfinance_cached(tickers, period=tf["period"], interval=tf["interval"])

    for index, asset in enumerate(assets):
        progress.progress((index + 1) / len(assets), text=f"กำลังวิเคราะห์ {asset.display}...")
        df = extract_df(all_yf, asset.yf_symbol, is_single)
        if df.empty:
            df = fallback_single_download(asset.yf_symbol, tf["period"], tf["interval"])

        state = compute_market_state(df)
        call = build_trade_call(asset, state)
        calls.append(call)
        if state.get("is_valid"):
            success_count += 1
        time.sleep(0.05)

    progress.empty()

    if success_count == 0:
        st.session_state.fail_count += 1
        if st.session_state.fail_count >= MAX_AUTO_FAIL:
            st.session_state.auto_mode = False
            st.error("API ล้มเหลวหลายครั้งติด ระบบหยุด Auto Refresh แล้ว")
    else:
        st.session_state.fail_count = 0

    ranked = sort_trade_calls(calls)
    actionable = [call for call in ranked if call["side"] in {"LONG", "SHORT"}]

    st.divider()
    st.subheader("ตัวที่น่าเทรดที่สุด")

    best = actionable[0] if actionable else ranked[0]
    best_asset = best["asset"]
    best_state = best["state"]
    st.markdown(
        f"""
<div class="trade-card">
  <div class="trade-title">{best["signal"]} · {best_asset.display} · {best_asset.market}</div>
  <div class="trade-meta">
    คะแนน {best["quality_score"]:.0f}/100 · มั่นใจ {best["confidence"]:.0f}% · {best["reason"]}<br>
    ราคา {price_format(best_asset.display, best_asset.market, best_state["price"])} ·
    ATR {atr_display(best_asset, best_state["atr"], best_state["price"])} ·
    สภาวะ: {best["run_state"]}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    rows = []
    for call in ranked[:top_n]:
        asset = call["asset"]
        state = call["state"]
        rows.append(
            {
                "อันดับ": len(rows) + 1,
                "Symbol": asset.display,
                "ตลาด": asset.market,
                "สัญญาณ": call["signal"],
                "คะแนน": f"{call['quality_score']:.0f}",
                "มั่นใจ": f"{call['confidence']:.0f}%",
                "ราคา": price_format(asset.display, asset.market, state["price"]),
                "ATR": atr_display(asset, state["atr"], state["price"]),
                "ADX": f"{state['adx']:.1f}",
                "RSI": f"{state['rsi']:.1f}",
                "สภาวะ": call["run_state"],
            }
        )

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("เจาะคะแนนรายตัว")

    for call in ranked[:top_n]:
        asset = call["asset"]
        state = call["state"]
        with st.expander(f"{call['signal']} · {asset.display} · คะแนน {call['quality_score']:.0f}/100"):
            for warning in state.get("warnings", []):
                st.caption(f"⚠️ {warning}")

            m1, m2, m3 = st.columns(3)
            with m1:
                st.metric("Trend", f"{state['trend_score']:+.0f}")
                st.metric("RSI", f"{state['rsi']:.1f}")
            with m2:
                st.metric("Momentum", f"{state['momentum_score']:+.0f}")
                st.metric("ADX", f"{state['adx']:.1f}")
            with m3:
                st.metric("Swing", f"{state['swing_score']:+.0f}")
                st.metric("ATR", atr_display(asset, state["atr"], state["price"]))

            score_df = pd.DataFrame(
                [
                    {"หัวข้อ": "Trend EMA20/50/200 + slope", "คะแนน": f"{state['trend_score']:+.1f}"},
                    {"หัวข้อ": "RSI + MACD Momentum", "คะแนน": f"{state['momentum_score']:+.1f}"},
                    {"หัวข้อ": "Swing / Volatility / ADX", "คะแนน": f"{state['swing_score']:+.1f}"},
                    {"หัวข้อ": "ราคาติดกรอบ / congestion", "คะแนน": f"{state['congestion_score']:+.1f}"},
                    {"หัวข้อ": "Breakout จากกรอบ 50 แท่ง", "คะแนน": f"{state['breakout_score']:+.1f}"},
                ]
            )
            st.dataframe(score_df, use_container_width=True, hide_index=True)

            if call["side"] in {"LONG", "SHORT"} and state["atr"] > 0:
                st.info(
                    f"แผนตัวอย่าง: {call['side']} | TP1 ประมาณ 1x ATR, TP2 ประมาณ 2x ATR, "
                    f"SL ประมาณ 0.7x ATR | ใช้เป็นกรอบวางแผน ไม่ใช่คำสั่งเทรด"
                )
            else:
                st.caption("ยังไม่ใช่จังหวะเด่น ระบบแนะนำให้รอราคาหลุดกรอบหรือมี momentum ชัดขึ้น")

    st.session_state.run_once = False

elif not assets:
    st.info("กรอก symbol หรือเลือกตลาดที่ต้องการสแกนก่อน แล้วกด `สแกนทันที`")
