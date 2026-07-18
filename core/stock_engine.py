from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

import yfinance as yf

try:
    from core.margin_data import get_margin_snapshot
    from core.news_engine import get_recent_news
except ImportError:
    from margin_data import get_margin_snapshot
    from news_engine import get_recent_news


LOGGER = logging.getLogger(__name__)

REQUIRED_HISTORY_DAYS = 60
SCAN_WORKERS = 6

# 台股高流動性候選池。
# 夜間預設掃描前 150 檔；不是使用 TG 的個人觀察清單。
LIQUID_TAIWAN_UNIVERSE = (
    "2330.TW", "2317.TW", "2454.TW", "2308.TW", "2382.TW",
    "3231.TW", "6669.TW", "3661.TW", "3711.TW", "3034.TW",
    "2379.TW", "2357.TW", "2383.TW", "2345.TW", "2327.TW",
    "3443.TW", "3037.TW", "2368.TW", "3017.TW", "5274.TW",
    "2603.TW", "2303.TW", "2408.TW", "2344.TW", "3264.TW",
    "3189.TW", "4958.TW", "2449.TW", "2313.TW", "3036.TW",
    "3044.TW", "2356.TW", "2376.TW", "4938.TW", "2377.TW",
    "3533.TW", "2455.TW", "3035.TW", "3008.TW", "2353.TW",
    "4904.TW", "8046.TW", "6415.TW", "6239.TW", "2395.TW",
    "2352.TW", "2301.TW", "2324.TW", "2354.TW", "2474.TW",
    "2498.TW", "3045.TW", "3450.TW", "6278.TW", "3702.TW",
    "1301.TW", "1303.TW", "1326.TW", "1402.TW", "1476.TW",
    "1504.TW", "1519.TW", "1536.TW", "1560.TW", "1590.TW",
    "1605.TW", "1717.TW", "1722.TW", "1802.TW", "2002.TW",
    "2006.TW", "2049.TW", "2059.TW", "2105.TW", "2201.TW",
    "2204.TW", "2207.TW", "2208.TW", "2231.TW", "2302.TW",
    "2312.TW", "2347.TW", "2358.TW", "2359.TW", "2360.TW",
    "2362.TW", "2363.TW", "2385.TW", "2388.TW", "2392.TW",
    "2393.TW", "2404.TW", "2409.TW", "2412.TW", "2481.TW",
    "2609.TW", "2610.TW", "2615.TW", "2618.TW", "2634.TW",
    "2637.TW", "2645.TW", "2801.TW", "2880.TW", "2881.TW",
    "2882.TW", "2883.TW", "2884.TW", "2885.TW", "2886.TW",
    "2887.TW", "2890.TW", "2891.TW", "2892.TW", "2912.TW",
    "3005.TW", "3006.TW", "3008.TW", "3034.TW", "3049.TW",
    "3059.TW", "3130.TW", "3293.TW", "3413.TW", "3532.TW",
    "3702.TW", "3714.TW", "4961.TW", "5269.TW", "5880.TW",
    "6005.TW", "6176.TW", "6189.TW", "6213.TW", "6257.TW",
    "6285.TW", "6442.TW", "6505.TW", "6770.TW", "6781.TW",
    "8150.TW", "8210.TW", "8454.TW", "8464.TW", "8996.TW",
    "3081.TWO", "3105.TWO", "3227.TWO", "3260.TWO", "3363.TWO",
    "3374.TWO", "3529.TWO", "3548.TWO", "3559.TWO", "3583.TWO",
    "3615.TWO", "3624.TWO", "4728.TWO", "4966.TWO", "4979.TWO",
    "5347.TWO", "5425.TWO", "5439.TWO", "5483.TWO", "5519.TWO",
    "6147.TWO", "6180.TWO", "6187.TWO", "6190.TWO", "6217.TWO",
    "6274.TWO", "6485.TWO", "6488.TWO", "6510.TWO", "6515.TWO",
    "6525.TWO", "6531.TWO", "6533.TWO", "6548.TWO", "6643.TWO",
    "8069.TWO", "8086.TWO", "8299.TWO", "8358.TWO",
)


def normalize_taiwan_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()

    if normalized.isdigit() and 4 <= len(normalized) <= 6:
        return f"{normalized}.TW"

    return normalized


def _failure(symbol: str, message: str) -> dict:
    return {
        "success": False,
        "symbol": symbol,
        "name": symbol,
        "error": message,
    }


