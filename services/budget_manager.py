"""Centralized API request budget manager enforcing the 120/month hard cap and daily limits."""

from __future__ import annotations
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.constants import (
    BUDGET_FILE,
    DAILY_SCHEDULED_LIMIT,
    DEFAULT_REFRESH_INTERVAL_HOURS,
    MIN_REFRESH_INTERVAL_HOURS,
    MONTHLY_HARD_LIMIT,
    MONTHLY_RESERVE_BUDGET,
    MONTHLY_SCHEDULED_BUDGET,
)
from services.i18n import tr
from storage.atomic_json import atomic_write_json, safe_read_json


@dataclass(frozen=True)
class BudgetStatus:
    """Snapshot of the current local API request budget."""
    month_key: str
    monthly_total_used: int
    monthly_scheduled_used: int
    monthly_manual_used: int
    daily_scheduled_used: int
    daily_total_used: int
    failed_count: int
    monthly_hard_limit: int = MONTHLY_HARD_LIMIT
    monthly_scheduled_budget: int = MONTHLY_SCHEDULED_BUDGET
    daily_scheduled_limit: int = DAILY_SCHEDULED_LIMIT
    last_request_timestamp: Optional[float] = None
    last_scheduled_timestamp: Optional[float] = None

    @property
    def monthly_remaining(self) -> int:
        return max(0, self.monthly_hard_limit - self.monthly_total_used)

    @property
    def scheduled_remaining(self) -> int:
        return max(
            0,
            min(
                self.monthly_scheduled_budget - self.monthly_scheduled_used,
                self.monthly_remaining,
            ),
        )

    @property
    def reserve_remaining(self) -> int:
        """Requests remaining in the 30-request manual reserve."""
        return max(0, min(MONTHLY_RESERVE_BUDGET, self.monthly_remaining))

    @property
    def is_exhausted(self) -> bool:
        return self.monthly_total_used >= self.monthly_hard_limit

    @property
    def is_in_reserve(self) -> bool:
        """True if scheduled budget is reached or remaining requests are within the 30 reserve."""
        return (
            self.monthly_scheduled_used >= self.monthly_scheduled_budget
            or self.monthly_remaining <= MONTHLY_RESERVE_BUDGET
        )


