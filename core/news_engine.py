from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import requests
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
NEWS_LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "48"))

# 可持續自行補充常查股票的中文、英文與常用簡稱。
STOCK_ALIASES = {
    "2330": (
        "台積電",
        "台灣積體電路",
        "TSMC",
        "Taiwan Semiconductor",
    ),
    "2454": (
        "聯發科",
        "聯發科技",
        "MediaTek",
    ),
}

POSITIVE_KEYWORDS = (
    "營收",
    "成長",
    "創高",
    "接單",
    "訂單",
    "擴產",
    "上修",
    "調升",
    "獲利",
    "法說",
    "買回",
    "股利",
)

RISK_KEYWORDS = (
    "虧損",
    "衰退",
    "下修",
    "訴訟",
    "調查",
    "事故",
    "火災",
    "停工",
    "違約",
    "跌停",
    "減資",
    "處分",
    "風險",
)


def classify_news(title: str) -> str:
    """依新聞標題給予保守的事件分類。"""
    text = title.lower()

    if any(keyword.lower() in text for keyword in RISK_KEYWORDS):
        return "risk"

    if any(keyword.lower() in text for keyword in POSITIVE_KEYWORDS):
        return "catalyst"

    return "neutral"


def build_news_query(symbol: str, company_name: str) -> str:
    """
    建立多關鍵字搜尋式。

    例如 2330 會同時查：
    2330、台積電、台灣積體電路、TSMC、英文公司名稱。
    """
    stock_code = symbol.split(".")[0]
    aliases = STOCK_ALIASES.get(stock_code, ())

    keywords = [
        stock_code,
        company_name,
        *aliases,
    ]

    unique_keywords = []
    seen = set()

    for keyword in keywords:
        cleaned = str(keyword).strip()

        if cleaned and cleaned.lower() not in seen:
            unique_keywords.append(cleaned)
            seen.add(cleaned.lower())

    return " OR ".join(
        f'"{keyword}"'
        for keyword in unique_keywords
    )


def get_recent_news(
    symbol: str,
    company_name: str,
    limit: int = 3,
) -> dict:
    """
    取得近期香港新聞標題。

    新聞僅作事件與風險輔助，不直接產生買賣建議。
    """
    query = build_news_query(symbol, company_name)

    url = (
        "https://news.google.com/rss/search?"
        + urlencode(
            {
                "q": query,
                "hl": "zh-TW",
                "gl": "TW",
                "ceid": "TW:zh-Hant",
            }
        )
    )

    try:
        response = requests.get(
            url,
            headers={"User-Agent": "RR-AI-Cloud/1.0"},
            timeout=15,
        )
        response.raise_for_status()

        root = ET.fromstring(response.content)
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=NEWS_LOOKBACK_HOURS
        )

        items = []
        seen_titles = set()

        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            published_raw = (item.findtext("pubDate") or "").strip()

            if not title or title in seen_titles:
                continue

            try:
                published_at = parsedate_to_datetime(published_raw)

                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)

                if published_at < cutoff:
                    continue

                published_display = published_at.astimezone(
                    TAIPEI_TZ
                ).strftime("%m-%d %H:%M")

            except (TypeError, ValueError):
                published_display = "時間未提供"

            seen_titles.add(title)

            items.append(
                {
                    "title": title,
                    "link": link,
                    "published_at": published_display,
                    "category": classify_news(title),
                }
            )

            if len(items) >= limit:
                break

        impact_score = 0

        for item in items:
            if item["category"] == "catalyst":
                impact_score += 3
            elif item["category"] == "risk":
                impact_score -= 6

        # 新聞不能凌駕技術面與風控，因此限制加減分幅度。
        impact_score = max(-10, min(5, impact_score))

        return {
            "success": True,
            "items": items,
            "impact_score": impact_score,
            "source": "Google News RSS",
            "query": query,
        }

    except Exception as exc:
        LOGGER.warning("取得 %s 新聞失敗：%s", symbol, exc)

        return {
            "success": False,
            "items": [],
            "impact_score": 0,
            "error": str(exc),
            "query": query,
        }