def get_stock_name(symbol: str, fallback: str | None = None) -> str:
    """只在最終入選的少數股票查詢名稱，降低 Yahoo 請求量。"""
    try:
        info = yf.Ticker(symbol).info
        return str(info.get("shortName") or fallback or symbol)
    except Exception:
        return str(fallback or symbol)


def get_stock_signal(symbol: str, resolve_name: bool = True) -> dict:
    """取得單一個股的技術面資料。"""
    query_symbol = normalize_taiwan_symbol(symbol)

    try:
        ticker = yf.Ticker(query_symbol)
        history = ticker.history(period="6mo", auto_adjust=False)

        if history.empty or len(history) < REQUIRED_HISTORY_DAYS:
            return _failure(query_symbol, "歷史資料不足，無法計算 60MA。")

        close = history["Close"].astype(float)
        volume = history["Volume"].astype(float)

        ma20_series = close.rolling(20).mean()
        ma60_series = close.rolling(60).mean()
        volume_20ma = volume.rolling(20).mean()

        last_close = float(close.iloc[-1])
        ma20 = float(ma20_series.iloc[-1])
        ma60 = float(ma60_series.iloc[-1])
        ma20_five_days_ago = float(ma20_series.iloc[-6])

        last_volume = float(volume.iloc[-1])
        volume_average = float(volume_20ma.iloc[-1])

        return_20 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100)
        high_60 = float(close.tail(60).max())

        volume_ratio = (
            last_volume / volume_average
            if volume_average > 0
            else 0
        )
        bias_60 = (last_close - ma60) / ma60 * 100

        if last_close > ma20 > ma60:
            trend_status = "多頭排列（收盤價 > 20MA > 60MA）"
            is_bull = True
        elif last_close > ma20:
            trend_status = "站上 20MA，尚未完整多頭排列"
            is_bull = True
        else:
            trend_status = "收盤價低於 20MA"
            is_bull = False

        stock_name = (
            get_stock_name(query_symbol, query_symbol)
            if resolve_name
            else query_symbol
        )

        return {
            "success": True,
            "symbol": query_symbol,
            "name": stock_name,
            "price": round(last_close, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "ma20_slope_up": ma20 > ma20_five_days_ago,
            "bias_60": round(bias_60, 2),
            "volume": int(last_volume),
            "vol_20ma": int(volume_average),
            "volume_ratio": round(volume_ratio, 2),
            "return_20": round(return_20, 2),
            "high_60": round(high_60, 2),
            "trend_status": trend_status,
            "volume_status": (
                "量能高於 20 日均量"
                if volume_ratio >= 1.2
                else "量能未達 20 日均量 1.2 倍"
            ),
            "is_bull": is_bull,
        }

    except Exception as exc:
        LOGGER.warning("取得 %s 技術資料失敗：%s", query_symbol, exc)
        return _failure(query_symbol, f"資料來源連線失敗：{exc}")


def get_benchmark_returns() -> dict[str, float | None]:
    """取得加權與櫃買指數的 20 日報酬率。"""
    result: dict[str, float | None] = {
        "TWII": None,
        "TWOII": None,
    }

    for key, yahoo_symbol in {
        "TWII": "^TWII",
        "TWOII": "^TWOII",
    }.items():
        try:
            history = yf.Ticker(yahoo_symbol).history(
                period="3mo",
                auto_adjust=False,
            )

            if len(history) >= 21:
                close = history["Close"].astype(float)
                result[key] = float(
                    (close.iloc[-1] / close.iloc[-21] - 1) * 100
                )
        except Exception as exc:
            LOGGER.warning("取得 %s 報酬率失敗：%s", yahoo_symbol, exc)

    return result


def calculate_strength_score(
    stock: dict,
    benchmark_return: float | None,
    margin: dict,
    news: dict,
) -> dict:
    """100 分制強勢股評分。"""
    score = 0
    reasons = []
    risks = []

    if stock["price"] > stock["ma20"] > stock["ma60"]:
        score += 30
        reasons.append("多頭排列")
    elif stock["price"] > stock["ma20"]:
        score += 15
        reasons.append("站上 20MA")
    else:
        risks.append("未站穩 20MA")

    if stock["ma20_slope_up"]:
        score += 5
        reasons.append("20MA 上揚")

    if benchmark_return is not None:
        relative_strength = stock["return_20"] - benchmark_return

        if relative_strength >= 5:
            score += 25
            reasons.append(f"20 日報酬強於大盤 {relative_strength:.1f}%")
        elif relative_strength > 0:
            score += 15
            reasons.append("20 日報酬優於大盤")
        else:
            risks.append("20 日表現落後大盤")

    if 1.2 <= stock["volume_ratio"] <= 3.5:
        score += 15
        reasons.append("量能健康放大")
    elif stock["volume_ratio"] >= 4:
        score += 5
        risks.append("爆量，留意短線過熱")

    if stock["price"] >= stock["high_60"] * 0.97:
        score += 15
        reasons.append("接近 60 日高點")

    if 2 <= stock["bias_60"] <= 25:
        score += 10
        reasons.append("60MA 乖離合理")
    elif stock["bias_60"] > 25:
        score -= 10
        risks.append("60MA 乖離過大")

    margin_change = margin.get("margin_change")

    if margin.get("success") and margin_change is not None:
        if not stock["is_bull"] and margin_change > 0:
            score -= 10
            risks.append("股價偏弱但融資增加")
        elif stock["is_bull"] and margin_change > 0:
            score -= 3
            risks.append("融資增加，留意追價槓桿")
        elif stock["is_bull"] and margin_change < 0:
            score += 5
            reasons.append("偏多趨勢下融資下降")

    news_score = news.get("impact_score", 0)

    if news_score > 0:
        score += news_score
        reasons.append("近期有正面事件催化")
    elif news_score < 0:
        score += news_score
        risks.append("近期存在新聞風險事件")

    score = max(0, min(100, score))

    if score >= 80:
        grade = "強勢候選"
    elif score >= 65:
        grade = "偏多觀察"
    else:
        grade = "暫不列入強勢清單"

    return {
        "score": score,
        "grade": grade,
        "reasons": reasons,
        "risks": risks,
    }


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


def _collect_technical_signals(symbols: Iterable[str]) -> list[dict]:
    """先平行取得技術面，避免夜間掃描耗時過久。"""
    unique_symbols = tuple(
        dict.fromkeys(
            normalize_taiwan_symbol(symbol)
            for symbol in symbols
            if str(symbol).strip()
        )
    )

    results = []

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        futures = {
            executor.submit(get_stock_signal, symbol, False): symbol
            for symbol in unique_symbols
        }

        for future in as_completed(futures):
            try:
                stock = future.result()
            except Exception as exc:
                LOGGER.warning("技術掃描失敗：%s", exc)
                continue

            if stock.get("success"):
                results.append(stock)

    return results


def _scan_and_rank(
    symbols: Iterable[str],
    limit: int,
    min_score: int,
    preselect_limit: int,
) -> list[dict]:
    """
    兩階段掃描：

    第一階段：全部股票只計算技術與相對強弱。
    第二階段：只有技術面前段班才加入融資與新聞。
    """
    benchmark_returns = get_benchmark_returns()
    technical_stocks = _collect_technical_signals(symbols)
    preliminary = []

    for stock in technical_stocks:
        market_key = "TWOII" if stock["symbol"].endswith(".TWO") else "TWII"

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

    finalists = preliminary[:preselect_limit]
    ranked = []

    for item in finalists:
        stock = item["stock"]
        margin = _safe_margin(stock["symbol"])
        news = _safe_news(stock["symbol"], stock["symbol"])

        strength = calculate_strength_score(
            stock=stock,
            benchmark_return=benchmark_returns[
                "TWOII" if stock["symbol"].endswith(".TWO") else "TWII"
            ],
            margin=margin,
            news=news,
        )

        if strength["score"] < min_score:
            continue

        ranked.append(
            {
                **stock,
                "name": get_stock_name(stock["symbol"], stock["symbol"]),
                "margin": margin,
                "news": news,
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

    return ranked[:limit]


def scan_strong_stocks(
    symbols: Iterable[str],
    limit: int = 5,
) -> list[dict]:
    """
    掃描指定股票清單。

    保留給個人觀察清單或其他模組使用。
    """
    symbol_list = list(symbols)

    return _scan_and_rank(
        symbols=symbol_list,
        limit=limit,
        min_score=0,
        preselect_limit=max(len(symbol_list), limit),
    )


def scan_market_strong_stocks(
    limit: int = 10,
    min_score: int = 65,
    universe_size: int = 150,
) -> list[dict]:
    """
    掃描市場候選池，自動選出真正符合條件的強勢股。

    不使用 TG /add 的觀察清單。
    """
    safe_universe_size = max(
        30,
        min(universe_size, len(LIQUID_TAIWAN_UNIVERSE)),
    )
    safe_limit = max(1, min(limit, 10))
    safe_min_score = max(0, min(min_score, 100))

    return _scan_and_rank(
        symbols=LIQUID_TAIWAN_UNIVERSE[:safe_universe_size],
        limit=safe_limit,
        min_score=safe_min_score,
        preselect_limit=max(25, safe_limit * 3),
    )