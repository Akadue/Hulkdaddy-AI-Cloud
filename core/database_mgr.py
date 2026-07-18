from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_FILE = DATA_DIR / "strong_stock_history.db"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def get_db_connection() -> sqlite3.Connection:
    """取得本機 SQLite 連線。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row

    return connection


def initialize_database() -> None:
    """建立夜間強勢股歷史資料表。"""
    with get_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS nightly_rankings (
                report_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                rank_no INTEGER NOT NULL,
                score INTEGER NOT NULL,
                price REAL NOT NULL,
                entry_label TEXT NOT NULL,
                market_regime TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (report_date, symbol)
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_nightly_rankings_date
            ON nightly_rankings (report_date)
            """
        )


def save_nightly_rankings(
    candidates: list[dict],
    report_date: str,
    market_regime: str,
) -> dict:
    """
    儲存今晚 Top 10，並和上一個有資料的交易日比較。

    回傳新進榜、連續入榜與跌出榜資訊。
    """
    try:
        initialize_database()

        with get_db_connection() as connection:
            cursor = connection.cursor()

            cursor.execute(
                """
                SELECT MAX(report_date) AS previous_date
                FROM nightly_rankings
                WHERE report_date < ?
                """,
                (report_date,),
            )

            previous_date = cursor.fetchone()["previous_date"]
            previous_rows = []

            if previous_date:
                cursor.execute(
                    """
                    SELECT symbol, name, rank_no, score
                    FROM nightly_rankings
                    WHERE report_date = ?
                    ORDER BY rank_no
                    """,
                    (previous_date,),
                )
                previous_rows = [
                    dict(row)
                    for row in cursor.fetchall()
                ]

            previous_symbols = {
                row["symbol"]
                for row in previous_rows
            }

            current_symbols = {
                candidate["symbol"]
                for candidate in candidates
            }

            if previous_date:
                new_symbols = current_symbols - previous_symbols
                continuous_symbols = current_symbols & previous_symbols
            else:
                new_symbols = set()
                continuous_symbols = set()

            dropped = [
                row
                for row in previous_rows
                if row["symbol"] not in current_symbols
            ]

            # 同一天重跑時，覆蓋該日舊資料。
            cursor.execute(
                """
                DELETE FROM nightly_rankings
                WHERE report_date = ?
                """,
                (report_date,),
            )

            created_at = datetime.now(TAIPEI_TZ).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            for rank, candidate in enumerate(candidates, start=1):
                cursor.execute(
                    """
                    INSERT INTO nightly_rankings (
                        report_date,
                        symbol,
                        name,
                        rank_no,
                        score,
                        price,
                        entry_label,
                        market_regime,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_date,
                        candidate["symbol"],
                        candidate["name"],
                        rank,
                        candidate["strength_score"],
                        candidate["price"],
                        candidate["entry_label"],
                        market_regime,
                        created_at,
                    ),
                )

            connection.commit()

        status_by_symbol = {}

        for candidate in candidates:
            symbol = candidate["symbol"]

            if not previous_date:
                status_by_symbol[symbol] = "首次建立基準"
            elif symbol in new_symbols:
                status_by_symbol[symbol] = "新進榜"
            elif symbol in continuous_symbols:
                status_by_symbol[symbol] = "連續入榜"
            else:
                status_by_symbol[symbol] = "本日入榜"

        return {
            "success": True,
            "database_path": str(DATABASE_FILE),
            "previous_date": previous_date,
            "status_by_symbol": status_by_symbol,
            "new_symbols": sorted(new_symbols),
            "continuous_symbols": sorted(continuous_symbols),
            "dropped": dropped,
        }

    except Exception as exc:
        LOGGER.exception("儲存夜間榜單失敗：%s", exc)

        return {
            "success": False,
            "error": str(exc),
            "previous_date": None,
            "status_by_symbol": {},
            "new_symbols": [],
            "continuous_symbols": [],
            "dropped": [],
        }