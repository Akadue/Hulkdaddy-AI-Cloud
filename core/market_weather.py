from __future__ import annotations

import html
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from dotenv import load_dotenv

try:
    from core.database_mgr import save_nightly_rankings
    from core.nightly_scanner import scan_nightly_strong_stocks
    from core.stock_engine import get_stock_signal
except ImportError:
    from database_mgr import save_nightly_rankings
    from nightly_scanner import scan_nightly_strong_stocks
    from stock_engine import get_stock_signal


CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
load_dotenv(PROJECT_ROOT / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_GROUP_CHAT_ID")
    or os.getenv("TELEGRAM_CHAT_ID", "")
).strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

REPORT_MODE = os.getenv("REPORT_MODE", "auto").strip().lower()

MARKET_REFERENCE = tuple(
    item.strip().upper()
    for item in os.getenv("MARKET_REFERENCE", "2330.TW,2454.TW").split(",")
    if item.strip()
)

STRONG_STOCK_LIMIT = max(
    1,
    min(10, int(os.getenv("STRONG_STOCK_LIMIT", "10"))),
)

MIN_STRENGTH_SCORE = max(
    0,
    min(100, int(os.getenv("MIN_STRENGTH_SCORE", "65"))),
)

MARKET_UNIVERSE_LIMIT = max(
    30,
    min(180, int(os.getenv("MARKET_UNIVERSE_LIMIT", "150"))),
)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
MAX_TELEGRAM_MESSAGE_LENGTH = 4096

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger(__name__)


def is_full_report() -> bool:
    """21:00 後為夜間完整選股；其他時間為大盤報告。"""
    if REPORT_MODE == "full":
        return True

    if REPORT_MODE == "market":
        return False

    return datetime.now(TAIPEI_TZ).hour >= 21


def get_market_weather() -> dict:
    """取得加權、櫃買指數的趨勢與 20 日報酬率。"""
    result: dict[str, object] = {"success": True}

    try:
        for key, yahoo_symbol in {
            "TWII": "^TWII",
            "TWOII": "^TWOII",
        }.items():
            history = yf.Ticker(yahoo_symbol).history(
                period="3mo",
                auto_adjust=False,
            )

            if history.empty or len(history) < 21:
                raise ValueError(f"{yahoo_symbol} 資料不足。")

            close = history["Close"].astype(float)
            ma20 = close.rolling(20).mean().iloc[-1]

            result[key] = {
                "close": float(close.iloc[-1]),
                "ma20": float(ma20),
                "is_bull": bool(close.iloc[-1] > ma20),
                "return_20": float(
                    (close.iloc[-1] / close.iloc[-21] - 1) * 100
                ),
            }

        return result

    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }


def get_market_regime(market: dict) -> tuple[str, int]:
    """
    加權與櫃買同時跌破 20MA 時，進入防禦模式，
    強勢股入榜門檻至少提高為 75 分。
    """
    twii_is_bull = market["TWII"]["is_bull"]
    twoii_is_bull = market["TWOII"]["is_bull"]

    if not twii_is_bull and not twoii_is_bull:
        return "防禦模式", max(75, MIN_STRENGTH_SCORE)

    return "一般模式", MIN_STRENGTH_SCORE


def get_reference_report(symbol: str, market: dict) -> list[str]:
    """白天報告使用：顯示 2330／2454 的大盤相對位階。"""
    stock = get_stock_signal(symbol)

    if not stock.get("success", False):
        return [f"• {symbol}：資料暫時無法取得"]

    market_key = "TWOII" if stock["symbol"].endswith(".TWO") else "TWII"
    market_name = "櫃買指數" if market_key == "TWOII" else "加權指數"
    benchmark = market[market_key]

    relative_return = stock["return_20"] - benchmark["return_20"]

    if stock["is_bull"] and relative_return > 0:
        status = "強於大盤"
    elif stock["is_bull"]:
        status = "站上月線，但短線弱於大盤"
    elif relative_return > 0:
        status = "跌破月線，但相對跌幅較小"
    else:
        status = "弱於大盤"

    name = html.escape(str(stock["name"]))

    return [
        f"📌 <b>{name}</b> ({stock['symbol']})",
        f"   • 股價：{stock['price']:.2f}",
        (
            f"   • 位階："
            f"{'站上 20MA' if stock['price'] > stock['ma20'] else '跌破 20MA'}"
            f"｜60MA 乖離 {stock['bias_60']:.2f}%"
        ),
        (
            f"   • 20 日報酬：{stock['return_20']:.2f}%｜"
            f"{market_name}：{benchmark['return_20']:.2f}%"
        ),
        f"   • 相對強弱：{relative_return:+.2f}%｜<b>{status}</b>",
    ]


def format_daytime_report(market: dict) -> str:
    """09:30、11:00、13:30 的大盤報告。"""
    twii = market["TWII"]
    twoii = market["TWOII"]

    lines = [
        "🌤️ <b>台股盤中大盤氣象報告</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        (
            f"🏛️ 加權指數：<b>{twii['close']:,.2f}</b>｜"
            f"{'站上 20MA（偏多）' if twii['is_bull'] else '跌破 20MA（偏弱）'}"
        ),
        (
            f"🏛️ 櫃買指數：<b>{twoii['close']:,.2f}</b>｜"
            f"{'站上 20MA（偏多）' if twoii['is_bull'] else '跌破 20MA（偏弱）'}"
        ),
        "━━━━━━━━━━━━━━━━━━━━",
        "📍 <b>權值股相對大盤位階</b>",
    ]

    for symbol in MARKET_REFERENCE:
        lines.extend(get_reference_report(symbol, market))

    timestamp = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")

    lines.extend(
        [
            f"⏰ 播報時間：{timestamp}（台北）",
            "⚠️ 僅供研究參考，非買賣建議。",
        ]
    )

    return "\n".join(lines)


