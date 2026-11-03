"""HTTP API client for Navasan (/latest/ and /usage/) and optional Free Iran-Market provider."""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

import requests

from config.constants import (
    ALL_INSTRUMENTS,
    DEFAULT_REFRESH_INTERVAL_HOURS,
    DEFAULT_TIMEOUT,
    FREE_IRAN_MARKET_URL,
    NAVASAN_LATEST_URL,
    NAVASAN_USAGE_URL,
)
from config.settings import redact_secrets
from services.budget_manager import BudgetStatus, RequestBudgetManager
from services.formatter import InstrumentQuote, parse_market_response, parse_numeric
from services.i18n import get_current_language, tr
from storage.cache_store import CacheStore


@dataclass
class ApiFetchResult:
    """Result returned by MarketApiClient.fetch_latest() or check_remote_usage()."""
    success: bool
    status_code: Optional[int] = None
    error_code: str = ""  # e.g. "missing_api_key", "budget_exhausted", "http_401", "network_error"
    message_fa: str = ""
    raw_data: Dict[str, Any] = field(default_factory=dict)
    quotes: Dict[str, InstrumentQuote] = field(default_factory=dict)
    from_cache: bool = False
    fetched_at: Optional[float] = None
    budget_status: Optional[BudgetStatus] = None
    usage_data: Optional[Dict[str, Any]] = None
    message_en: str = ""

    def get_message(self, lang: Optional[str] = None) -> str:
        """Return localized message in the specified or currently active language."""
        effective_lang = (lang or get_current_language()).strip().lower()
        if effective_lang == "en" and self.message_en:
            return self.message_en
        return self.message_fa


