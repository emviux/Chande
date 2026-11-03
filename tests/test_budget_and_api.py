"""Unit tests for quota enforcement, API client, cache persistence, and price formatting.

All network requests are mocked; zero real API requests are consumed during tests.
"""

from __future__ import annotations
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from config.constants import (
    DAILY_SCHEDULED_LIMIT,
    MONTHLY_HARD_LIMIT,
    MONTHLY_RESERVE_BUDGET,
    MONTHLY_SCHEDULED_BUDGET,
    REQUIRED_SYMBOLS,
)
from config.settings import AppSettings, SettingsManager, mask_api_key, redact_secrets
from services.api_client import MarketApiClient
from services.budget_manager import RequestBudgetManager
from services.formatter import (
    calculate_percentage_change,
    determine_instrument_unit,
    format_cache_age,
    format_change_text,
    format_number,
    parse_market_response,
    parse_numeric,
)
from storage.cache_store import CacheStore


class FakeClock:
    """Deterministic clock for testing time intervals, daily limits, and monthly rollovers."""

    def __init__(self, initial_dt: datetime) -> None:
        self.current_ts = initial_dt.timestamp()

    def __call__(self) -> float:
        return self.current_ts

    def advance_hours(self, hours: float) -> None:
        self.current_ts += hours * 3600.0

    def set_datetime(self, dt: datetime) -> None:
        self.current_ts = dt.timestamp()


@pytest.fixture()
def sample_navasan_payload() -> dict:
    return {
        "usd_sell": {
            "value": "68500",
            "change": 450,
            "timestamp": 1761710000,
            "date": "1405-08-07 09:30:00",
        },
        "usd_buy": {
            "value": "68100",
            "change": 400,
            "timestamp": 1761710000,
            "date": "1405-08-07 09:30:00",
        },
        "18ayar": {
            "value": "4385000",
            "change": -25000,
            "timestamp": 1761710000,
            "date": "1405-08-07 09:30:00",
        },
        "btc": {
            "value": "4750000000",
            "change": 35000000,
            "timestamp": 1761710000,
            "date": "1405-08-07 09:30:00",
            "dollar_rate": "69200",
        },
        "usdt": {
            "value": "68750",
            "change": 320,
            "timestamp": 1761710000,
            "date": "1405-08-07 09:30:00",
        },
    }


