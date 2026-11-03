"""Local cache store for market prices with atomic persistence and freshness calculation."""

from __future__ import annotations
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.constants import CACHE_FILE
from storage.atomic_json import atomic_write_json, safe_read_json


class CacheStore:
    """Manages local persistence of the latest valid market response and price history."""

    MAX_HISTORY_POINTS = 24

    def __init__(self, cache_path: Path = CACHE_FILE) -> None:
        self.cache_path = Path(cache_path)
        self._data: Dict[str, Any] = self._load()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "latest_response": {},
            "previous_prices": {},
            "price_history": {},
            "last_fetch_timestamp": None,
            "last_fetch_iso": None,
            "source": None,
        }

    def _load(self) -> Dict[str, Any]:
        raw = safe_read_json(self.cache_path, default=self._default_state())
        state = self._default_state()
        if isinstance(raw.get("latest_response"), dict):
            state["latest_response"] = raw["latest_response"]
        if isinstance(raw.get("previous_prices"), dict):
            state["previous_prices"] = raw["previous_prices"]
        if isinstance(raw.get("price_history"), dict):
            state["price_history"] = raw["price_history"]
        if isinstance(raw.get("last_fetch_timestamp"), (int, float)):
            state["last_fetch_timestamp"] = float(raw["last_fetch_timestamp"])
        if isinstance(raw.get("last_fetch_iso"), str):
            state["last_fetch_iso"] = raw["last_fetch_iso"]
        if isinstance(raw.get("source"), str):
            state["source"] = raw["source"]
        return state

    def save(self) -> None:
        atomic_write_json(self.cache_path, self._data)

    def has_cached_data(self) -> bool:
        return bool(self._data.get("latest_response"))

    def get_latest_response(self) -> Dict[str, Any]:
        return dict(self._data.get("latest_response") or {})

    def get_previous_prices(self) -> Dict[str, float]:
        return dict(self._data.get("previous_prices") or {})

    def get_price_history(self, symbol: str) -> List[float]:
        hist = (self._data.get("price_history") or {}).get(symbol, [])
        if isinstance(hist, list):
            return [float(x) for x in hist if isinstance(x, (int, float))]
        return []

    def get_last_fetch_timestamp(self) -> Optional[float]:
        ts = self._data.get("last_fetch_timestamp")
        return float(ts) if ts is not None else None

    def get_last_fetch_iso(self) -> Optional[str]:
        return self._data.get("last_fetch_iso")

    def get_source(self) -> Optional[str]:
        return self._data.get("source")

    def update_success(
        self,
        response_data: Dict[str, Any],
        fetched_at: Optional[float] = None,
        source: str = "navasan",
    ) -> None:
        """Persist a new successful API response while preserving valid symbols and history.

        Never overwrites valid cached prices with empty/invalid data.
        """
        if not isinstance(response_data, dict) or not response_data:
            return

        now_ts = float(fetched_at if fetched_at is not None else time.time())
        now_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

        current_latest = dict(self._data.get("latest_response") or {})
        prev_prices = dict(self._data.get("previous_prices") or {})
        history_map = dict(self._data.get("price_history") or {})

        from services.formatter import parse_numeric

        for sym, item in response_data.items():
            if not isinstance(item, dict):
                continue
            new_val = parse_numeric(item.get("value"))
            if new_val is None or new_val <= 0:
                # Never overwrite a valid cached price with zero or unparseable value
                continue

            # Record previous price if we had an older distinct valid price
            old_item = current_latest.get(sym)
            if isinstance(old_item, dict):
                old_val = parse_numeric(old_item.get("value"))
                if old_val is not None and old_val > 0 and old_val != new_val:
                    prev_prices[sym] = old_val

            current_latest[sym] = item

            # Maintain sparkline price history (up to MAX_HISTORY_POINTS)
            sym_hist = list(history_map.get(sym) or [])
            sym_hist.append(new_val)
            if len(sym_hist) > self.MAX_HISTORY_POINTS:
                sym_hist = sym_hist[-self.MAX_HISTORY_POINTS :]
            history_map[sym] = sym_hist

        self._data["latest_response"] = current_latest
        self._data["previous_prices"] = prev_prices
        self._data["price_history"] = history_map
        self._data["last_fetch_timestamp"] = now_ts
        self._data["last_fetch_iso"] = now_iso
        self._data["source"] = source
        self.save()

    def clear(self) -> None:
        """Clear all cached market prices and timestamps."""
        self._data = self._default_state()
        self.save()

    def get_cache_age_seconds(self, now: Optional[float] = None) -> Optional[float]:
        last_ts = self.get_last_fetch_timestamp()
        if last_ts is None:
            return None
        current = float(now if now is not None else time.time())
        return max(0.0, current - last_ts)