def format_news_line(news: dict) -> str:
    if not news.get("success") or not news.get("items"):
        return "   • 新聞：近期沒有可用標題"

    item = news["items"][0]
    title = html.escape(str(item["title"]))

    icon = (
        "🟢"
        if item.get("category") == "catalyst"
        else "🔴"
        if item.get("category") == "risk"
        else "⚪"
    )

    return f"   • 新聞：{icon} {title}"


def format_history_changes(history_result: dict) -> list[str]:
    """整理新進、連續與跌出榜資訊。"""
    if not history_result.get("success"):
        return ["📌 榜單歷史：儲存失敗，本次不顯示異動。"]

    if not history_result.get("previous_date"):
        return ["📌 榜單歷史：今日首次建立基準，明日開始比對異動。"]

    lines = [
        f"📌 <b>榜單異動（對比 {history_result['previous_date']}）</b>",
    ]

    new_symbols = history_result.get("new_symbols", [])
    continuous_symbols = history_result.get("continuous_symbols", [])
    dropped = history_result.get("dropped", [])

    lines.append(
        f"• 新進榜：{'、'.join(new_symbols) if new_symbols else '無'}"
    )
    lines.append(
        f"• 連續入榜：{'、'.join(continuous_symbols) if continuous_symbols else '無'}"
    )

    if dropped:
        dropped_text = "、".join(
            f"{item['name']}({item['symbol']})"
            for item in dropped[:5]
        )
        lines.append(f"• 跌出榜：{dropped_text}")
    else:
        lines.append("• 跌出榜：無")

    return lines


def format_nightly_report(
    candidates: list[dict],
    market_regime: str,
    min_score: int,
    history_result: dict,
) -> str:
    """只輸出系統自動選出的強勢股，不含觀察清單與大盤。"""
    lines = [
        "🌙 <b>AI 強勢股 Top 10</b>",
        (
            f"風控模式：<b>{market_regime}</b>｜"
            f"入榜門檻：<b>{min_score} 分</b>"
        ),
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if not candidates:
        lines.append(
            "目前沒有股票同時符合趨勢、強弱與風控門檻。"
        )
        lines.append("保留現金、等待下一次訊號。")

    for rank, stock in enumerate(candidates, start=1):
        name = html.escape(str(stock["name"]))
        symbol = html.escape(str(stock["symbol"]))
        status = html.escape(
            str(stock.get("history_status", "本日入榜"))
        )

        reasons = html.escape(
            "、".join(stock["strength_reasons"]) or "資料不足"
        )
        risks = html.escape(
            "、".join(stock["strength_risks"]) or "未偵測明顯風險"
        )

        lines.extend(
            [
                (
                    f"{rank}. <b>{name}</b> ({symbol})｜"
                    f"<b>{stock['strength_score']} 分</b>｜"
                    f"📌 {status}"
                ),
                (
                    f"   • 進場標籤："
                    f"<b>{stock['entry_label']}</b>"
                ),
                (
                    f"   • 收盤：{stock['price']:.2f}｜"
                    f"20 日報酬：{stock['return_20']:.2f}%｜"
                    f"20 日波動：{stock['volatility_20']:.2f}%"
                ),
                f"   • 訊號：{reasons}",
                f"   • 風險：{risks}",
                format_news_line(stock["news"]),
                "---------------------",
            ]
        )

    lines.extend(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            *format_history_changes(history_result),
        ]
    )

    timestamp = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")

    lines.extend(
        [
            f"⏰ 選股時間：{timestamp}（台北）",
            "⚠️ 僅供研究參考，非買賣建議。",
        ]
    )

    return "\n".join(lines)


def split_message(text: str) -> list[str]:
    """Telegram 單則訊息超過 4096 字元時自動分段。"""
    if len(text) <= MAX_TELEGRAM_MESSAGE_LENGTH:
        return [text]

    chunks = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > MAX_TELEGRAM_MESSAGE_LENGTH:
            if current:
                chunks.append(current)
            current = line
        else:
            current += line

    if current:
        chunks.append(current)

    return chunks


def send_telegram_broadcast(text: str) -> None:
    """推播訊息至 Telegram。"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("未設定 TELEGRAM_TOKEN 或 TELEGRAM_CHAT_ID。")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    with requests.Session() as session:
        for message in split_message(text):
            response = session.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            response.raise_for_status()

            if not response.json().get("ok"):
                raise RuntimeError("Telegram 未接受市場報告。")


def main() -> None:
    market = get_market_weather()

    if not market["success"]:
        raise RuntimeError(f"大盤資料失敗：{market['error']}")

    if is_full_report():
        market_regime, effective_min_score = get_market_regime(market)

        candidates = scan_nightly_strong_stocks(
            limit=STRONG_STOCK_LIMIT,
            min_score=effective_min_score,
            universe_size=MARKET_UNIVERSE_LIMIT,
        )

        report_date = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")

        history_result = save_nightly_rankings(
            candidates=candidates,
            report_date=report_date,
            market_regime=market_regime,
        )

        status_by_symbol = history_result.get(
            "status_by_symbol",
            {},
        )

        for candidate in candidates:
            candidate["history_status"] = status_by_symbol.get(
                candidate["symbol"],
                "本日入榜",
            )

        report = format_nightly_report(
            candidates=candidates,
            market_regime=market_regime,
            min_score=effective_min_score,
            history_result=history_result,
        )

        LOGGER.info("夜間強勢股報告已完成。")

    else:
        report = format_daytime_report(market)
        LOGGER.info("盤中大盤報告已完成。")

    send_telegram_broadcast(report)
    LOGGER.info("Telegram 推播完成。")


if __name__ == "__main__":
    main()