def make_mock_response(status_code: int = 200, json_data: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
        resp.text = text or "{}"
    else:
        resp.json.side_effect = ValueError("No valid JSON")
        resp.text = text or "Invalid JSON"
    return resp


# ---------------------------------------------------------------------------
# 1. Parsing the four required symbols & never substituting usd_buy
# ---------------------------------------------------------------------------


def test_parse_four_required_symbols_and_ignore_usd_buy(sample_navasan_payload: dict) -> None:
    quotes = parse_market_response(sample_navasan_payload, include_secondary=False)
    assert set(quotes.keys()) == set(REQUIRED_SYMBOLS)
    assert quotes["usd_sell"].available is True
    assert quotes["usd_sell"].value == 68500.0  # Must be usd_sell (68500), NOT usd_buy (68100)
    assert quotes["usd_sell"].change == 450.0
    assert quotes["usd_sell"].name_fa == "دلار تهران"

    assert quotes["18ayar"].available is True
    assert quotes["18ayar"].value == 4385000.0
    assert quotes["18ayar"].change == -25000.0
    assert quotes["18ayar"].name_fa == "طلای ۱۸ عیار"

    assert quotes["btc"].available is True
    assert quotes["btc"].value == 4750000000.0
    assert quotes["btc"].dollar_rate == 69200.0
    assert quotes["btc"].name_fa == "بیتکوین"

    assert quotes["usdt"].available is True
    assert quotes["usdt"].value == 68750.0
    assert quotes["usdt"].name_fa == "تتر"


def test_missing_symbols_and_malformed_values_do_not_crash() -> None:
    partial_payload = {
        "usd_sell": {"value": "68500", "change": "100"},
        "18ayar": {"value": "not_a_number", "change": 0},
        "btc": {"value": "-50", "change": 0},
        # usdt is completely missing
    }
    quotes = parse_market_response(partial_payload, include_secondary=False)
    assert quotes["usd_sell"].available is True
    assert quotes["usd_sell"].value == 68500.0

    assert quotes["18ayar"].available is False
    assert quotes["18ayar"].value is None

    assert quotes["btc"].available is False
    assert quotes["btc"].value is None

    assert quotes["usdt"].available is False
    assert quotes["usdt"].value is None


# ---------------------------------------------------------------------------
# 2. Price formatting, numerals, and unit verification (no silent scaling)
# ---------------------------------------------------------------------------


def test_price_formatting_and_no_silent_scaling(sample_navasan_payload: dict) -> None:
    quotes = parse_market_response(sample_navasan_payload, include_secondary=False)
    # Verify values are never silently multiplied or divided by 10 or 1,000,000
    assert quotes["usd_sell"].value == 68500.0
    assert quotes["18ayar"].value == 4385000.0
    assert quotes["btc"].value == 4750000000.0
    assert quotes["usdt"].value == 68750.0

    # Persian and English numeral formatting
    assert format_number(68500, use_persian=True) == "۶۸,۵۰۰"
    assert format_number(68500, use_persian=False) == "68,500"
    assert format_number(4385000, use_persian=True) == "۴,۳۸۵,۰۰۰"

    # Unit verification
    assert determine_instrument_unit("usd_sell", 68500, {}) == "تومان"
    assert determine_instrument_unit("18ayar", 4385000, {}) == "تومان / گرم"
    assert determine_instrument_unit("usdt", 68750, {}) == "تومان"
    assert determine_instrument_unit("usdt", 1.0, {}) == "دلار"
    assert determine_instrument_unit("btc", 4750000000, {"dollar_rate": "69200"}) == "تومان"
    assert determine_instrument_unit("btc", 69200, {"dollar_rate": "69200"}) == "دلار"


def test_percentage_change_calculation() -> None:
    # From current_value and daily change (prev = 50000 - 1000 = 49000 -> +2.04%)
    pct = calculate_percentage_change(current_value=50000, change=1000)
    assert pct == pytest.approx(2.04, rel=1e-2)

    # When change is None and previous_value is None, percentage cannot be calculated
    assert calculate_percentage_change(current_value=50000, change=None, previous_value=None) is None

    # Change text formatting
    txt_fa = format_change_text(1240, 2.4, use_persian=True)
    assert "▲" in txt_fa and "۱,۲۴۰" in txt_fa and "۲.۴٪" in txt_fa
    txt_en = format_change_text(-230000, -0.7, use_persian=False)
    assert "▼" in txt_en and "230,000" in txt_en and "0.7%" in txt_en


# ---------------------------------------------------------------------------
# 3. Cache loading, saving, and preserving valid prices on failure
# ---------------------------------------------------------------------------


def test_cache_persistence_and_preservation_on_failure(
    tmp_path: Path, sample_navasan_payload: dict
) -> None:
    cache_file = tmp_path / "market_cache.json"
    budget_file = tmp_path / "request_budget.json"
    clock = FakeClock(datetime(2026, 10, 10, 9, 0, 0))

    cache = CacheStore(cache_file)
    budget = RequestBudgetManager(budget_file, time_fn=clock)
    session = MagicMock()
    session.get.return_value = make_mock_response(200, sample_navasan_payload)

    client = MarketApiClient(budget, cache, session=session, time_fn=clock)
    res1 = client.fetch_latest(api_key="test_secret_key", kind="manual")
    assert res1.success is True
    assert cache.has_cached_data() is True

    # Reload cache from disk to verify persistence (offline startup simulation)
    reloaded_cache = CacheStore(cache_file)
    assert reloaded_cache.has_cached_data() is True
    assert reloaded_cache.get_latest_response()["usd_sell"]["value"] == "68500"

    # Now simulate a failing HTTP 503 request; cached prices MUST be preserved
    clock.advance_hours(1.0)
    session.get.return_value = make_mock_response(503, text="Service Unavailable")
    res2 = client.fetch_latest(api_key="test_secret_key", kind="manual")
    assert res2.success is False
    assert res2.error_code == "http_503"
    assert res2.from_cache is True
    assert res2.quotes["usd_sell"].value == 68500.0
    assert res2.quotes["18ayar"].value == 4385000.0


# ---------------------------------------------------------------------------
# 4. Monthly request limit (120 total, 90 scheduled, 30 reserve)
# ---------------------------------------------------------------------------


def test_monthly_scheduled_and_hard_budget_enforcement(
    tmp_path: Path, sample_navasan_payload: dict
) -> None:
    budget_file = tmp_path / "budget.json"
    clock = FakeClock(datetime(2026, 10, 1, 0, 0, 0))
    budget = RequestBudgetManager(budget_file, time_fn=clock)

    # Issue 90 scheduled requests across 30 days (3 per day, 8 hours apart)
    for i in range(MONTHLY_SCHEDULED_BUDGET):
        ok, reason, idx = budget.acquire_and_record_dispatch(
            kind="scheduled", refresh_interval_hours=8
        )
        assert ok is True, f"Scheduled request {i+1} should be allowed, got: {reason}"
        budget.complete_dispatch(idx, success=True, status_code=200)
        clock.advance_hours(8.0)

    status_after_90 = budget.get_status()
    assert status_after_90.monthly_scheduled_used == MONTHLY_SCHEDULED_BUDGET
    assert status_after_90.monthly_total_used == MONTHLY_SCHEDULED_BUDGET
    assert status_after_90.monthly_remaining == MONTHLY_RESERVE_BUDGET
    assert status_after_90.is_in_reserve is True

    # 91st scheduled request in the same month (Wait: 30 days * 24h moved into Nov if Oct has 31 days;
    # Oct 1 + 30 days = Oct 31, still month 2026-10!)
    assert status_after_90.month_key == "2026-10"
    ok_sched, reason_sched = budget.can_issue_scheduled_request(refresh_interval_hours=8)
    assert ok_sched is False
    assert "۹۰" in reason_sched

    # Manual requests can still use the 30-request reserve up to 120
    for j in range(MONTHLY_RESERVE_BUDGET):
        ok_man, _, idx_man = budget.acquire_and_record_dispatch(kind="manual")
        assert ok_man is True, f"Manual reserve request {j+1} should be allowed"
        budget.complete_dispatch(idx_man, success=True, status_code=200)

    status_after_120 = budget.get_status()
    assert status_after_120.monthly_total_used == MONTHLY_HARD_LIMIT
    assert status_after_120.monthly_remaining == 0
    assert status_after_120.is_exhausted is True

    # 121st request (manual or scheduled) must be strictly blocked
    ok_over, reason_over, _ = budget.acquire_and_record_dispatch(kind="manual")
    assert ok_over is False
    assert "۱۲۰" in reason_over


# ---------------------------------------------------------------------------
# 5. Daily scheduled limit (max 3 per day, 8h interval)
# ---------------------------------------------------------------------------


def test_daily_scheduled_limit_and_interval_safeguard(tmp_path: Path) -> None:
    budget_file = tmp_path / "budget_daily.json"
    clock = FakeClock(datetime(2026, 10, 15, 0, 10, 0))
    budget = RequestBudgetManager(budget_file, time_fn=clock)

    # Request 1 at 00:10
    ok1, _, idx1 = budget.acquire_and_record_dispatch(kind="scheduled", refresh_interval_hours=8)
    assert ok1 is True
    budget.complete_dispatch(idx1, success=True)

    # Trying again 2 hours later must be blocked by 8-hour interval
    clock.advance_hours(2.0)
    ok_early, reason_early = budget.can_issue_scheduled_request(refresh_interval_hours=8)
    assert ok_early is False
    assert "فاصله زمانی" in reason_early

    # Advance another 6 hours (total 8h -> 08:10): Request 2 allowed
    clock.advance_hours(6.0)
    ok2, _, idx2 = budget.acquire_and_record_dispatch(kind="scheduled", refresh_interval_hours=8)
    assert ok2 is True
    budget.complete_dispatch(idx2, success=True)

    # Advance 8 hours (16:10): Request 3 allowed
    clock.advance_hours(8.0)
    ok3, _, idx3 = budget.acquire_and_record_dispatch(kind="scheduled", refresh_interval_hours=8)
    assert ok3 is True
    budget.complete_dispatch(idx3, success=True)

    assert budget.get_status().daily_scheduled_used == DAILY_SCHEDULED_LIMIT

    # Even if we force a check before midnight on the same day, daily limit of 3 blocks a 4th scheduled request
    clock.set_datetime(datetime(2026, 10, 15, 23, 59, 0))
    budget._state["last_scheduled_timestamp"] = datetime(2026, 10, 15, 14, 0, 0).timestamp()
    ok4, reason4 = budget.can_issue_scheduled_request(refresh_interval_hours=8)
    assert ok4 is False
    assert "روزانه" in reason4


# ---------------------------------------------------------------------------
# 6. Failed requests count toward usage & HTTP errors handled gracefully
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code,expected_err", [
    (401, "http_401"),
    (422, "http_422"),
    (429, "http_429"),
    (503, "http_503"),
])
def test_http_errors_count_toward_budget_and_never_retry(
    tmp_path: Path, status_code: int, expected_err: str
) -> None:
    clock = FakeClock(datetime(2026, 10, 20, 10, 0, 0))
    budget = RequestBudgetManager(tmp_path / "b.json", time_fn=clock)
    cache = CacheStore(tmp_path / "c.json")
    session = MagicMock()
    session.get.return_value = make_mock_response(status_code, text="error")

    client = MarketApiClient(budget, cache, session=session, time_fn=clock)
    result = client.fetch_latest(api_key="my_secret_api_key_12345", kind="manual")

    assert result.success is False
    assert result.error_code == expected_err
    # Must only call session.get ONCE (never retry automatically in a loop!)
    assert session.get.call_count == 1
    # Failed request MUST count toward monthly budget
    st = budget.get_status()
    assert st.monthly_total_used == 1
    assert st.failed_count == 1
    # Secret API key must never appear in error message
    assert "my_secret_api_key_12345" not in result.message_fa


