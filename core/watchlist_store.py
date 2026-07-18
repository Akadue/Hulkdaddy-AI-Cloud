from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"


def get_watchlist() -> list[str]:
    """讀取本機追蹤清單。"""
    if not WATCHLIST_FILE.exists():
        return []

    try:
        payload = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        symbols = payload.get("symbols", [])

        if not isinstance(symbols, list):
            return []

        return [
            str(symbol).strip().upper()
            for symbol in symbols
            if str(symbol).strip()
        ]
    except (OSError, json.JSONDecodeError):
        return []


def _save_watchlist(symbols: list[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "symbols": symbols,
    }

    temporary_file = WATCHLIST_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(WATCHLIST_FILE)


def add_to_watchlist(symbol: str) -> bool:
    """新增代號；已存在時回傳 False。"""
    normalized = symbol.strip().upper()
    symbols = get_watchlist()

    if normalized in symbols:
        return False

    symbols.append(normalized)
    _save_watchlist(symbols)
    return True


def remove_from_watchlist(symbol: str) -> bool:
    """移除代號；不存在時回傳 False。"""
    normalized = symbol.strip().upper()
    symbols = get_watchlist()

    if normalized not in symbols:
        return False

    symbols.remove(normalized)
    _save_watchlist(symbols)
    return True