class RequestBudgetManager:
    """Enforces monthly (120 total / 90 scheduled) and daily (3 scheduled) API limits.

    Every request is counted immediately upon dispatch (before network I/O completes),
    ensuring timeouts and HTTP errors are never treated as free.
    """

    def __init__(
        self,
        budget_path: Path = BUDGET_FILE,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self.budget_path = Path(budget_path)
        self._time_fn = time_fn
        self._lock = threading.RLock()
        self._in_flight = False
        self._state: Dict[str, Any] = self._load()
        self._rollover_if_needed(self._time_fn())

    @staticmethod
    def _month_key(ts: float) -> str:
        dt = datetime.fromtimestamp(ts)
        return f"{dt.year:04d}-{dt.month:02d}"

    @staticmethod
    def _date_key(ts: float) -> str:
        dt = datetime.fromtimestamp(ts)
        return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"

    def _default_state(self, now_ts: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now_ts if now_ts is not None else self._time_fn())
        return {
            "current_month": self._month_key(ts),
            "monthly_total_count": 0,
            "monthly_scheduled_count": 0,
            "monthly_manual_count": 0,
            "failed_request_count": 0,
            "last_request_timestamp": None,
            "last_scheduled_timestamp": None,
            "request_log": [],
        }

    def _load(self) -> Dict[str, Any]:
        now_ts = self._time_fn()
        raw = safe_read_json(self.budget_path, default=self._default_state(now_ts))
        state = self._default_state(now_ts)
        if isinstance(raw.get("current_month"), str) and len(raw["current_month"]) == 7:
            state["current_month"] = raw["current_month"]
        for int_key in (
            "monthly_total_count",
            "monthly_scheduled_count",
            "monthly_manual_count",
            "failed_request_count",
        ):
            val = raw.get(int_key)
            if isinstance(val, int) and val >= 0:
                state[int_key] = val
        for ts_key in ("last_request_timestamp", "last_scheduled_timestamp"):
            val = raw.get(ts_key)
            if isinstance(val, (int, float)):
                state[ts_key] = float(val)
        if isinstance(raw.get("request_log"), list):
            state["request_log"] = [
                entry for entry in raw["request_log"] if isinstance(entry, dict)
            ]
        return state

    def _save(self) -> None:
        atomic_write_json(self.budget_path, self._state)

    def _rollover_if_needed(self, now_ts: float) -> bool:
        """Reset monthly counters if the calendar month has changed."""
        with self._lock:
            current_month = self._month_key(now_ts)
            if self._state.get("current_month") != current_month:
                self._state = self._default_state(now_ts)
                self._save()
                return True
            return False

    def _daily_counts(self, now_ts: float) -> Tuple[int, int]:
        """Return (daily_scheduled_count, daily_total_count) for the calendar day of `now_ts`."""
        today_str = self._date_key(now_ts)
        sched = 0
        total = 0
        for entry in self._state.get("request_log", []):
            if entry.get("date") == today_str:
                total += 1
                if entry.get("kind") == "scheduled":
                    sched += 1
        return sched, total

    @property
    def is_in_flight(self) -> bool:
        with self._lock:
            return self._in_flight

    def get_status(self, now: Optional[float] = None) -> BudgetStatus:
        with self._lock:
            now_ts = float(now if now is not None else self._time_fn())
            self._rollover_if_needed(now_ts)
            daily_sched, daily_total = self._daily_counts(now_ts)
            return BudgetStatus(
                month_key=self._state["current_month"],
                monthly_total_used=int(self._state["monthly_total_count"]),
                monthly_scheduled_used=int(self._state["monthly_scheduled_count"]),
                monthly_manual_used=int(self._state["monthly_manual_count"]),
                daily_scheduled_used=daily_sched,
                daily_total_used=daily_total,
                failed_count=int(self._state["failed_request_count"]),
                last_request_timestamp=self._state.get("last_request_timestamp"),
                last_scheduled_timestamp=self._state.get("last_scheduled_timestamp"),
            )

    def can_issue_scheduled_request(
        self,
        refresh_interval_hours: int = DEFAULT_REFRESH_INTERVAL_HOURS,
        now: Optional[float] = None,
        lang: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Verify whether a scheduled (automatic) API request is permitted right now."""
        with self._lock:
            now_ts = float(now if now is not None else self._time_fn())
            self._rollover_if_needed(now_ts)

            if self._in_flight:
                return False, tr("budget.in_flight", lang=lang)

            total_used = int(self._state["monthly_total_count"])
            if total_used >= MONTHLY_HARD_LIMIT:
                return False, tr("budget.monthly_exhausted", lang=lang)

            sched_used = int(self._state["monthly_scheduled_count"])
            if sched_used >= MONTHLY_SCHEDULED_BUDGET:
                return False, tr("budget.scheduled_exhausted", lang=lang)

            daily_sched, _ = self._daily_counts(now_ts)
            if daily_sched >= DAILY_SCHEDULED_LIMIT:
                return (
                    False,
                    tr("budget.daily_exhausted", lang=lang, limit=DAILY_SCHEDULED_LIMIT),
                )

            effective_hours = max(MIN_REFRESH_INTERVAL_HOURS, int(refresh_interval_hours))
            min_interval_sec = effective_hours * 3600.0
            last_sched = self._state.get("last_scheduled_timestamp")
            if last_sched is not None and (now_ts - float(last_sched)) < min_interval_sec:
                remaining_sec = int(min_interval_sec - (now_ts - float(last_sched)))
                return (
                    False,
                    tr(
                        "budget.interval_not_elapsed",
                        lang=lang,
                        hours=effective_hours,
                        seconds=remaining_sec,
                    ),
                )

            return True, tr("budget.permitted", lang=lang)

    def can_issue_manual_request(
        self, now: Optional[float] = None, lang: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Verify whether a manual/user-initiated API request is permitted right now."""
        with self._lock:
            now_ts = float(now if now is not None else self._time_fn())
            self._rollover_if_needed(now_ts)

            if self._in_flight:
                return False, tr("budget.in_flight", lang=lang)

            total_used = int(self._state["monthly_total_count"])
            if total_used >= MONTHLY_HARD_LIMIT:
                return False, tr("budget.monthly_exhausted", lang=lang)

            return True, tr("budget.permitted", lang=lang)

    def acquire_and_record_dispatch(
        self,
        kind: str = "manual",
        refresh_interval_hours: int = DEFAULT_REFRESH_INTERVAL_HOURS,
        now: Optional[float] = None,
        lang: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[int]]:
        """Atomically check permission, lock in-flight state, and count the request BEFORE network I/O.

        Returns `(permitted, message, log_index)`.
        If `permitted` is True, the caller MUST call `complete_dispatch(log_index, success=...)`
        in a `finally` block.
        """
        with self._lock:
            now_ts = float(now if now is not None else self._time_fn())
            if kind == "scheduled":
                ok, reason = self.can_issue_scheduled_request(
                    refresh_interval_hours=refresh_interval_hours, now=now_ts, lang=lang
                )
            else:
                ok, reason = self.can_issue_manual_request(now=now_ts, lang=lang)

            if not ok:
                return False, reason, None

            self._in_flight = True
            self._state["monthly_total_count"] = int(self._state["monthly_total_count"]) + 1
            if kind == "scheduled":
                self._state["monthly_scheduled_count"] = (
                    int(self._state["monthly_scheduled_count"]) + 1
                )
                self._state["last_scheduled_timestamp"] = now_ts
            else:
                self._state["monthly_manual_count"] = (
                    int(self._state["monthly_manual_count"]) + 1
                )

            self._state["last_request_timestamp"] = now_ts

            entry = {
                "timestamp": now_ts,
                "date": self._date_key(now_ts),
                "kind": kind,
                "outcome": "dispatched",
            }
            log_list: List[Dict[str, Any]] = self._state.setdefault("request_log", [])
            log_list.append(entry)
            log_idx = len(log_list) - 1
            self._save()
            return True, tr("budget.dispatched", lang=lang), log_idx

    def complete_dispatch(
        self, log_index: Optional[int], success: bool, status_code: Optional[int] = None
    ) -> None:
        """Record the outcome of a dispatched request and release the in-flight lock.

        Note: Failed requests remain counted in `monthly_total_count` because every
        dispatched request consumes budget.
        """
        with self._lock:
            try:
                if not success:
                    self._state["failed_request_count"] = (
                        int(self._state.get("failed_request_count", 0)) + 1
                    )
                log_list: List[Dict[str, Any]] = self._state.get("request_log", [])
                if log_index is not None and 0 <= log_index < len(log_list):
                    log_list[log_index]["outcome"] = "success" if success else "failed"
                    if status_code is not None:
                        log_list[log_index]["status_code"] = int(status_code)
                self._save()
            finally:
                self._in_flight = False

    def seconds_until_next_scheduled(
        self,
        refresh_interval_hours: int = DEFAULT_REFRESH_INTERVAL_HOURS,
        now: Optional[float] = None,
    ) -> Optional[float]:
        """Return seconds until the next scheduled request is permitted, or None if monthly budget is full."""
        with self._lock:
            now_ts = float(now if now is not None else self._time_fn())
            self._rollover_if_needed(now_ts)

            if (
                int(self._state["monthly_total_count"]) >= MONTHLY_HARD_LIMIT
                or int(self._state["monthly_scheduled_count"]) >= MONTHLY_SCHEDULED_BUDGET
            ):
                return None

            effective_hours = max(MIN_REFRESH_INTERVAL_HOURS, int(refresh_interval_hours))
            interval_sec = effective_hours * 3600.0

            wait_for_interval = 0.0
            last_sched = self._state.get("last_scheduled_timestamp")
            if last_sched is not None:
                elapsed = max(0.0, now_ts - float(last_sched))
                if elapsed < interval_sec:
                    wait_for_interval = interval_sec - elapsed

            daily_sched, _ = self._daily_counts(now_ts)
            wait_for_daily_reset = 0.0
            if daily_sched >= DAILY_SCHEDULED_LIMIT:
                dt = datetime.fromtimestamp(now_ts)
                seconds_since_midnight = dt.hour * 3600 + dt.minute * 60 + dt.second
                wait_for_daily_reset = float(86400 - seconds_since_midnight + 5)

            return max(wait_for_interval, wait_for_daily_reset)

    def reset_local_statistics(self, now: Optional[float] = None) -> None:
        """Reset local request statistics after explicit user confirmation in Settings.

        Warning: Resetting local statistics does NOT reset the provider's remote quota.
        """
        with self._lock:
            now_ts = float(now if now is not None else self._time_fn())
            self._state = self._default_state(now_ts)
            self._save()