def test_network_timeout_counts_toward_budget(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 20, 12, 0, 0))
    budget = RequestBudgetManager(tmp_path / "b_timeout.json", time_fn=clock)
    cache = CacheStore(tmp_path / "c_timeout.json")
    session = MagicMock()
    session.get.side_effect = requests.exceptions.ConnectTimeout("Connection timed out")

    client = MarketApiClient(budget, cache, session=session, time_fn=clock)
    result = client.fetch_latest(api_key="my_secret_api_key_12345", kind="manual")

    assert result.success is False
    assert result.error_code == "timeout"
    assert session.get.call_count == 1
    st = budget.get_status()
    assert st.monthly_total_used == 1
    assert st.failed_count == 1
    assert budget.is_in_flight is False


# ---------------------------------------------------------------------------
# 7. Preventing concurrent & duplicate requests + single request for all symbols
# ---------------------------------------------------------------------------


def test_prevent_concurrent_requests_and_single_endpoint_call(
    tmp_path: Path, sample_navasan_payload: dict
) -> None:
    clock = FakeClock(datetime(2026, 10, 21, 9, 0, 0))
    budget = RequestBudgetManager(tmp_path / "b_conc.json", time_fn=clock)

    ok1, _, idx1 = budget.acquire_and_record_dispatch(kind="manual")
    assert ok1 is True
    assert budget.is_in_flight is True

    # While first request is in-flight, a concurrent second request MUST be rejected
    ok2, reason2, _ = budget.acquire_and_record_dispatch(kind="manual")
    assert ok2 is False
    assert "در حال ارسال" in reason2
    assert budget.get_status().monthly_total_used == 1

    budget.complete_dispatch(idx1, success=True)
    assert budget.is_in_flight is False

    # Verify MarketApiClient makes exactly ONE GET request without `item` parameter
    cache = CacheStore(tmp_path / "c_conc.json")
    session = MagicMock()
    session.get.return_value = make_mock_response(200, sample_navasan_payload)
    client = MarketApiClient(budget, cache, session=session, time_fn=clock)
    res = client.fetch_latest(api_key="secret_key", kind="manual")
    assert res.success is True
    assert session.get.call_count == 1
    called_kwargs = session.get.call_args.kwargs
    assert "item" not in called_kwargs["params"]
    assert called_kwargs["params"] == {"api_key": "secret_key"}
    assert called_kwargs["verify"] is True


