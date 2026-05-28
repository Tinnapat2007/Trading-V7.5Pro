"""
AI Trade Hunter Pro
===================
Streamlit scanner for Crypto, Forex, and Gold.

Data sources:
  OHLCV       -> Yahoo Finance
  Indicators  -> ta library (RSI, MACD, EMA, ATR, Bollinger Bands, ADX)
  Signals     -> Local scoring model focused on trend, swing, congestion, and momentum

Run:
  streamlit run app.py
"""

from __future__ import annotations

import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    st_autorefresh = None

try:
    from tradingview_ta import Interval, TA_Handler
except Exception:
    Interval = None
    TA_Handler = None


# ============================================================================
# Config
# ============================================================================
APP_VERSION = "v3.2 Market Hours"
MAX_ASSETS = 20
MIN_BARS_REQUIRED = 80
YF_DOWNLOAD_TIMEOUT = 35
BINANCE_TIMEOUT = 15
TV_TIMEOUT = 12
NEWS_TIMEOUT = 10
MAX_AUTO_FAIL = 3

NEWS_RISK_KEYWORDS = {
    "attack": 18, "attacks": 18, "missile": 18, "strike": 18, "war": 18,
    "invasion": 20, "ceasefire": 16, "truce": 14, "conflict": 14,
    "sanction": 14, "sanctions": 14, "tariff": 12,
    "fed": 14, "fomc": 16, "rate decision": 18, "interest rate": 14,
    "cpi": 16, "inflation": 14, "nfp": 16, "payrolls": 16,
    "gdp": 10, "recession": 14, "default": 18, "bank crisis": 18,
    "etf": 8, "sec": 10, "lawsuit": 10, "hack": 18, "exploit": 18,
    "หยุดยิง": 16, "โจมตี": 18, "สงคราม": 18, "เงินเฟ้อ": 14,
    "ดอกเบี้ย": 14, "เฟด": 14,
}

DEFAULT_FOREX = "EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD, EURJPY"
DEFAULT_CRYPTO = "BTC-USD, ETH-USD, SOL-USD, BNB-USD, XRP-USD, ADA-USD, DOGE-USD"
DEFAULT_GOLD = "XAUUSD, GC=F"

FOREX_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "NOK", "SEK", "DKK",
    "SGD", "HKD", "CNY", "CNH", "TWD", "KRW", "THB", "MYR", "IDR", "PHP", "VND", "INR",
    "SAR", "AED", "QAR", "KWD", "BHD", "OMR", "EGP", "ZAR", "NGN", "KES", "GHS",
    "PLN", "HUF", "CZK", "RUB", "UAH", "ILS", "TRY", "MXN", "BRL", "CLP", "COP", "PEN", "ARS",
}

CRYPTO_ALIASES = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "BNB": "BNB-USD",
    "XRP": "XRP-USD",
    "ADA": "ADA-USD",
    "DOGE": "DOGE-USD",
    "AVAX": "AVAX-USD",
    "LINK": "LINK-USD",
    "DOT": "DOT-USD",
    "MATIC": "MATIC-USD",
    "LTC": "LTC-USD",
}

GOLD_ALIASES = {
    "XAU": "GC=F",
    "XAUUSD": "GC=F",
    "GOLD": "GC=F",
    "GC": "GC=F",
    "GC=F": "GC=F",
}

TF_MAP = {
    "5m": {"period": "5d", "interval": "5m", "binance": "5m", "label": "5m Scalping"},
    "15m": {"period": "10d", "interval": "15m", "binance": "15m", "label": "15m Intraday"},
    "1h": {"period": "90d", "interval": "1h", "binance": "1h", "label": "1h Day Trade"},
    "1d": {"period": "1y", "interval": "1d", "binance": "1d", "label": "1D Position"},
}

HIGHER_TF = {
    "5m": "15m",
    "15m": "1h",
    "1h": "1d",
    "1d": "1d",
}

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("trade_hunter")


# ============================================================================
# Models
# ============================================================================
@dataclass(frozen=True)
class Asset:
    display: str
    yf_symbol: str
    market: str


# ============================================================================
# Helpers
# ============================================================================
def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def clean_token(raw: str) -> str:
    return raw.strip().upper().replace("/", "").replace(" ", "")


def normalize_asset(raw: str, allowed_markets: set[str]) -> tuple[Asset | None, str | None]:
    token = clean_token(raw)
    if not token:
        return None, None

    if "Gold" in allowed_markets and token in GOLD_ALIASES:
        return Asset(display="XAUUSD", yf_symbol=GOLD_ALIASES[token], market="Gold"), None

    if "Forex" in allowed_markets:
        forex_symbol = re.sub(r"[^A-Z]", "", token)
        if len(forex_symbol) == 6:
            base = forex_symbol[:3]
            quote = forex_symbol[3:]
            if base in FOREX_CURRENCIES and quote in FOREX_CURRENCIES and base != quote:
                return Asset(display=forex_symbol, yf_symbol=f"{forex_symbol}=X", market="Forex"), None

    if "Crypto" in allowed_markets:
        crypto_symbol = token
        if crypto_symbol in CRYPTO_ALIASES:
            crypto_symbol = CRYPTO_ALIASES[crypto_symbol]
        elif re.fullmatch(r"[A-Z0-9]{2,12}USD", crypto_symbol):
            crypto_symbol = f"{crypto_symbol[:-3]}-USD"
        elif re.fullmatch(r"[A-Z0-9]{2,12}-USD", crypto_symbol):
            crypto_symbol = crypto_symbol

        if crypto_symbol.endswith("-USD"):
            display = crypto_symbol.replace("-USD", "USD")
            return Asset(display=display, yf_symbol=crypto_symbol, market="Crypto"), None

    return None, f"`{raw}` ไม่ตรงกับตลาดที่เลือก หรือ Yahoo Finance ไม่รองรับ"


def dedupe_assets(assets: Iterable[Asset]) -> list[Asset]:
    seen: set[str] = set()
    output: list[Asset] = []
    for asset in assets:
        if asset.yf_symbol not in seen:
            output.append(asset)
            seen.add(asset.yf_symbol)
    return output


def price_format(symbol: str, market: str, price: float) -> str:
    if market == "Forex" and "JPY" not in symbol:
        return f"{price:,.5f}"
    if market == "Forex":
        return f"{price:,.3f}"
    if market == "Crypto" and price < 10:
        return f"{price:,.4f}"
    return f"{price:,.2f}"


