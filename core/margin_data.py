from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import requests
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)

TWSE_MARGIN_URL = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"
TPEX_MARGIN_URL = (
    "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance"
)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
HEADERS = {
    "User-Agent": "RR-AI-Cloud/1.0 (Taiwan stock research bot)",
}

CODE_FIELDS = (
    "股票代號",
    "證券代號",
    "代號",
    "SecuritiesCompanyCode",
    "Code",
)

NAME_FIELDS = (
    "股票名稱",
    "證券名稱",
    "名稱",
    "SecuritiesCompanyName",
    "Name",
)

MARGIN_PREVIOUS_FIELDS = (
    "融資前日餘額",
    "前日融資餘額",
    "MarginPurchasePreviousBalance",
)

MARGIN_CURRENT_FIELDS = (
    "融資今日餘額",
    "今日融資餘額",
    "MarginPurchaseCurrentBalance",
)

SHORT_PREVIOUS_FIELDS = (
    "融券前日餘額",
    "前日融券餘額",
    "ShortSalePreviousBalance",
)

SHORT_CURRENT_FIELDS = (
    "融券今日餘額",
    "今日融券餘額",
    "ShortSaleCurrentBalance",
)

DATE_FIELDS = (
    "資料日期",
    "日期",
    "Date",
    "DataDate",
)


def _pick_value(row: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    for field in candidates:
        if field in row and row[field] not in (None, "", "--", "-"):
            return row[field]
    return None


def _to_int(value: Any) -> int | None:
    if value in (None, "", "--", "-"):
        return None

    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("data", "records", "aaData", "tables"):
            records = payload.get(key)

            if isinstance(records, list):
                return [item for item in records if isinstance(item, dict)]

    return []


def _fetch_records(url: str) -> list[dict[str, Any]]:
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()

    return _extract_records(response.json())


def _find_stock_row(
    records: list[dict[str, Any]],
    stock_code: str,
) -> dict[str, Any] | None:
    for row in records:
        row_code = str(_pick_value(row, CODE_FIELDS) or "").strip()

        if row_code == stock_code:
            return row

    return None


def get_margin_snapshot(symbol: str) -> dict[str, Any]:
    """
    取得個股最新融資融券餘額。

    回傳的是官方盤後最新資料；不是盤中即時融資餘額。
    """
    normalized_symbol = symbol.strip().upper()
    stock_code = normalized_symbol.split(".")[0]

    is_otc = normalized_symbol.endswith(".TWO")
    source = "TPEX" if is_otc else "TWSE"
    url = TPEX_MARGIN_URL if is_otc else TWSE_MARGIN_URL

    try:
        records = _fetch_records(url)
        row = _find_stock_row(records, stock_code)

        if row is None:
            return {
                "success": False,
                "symbol": normalized_symbol,
                "source": source,
                "error": "官方融資融券資料中找不到此股票代號。",
            }

        margin_previous = _to_int(_pick_value(row, MARGIN_PREVIOUS_FIELDS))
        margin_current = _to_int(_pick_value(row, MARGIN_CURRENT_FIELDS))
        short_previous = _to_int(_pick_value(row, SHORT_PREVIOUS_FIELDS))
        short_current = _to_int(_pick_value(row, SHORT_CURRENT_FIELDS))

        if margin_current is None or short_current is None:
            return {
                "success": False,
                "symbol": normalized_symbol,
                "source": source,
                "error": "官方 API 回傳欄位格式不符，暫時無法解析。",
            }

        margin_change = (
            margin_current - margin_previous
            if margin_previous is not None
            else None
        )
        short_change = (
            short_current - short_previous
            if short_previous is not None
            else None
        )
        short_margin_ratio = (
            round(short_current / margin_current * 100, 2)
            if margin_current > 0
            else None
        )

        source_date = _pick_value(row, DATE_FIELDS)

        return {
            "success": True,
            "symbol": normalized_symbol,
            "name": str(_pick_value(row, NAME_FIELDS) or stock_code),
            "source": source,
            "source_date": str(source_date) if source_date else None,
            "fetched_at": datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "margin_previous": margin_previous,
            "margin_current": margin_current,
            "margin_change": margin_change,
            "short_previous": short_previous,
            "short_current": short_current,
            "short_change": short_change,
            "short_margin_ratio": short_margin_ratio,
        }

    except requests.RequestException as exc:
        LOGGER.warning("取得 %s 融資融券資料失敗：%s", normalized_symbol, exc)
        return {
            "success": False,
            "symbol": normalized_symbol,
            "source": source,
            "error": f"官方資料連線失敗：{exc}",
        }
    except Exception as exc:
        LOGGER.exception("解析 %s 融資融券資料失敗。", normalized_symbol)
        return {
            "success": False,
            "symbol": normalized_symbol,
            "source": source,
            "error": f"融資融券資料解析失敗：{exc}",
        }


def get_margin_interpretation(
    margin_change: int | None,
    stock_is_bull: bool,
) -> str:
    """以趨勢搭配融資單日變化，提供保守的風險說明。"""
    if margin_change is None:
        return "資料不足，無法比較前一交易日融資變化。"

    if stock_is_bull and margin_change > 0:
        return "股價趨勢偏多且融資增加，留意槓桿追價與短線過熱風險。"

    if stock_is_bull and margin_change < 0:
        return "股價趨勢偏多、融資下降，籌碼槓桿壓力相對較低。"

    if not stock_is_bull and margin_change > 0:
        return "股價趨勢偏弱但融資增加，可能存在攤平或接刀風險。"

    if not stock_is_bull and margin_change < 0:
        return "股價與融資同步下降，可能在去槓桿；仍應等待價格止穩。"

    return "融資餘額與前一交易日持平。"