class MarketApiClient:
    """Client for fetching market quotes while strictly enforcing the monthly request budget.

    Key invariants:
    - Makes at most ONE GET request to `/latest/` without an `item` parameter per refresh.
    - Never hardcodes or logs the API key.
    - Counts every dispatched request against `RequestBudgetManager` before/regardless of outcome.
    - Never retries automatically in a loop or on HTTP 429 / 503.
    - Preserves last valid cached prices when any request fails.
    """

    def __init__(
        self,
        budget_manager: RequestBudgetManager,
        cache_store: CacheStore,
        session: Optional[requests.Session] = None,
        latest_url: str = NAVASAN_LATEST_URL,
        usage_url: str = NAVASAN_USAGE_URL,
        free_market_url: str = FREE_IRAN_MARKET_URL,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self.budget_manager = budget_manager
        self.cache_store = cache_store
        self._session = session or requests.Session()
        self.latest_url = latest_url
        self.usage_url = usage_url
        self.free_market_url = free_market_url
        self.timeout = timeout
        self._time_fn = time_fn

    def get_cached_quotes(self, include_secondary: bool = True) -> Dict[str, InstrumentQuote]:
        """Parse and return the currently cached prices without making any network call."""
        raw = self.cache_store.get_latest_response()
        if not raw:
            return {}
        prev = self.cache_store.get_previous_prices()
        hist = {
            spec.symbol: self.cache_store.get_price_history(spec.symbol)
            for spec in ALL_INSTRUMENTS
        }
        return parse_market_response(
            raw,
            previous_prices=prev,
            price_history=hist,
            include_secondary=include_secondary,
        )

    def fetch_latest(
        self,
        api_key: str,
        kind: str = "manual",
        provider: str = "navasan",
        refresh_interval_hours: int = DEFAULT_REFRESH_INTERVAL_HOURS,
        include_secondary: bool = True,
    ) -> ApiFetchResult:
        """Fetch all required instruments in a single GET request, subject to budget rules."""
        now_ts = self._time_fn()
        clean_key = (api_key or "").strip()

        # Validate API key before dispatching if using Navasan provider
        if provider == "navasan" and not clean_key:
            cached = self.get_cached_quotes(include_secondary=include_secondary)
            return ApiFetchResult(
                success=False,
                error_code="missing_api_key",
                message_fa=tr("errors.missing_api_key", lang="fa"),
                message_en=tr("errors.missing_api_key", lang="en"),
                quotes=cached,
                from_cache=bool(cached),
                fetched_at=self.cache_store.get_last_fetch_timestamp(),
                budget_status=self.budget_manager.get_status(now_ts),
            )

        # Acquire budget lock and immediately count the request before dispatching
        _, reason_en_pre = (
            self.budget_manager.can_issue_scheduled_request(
                refresh_interval_hours=refresh_interval_hours, now=now_ts, lang="en"
            )
            if kind == "scheduled"
            else self.budget_manager.can_issue_manual_request(now=now_ts, lang="en")
        )
        permitted, reason_fa, log_idx = self.budget_manager.acquire_and_record_dispatch(
            kind=kind,
            refresh_interval_hours=refresh_interval_hours,
            now=now_ts,
            lang="fa",
        )
        if not permitted:
            status = self.budget_manager.get_status(now_ts)
            err_code = "budget_exhausted" if status.is_exhausted else "budget_blocked"
            cached = self.get_cached_quotes(include_secondary=include_secondary)
            return ApiFetchResult(
                success=False,
                error_code=err_code,
                message_fa=reason_fa,
                message_en=reason_en_pre,
                quotes=cached,
                from_cache=bool(cached),
                fetched_at=self.cache_store.get_last_fetch_timestamp(),
                budget_status=status,
            )

        http_status: Optional[int] = None
        request_succeeded = False
        try:
            if provider == "free_iran_market":
                return self._execute_free_market_request(
                    now_ts=now_ts,
                    include_secondary=include_secondary,
                    log_idx=log_idx,
                )

            # Single GET request to /latest/ without an `item` parameter
            response = self._session.get(
                self.latest_url,
                params={"api_key": clean_key},
                headers={
                    "Accept": "application/json",
                    "User-Agent": "ChandeDesktopWidget/1.0 (Windows; PySide6)",
                },
                timeout=self.timeout,
                verify=True,
            )
            http_status = response.status_code

            if http_status != 200:
                err_code, msg_fa, msg_en = self._map_http_error(http_status, response.text, clean_key)
                cached = self.get_cached_quotes(include_secondary=include_secondary)
                return ApiFetchResult(
                    success=False,
                    status_code=http_status,
                    error_code=err_code,
                    message_fa=msg_fa,
                    message_en=msg_en,
                    quotes=cached,
                    from_cache=bool(cached),
                    fetched_at=self.cache_store.get_last_fetch_timestamp(),
                )

            try:
                payload = response.json()
            except ValueError:
                cached = self.get_cached_quotes(include_secondary=include_secondary)
                return ApiFetchResult(
                    success=False,
                    status_code=http_status,
                    error_code="malformed_json",
                    message_fa=tr("errors.malformed_json", lang="fa"),
                    message_en=tr("errors.malformed_json", lang="en"),
                    quotes=cached,
                    from_cache=bool(cached),
                    fetched_at=self.cache_store.get_last_fetch_timestamp(),
                )

            if not isinstance(payload, dict) or not payload:
                cached = self.get_cached_quotes(include_secondary=include_secondary)
                return ApiFetchResult(
                    success=False,
                    status_code=http_status,
                    error_code="invalid_structure",
                    message_fa=tr("errors.invalid_structure", lang="fa"),
                    message_en=tr("errors.invalid_structure", lang="en"),
                    quotes=cached,
                    from_cache=bool(cached),
                    fetched_at=self.cache_store.get_last_fetch_timestamp(),
                )

            # Check if the API returned a 200 with an error message object only
            if "message" in payload and len(payload) == 1:
                safe_msg = redact_secrets(str(payload.get("message", "")), clean_key)
                cached = self.get_cached_quotes(include_secondary=include_secondary)
                return ApiFetchResult(
                    success=False,
                    status_code=http_status,
                    error_code="api_error_message",
                    message_fa=tr("errors.api_error_message", lang="fa", detail=safe_msg),
                    message_en=tr("errors.api_error_message", lang="en", detail=safe_msg),
                    quotes=cached,
                    from_cache=bool(cached),
                    fetched_at=self.cache_store.get_last_fetch_timestamp(),
                )

            # Update cache atomically (preserves previous valid symbols if any symbol is invalid)
            self.cache_store.update_success(
                response_data=payload,
                fetched_at=now_ts,
                source="navasan",
            )
            request_succeeded = True

            # Parse quotes using updated cache state and raw payload so missing symbols in payload are reflected
            prev = self.cache_store.get_previous_prices()
            hist = {
                spec.symbol: self.cache_store.get_price_history(spec.symbol)
                for spec in ALL_INSTRUMENTS
            }
            quotes = parse_market_response(
                payload,
                previous_prices=prev,
                price_history=hist,
                include_secondary=include_secondary,
            )

            return ApiFetchResult(
                success=True,
                status_code=http_status,
                message_fa=tr("errors.fetch_success", lang="fa"),
                message_en=tr("errors.fetch_success", lang="en"),
                raw_data=payload,
                quotes=quotes,
                from_cache=False,
                fetched_at=now_ts,
            )

        except requests.exceptions.Timeout:
            cached = self.get_cached_quotes(include_secondary=include_secondary)
            return ApiFetchResult(
                success=False,
                error_code="timeout",
                message_fa=tr("errors.timeout", lang="fa"),
                message_en=tr("errors.timeout", lang="en"),
                quotes=cached,
                from_cache=bool(cached),
                fetched_at=self.cache_store.get_last_fetch_timestamp(),
            )
        except requests.exceptions.SSLError:
            cached = self.get_cached_quotes(include_secondary=include_secondary)
            return ApiFetchResult(
                success=False,
                error_code="ssl_error",
                message_fa=tr("errors.ssl_error", lang="fa"),
                message_en=tr("errors.ssl_error", lang="en"),
                quotes=cached,
                from_cache=bool(cached),
                fetched_at=self.cache_store.get_last_fetch_timestamp(),
            )
        except requests.exceptions.RequestException as exc:
            safe_err = redact_secrets(str(exc), clean_key)[:80]
            cached = self.get_cached_quotes(include_secondary=include_secondary)
            return ApiFetchResult(
                success=False,
                error_code="network_error",
                message_fa=tr("errors.network_error", lang="fa", detail=safe_err),
                message_en=tr("errors.network_error", lang="en", detail=safe_err),
                quotes=cached,
                from_cache=bool(cached),
                fetched_at=self.cache_store.get_last_fetch_timestamp(),
            )
        finally:
            self.budget_manager.complete_dispatch(
                log_idx, success=request_succeeded, status_code=http_status
            )

    def _execute_free_market_request(
        self,
        now_ts: float,
        include_secondary: bool,
        log_idx: Optional[int],
    ) -> ApiFetchResult:
        """Fetch and normalize all required instruments in a single GET from iran-market.github.io."""
        response = self._session.get(
            self.free_market_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "ChandeDesktopWidget/1.0 (Windows; PySide6)",
            },
            timeout=self.timeout,
            verify=True,
        )
        http_status = response.status_code
        if http_status != 200:
            cached = self.get_cached_quotes(include_secondary=include_secondary)
            return ApiFetchResult(
                success=False,
                status_code=http_status,
                error_code=f"http_{http_status}",
                message_fa=tr("errors.free_http_error", lang="fa", code=http_status),
                message_en=tr("errors.free_http_error", lang="en", code=http_status),
                quotes=cached,
                from_cache=bool(cached),
                fetched_at=self.cache_store.get_last_fetch_timestamp(),
            )

        try:
            payload = response.json()
        except ValueError:
            cached = self.get_cached_quotes(include_secondary=include_secondary)
            return ApiFetchResult(
                success=False,
                status_code=http_status,
                error_code="malformed_json",
                message_fa=tr("errors.free_malformed_json", lang="fa"),
                message_en=tr("errors.free_malformed_json", lang="en"),
                quotes=cached,
                from_cache=bool(cached),
                fetched_at=self.cache_store.get_last_fetch_timestamp(),
            )

        normalized = self._convert_free_market_to_navasan_schema(payload, now_ts)
        if not normalized:
            cached = self.get_cached_quotes(include_secondary=include_secondary)
            return ApiFetchResult(
                success=False,
                status_code=http_status,
                error_code="invalid_structure",
                message_fa=tr("errors.free_empty_data", lang="fa"),
                message_en=tr("errors.free_empty_data", lang="en"),
                quotes=cached,
                from_cache=bool(cached),
                fetched_at=self.cache_store.get_last_fetch_timestamp(),
            )

        self.cache_store.update_success(
            response_data=normalized,
            fetched_at=now_ts,
            source="free_iran_market",
        )
        self.budget_manager.complete_dispatch(log_idx, success=True, status_code=200)

        prev = self.cache_store.get_previous_prices()
        hist = {
            spec.symbol: self.cache_store.get_price_history(spec.symbol)
            for spec in ALL_INSTRUMENTS
        }
        quotes = parse_market_response(
            normalized,
            previous_prices=prev,
            price_history=hist,
            include_secondary=include_secondary,
        )
        return ApiFetchResult(
            success=True,
            status_code=200,
            message_fa=tr("errors.free_fetch_success", lang="fa"),
            message_en=tr("errors.free_fetch_success", lang="en"),
            raw_data=normalized,
            quotes=quotes,
            from_cache=False,
            fetched_at=now_ts,
        )

    @staticmethod
    def _convert_free_market_to_navasan_schema(
        payload: Dict[str, Any], now_ts: float
    ) -> Dict[str, Any]:
        """Map `latest-toman.json` from iran-market.github.io to Navasan's exact symbol schema."""
        if not isinstance(payload, dict):
            return {}
        data_sec = payload.get("data")
        flat_by_sym: Dict[str, Dict[str, Any]] = {}
        if isinstance(data_sec, dict) and isinstance(data_sec.get("categories"), dict):
            for cat in data_sec["categories"].values():
                items = cat.get("items", []) if isinstance(cat, dict) else cat
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) and it.get("symbol"):
                            flat_by_sym[str(it["symbol"])] = it
        elif isinstance(data_sec, list):
            for it in data_sec:
                if isinstance(it, dict) and it.get("symbol"):
                    flat_by_sym[str(it["symbol"])] = it

        if not flat_by_sym:
            return {}

        btc_usd_item = flat_by_sym.get("BTC_USD", {})
        btc_usd_price = parse_numeric(btc_usd_item.get("price"))

        result: Dict[str, Any] = {}
        for spec in ALL_INSTRUMENTS:
            src = flat_by_sym.get(spec.free_api_symbol)
            if not src:
                continue
            price = parse_numeric(src.get("price"))
            if price is None or price <= 0:
                continue
            entry: Dict[str, Any] = {
                "value": str(int(price) if price == int(price) else price),
                "change": src.get("change", 0),
                "change_pct": src.get("change_pct"),
                "prev_close": src.get("prev_close"),
                "currency": src.get("currency", "IRT"),
                "timestamp": int(now_ts),
                "date": str(src.get("timestamp_jalali") or "").replace("/", "-"),
            }
            if spec.symbol == "btc" and btc_usd_price is not None:
                entry["dollar_rate"] = str(btc_usd_price)
            result[spec.symbol] = entry
        return result

    def check_remote_usage(self, api_key: str) -> ApiFetchResult:
        """Query Navasan's `/usage/` endpoint ONLY when explicitly requested by the user.

        Counts as 1 request against the local monthly budget.
        """
        now_ts = self._time_fn()
        clean_key = (api_key or "").strip()
        if not clean_key:
            return ApiFetchResult(
                success=False,
                error_code="missing_api_key",
                message_fa=tr("errors.missing_api_key_usage", lang="fa"),
                message_en=tr("errors.missing_api_key_usage", lang="en"),
                budget_status=self.budget_manager.get_status(now_ts),
            )

        _, reason_en_pre = self.budget_manager.can_issue_manual_request(now=now_ts, lang="en")
        permitted, reason_fa, log_idx = self.budget_manager.acquire_and_record_dispatch(
            kind="usage",
            now=now_ts,
            lang="fa",
        )
        if not permitted:
            return ApiFetchResult(
                success=False,
                error_code="budget_blocked",
                message_fa=reason_fa,
                message_en=reason_en_pre,
                budget_status=self.budget_manager.get_status(now_ts),
            )

        http_status: Optional[int] = None
        succeeded = False
        try:
            response = self._session.get(
                self.usage_url,
                params={"api_key": clean_key},
                headers={
                    "Accept": "application/json",
                    "User-Agent": "ChandeDesktopWidget/1.0 (Windows; PySide6)",
                },
                timeout=self.timeout,
                verify=True,
            )
            http_status = response.status_code
            if http_status != 200:
                err_code, msg_fa, msg_en = self._map_http_error(http_status, response.text, clean_key)
                return ApiFetchResult(
                    success=False,
                    status_code=http_status,
                    error_code=err_code,
                    message_fa=msg_fa,
                    message_en=msg_en,
                )

            data = response.json()
            if not isinstance(data, dict):
                return ApiFetchResult(
                    success=False,
                    status_code=http_status,
                    error_code="invalid_structure",
                    message_fa=tr("errors.usage_invalid_structure", lang="fa"),
                    message_en=tr("errors.usage_invalid_structure", lang="en"),
                )

            # Never expose raw api_key back to UI logs
            sanitized_usage = {
                "monthly_usage": data.get("monthly_usage", "—"),
                "daily_usage": data.get("daily_usage", "—"),
                "hourly_usage": data.get("hourly_usage", "—"),
                "last_use": data.get("last_use", "—"),
            }
            succeeded = True
            return ApiFetchResult(
                success=True,
                status_code=200,
                message_fa=tr("errors.usage_success", lang="fa"),
                message_en=tr("errors.usage_success", lang="en"),
                usage_data=sanitized_usage,
            )
        except requests.exceptions.RequestException as exc:
            safe_err = redact_secrets(str(exc), clean_key)[:80]
            return ApiFetchResult(
                success=False,
                error_code="network_error",
                message_fa=tr("errors.usage_network_error", lang="fa", detail=safe_err),
                message_en=tr("errors.usage_network_error", lang="en", detail=safe_err),
            )
        finally:
            self.budget_manager.complete_dispatch(
                log_idx, success=succeeded, status_code=http_status
            )

    @staticmethod
    def _map_http_error(
        status_code: int, body_text: str, api_key: str
    ) -> tuple[str, str, str]:
        """Map HTTP status codes (401, 422, 429, 503, etc.) to localized Persian and English messages."""
        if status_code == 401:
            return (
                "http_401",
                tr("errors.http_401", lang="fa"),
                tr("errors.http_401", lang="en"),
            )
        if status_code == 422:
            return (
                "http_422",
                tr("errors.http_422", lang="fa"),
                tr("errors.http_422", lang="en"),
            )
        if status_code == 429:
            return (
                "http_429",
                tr("errors.http_429", lang="fa"),
                tr("errors.http_429", lang="en"),
            )
        if status_code == 503:
            return (
                "http_503",
                tr("errors.http_503", lang="fa"),
                tr("errors.http_503", lang="en"),
            )
        safe_excerpt = redact_secrets(body_text[:100], api_key)
        return (
            f"http_{status_code}",
            tr("errors.http_generic", lang="fa", code=status_code, detail=safe_excerpt),
            tr("errors.http_generic", lang="en", code=status_code, detail=safe_excerpt),
        )
