from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

try:
    from core.margin_data import get_margin_snapshot
    from core.news_engine import get_recent_news
    from core.stock_engine import (
        LIQUID_TAIWAN_UNIVERSE,
        calculate_strength_score,
        get_benchmark_returns,
        get_stock_name,
    )
except ImportError:
    from margin_data import get_margin_snapshot
    from news_engine import get_recent_news
    from stock_engine import (
        LIQUID_TAIWAN_UNIVERSE,
        calculate_strength_score,
        get_benchmark_returns,
        get_stock_name,
    )


LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
REQUIRED_HISTORY_DAYS = 60
DOWNLOAD_CHUNK_SIZE = 50


def _chunked(items: list[str], size: int) -> list[list[str]]:
    return [
        items[index:index + size]
        for index in range(0, len(items), size)
    ]


def _get_cache_file(symbols: list[str]) -> Path:
    """同一交易日、同一股票池共用一份快取。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cache_date = datetime.now(TAIPEI_TZ).strftime("%Y%m%d")
    fingerprint = hashlib.sha1(
        "|".join(symbols).encode("utf-8")
    ).hexdigest()[:12]

    return CACHE_DIR / f"daily_history_{cache_date}_{fingerprint}.pkl"


def _extract_history(
    downloaded: pd.DataFrame,
    symbol: str,
    chunk_size: int,
) -> pd.DataFrame:
    """從 yfinance 批次下載結果中取出單一股票資料。"""
    if downloaded.empty:
        return pd.DataFrame()

    if not isinstance(downloaded.columns, pd.MultiIndex):
        return downloaded.copy() if chunk_size == 1 else pd.DataFrame()

    first_level = downloaded.columns.get_level_values(0)
    second_level = downloaded.columns.get_level_values(1)

    if symbol in first_level:
        return downloaded[symbol].dropna(how="all")

    if symbol in second_level:
        return downloaded.xs(
            symbol,
            axis=1,
            level=1,
        ).dropna(how="all")

    return pd.DataFrame()


def download_market_histories(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """
    分批下載股票日線。

    同一天重跑會優先讀取 data/cache 快取，
    避免反覆呼叫 Yahoo Finance。
    """
    cache_file = _get_cache_file(symbols)

    if cache_file.exists():
        try:
            cached = pd.read_pickle(cache_file)

            if isinstance(cached, dict):
                LOGGER.info("使用日線快取：%s", cache_file.name)
                return cached
        except Exception as exc:
            LOGGER.warning("讀取日線快取失敗，重新下載：%s", exc)

    histories: dict[str, pd.DataFrame] = {}

    for chunk in _chunked(symbols, DOWNLOAD_CHUNK_SIZE):
        try:
            downloaded = yf.download(
                tickers=chunk,
                period="6mo",
                interval="1d",
                auto_adjust=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )

            for symbol in chunk:
                history = _extract_history(
                    downloaded=downloaded,
                    symbol=symbol,
                    chunk_size=len(chunk),
                )

                if not history.empty:
                    histories[symbol] = history

        except Exception as exc:
            LOGGER.warning("批次下載日線失敗：%s", exc)

    try:
        pd.to_pickle(histories, cache_file)
        LOGGER.info("日線快取已建立：%s", cache_file.name)
    except Exception as exc:
        LOGGER.warning("建立日線快取失敗：%s", exc)

    return histories


def build_stock_signal(
    symbol: str,
    history: pd.DataFrame,
) -> dict:
    """由批次下載的歷史日線計算技術指標。"""
    try:
        if history.empty or len(history) < REQUIRED_HISTORY_DAYS:
            return {
                "success": False,
                "symbol": symbol,
                "error": "歷史資料不足。",
            }

        close = history["Close"].astype(float).dropna()
        volume = history["Volume"].astype(float).fillna(0)

        if len(close) < REQUIRED_HISTORY_DAYS:
            return {
                "success": False,
                "symbol": symbol,
                "error": "有效收盤資料不足。",
            }

        ma20_series = close.rolling(20).mean()
        ma60_series = close.rolling(60).mean()
        volume_20ma = volume.rolling(20).mean()

        last_close = float(close.iloc[-1])
        ma20 = float(ma20_series.iloc[-1])
        ma60 = float(ma60_series.iloc[-1])
        ma20_five_days_ago = float(ma20_series.iloc[-6])

        last_volume = float(volume.iloc[-1])
        volume_average = float(volume_20ma.iloc[-1])

        return_20 = float(
            (close.iloc[-1] / close.iloc[-21] - 1) * 100
        )

        daily_returns = close.pct_change().dropna().tail(20)
        volatility_20 = float(daily_returns.std() * 100)

        volume_ratio = (
            last_volume / volume_average
            if volume_average > 0
            else 0
        )

        bias_60 = (last_close - ma60) / ma60 * 100
        high_60 = float(close.tail(60).max())

        if last_close > ma20 > ma60:
            trend_status = "多頭排列"
            is_bull = True
        elif last_close > ma20:
            trend_status = "站上 20MA"
            is_bull = True
        else:
            trend_status = "跌破 20MA"
            is_bull = False

        return {
            "success": True,
            "symbol": symbol,
            "name": symbol,
            "price": round(last_close, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "ma20_slope_up": ma20 > ma20_five_days_ago,
            "bias_60": round(bias_60, 2),
            "volume": int(last_volume),
            "vol_20ma": int(volume_average),
            "volume_ratio": round(volume_ratio, 2),
            "return_20": round(return_20, 2),
            "volatility_20": round(volatility_20, 2),
            "high_60": round(high_60, 2),
            "trend_status": trend_status,
            "is_bull": is_bull,
        }

    except Exception as exc:
        LOGGER.warning("計算 %s 技術資料失敗：%s", symbol, exc)

        return {
            "success": False,
            "symbol": symbol,
            "error": str(exc),
        }


def get_entry_label(stock: dict) -> str:
    """
    依趨勢、乖離、量能與波動度給出進場風險標籤。
    """
    if not stock["is_bull"]:
        return "等待拉回"

    if (
        stock["bias_60"] >= 18
        or stock["volume_ratio"] >= 3.5
        or stock["volatility_20"] >= 4.5
    ):
        return "避免追價"

    if (
        stock["price"] <= stock["ma20"] * 1.05
        and stock["ma20_slope_up"]
    ):
        return "可觀察"

    return "等待拉回"


def _safe_margin(symbol: str) -> dict:
    try:
        return get_margin_snapshot(symbol)
    except Exception as exc:
        LOGGER.warning("取得 %s 融資資料失敗：%s", symbol, exc)
        return {"success": False}


def _safe_news(symbol: str, name: str) -> dict:
    try:
        return get_recent_news(symbol, name)
    except Exception as exc:
        LOGGER.warning("取得 %s 新聞資料失敗：%s", symbol, exc)

        return {
            "success": False,
            "items": [],
            "impact_score": 0,
        }


def scan_nightly_strong_stocks(
    limit: int = 10,
    min_score: int = 65,
    universe_size: int = 150,
) -> list[dict]:
    """
    夜間兩階段選股：

    1. 批次下載 150 檔日線並以技術面初篩。
    2. 只對前 30 名查融資與新聞，完成最終排名。
    """
    safe_universe_size = max(
        30,
        min(universe_size, len(LIQUID_TAIWAN_UNIVERSE)),
    )
    safe_limit = max(1, min(limit, 10))
    safe_min_score = max(0, min(min_score, 100))

    symbols = list(
        LIQUID_TAIWAN_UNIVERSE[:safe_universe_size]
    )
    histories = download_market_histories(symbols)
    benchmark_returns = get_benchmark_returns()

    preliminary = []

    for symbol in symbols:
        history = histories.get(symbol)

        if history is None:
            continue

        stock = build_stock_signal(symbol, history)

        if not stock.get("success"):
            continue

        market_key = (
            "TWOII"
            if symbol.endswith(".TWO")
            else "TWII"
        )

        strength = calculate_strength_score(
            stock=stock,
            benchmark_return=benchmark_returns[market_key],
            margin={"success": False},
            news={"impact_score": 0},
        )

        preliminary.append(
            {
                "stock": stock,
                "preliminary_score": strength["score"],
            }
        )

    preliminary.sort(
        key=lambda item: item["preliminary_score"],
        reverse=True,
    )

    finalists = preliminary[:max(30, safe_limit * 3)]
    ranked = []

    for item in finalists:
        stock = item["stock"]
        symbol = stock["symbol"]
        name = get_stock_name(symbol, symbol)

        margin = _safe_margin(symbol)
        news = _safe_news(symbol, name)

        market_key = (
            "TWOII"
            if symbol.endswith(".TWO")
            else "TWII"
        )

        strength = calculate_strength_score(
            stock=stock,
            benchmark_return=benchmark_returns[market_key],
            margin=margin,
            news=news,
        )

        if strength["score"] < safe_min_score:
            continue

        ranked.append(
            {
                **stock,
                "name": name,
                "margin": margin,
                "news": news,
                "entry_label": get_entry_label(stock),
                "strength_score": strength["score"],
                "strength_grade": strength["grade"],
                "strength_reasons": strength["reasons"],
                "strength_risks": strength["risks"],
            }
        )

    ranked.sort(
        key=lambda item: item["strength_score"],
        reverse=True,
    )

    return ranked[:safe_limit]