def pip_or_point_label(market: str) -> str:
    if market == "Forex":
        return "Pips"
    if market == "Gold":
        return "Points"
    return "%"


def atr_display(asset: Asset, atr: float, price: float) -> str:
    if atr <= 0 or price <= 0:
        return "N/A"
    if asset.market == "Forex":
        mult = 100 if "JPY" in asset.display else 10_000
        return f"{atr * mult:.1f} Pips"
    if asset.market == "Gold":
        return f"{atr:.2f} Points"
    return f"{(atr / price) * 100:.2f}%"


def next_weekday_time(now_ny: datetime, weekday: int, target_time: dt_time) -> datetime:
    days_ahead = (weekday - now_ny.weekday()) % 7
    candidate = datetime.combine(
        (now_ny + timedelta(days=days_ahead)).date(),
        target_time,
        tzinfo=now_ny.tzinfo,
    )
    if candidate <= now_ny:
        candidate += timedelta(days=7)
    return candidate


def market_hours_info(asset: Asset) -> dict:
    bkk_tz = ZoneInfo("Asia/Bangkok")
    ny_tz = ZoneInfo("America/New_York")
    now_bkk = datetime.now(bkk_tz)
    now_ny = now_bkk.astimezone(ny_tz)
    weekday = now_ny.weekday()

    def fmt(dt_value: datetime) -> str:
        return dt_value.astimezone(bkk_tz).strftime("%a %H:%M BKK")

    if asset.market == "Crypto":
        return {
            "status": "OPEN",
            "status_th": "เปิด 24/7",
            "next_event": "ไม่มีเวลาปิดประจำ",
            "hours": "เปิดทุกวัน 24 ชั่วโมง",
            "closed": "ไม่มีวันปิดประจำ",
            "note": "บาง exchange อาจปิดซ่อมบำรุงเป็นครั้งคราว",
        }

    if asset.market == "Forex":
        open_time = dt_time(17, 0)
        is_open = (
            (weekday == 6 and now_ny.time() >= open_time)
            or weekday in {0, 1, 2, 3}
            or (weekday == 4 and now_ny.time() < open_time)
        )
        if is_open:
            close_dt = next_weekday_time(now_ny, 4, open_time)
            next_event = f"ปิดถัดไป {fmt(close_dt)}"
            status = "OPEN"
            status_th = "เปิดอยู่"
        else:
            open_dt = next_weekday_time(now_ny, 6, open_time)
            next_event = f"เปิดถัดไป {fmt(open_dt)}"
            status = "CLOSED"
            status_th = "ปิดอยู่"
        return {
            "status": status,
            "status_th": status_th,
            "next_event": next_event,
            "hours": "Sun 17:00 ถึง Fri 17:00 เวลา New York",
            "closed": "ปิดสุดสัปดาห์: Fri 17:00 ถึง Sun 17:00 เวลา New York",
            "note": "เวลาอาจเลื่อนตาม DST และวันหยุดธนาคาร",
        }

    if asset.market == "Gold":
        open_time = dt_time(18, 0)
        close_time = dt_time(17, 0)
        in_weekly_session = (
            (weekday == 6 and now_ny.time() >= open_time)
            or weekday in {0, 1, 2, 3}
            or (weekday == 4 and now_ny.time() < close_time)
        )
        in_daily_break = weekday in {0, 1, 2, 3} and close_time <= now_ny.time() < open_time
        is_open = in_weekly_session and not in_daily_break

        if is_open:
            today_close = datetime.combine(now_ny.date(), close_time, tzinfo=ny_tz)
            if now_ny.time() >= close_time:
                today_close += timedelta(days=1)
            if today_close.weekday() > 4:
                today_close = next_weekday_time(now_ny, 4, close_time)
            next_event = f"ปิด/พักถัดไป {fmt(today_close)}"
            status = "OPEN"
            status_th = "เปิดอยู่"
        else:
            if in_daily_break:
                next_open = datetime.combine(now_ny.date(), open_time, tzinfo=ny_tz)
            else:
                next_open = next_weekday_time(now_ny, 6, open_time)
            next_event = f"เปิดถัดไป {fmt(next_open)}"
            status = "CLOSED"
            status_th = "ปิด/พักตลาด"
        return {
            "status": status,
            "status_th": status_th,
            "next_event": next_event,
            "hours": "CME/Globex Gold: Sun-Fri 18:00-17:00 เวลา New York",
            "closed": "พักทุกวัน 17:00-18:00 New York และปิดสุดสัปดาห์",
            "note": "ใช้ตาราง Globex ทั่วไป ไม่รวมวันหยุดพิเศษของ CME",
        }

    return {
        "status": "N/A",
        "status_th": "ไม่ทราบ",
        "next_event": "N/A",
        "hours": "N/A",
        "closed": "N/A",
        "note": "",
    }


def crypto_to_binance_symbol(asset: Asset) -> str:
    return asset.yf_symbol.replace("-USD", "USDT").replace("-", "")


def tradingview_interval(tf_key: str):
    if Interval is None:
        return None
    return {
        "5m": Interval.INTERVAL_5_MINUTES,
        "15m": Interval.INTERVAL_15_MINUTES,
        "1h": Interval.INTERVAL_1_HOUR,
        "1d": Interval.INTERVAL_1_DAY,
    }.get(tf_key, Interval.INTERVAL_1_HOUR)


def tradingview_exchange(asset: Asset) -> tuple[str, str, str] | None:
    if asset.market == "Crypto":
        return asset.display.replace("USD", "USDT"), "BINANCE", "crypto"
    if asset.market == "Forex":
        return asset.display, "FX_IDC", "forex"
    if asset.market == "Gold":
        return "XAUUSD", "OANDA", "forex"
    return None


def tv_recommendation_score(recommendation: str) -> float:
    rec = recommendation.upper().replace("_", " ")
    if "STRONG BUY" in rec:
        return 18.0
    if rec == "BUY":
        return 10.0
    if "STRONG SELL" in rec:
        return -18.0
    if rec == "SELL":
        return -10.0
    return 0.0


def news_query_for_asset(asset: Asset) -> str:
    if asset.market == "Crypto":
        coin = asset.display.replace("USD", "")
        return f"{coin} crypto OR {coin} price"
    if asset.market == "Forex":
        base = asset.display[:3]
        quote = asset.display[3:]
        return f"{base} {quote} forex central bank inflation interest rate"
    if asset.market == "Gold":
        return "gold XAUUSD price Fed inflation war ceasefire"
    return asset.display