# ---------------------------------------------------------------------------
# 8. Monthly counter rollover & persistence across restarts
# ---------------------------------------------------------------------------


def test_monthly_rollover_and_restart_persistence(tmp_path: Path) -> None:
    budget_file = tmp_path / "b_rollover.json"
    clock = FakeClock(datetime(2026, 10, 31, 22, 0, 0))
    budget1 = RequestBudgetManager(budget_file, time_fn=clock)

    ok, _, idx = budget1.acquire_and_record_dispatch(kind="manual")
    assert ok is True
    budget1.complete_dispatch(idx, success=True)
    assert budget1.get_status().monthly_total_used == 1

    # Simulate application restart in the same month
    budget2 = RequestBudgetManager(budget_file, time_fn=clock)
    assert budget2.get_status().monthly_total_used == 1
    assert budget2.get_status().month_key == "2026-10"

    # Advance clock to November 1st -> counter must automatically roll over to 0
    clock.set_datetime(datetime(2026, 11, 1, 0, 5, 0))
    status_nov = budget2.get_status()
    assert status_nov.month_key == "2026-11"
    assert status_nov.monthly_total_used == 0
    assert status_nov.monthly_scheduled_used == 0
    assert status_nov.failed_count == 0


# ---------------------------------------------------------------------------
# 9. Settings validation & API key redaction
# ---------------------------------------------------------------------------


def test_settings_interval_clamping_and_secret_redaction(tmp_path: Path) -> None:
    cfg_file = tmp_path / "user_config.json"
    mgr = SettingsManager(cfg_file)
    # Attempting to set refresh_interval_hours below 8 must clamp to 8
    updated = mgr.update(refresh_interval_hours=1, opacity=2.5)
    assert updated.refresh_interval_hours == 8
    assert updated.opacity == 1.0

    # Secret redaction
    raw_err = "HTTPSConnectionPool: url /latest/?api_key=SUPER_SECRET_TOKEN_999 failed"
    redacted = redact_secrets(raw_err, "SUPER_SECRET_TOKEN_999")
    assert "SUPER_SECRET_TOKEN_999" not in redacted
    assert "***REDACTED***" in redacted
    assert "SUPER_SECRET_TOKEN_999" not in mask_api_key("SUPER_SECRET_TOKEN_999")
