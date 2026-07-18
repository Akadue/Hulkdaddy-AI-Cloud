import html
import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from dotenv import load_dotenv

from core.margin_data import get_margin_interpretation, get_margin_snapshot
from core.news_engine import get_recent_news
from core.stock_engine import calculate_strength_score, get_stock_signal
from core.watchlist_store import (
    add_to_watchlist,
    get_watchlist,
    remove_from_watchlist,
)

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(CURRENT_DIR, ".env"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

STOCK_CODE_PATTERN = re.compile(r"^\d{4,6}(?:\.(?:TW|TWO))?$")

ADD_PATTERN = re.compile(
    r"^(?:/add|新增)\s+(\d{4,6}(?:\.(?:TW|TWO))?)$",
    re.IGNORECASE,
)

REMOVE_PATTERN = re.compile(
    r"^(?:/remove|移除)\s+(\d{4,6}(?:\.(?:TW|TWO))?)$",
    re.IGNORECASE,
)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger(__name__)


class TelegramClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise RuntimeError("未設定 TELEGRAM_TOKEN。")

        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def send_reply(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id

        response = self.session.post(
            f"{self.base_url}/sendMessage",
            json=payload,
            timeout=20,
        )
        response.raise_for_status()

        if not response.json().get("ok"):
            raise RuntimeError("Telegram 未接受訊息。")

    def get_updates(self, offset: int | None) -> list[dict]:
        params: dict[str, object] = {
            "timeout": 30,
            "allowed_updates": ["message"],
        }

        if offset is not None:
            params["offset"] = offset

        response = self.session.get(
            f"{self.base_url}/getUpdates",
            params=params,
            timeout=40,
        )
        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Telegram 更新失敗：{result}")

        return result.get("result", [])


def normalize_symbol(text: str) -> str:
    symbol = text.strip().upper()
    return f"{symbol}.TW" if symbol.isdigit() else symbol


def is_valid_stock_code(text: str) -> bool:
    return bool(STOCK_CODE_PATTERN.fullmatch(text.strip().upper()))


def resolve_stock_symbol(user_input: str) -> dict:
    """先查上市；純數字查不到時自動改查上櫃。"""
    requested = user_input.strip().upper()
    stock = get_stock_signal(normalize_symbol(requested))

    if not stock["success"] and requested.isdigit():
        stock = get_stock_signal(f"{requested}.TWO")

    return stock


def format_watchlist() -> str:
    symbols = get_watchlist()

    if not symbols:
        return (
            "📋 <b>目前追蹤清單是空的</b>\n"
            "請私訊 Bot 使用 <code>/add 7610</code> 新增股票。"
        )

    symbol_lines = "\n".join(
        f"{index}. <code>{symbol}</code>"
        for index, symbol in enumerate(symbols, start=1)
    )

    return f"📋 <b>目前追蹤清單</b>\n{symbol_lines}"


def remove_requested_symbol(user_input: str) -> str | None:
    """支援 /remove 7610，也支援指定 .TW 或 .TWO。"""
    requested = user_input.strip().upper()

    if requested.isdigit():
        candidates = (f"{requested}.TW", f"{requested}.TWO")
    else:
        candidates = (requested,)

    for symbol in candidates:
        if remove_from_watchlist(symbol):
            return symbol

    return None


def get_market_status() -> dict:
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
                raise ValueError(f"{yahoo_symbol} 歷史資料不足。")

            close = history["Close"].astype(float)
            ma20 = close.rolling(20).mean().iloc[-1]

            result[key] = {
                "close": float(close.iloc[-1]),
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


def format_news_section(news: dict) -> str:
    if not news.get("success") or not news.get("items"):
        return "📰 <b>近 48 小時新聞</b>：暫無可用標題。"

    lines = ["📰 <b>近 48 小時新聞</b>"]

    for item in news["items"]:
        icon = (
            "🟢"
            if item["category"] == "catalyst"
            else "🔴"
            if item["category"] == "risk"
            else "⚪"
        )

        title = html.escape(item["title"])
        link = html.escape(item["link"], quote=True)

        lines.append(
            f'{icon} <a href="{link}">{title}</a>（{item["published_at"]}）'
        )

    return "\n".join(lines)


def format_margin_section(margin: dict, stock_is_bull: bool) -> str:
    if not margin.get("success"):
        return "💳 <b>融資融券</b>：官方資料暫時無法取得。"

    change = margin["margin_change"]
    change_text = f"{change:+,}" if change is not None else "資料不足"

    ratio = margin["short_margin_ratio"]
    ratio_text = f"{ratio:.2f}%" if ratio is not None else "不適用"

    return (
        "💳 <b>融資融券（盤後資料）</b>\n"
        f"融資餘額：{margin['margin_current']:,}｜單日：{change_text}\n"
        f"融券餘額：{margin['short_current']:,}｜"
        f"融券／融資比：{ratio_text}\n"
        f"解讀：{get_margin_interpretation(change, stock_is_bull)}"
    )


def format_report(
    stock: dict,
    market: dict,
    margin: dict,
    news: dict,
    strength: dict,
) -> str:
    market_key = "TWOII" if stock["symbol"].endswith(".TWO") else "TWII"
    index_name = "櫃買指數" if market_key == "TWOII" else "加權指數"
    index = market[market_key]

    name = html.escape(str(stock["name"]))
    symbol = html.escape(str(stock["symbol"]))
    reasons = "、".join(strength["reasons"]) or "資料不足"
    risks = "、".join(strength["risks"]) or "未偵測明顯風險"
    now = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")

    return (
        "📊 <b>AI 個股強勢與籌碼報告</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>{name}</b> ({symbol})\n"
        f"💰 收盤價：<b>{stock['price']:.2f}</b>\n"
        f"📈 趨勢：{html.escape(stock['trend_status'])}\n"
        f"📊 量能比：{stock['volume_ratio']:.2f} 倍\n"
        f"📏 20 日報酬：{stock['return_20']:.2f}%\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>強勢分數：{strength['score']}／100</b>\n"
        f"評級：{strength['grade']}\n"
        f"加分原因：{html.escape(reasons)}\n"
        f"風險提示：{html.escape(risks)}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🌤️ {index_name}：{index['close']:,.2f}｜"
        f"{'偏多' if index['is_bull'] else '偏弱'}\n\n"
        f"{format_margin_section(margin, stock['is_bull'])}\n\n"
        f"{format_news_section(news)}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ 更新時間：{now}（台北）\n"
        "⚠️ 僅供研究參考，非買賣建議。"
    )


def handle_message(
    client: TelegramClient,
    chat_id: int,
    user_text: str,
    message_id: int | None,
    chat_type: str,
) -> None:
    raw_text = user_text.strip()
    command = raw_text.upper()

    add_match = ADD_PATTERN.fullmatch(raw_text)
    remove_match = REMOVE_PATTERN.fullmatch(raw_text)

    # 群組允許 /list，但新增與移除必須私訊 Bot。
    if chat_type in {"group", "supergroup"} and (
        add_match or remove_match
    ):
        client.send_reply(
            chat_id,
            "🔒 新增與移除追蹤清單，請私訊 Bot 操作。",
            message_id,
        )
        return

    if command in {"/LIST", "/WATCHLIST", "清單"}:
        client.send_reply(chat_id, format_watchlist(), message_id)
        return

    if add_match:
        requested_symbol = add_match.group(1)
        stock = resolve_stock_symbol(requested_symbol)

        if not stock["success"]:
            client.send_reply(
                chat_id,
                (
                    "❌ 無法新增：找不到 "
                    f"<code>{html.escape(requested_symbol)}</code>。"
                ),
                message_id,
            )
            return

        if add_to_watchlist(stock["symbol"]):
            client.send_reply(
                chat_id,
                (
                    f"✅ 已新增 <b>{html.escape(stock['name'])}</b> "
                    f"（<code>{stock['symbol']}</code>）至追蹤清單。\n"
                    "每日市場報告會開始納入此股票。"
                ),
                message_id,
            )
        else:
            client.send_reply(
                chat_id,
                f"ℹ️ <code>{stock['symbol']}</code> 已在追蹤清單內。",
                message_id,
            )
        return

    if remove_match:
        removed_symbol = remove_requested_symbol(remove_match.group(1))

        if removed_symbol:
            client.send_reply(
                chat_id,
                f"🗑️ 已移除 <code>{removed_symbol}</code>。",
                message_id,
            )
        else:
            client.send_reply(
                chat_id,
                "ℹ️ 此股票不在追蹤清單內。",
                message_id,
            )
        return

    if command in {"/START", "/HELP", "START", "HELP", "你好", "HELLO"}:
        client.send_reply(
            chat_id,
            (
                "🤖 <b>台股強勢股分析 Bot</b>\n\n"
                "查詢股票：<code>2330</code>、<code>6488.TWO</code>\n\n"
                "追蹤清單指令：\n"
                "• <code>/list</code> 在群組或私訊查看清單\n"
                "• <code>/add 7610</code> 私訊新增追蹤\n"
                "• <code>/remove 7610</code> 私訊移除追蹤"
            ),
            message_id,
        )
        return

    if not is_valid_stock_code(command):
        if chat_type in {"group", "supergroup"}:
            return

        client.send_reply(
            chat_id,
            "⚠️ 請輸入股票代號，例如 <code>2330</code>。",
            message_id,
        )
        return

    stock = resolve_stock_symbol(command)

    if not stock["success"]:
        client.send_reply(
            chat_id,
            f"❌ 查詢失敗：{html.escape(stock['error'])}",
            message_id,
        )
        return

    market = get_market_status()

    if not market["success"]:
        client.send_reply(
            chat_id,
            "⚠️ 大盤資料暫時無法取得，請稍後再試。",
            message_id,
        )
        return

    market_key = "TWOII" if stock["symbol"].endswith(".TWO") else "TWII"
    margin = get_margin_snapshot(stock["symbol"])
    news = get_recent_news(stock["symbol"], stock["name"])

    strength = calculate_strength_score(
        stock=stock,
        benchmark_return=market[market_key]["return_20"],
        margin=margin,
        news=news,
    )

    client.send_reply(
        chat_id,
        format_report(stock, market, margin, news, strength),
        message_id,
    )


def start_bot() -> None:
    client = TelegramClient(TELEGRAM_TOKEN)
    offset: int | None = None
    retry_delay = 2

    LOGGER.info("Telegram Bot 已啟動。")

    while True:
        try:
            updates = client.get_updates(offset)
            retry_delay = 2

            for update in updates:
                offset = int(update["update_id"]) + 1
                message = update.get("message")

                if not message or not isinstance(message.get("text"), str):
                    continue

                try:
                    chat = message["chat"]

                    handle_message(
                        client=client,
                        chat_id=chat["id"],
                        user_text=message["text"],
                        message_id=message.get("message_id"),
                        chat_type=chat.get("type", "private"),
                    )
                except Exception:
                    LOGGER.exception("處理 Telegram 訊息失敗。")

        except requests.RequestException as exc:
            LOGGER.warning("Telegram 連線異常：%s", exc)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

        except Exception:
            LOGGER.exception("Telegram 輪詢錯誤。")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    start_bot()