def parse_news_date(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def assess_news_risk(items: list[dict]) -> dict:
    risk_score = 0
    matched: list[str] = []

    for item in items:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        for keyword, score in NEWS_RISK_KEYWORDS.items():
            if keyword.lower() in text:
                risk_score += score
                matched.append(keyword)

    risk_score = int(clamp(risk_score, 0, 100))
    unique_matches = sorted(set(matched))[:8]

    if risk_score >= 45:
        label = "HIGH"
        trade_action = "News Guard: รอข่าวนิ่งก่อน"
        score_adjust = -28.0
    elif risk_score >= 22:
        label = "MEDIUM"
        trade_action = "News Guard: ลดขนาดไม้/รอยืนยัน"
        score_adjust = -12.0
    elif risk_score > 0:
        label = "LOW"
        trade_action = "News Guard: มีข่าวประกอบ"
        score_adjust = -4.0
    else:
        label = "CALM"
        trade_action = "News Guard: ข่าวไม่แรง"
        score_adjust = 0.0

    return {
        "risk_score": risk_score,
        "risk_label": label,
        "score_adjust": score_adjust,
        "keywords": ", ".join(unique_matches) if unique_matches else "-",
        "trade_action": trade_action,
    }


# ============================================================================
# Data
# ============================================================================
@st.cache_data(ttl=300, show_spinner=False)
def fetch_google_news_cached(query: str, limit: int = 5) -> dict:
    url = "https://news.google.com/rss/search"
    params = {"q": query, "hl": "th", "gl": "TH", "ceid": "TH:th"}
    try:
        response = requests.get(url, params=params, timeout=NEWS_TIMEOUT)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items: list[dict] = []
        for item in root.findall(".//item")[:limit]:
            published_raw = item.findtext("pubDate", default="")
            published = parse_news_date(published_raw)
            items.append(
                {
                    "title": item.findtext("title", default=""),
                    "link": item.findtext("link", default=""),
                    "source": item.findtext("source", default="Google News"),
                    "published": published.isoformat() if published else published_raw,
                    "summary": item.findtext("description", default=""),
                }
            )
        risk = assess_news_risk(items)
        return {"items": items, **risk}
    except Exception as exc:
        logger.warning("Google News failed for %s: %s", query, exc)
        return {
            "items": [],
            "risk_score": 0,
            "risk_label": "N/A",
            "score_adjust": 0.0,
            "keywords": "-",
            "trade_action": "News unavailable",
        }


def fetch_asset_news(asset: Asset, enabled: bool) -> dict:
    if not enabled:
        return {
            "items": [],
            "risk_score": 0,
            "risk_label": "OFF",
            "score_adjust": 0.0,
            "keywords": "-",
            "trade_action": "News Guard off",
        }
    return fetch_google_news_cached(news_query_for_asset(asset))


@st.cache_data(ttl=25, show_spinner=False)
def fetch_binance_klines_cached(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        response = requests.get(url, params=params, timeout=BINANCE_TIMEOUT)
        response.raise_for_status()
        rows = response.json()
        df = pd.DataFrame(
            rows,
            columns=[
                "OpenTime", "Open", "High", "Low", "Close", "Volume",
                "CloseTime", "QuoteVolume", "TradeCount", "TakerBuyBase",
                "TakerBuyQuote", "Ignore",
            ],
        )
        df["Datetime"] = pd.to_datetime(df["OpenTime"], unit="ms", utc=True)
        df = df.set_index("Datetime")
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    except Exception as exc:
        logger.warning("Binance klines failed for %s: %s", symbol, exc)
        return pd.DataFrame()


@st.cache_data(ttl=55, show_spinner=False)
def fetch_yfinance_cached(tickers_tuple: tuple[str, ...], period: str, interval: str) -> pd.DataFrame:
    tickers = list(tickers_tuple)

    def _download() -> pd.DataFrame:
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
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(_download).result(timeout=YF_DOWNLOAD_TIMEOUT)
    except FuturesTimeoutError:
        logger.error("Yahoo Finance download timed out")
    except Exception as exc:
        logger.error("Yahoo Finance download failed: %s", exc)
    return pd.DataFrame()


def extract_df(all_yf: pd.DataFrame, yf_symbol: str, is_single: bool) -> pd.DataFrame:
    if all_yf is None or all_yf.empty:
        return pd.DataFrame()

    field_names = {"Open", "High", "Low", "Close", "Volume", "Adj Close"}

    try:
        if is_single:
            df = all_yf.copy()
            if isinstance(df.columns, pd.MultiIndex):
                lvl0 = set(df.columns.get_level_values(0))
                df.columns = df.columns.get_level_values(0) if lvl0 & field_names else df.columns.get_level_values(1)
        else:
            if not isinstance(all_yf.columns, pd.MultiIndex):
                return pd.DataFrame()
            lvl0 = set(all_yf.columns.get_level_values(0))
            if lvl0 & field_names:
                swapped = all_yf.swaplevel(axis=1)
                if yf_symbol not in swapped.columns.get_level_values(0):
                    return pd.DataFrame()
                df = swapped[yf_symbol].copy()
            else:
                if yf_symbol not in lvl0:
                    return pd.DataFrame()
                df = all_yf[yf_symbol].copy()

        needed = [c for c in ["Open", "High", "Low", "Close"] if c in df.columns]
        return df.dropna(subset=needed)
    except Exception as exc:
        logger.error("extract_df failed for %s: %s", yf_symbol, exc)
        return pd.DataFrame()


def fallback_single_download(yf_symbol: str, period: str, interval: str) -> pd.DataFrame:
    try:
        df = yf.download(
            tickers=yf_symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        needed = [c for c in ["Open", "High", "Low", "Close"] if c in df.columns]
        return df.dropna(subset=needed)
    except Exception as exc:
        logger.error("fallback download failed for %s: %s", yf_symbol, exc)
        return pd.DataFrame()


@st.cache_data(ttl=55, show_spinner=False)
def fetch_tradingview_cached(display: str, market: str, tf_key: str) -> dict:
    if TA_Handler is None or Interval is None:
        return {"score": 0.0, "recommend": "N/A", "buy": 0, "sell": 0, "neutral": 0, "source": "TV unavailable"}

    asset = Asset(display=display, yf_symbol="", market=market)
    tv_target = tradingview_exchange(asset)
    tv_interval = tradingview_interval(tf_key)
    if tv_target is None or tv_interval is None:
        return {"score": 0.0, "recommend": "N/A", "buy": 0, "sell": 0, "neutral": 0, "source": "TV skipped"}

    symbol, exchange, screener = tv_target

    def _fetch() -> dict:
        handler = TA_Handler(
            symbol=symbol,
            exchange=exchange,
            screener=screener,
            interval=tv_interval,
        )
        analysis = handler.get_analysis()
        rec = analysis.summary.get("RECOMMENDATION", "NEUTRAL")
        return {
            "score": tv_recommendation_score(rec),
            "recommend": rec,
            "buy": analysis.summary.get("BUY", 0),
            "sell": analysis.summary.get("SELL", 0),
            "neutral": analysis.summary.get("NEUTRAL", 0),
            "source": f"TradingView {exchange}",
        }

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(_fetch).result(timeout=TV_TIMEOUT + 3)
    except Exception as exc:
        logger.warning("TradingView failed for %s: %s", display, exc)
        return {"score": 0.0, "recommend": "N/A", "buy": 0, "sell": 0, "neutral": 0, "source": "TV failed"}


def fetch_asset_ohlcv(asset: Asset, all_yf: pd.DataFrame, is_single_yf: bool, tf: dict) -> tuple[pd.DataFrame, str]:
    if asset.market == "Crypto":
        binance_symbol = crypto_to_binance_symbol(asset)
        df = fetch_binance_klines_cached(binance_symbol, tf["binance"])
        if not df.empty:
            return df, f"Binance Spot {binance_symbol}"

    df = extract_df(all_yf, asset.yf_symbol, is_single_yf)
    if df.empty:
        df = fallback_single_download(asset.yf_symbol, tf["period"], tf["interval"])
    source = "Yahoo Finance"
    if asset.market == "Crypto":
        source += " fallback"
    return df, source


def fetch_single_asset_ohlcv(asset: Asset, tf: dict) -> tuple[pd.DataFrame, str]:
    if asset.market == "Crypto":
        binance_symbol = crypto_to_binance_symbol(asset)
        df = fetch_binance_klines_cached(binance_symbol, tf["binance"])
        if not df.empty:
            return df, f"Binance Spot {binance_symbol}"

    df = fallback_single_download(asset.yf_symbol, tf["period"], tf["interval"])
    source = "Yahoo Finance"
    if asset.market == "Crypto":
        source += " fallback"
    return df, source


# ============================================================================
# Scoring
# ============================================================================
def find_support_resistance(df: pd.DataFrame, price: float, lookback: int = 90) -> dict:
    if df.empty or price <= 0:
        return {"support": 0.0, "resistance": 0.0, "support_dist_pct": 0.0, "resistance_dist_pct": 0.0}

    recent = df.tail(min(lookback, len(df))).copy()
    highs = recent["High"].astype(float)
    lows = recent["Low"].astype(float)

    resistance_candidates = highs[highs > price]
    support_candidates = lows[lows < price]

    resistance = safe_float(resistance_candidates.min() if not resistance_candidates.empty else highs.max())
    support = safe_float(support_candidates.max() if not support_candidates.empty else lows.min())

    return {
        "support": support,
        "resistance": resistance,
        "support_dist_pct": ((price - support) / price) * 100 if support > 0 else 0.0,
        "resistance_dist_pct": ((resistance - price) / price) * 100 if resistance > 0 else 0.0,
    }


def quick_backtest(df: pd.DataFrame, max_trades: int = 80, horizon: int = 12) -> dict:
    result = {"trades": 0, "wins": 0, "losses": 0, "winrate": 0.0, "pf": 0.0}
    if df.empty or len(df) < 90:
        return result

    try:
        data = df[["High", "Low", "Close"]].astype(float).dropna().copy()
        high = data["High"]
        low = data["Low"]
        close = data["Close"]

        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        rsi = RSIIndicator(close=close, window=14).rsi()
        macd_hist = MACD(close=close).macd_diff()
        atr = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()

        start = max(60, len(data) - max_trades - horizon)
        gross_win = 0.0
        gross_loss = 0.0

        for i in range(start, len(data) - horizon):
            entry = safe_float(close.iloc[i])
            atr_i = safe_float(atr.iloc[i])
            if entry <= 0 or atr_i <= 0:
                continue

            long_signal = close.iloc[i] > ema20.iloc[i] > ema50.iloc[i] and rsi.iloc[i] > 52 and macd_hist.iloc[i] > 0
            short_signal = close.iloc[i] < ema20.iloc[i] < ema50.iloc[i] and rsi.iloc[i] < 48 and macd_hist.iloc[i] < 0
            if not long_signal and not short_signal:
                continue

            result["trades"] += 1
            risk = atr_i * 0.7
            reward = atr_i * 1.5

            future_high = high.iloc[i + 1 : i + 1 + horizon].max()
            future_low = low.iloc[i + 1 : i + 1 + horizon].min()

            if long_signal:
                hit_tp = future_high >= entry + reward
                hit_sl = future_low <= entry - risk
            else:
                hit_tp = future_low <= entry - reward
                hit_sl = future_high >= entry + risk

            if hit_tp and not hit_sl:
                result["wins"] += 1
                gross_win += reward
            elif hit_sl and not hit_tp:
                result["losses"] += 1
                gross_loss += risk
            elif hit_tp and hit_sl:
                result["losses"] += 1
                gross_loss += risk

        if result["trades"] > 0:
            result["winrate"] = (result["wins"] / result["trades"]) * 100
            result["pf"] = gross_win / gross_loss if gross_loss > 0 else gross_win
    except Exception as exc:
        logger.warning("quick_backtest failed: %s", exc)
    return result


def compute_market_state(df: pd.DataFrame) -> dict:
    result = {
        "is_valid": False,
        "warnings": [],
        "bar_count": len(df),
        "price": 0.0,
        "rsi": 50.0,
        "macd_hist": 0.0,
        "ema20": 0.0,
        "ema50": 0.0,
        "ema200": 0.0,
        "atr": 0.0,
        "atr_pct": 0.0,
        "adx": 0.0,
        "bb_width_pct": 0.0,
        "range_position": 50.0,
        "slope20_pct": 0.0,
        "swing_score": 0.0,
        "trend_score": 0.0,
        "momentum_score": 0.0,
        "congestion_score": 0.0,
        "breakout_score": 0.0,
        "volume_score": 0.0,
        "sr_score": 0.0,
        "rr_score": 0.0,
        "mtf_score": 0.0,
        "support": 0.0,
        "resistance": 0.0,
        "support_dist_pct": 0.0,
        "resistance_dist_pct": 0.0,
        "volume_ratio": 0.0,
        "backtest_trades": 0,
        "backtest_winrate": 0.0,
        "backtest_pf": 0.0,
        "news_score": 0.0,
        "news_risk": "N/A",
        "news_keywords": "-",
        "news_action": "N/A",
        "news_items": [],
        "tv_score": 0.0,
        "tv_recommend": "N/A",
        "tv_votes": "0/0/0",
        "data_source": "N/A",
        "direction_bias": 0.0,
    }

    if df.empty or len(df) < 35:
        result["warnings"].append(f"ข้อมูลน้อยเกินไป ({len(df)} แท่ง)")
        return result

    try:
        close = df["Close"].astype(float).squeeze()
        high = df["High"].astype(float).squeeze()
        low = df["Low"].astype(float).squeeze()

        price = safe_float(close.iloc[-1])
        result["price"] = price

        sr = find_support_resistance(df, price)
        result.update(sr)
        result["backtest"] = quick_backtest(df)
        result["backtest_trades"] = result["backtest"]["trades"]
        result["backtest_winrate"] = result["backtest"]["winrate"]
        result["backtest_pf"] = result["backtest"]["pf"]

        rsi = RSIIndicator(close=close, window=14).rsi()
        macd_hist = MACD(close=close).macd_diff()
        ema20 = EMAIndicator(close=close, window=20).ema_indicator()
        ema50 = EMAIndicator(close=close, window=50).ema_indicator()
        atr = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()
        adx = ADXIndicator(high=high, low=low, close=close, window=14).adx()
        bb = BollingerBands(close=close, window=20, window_dev=2)

        result["rsi"] = safe_float(rsi.dropna().iloc[-1] if not rsi.dropna().empty else 50.0, 50.0)
        result["macd_hist"] = safe_float(macd_hist.dropna().iloc[-1] if not macd_hist.dropna().empty else 0.0)
        result["ema20"] = safe_float(ema20.dropna().iloc[-1] if not ema20.dropna().empty else price)
        result["ema50"] = safe_float(ema50.dropna().iloc[-1] if not ema50.dropna().empty else price)
        result["atr"] = safe_float(atr.dropna().iloc[-1] if not atr.dropna().empty else 0.0)
        result["adx"] = safe_float(adx.dropna().iloc[-1] if not adx.dropna().empty else 0.0)
        result["atr_pct"] = (result["atr"] / price) * 100 if price > 0 else 0.0

        bb_high = bb.bollinger_hband()
        bb_low = bb.bollinger_lband()
        latest_bb_high = safe_float(bb_high.dropna().iloc[-1] if not bb_high.dropna().empty else price)
        latest_bb_low = safe_float(bb_low.dropna().iloc[-1] if not bb_low.dropna().empty else price)
        result["bb_width_pct"] = ((latest_bb_high - latest_bb_low) / price) * 100 if price > 0 else 0.0

        lookback = min(50, len(df))
        range_high = safe_float(high.tail(lookback).max())
        range_low = safe_float(low.tail(lookback).min())
        if range_high > range_low:
            result["range_position"] = ((price - range_low) / (range_high - range_low)) * 100

        if len(df) >= 80:
            ema200 = EMAIndicator(close=close, window=min(200, len(df) - 1)).ema_indicator()
            result["ema200"] = safe_float(ema200.dropna().iloc[-1] if not ema200.dropna().empty else price)
        else:
            result["warnings"].append("ข้อมูลไม่พอสำหรับ EMA200 แบบเต็ม ใช้ EMA20/50 เป็นหลัก")

        if len(ema20.dropna()) >= 8 and price > 0:
            now = safe_float(ema20.dropna().iloc[-1])
            before = safe_float(ema20.dropna().iloc[-8])
            result["slope20_pct"] = ((now - before) / price) * 100

        trend_score = 0.0
        if price > result["ema20"] > result["ema50"]:
            trend_score += 28
        elif price > result["ema20"]:
            trend_score += 14
        elif price < result["ema20"] < result["ema50"]:
            trend_score -= 28
        elif price < result["ema20"]:
            trend_score -= 14

        if result["ema200"] > 0:
            if price > result["ema200"] and result["ema50"] > result["ema200"]:
                trend_score += 12
            elif price < result["ema200"] and result["ema50"] < result["ema200"]:
                trend_score -= 12

        slope_boost = clamp(result["slope20_pct"] * 60, -10, 10)
        trend_score += slope_boost

        momentum_score = 0.0
        momentum_score += 12 if result["macd_hist"] > 0 else -12
        if result["rsi"] >= 58:
            momentum_score += 16
        elif result["rsi"] > 51:
            momentum_score += 8
        elif result["rsi"] <= 42:
            momentum_score -= 16
        elif result["rsi"] < 49:
            momentum_score -= 8

        # Swing is good when volatility is tradable but not chaotic.
        atr_pct = result["atr_pct"]
        if 0.20 <= atr_pct <= 2.80:
            swing_score = 18
        elif 0.08 <= atr_pct < 0.20 or 2.80 < atr_pct <= 4.50:
            swing_score = 8
        else:
            swing_score = -12

        if result["adx"] >= 25:
            swing_score += 7
        elif result["adx"] < 15:
            swing_score -= 10

        # Congestion: narrow bands + weak ADX + stuck in middle of range.
        stuck_mid_range = 35 <= result["range_position"] <= 65
        narrow_band = result["bb_width_pct"] < max(0.35, atr_pct * 1.8)
        if stuck_mid_range and narrow_band and result["adx"] < 18:
            congestion_score = -25
        elif stuck_mid_range and result["adx"] < 16:
            congestion_score = -15
        else:
            congestion_score = 6

        breakout_score = 0.0
        if result["range_position"] >= 82 and result["adx"] >= 20 and result["macd_hist"] > 0:
            breakout_score = 12
        elif result["range_position"] <= 18 and result["adx"] >= 20 and result["macd_hist"] < 0:
            breakout_score = -12

        volume_score = 0.0
        if "Volume" in df.columns:
            volume = df["Volume"].astype(float).dropna()
            if len(volume) >= 21:
                avg_volume = safe_float(volume.tail(21).iloc[:-1].mean())
                latest_volume = safe_float(volume.iloc[-1])
                if avg_volume > 0:
                    result["volume_ratio"] = latest_volume / avg_volume
                    if result["volume_ratio"] >= 1.45:
                        volume_score = 10
                    elif result["volume_ratio"] >= 1.10:
                        volume_score = 5
                    elif result["volume_ratio"] < 0.65:
                        volume_score = -8

        sr_score = 0.0
        if result["resistance_dist_pct"] > 0 and result["resistance_dist_pct"] < max(result["atr_pct"] * 0.8, 0.12):
            sr_score -= 10
        if result["support_dist_pct"] > 0 and result["support_dist_pct"] < max(result["atr_pct"] * 0.8, 0.12):
            sr_score -= 10

        result["trend_score"] = clamp(trend_score, -45, 45)
        result["momentum_score"] = clamp(momentum_score, -30, 30)
        result["swing_score"] = clamp(swing_score, -20, 25)
        result["congestion_score"] = congestion_score
        result["breakout_score"] = breakout_score
        result["volume_score"] = volume_score
        result["sr_score"] = sr_score
        result["direction_bias"] = (
            result["trend_score"]
            + result["momentum_score"]
            + result["breakout_score"]
            + result["volume_score"]
        )
        result["is_valid"] = True
    except Exception as exc:
        logger.error("compute_market_state failed: %s", exc)
        result["warnings"].append("คำนวณ indicator ไม่สำเร็จ")

    return result


def apply_mtf_confirmation(state: dict, higher_state: dict | None) -> None:
    if not higher_state or not higher_state.get("is_valid") or not state.get("is_valid"):
        state["mtf_label"] = "N/A"
        state["mtf_score"] = 0.0
        return

    current_bias = safe_float(state.get("direction_bias"))
    higher_bias = safe_float(higher_state.get("direction_bias"))

    if current_bias == 0 or higher_bias == 0:
        state["mtf_label"] = "Neutral"
        state["mtf_score"] = 0.0
    elif (current_bias > 0 and higher_bias > 0) or (current_bias < 0 and higher_bias < 0):
        state["mtf_label"] = "Aligned"
        state["mtf_score"] = 14.0
    else:
        state["mtf_label"] = "Against HTF"
        state["mtf_score"] = -18.0

    state["htf_trend"] = safe_float(higher_state.get("trend_score"))
    state["htf_adx"] = safe_float(higher_state.get("adx"))
    state["direction_bias"] = current_bias + state["mtf_score"]


def build_trade_call(
    asset: Asset,
    state: dict,
    tv: dict | None = None,
    news: dict | None = None,
    data_source: str = "N/A",
    higher_state: dict | None = None,
) -> dict:
    tv = tv or {"score": 0.0, "recommend": "N/A", "buy": 0, "sell": 0, "neutral": 0}
    news = news or {
        "score_adjust": 0.0,
        "risk_score": 0,
        "risk_label": "N/A",
        "keywords": "-",
        "trade_action": "N/A",
        "items": [],
    }
    state["tv_score"] = safe_float(tv.get("score"))
    state["tv_recommend"] = str(tv.get("recommend", "N/A"))
    state["tv_votes"] = f"{tv.get('buy', 0)}/{tv.get('sell', 0)}/{tv.get('neutral', 0)}"
    state["news_score"] = safe_float(news.get("score_adjust"))
    state["news_risk_score"] = int(news.get("risk_score", 0))
    state["news_risk"] = str(news.get("risk_label", "N/A"))
    state["news_keywords"] = str(news.get("keywords", "-"))
    state["news_action"] = str(news.get("trade_action", "N/A"))
    state["news_items"] = news.get("items", [])
    state["market_hours"] = market_hours_info(asset)
    state["data_source"] = data_source
    state["direction_bias"] = safe_float(state.get("direction_bias")) + state["tv_score"] + state["news_score"]
    apply_mtf_confirmation(state, higher_state)

    direction_bias = safe_float(state.get("direction_bias"))
    quality_score = (
        abs(direction_bias)
        + safe_float(state.get("swing_score"))
        + safe_float(state.get("congestion_score"))
        + safe_float(state.get("sr_score"))
        + safe_float(state.get("news_score"))
    )
    quality_score = clamp(quality_score, 0, 100)

    if not state.get("is_valid"):
        side = "WAIT"
        signal = "⚪ WAIT"
        confidence = 0.0
        reason = "ข้อมูลไม่พอ"
    elif state["congestion_score"] <= -20:
        side = "WAIT"
        signal = "🟡 WAIT - ราคาติดกรอบ"
        confidence = clamp(quality_score, 35, 65)
        reason = "ราคาแกว่งในกรอบแคบ ADX อ่อน ยังไม่คุ้มเสี่ยง"
    elif direction_bias >= 28 and quality_score >= 45:
        side = "LONG"
        signal = "🟢 LONG เด่น"
        confidence = quality_score
        reason = "แนวโน้มขึ้น + momentum หนุน + volatility พอเทรด"
    elif direction_bias <= -28 and quality_score >= 45:
        side = "SHORT"
        signal = "🔴 SHORT เด่น"
        confidence = quality_score
        reason = "แนวโน้มลง + momentum กดลง + volatility พอเทรด"
    elif abs(direction_bias) >= 18:
        side = "WATCH"
        signal = "🔎 WATCH"
        confidence = clamp(quality_score, 35, 70)
        reason = "เริ่มมีทิศ แต่คะแนนยังไม่ครบ รอจังหวะยืนยัน"
    else:
        side = "WAIT"
        signal = "🟡 WAIT"
        confidence = clamp(quality_score, 25, 55)
        reason = "ทิศทางยังไม่ชัด"

    if state["news_risk"] == "HIGH" and side in {"LONG", "SHORT"}:
        side = "WATCH"
        signal = "📰 WATCH - ข่าวแรง"
        confidence = clamp(quality_score, 25, 70)
        reason = "มีข่าวแรงกระทบตลาด ควรรอความผันผวนสงบก่อนเข้า"

    entry = safe_float(state.get("price"))
    atr = safe_float(state.get("atr"))
    risk = atr * 0.7 if atr > 0 else 0.0
    rr = 0.0
    tp1 = 0.0
    tp2 = 0.0
    sl = 0.0

    if entry > 0 and risk > 0 and side in {"LONG", "SHORT", "WATCH"}:
        if direction_bias >= 0:
            sl = entry - risk
            raw_tp1 = entry + atr
            raw_tp2 = entry + (atr * 2)
            resistance = safe_float(state.get("resistance"))
            if resistance > entry:
                raw_tp2 = min(raw_tp2, resistance)
            tp1, tp2 = raw_tp1, raw_tp2
            reward = max(tp2 - entry, 0.0)
        else:
            sl = entry + risk
            raw_tp1 = entry - atr
            raw_tp2 = entry - (atr * 2)
            support = safe_float(state.get("support"))
            if 0 < support < entry:
                raw_tp2 = max(raw_tp2, support)
            tp1, tp2 = raw_tp1, raw_tp2
            reward = max(entry - tp2, 0.0)

        rr = reward / risk if risk > 0 else 0.0
        if rr >= 2.0:
            state["rr_score"] = 12.0
        elif rr >= 1.5:
            state["rr_score"] = 7.0
        elif rr < 1.0:
            state["rr_score"] = -18.0
        else:
            state["rr_score"] = -6.0

        quality_score = clamp(quality_score + state["rr_score"], 0, 100)
        confidence = quality_score if side in {"LONG", "SHORT"} else confidence
        if side in {"LONG", "SHORT"} and rr < 1.0:
            side = "WATCH"
            signal = "🔎 WATCH - RR ยังไม่คุ้ม"
            reason = "ทิศทางมี แต่ระยะ TP/SL ยังไม่คุ้มจากแนวรับแนวต้าน"

    state["entry"] = entry
    state["sl"] = sl
    state["tp1"] = tp1
    state["tp2"] = tp2
    state["rr"] = rr

    long_run = "ขึ้นยาว" if state["trend_score"] >= 30 and state["adx"] >= 22 else ""
    down_run = "ลงยาว" if state["trend_score"] <= -30 and state["adx"] >= 22 else ""
    choppy = "ติดขัด/ไซด์เวย์" if state["congestion_score"] < 0 else "ไม่ติดมาก"

    return {
        "asset": asset,
        "side": side,
        "signal": signal,
        "confidence": confidence,
        "quality_score": quality_score,
        "reason": reason,
        "run_state": long_run or down_run or choppy,
        "state": state,
    }


def sort_trade_calls(calls: list[dict]) -> list[dict]:
    rank = {"LONG": 0, "SHORT": 0, "WATCH": 1, "WAIT": 2}
    return sorted(calls, key=lambda item: (rank.get(item["side"], 9), -item["quality_score"]))


# ============================================================================
# UI
# ============================================================================
st.set_page_config(
    page_title="AI Trade Hunter Pro",
    page_icon="🎯",
    layout="wide",
)

st.markdown(
    """
<style>
    .block-container {
        padding-top: 1rem;
        padding-bottom: 1rem;
        max-width: 1160px;
    }
    .trade-card {
        border: 1px solid rgba(128, 128, 128, 0.25);
        border-radius: 8px;
        padding: 0.85rem;
        margin-bottom: 0.75rem;
        background: rgba(250, 250, 250, 0.03);
    }
    .trade-title {
        font-size: 1.05rem;
        font-weight: 700;
        margin-bottom: 0.25rem;
    }
    .trade-meta {
        font-size: 0.82rem;
        color: rgba(128, 128, 128, 0.95);
    }
    @media (max-width: 768px) {
        h1 { font-size: 1.35rem !important; }
        h2, h3 { font-size: 1.05rem !important; }
        .block-container { padding-left: 0.7rem; padding-right: 0.7rem; }
        .stButton > button { width: 100% !important; padding: 0.45rem !important; }
        [data-testid="metric-container"] label { font-size: 0.68rem !important; }
        [data-testid="metric-container"] [data-testid="stMetricValue"] { font-size: 1rem !important; }
        .trade-card { padding: 0.7rem; }
        .trade-title { font-size: 0.98rem; }
    }
    .stDataFrame { overflow-x: auto !important; -webkit-overflow-scrolling: touch; }
</style>
""",
    unsafe_allow_html=True,
)

st.title(f"🎯 AI Trade Hunter Pro ({APP_VERSION})")
st.caption("สแกน Crypto / Forex / Gold เพื่อหา symbol ที่น่าเทรดที่สุด จาก trend, swing, momentum, volatility และภาวะราคาติดกรอบ")
st.caption("Data: Binance Spot, Yahoo Finance fallback, TradingView consensus, Multi-Timeframe, S/R, RR, Volume, News Guard และ Market Hours")

st.warning(
    "**คำเตือน:** ระบบนี้เป็นตัวช่วยวิเคราะห์เชิงเทคนิค ไม่ใช่คำแนะนำการลงทุน "
    "ตลาด Crypto, Forex และ Gold มีความเสี่ยงสูง ควรกำหนด SL และขนาดสัญญาเองทุกครั้ง"
)

for key, value in {"auto_mode": False, "run_once": False, "fail_count": 0}.items():
    if key not in st.session_state:
        st.session_state[key] = value

with st.sidebar:
    st.header("ตั้งค่า")
    market_choices = st.multiselect(
        "ตลาดที่ต้องการสแกน",
        options=["Crypto", "Forex", "Gold"],
        default=["Crypto", "Forex", "Gold"],
    )
    tf_key = st.selectbox(
        "Timeframe",
        options=list(TF_MAP.keys()),
        index=2,
        format_func=lambda x: TF_MAP[x]["label"],
    )
    top_n = st.slider("แสดง Top", min_value=3, max_value=20, value=10)
    enable_news = st.toggle("เปิด News Guard", value=True)

default_symbols = ", ".join(
    part
    for market, part in [
        ("Crypto", DEFAULT_CRYPTO),
        ("Forex", DEFAULT_FOREX),
        ("Gold", DEFAULT_GOLD),
    ]
    if market in market_choices
)

user_input = st.text_area(
    f"Symbols ที่ต้องการสแกน สูงสุด {MAX_ASSETS} ตัว",
    value=default_symbols,
    height=100,
    help="ตัวอย่าง: BTC, ETH, SOL, EURUSD, USDJPY, XAUUSD หรือ GC=F",
)

btn1, btn2, btn3 = st.columns(3)
with btn1:
    if st.button("🔍 สแกนทันที", use_container_width=True):
        st.session_state.update({"auto_mode": False, "run_once": True, "fail_count": 0})
        st.rerun()
with btn2:
    if st.button("🔄 Auto 60s", use_container_width=True):
        st.session_state.update({"auto_mode": True, "run_once": False, "fail_count": 0})
        st.rerun()
with btn3:
    if st.button("🛑 หยุด", use_container_width=True):
        st.session_state.update({"auto_mode": False, "run_once": False, "fail_count": 0})
        st.rerun()

if st.session_state.auto_mode:
    if st_autorefresh:
        st_autorefresh(interval=60_000, key="trade_hunter_refresh")
    else:
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

    yf_assets = assets
    tickers = tuple(asset.yf_symbol for asset in yf_assets)
    is_single = len(yf_assets) == 1

    progress = st.progress(0, text="กำลังโหลดข้อมูลหลายแหล่ง...")
    with st.spinner("กำลังดาวน์โหลด OHLCV: Binance สำหรับ Crypto และ Yahoo Finance เป็น fallback..."):
        all_yf = fetch_yfinance_cached(tickers, period=tf["period"], interval=tf["interval"])

    for index, asset in enumerate(assets):
        progress.progress((index + 1) / len(assets), text=f"กำลังวิเคราะห์ {asset.display}...")
        df, data_source = fetch_asset_ohlcv(asset, all_yf, is_single, tf)
        higher_tf_key = HIGHER_TF.get(tf_key, tf_key)
        higher_df = pd.DataFrame()
        higher_state = None
        if higher_tf_key != tf_key:
            higher_df, _higher_source = fetch_single_asset_ohlcv(asset, TF_MAP[higher_tf_key])
            higher_state = compute_market_state(higher_df)
        tv = fetch_tradingview_cached(asset.display, asset.market, tf_key)
        news = fetch_asset_news(asset, enable_news)

        state = compute_market_state(df)
        call = build_trade_call(asset, state, tv=tv, news=news, data_source=data_source, higher_state=higher_state)
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
    RR {best_state["rr"]:.2f} · TV {best_state["tv_recommend"]} · MTF {best_state.get("mtf_label", "N/A")} · สภาวะ: {best["run_state"]}<br>
    ข่าว: {best_state["news_risk"]} ({best_state["news_risk_score"]}/100) · {best_state["news_action"]}<br>
    ตลาด: {best_state["market_hours"]["status_th"]} · {best_state["market_hours"]["next_event"]}<br>
    แหล่งราคา: {best_state["data_source"]}
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
                "RR": f"{state['rr']:.2f}",
                "ADX": f"{state['adx']:.1f}",
                "RSI": f"{state['rsi']:.1f}",
                "TV": state["tv_recommend"],
                "MTF": state.get("mtf_label", "N/A"),
                "News": f"{state['news_risk']} {state['news_risk_score']}/100",
                "Market": state["market_hours"]["status_th"],
                "Next": state["market_hours"]["next_event"],
                "BT Win": f"{state['backtest_winrate']:.0f}%/{state['backtest_trades']}",
                "Data": state["data_source"],
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
                st.metric("RR", f"{state['rr']:.2f}")
            with m2:
                st.metric("Momentum", f"{state['momentum_score']:+.0f}")
                st.metric("ADX", f"{state['adx']:.1f}")
                st.metric("MTF", state.get("mtf_label", "N/A"))
            with m3:
                st.metric("Swing", f"{state['swing_score']:+.0f}")
                st.metric("ATR", atr_display(asset, state["atr"], state["price"]))
                st.metric("Backtest", f"{state['backtest_winrate']:.0f}%/{state['backtest_trades']}")
                st.metric("News", f"{state['news_risk']} {state['news_risk_score']}/100")

            market_info = state["market_hours"]
            st.info(
                f"ตลาด: {market_info['status_th']} | {market_info['next_event']}\n\n"
                f"เวลาเทรด: {market_info['hours']}\n\n"
                f"ช่วงปิด: {market_info['closed']}\n\n"
                f"{market_info['note']}"
            )

            score_df = pd.DataFrame(
                [
                    {"หัวข้อ": "Trend EMA20/50/200 + slope", "คะแนน": f"{state['trend_score']:+.1f}"},
                    {"หัวข้อ": "RSI + MACD Momentum", "คะแนน": f"{state['momentum_score']:+.1f}"},
                    {"หัวข้อ": "Swing / Volatility / ADX", "คะแนน": f"{state['swing_score']:+.1f}"},
                    {"หัวข้อ": f"Multi-Timeframe ({state.get('mtf_label', 'N/A')})", "คะแนน": f"{state['mtf_score']:+.1f}"},
                    {"หัวข้อ": f"Risk:Reward ({state['rr']:.2f})", "คะแนน": f"{state['rr_score']:+.1f}"},
                    {"หัวข้อ": f"Volume ratio ({state['volume_ratio']:.2f}x)", "คะแนน": f"{state['volume_score']:+.1f}"},
                    {"หัวข้อ": "Support / Resistance distance", "คะแนน": f"{state['sr_score']:+.1f}"},
                    {"หัวข้อ": f"News Guard ({state['news_keywords']})", "คะแนน": f"{state['news_score']:+.1f}"},
                    {"หัวข้อ": "ราคาติดกรอบ / congestion", "คะแนน": f"{state['congestion_score']:+.1f}"},
                    {"หัวข้อ": "Breakout จากกรอบ 50 แท่ง", "คะแนน": f"{state['breakout_score']:+.1f}"},
                    {"หัวข้อ": f"TradingView consensus ({state['tv_votes']})", "คะแนน": f"{state['tv_score']:+.1f}"},
                ]
            )
            st.dataframe(score_df, use_container_width=True, hide_index=True)

            if state["news_items"]:
                news_rows = [
                    {
                        "เวลา": item.get("published", "")[:16],
                        "หัวข้อข่าว": item.get("title", ""),
                        "แหล่ง": item.get("source", ""),
                        "ลิงก์": item.get("link", ""),
                    }
                    for item in state["news_items"][:5]
                ]
                st.caption(f"News Guard: {state['news_action']} | Keywords: {state['news_keywords']}")
                st.dataframe(pd.DataFrame(news_rows), use_container_width=True, hide_index=True)
            elif enable_news:
                st.caption("News Guard: ยังดึงข่าวไม่ได้หรือไม่มีข่าวล่าสุดจาก RSS")

            if call["side"] in {"LONG", "SHORT"} and state["atr"] > 0:
                st.info(
                    f"แผนตัวอย่าง: {call['side']} | Entry {price_format(asset.display, asset.market, state['entry'])} | "
                    f"TP1 {price_format(asset.display, asset.market, state['tp1'])} | "
                    f"TP2 {price_format(asset.display, asset.market, state['tp2'])} | "
                    f"SL {price_format(asset.display, asset.market, state['sl'])} | RR {state['rr']:.2f} | "
                    f"ใช้เป็นกรอบวางแผน ไม่ใช่คำสั่งเทรด"
                )
            else:
                st.caption("ยังไม่ใช่จังหวะเด่น ระบบแนะนำให้รอราคาหลุดกรอบหรือมี momentum ชัดขึ้น")

    st.session_state.run_once = False

elif not assets:
    st.info("กรอก symbol หรือเลือกตลาดที่ต้องการสแกนก่อน แล้วกด `สแกนทันที`")
