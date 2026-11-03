"""Automated test suite for Intelligent Background Sleep & Instant Wake (`ACTIVE`, `IDLE`, `DORMANT`)."""

from __future__ import annotations
import logging
from pathlib import Path
import sys
import time
import tracemalloc
from typing import Dict

import pytest
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from config.settings import SettingsManager
from services.api_client import MarketApiClient
from services.budget_manager import RequestBudgetManager
from services.power_state import WidgetOperatingState, WidgetStateManager
from services.windows_gadget import (
    HWND_TOPMOST,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_SHOWWINDOW,
    compute_rect_occlusion,
    is_window_obscured,
    is_window_stealing_foreground,
)
from storage.cache_store import CacheStore
from ui.main_window import MainWindow


SAMPLE_RATES: Dict[str, dict] = {
    "usd_sell": {"value": "685000", "change": 2500, "timestamp": 1711800000},
    "18ayar": {"value": "38450000", "change": -120000, "timestamp": 1711800000},
    "btc": {"value": "61250000000", "change": 450000000, "timestamp": 1711800000},
    "usdt": {"value": "688500", "change": 1500, "timestamp": 1711800000},
}


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _build_window(
    tmp_path: Path,
    obscured_state: dict,
    debounce_ms: int = 60,
    language: str = "fa",
) -> tuple[MainWindow, MarketApiClient, RequestBudgetManager, CacheStore]:
    settings_mgr = SettingsManager(config_path=tmp_path / "settings.json")
    settings_mgr.update(
        language=language,
        use_persian_numerals=(language == "fa"),
        auto_refresh_enabled=False,
        display_mode="expanded",
        show_chart=True,
        window_width=540,
        window_height=480,
        window_x=120,
        window_y=120,
    )
    budget_mgr = RequestBudgetManager(budget_path=tmp_path / "budget.json")
    cache_store = CacheStore(cache_path=tmp_path / "cache.json")
    now_ts = float(int(time.time()) - 180)
    cache_store.update_success(SAMPLE_RATES, fetched_at=now_ts - 60.0)
    cache_store.update_success(SAMPLE_RATES, fetched_at=now_ts)
    api_client = MarketApiClient(budget_manager=budget_mgr, cache_store=cache_store)

    window = MainWindow(
        settings_manager=settings_mgr,
        budget_manager=budget_mgr,
        cache_store=cache_store,
        api_client=api_client,
        enable_tray=False,
        auto_start_schedule=False,
        obscured_debounce_ms=debounce_ms,
        occlusion_checker=lambda: bool(obscured_state.get("obscured", False)),
    )
    return window, api_client, budget_mgr, cache_store


def test_geometry_occlusion_exact_rect_subtraction():
    """Verify 2D rectangle subtraction distinguishes uncovered, partially covered, and 100% covered rects."""
    target = (100, 100, 400, 300)

    # 1. No covering windows -> visible (False)
    assert compute_rect_occlusion(target, []) is False

    # 2. Disjoint windows -> visible (False)
    assert compute_rect_occlusion(target, [(0, 0, 90, 90), (410, 310, 600, 500)]) is False

    # 3. Partially overlapping window (covers left half only) -> still visible (False)
    assert compute_rect_occlusion(target, [(50, 50, 250, 350)]) is False

    # 4. Covers 99% of target (leaves a 2px strip on the right) -> still visible (False)
    assert compute_rect_occlusion(target, [(100, 100, 398, 300)]) is False

    # 5. Single large/maximized window covering 100% of target -> obscured (True)
    assert compute_rect_occlusion(target, [(0, 0, 1920, 1080)]) is True

    # 6. Two side-by-side windows that together cover 100% of target -> obscured (True)
    left_half = (80, 80, 260, 320)
    right_half = (250, 80, 450, 320)
    assert compute_rect_occlusion(target, [left_half, right_half]) is True


def test_covering_widget_enters_idle_after_debounce_and_wakes_instantly(qapp, tmp_path: Path):
    """Covering the widget enters IDLE after debounce; uncovering wakes to ACTIVE immediately (< 100 ms)."""
    state = {"obscured": False}
    window, api_client, budget_mgr, _ = _build_window(tmp_path, state, debounce_ms=60)
    window.show()
    QTest.qWait(30)

    assert window.operating_state == WidgetOperatingState.ACTIVE
    assert window.is_power_active is True
    assert window._age_timer.isActive() is True
    for card in window.primary_cards.values():
        assert card.sparkline.is_rendering_suspended is False

    # Cover the widget with another window
    state["obscured"] = True
    window.power_manager.evaluate_now()
    # Immediately after being covered, debounce is pending so widget is still ACTIVE
    assert window.power_manager.is_debounce_pending is True
    assert window.operating_state == WidgetOperatingState.ACTIVE

    # Wait for debounce period to elapse -> transitions to IDLE
    QTest.qWait(95)
    assert window.operating_state == WidgetOperatingState.IDLE
    assert window.is_power_idle is True
    assert window._age_timer.isActive() is False
    for card in window.primary_cards.values():
        assert card.sparkline.is_rendering_suspended is True
        assert card.sparkline.has_cached_buffers is False

    # Uncover widget (return to desktop) -> wakes to ACTIVE immediately with 0 ms debounce
    state["obscured"] = False
    t0 = time.perf_counter()
    window.power_manager.evaluate_now()
    wake_elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert window.operating_state == WidgetOperatingState.ACTIVE
    assert window.is_power_active is True
    assert wake_elapsed_ms < 100.0
    assert window.power_manager.last_wake_latency_ms < 100.0
    assert window._age_timer.isActive() is True
    for card in window.primary_cards.values():
        assert card.sparkline.is_rendering_suspended is False

    assert budget_mgr.get_status().monthly_total_used == 0
    window.close()


def test_brief_window_switch_does_not_trigger_idle_transition(qapp, tmp_path: Path):
    """Brief occlusion shorter than the debounce interval must not trigger any state change."""
    state = {"obscured": False}
    window, _, _, _ = _build_window(tmp_path, state, debounce_ms=140)
    window.show()
    QTest.qWait(20)

    initial_transitions = window.power_manager.transition_count
    assert window.operating_state == WidgetOperatingState.ACTIVE

    # Briefly obscure widget for 35 ms (< 140 ms debounce), then uncover
    state["obscured"] = True
    window.power_manager.evaluate_now()
    assert window.power_manager.is_debounce_pending is True
    QTest.qWait(35)

    state["obscured"] = False
    window.power_manager.evaluate_now()
    assert window.power_manager.is_debounce_pending is False

    # Wait past original debounce timeout and verify zero transitions occurred
    QTest.qWait(140)
    assert window.operating_state == WidgetOperatingState.ACTIVE
    assert window.power_manager.transition_count == initial_transitions
    window.close()


def test_losing_focus_alone_does_not_put_widget_into_idle(qapp, tmp_path: Path):
    """Losing focus to another window that does not cover Chande must keep Chande ACTIVE."""
    state = {"obscured": False}
    window, _, _, _ = _build_window(tmp_path, state, debounce_ms=50)
    window.move(50, 50)
    window.resize(460, 380)
    window.show()

    # Open another active application window on a non-overlapping area of the desktop
    other_app = QWidget()
    other_app.setGeometry(620, 100, 400, 300)
    other_app.show()
    other_app.activateWindow()
    other_app.raise_()
    QTest.qWait(80)

    # Even though `other_app` has focus, Chande is not covered and remains ACTIVE
    window.power_manager.evaluate_now()
    QTest.qWait(80)
    assert window.operating_state == WidgetOperatingState.ACTIVE
    assert is_window_stealing_foreground(int(window.winId())) is False

    other_app.close()
    window.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 Z-order occlusion test")
def test_live_win32_partial_vs_full_occlusion_detection(qapp, tmp_path: Path):
    """Verify real Win32 Z-order occlusion detection with an actual covering window."""
    settings_mgr = SettingsManager(config_path=tmp_path / "settings.json")
    settings_mgr.update(
        auto_refresh_enabled=False,
        always_on_top=False,
        window_x=150,
        window_y=150,
        window_width=360,
        window_height=290,
    )
    budget_mgr = RequestBudgetManager(budget_path=tmp_path / "budget.json")
    cache_store = CacheStore(cache_path=tmp_path / "cache.json")
    cache_store.update_success(SAMPLE_RATES, fetched_at=float(int(time.time()) - 60))
    api_client = MarketApiClient(budget_manager=budget_mgr, cache_store=cache_store)

    window = MainWindow(
        settings_manager=settings_mgr,
        budget_manager=budget_mgr,
        cache_store=cache_store,
        api_client=api_client,
        enable_tray=False,
        auto_start_schedule=False,
        obscured_debounce_ms=60,
    )
    window.show()
    QTest.qWait(40)
    hwnd = int(window.winId())

    # Create a topmost blocker window that first partially overlaps, then fully covers Chande
    blocker = QWidget(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    blocker.setStyleSheet("background-color: #202020;")
    # 1. Partial overlap (covers only left half of Chande) -> must stay ACTIVE
    w_geom = window.geometry()
    blocker.setGeometry(w_geom.left() - 20, w_geom.top() - 20, w_geom.width() // 2, w_geom.height() + 40)
    blocker.show()
    blocker.raise_()
    QTest.qWait(80)

    assert is_window_obscured(hwnd) is False
    window.power_manager.evaluate_now()
    QTest.qWait(80)
    assert window.operating_state == WidgetOperatingState.ACTIVE

    # 2. Full overlap (maximized / covers entire Chande bounding box) -> enters IDLE after debounce
    blocker.setGeometry(
        w_geom.left() - 40,
        w_geom.top() - 40,
        w_geom.width() + 80,
        w_geom.height() + 80,
    )
    blocker.raise_()
    QTest.qWait(30)
    assert is_window_obscured(hwnd) is True

    window.power_manager.evaluate_now()
    QTest.qWait(90)
    assert window.operating_state == WidgetOperatingState.IDLE

    # 3. Hide blocker (return to desktop) -> wakes immediately to ACTIVE
    blocker.hide()
    QTest.qWait(20)
    assert is_window_obscured(hwnd) is False
    window.power_manager.evaluate_now()
    assert window.operating_state == WidgetOperatingState.ACTIVE

    blocker.close()
    window.close()


def test_hiding_widget_enters_dormant_and_stops_visibility_polling(qapp, tmp_path: Path):
    """Hiding the widget intentionally puts it into DORMANT mode and stops all visibility polling."""
    state = {"obscured": False}
    window, _, budget_mgr, _ = _build_window(tmp_path, state, debounce_ms=50)
    window.show()
    QTest.qWait(30)
    assert window.operating_state == WidgetOperatingState.ACTIVE
    assert window.power_manager.is_polling_active is True

    # Hide via tray toggle -> enters DORMANT and stops polling timer
    window._toggle_visibility_from_tray()
    QTest.qWait(20)
    assert window.isVisible() is False
    assert window.operating_state == WidgetOperatingState.DORMANT
    assert window.is_power_dormant is True
    assert window.power_manager.is_polling_active is False
    assert window._age_timer.isActive() is False

    # Restore via tray toggle -> returns to ACTIVE immediately without stealing focus or calling API
    window._toggle_visibility_from_tray()
    QTest.qWait(20)
    assert window.isVisible() is True
    assert window.operating_state == WidgetOperatingState.ACTIVE
    assert window.power_manager.is_polling_active is True
    assert window._age_timer.isActive() is True
    assert budget_mgr.get_status().monthly_total_used == 0
    window.close()


def test_cached_prices_preserved_and_background_cache_update_applied_on_wake(qapp, tmp_path: Path):
    """Cached prices remain intact across sleep/wake, and scheduled updates while IDLE appear on wake."""
    state = {"obscured": False}
    window, api_client, budget_mgr, cache_store = _build_window(tmp_path, state, debounce_ms=50, language="en")
    window.show()
    QTest.qWait(20)

    initial_usd_text = window.primary_cards["usd_sell"].price_label.text()
    assert "685,000" in initial_usd_text

    # Enter IDLE
    state["obscured"] = True
    window.power_manager.evaluate_now()
    QTest.qWait(120)
    assert window.operating_state == WidgetOperatingState.IDLE

    # Simulate a scheduled background cache update while IDLE
    updated_rates = dict(SAMPLE_RATES)
    updated_rates["usd_sell"] = {"value": "712000", "change": 5200, "timestamp": int(time.time())}
    cache_store.update_success(updated_rates, fetched_at=float(int(time.time())))
    window._dirty_while_sleeping = True

    # Wake widget -> updated cached value is immediately displayed without any API request
    state["obscured"] = False
    window.power_manager.evaluate_now()
    assert window.operating_state == WidgetOperatingState.ACTIVE
    assert "712,000" in window.primary_cards["usd_sell"].price_label.text()
    assert budget_mgr.get_status().monthly_total_used == 0
    window.close()


def test_no_position_or_size_jump_and_zero_api_calls_across_transitions(qapp, tmp_path: Path, monkeypatch):
    """Transitions between ACTIVE, IDLE, and DORMANT must never alter window geometry or trigger API calls."""
    state = {"obscured": False}
    window, api_client, budget_mgr, _ = _build_window(tmp_path, state, debounce_ms=40)

    api_calls = []
    monkeypatch.setattr(api_client, "fetch_latest", lambda *a, **kw: api_calls.append((a, kw)))

    window.show()
    window.move(180, 160)
    window.resize(500, 420)
    QTest.qWait(25)
    expected_geom = window.geometry()

    # ACTIVE -> IDLE -> ACTIVE -> DORMANT -> ACTIVE
    state["obscured"] = True
    window.power_manager.evaluate_now()
    QTest.qWait(110)
    assert window.operating_state == WidgetOperatingState.IDLE
    assert window.geometry() == expected_geom

    state["obscured"] = False
    window.power_manager.evaluate_now()
    assert window.operating_state == WidgetOperatingState.ACTIVE
    assert window.geometry() == expected_geom

    window.hide()
    assert window.operating_state == WidgetOperatingState.DORMANT

    window.show_gadget_without_focus()
    assert window.operating_state == WidgetOperatingState.ACTIVE
    assert window.geometry() == expected_geom
    assert len(api_calls) == 0
    assert budget_mgr.get_status().monthly_total_used == 0
    window.close()


def test_compatibility_with_bilingual_switching_and_compact_resizing(qapp, tmp_path: Path):
    """Verify Persian/English switching and responsive Expanded/Compact/Ultra-Compact resizing across sleep/wake."""
    state = {"obscured": False}
    window, _, _, _ = _build_window(tmp_path, state, debounce_ms=40, language="fa")
    window.show()
    QTest.qWait(20)

    # Enter IDLE in Persian Expanded mode
    state["obscured"] = True
    window.power_manager.evaluate_now()
    QTest.qWait(110)
    assert window.operating_state == WidgetOperatingState.IDLE

    # Switch language and resize to Compact while IDLE, then wake
    window.set_language("en")
    window.resize(350, 280)
    state["obscured"] = False
    window.power_manager.evaluate_now()

    assert window.operating_state == WidgetOperatingState.ACTIVE
    assert window.language == "en"
    assert window.responsive_mode == "compact"
    assert "US Dollar" in window.primary_cards["usd_sell"].name_label.text()
    # In compact mode, sparkline remains hidden & rendering-suspended even when window is ACTIVE
    assert window.primary_cards["usd_sell"].sparkline.isVisible() is False
    assert window.primary_cards["usd_sell"].sparkline.is_rendering_suspended is True

    # Resize back to Expanded in ACTIVE mode -> sparkline resumes immediately
    window.resize(540, 480)
    QTest.qWait(20)
    assert window.responsive_mode == "expanded"
    assert window.primary_cards["usd_sell"].sparkline.isVisible() is True
    assert window.primary_cards["usd_sell"].sparkline.is_rendering_suspended is False
    window.close()


def test_memory_stability_and_idempotency_across_repeated_transitions(qapp, tmp_path: Path):
    """Repeated ACTIVE <-> IDLE <-> DORMANT transitions must be idempotent and leak-free."""
    state = {"obscured": False}
    window, _, _, _ = _build_window(tmp_path, state, debounce_ms=10)
    window.show()
    QTest.qWait(20)

    # Idempotency check: repeated evaluate_now in same state does not increment transition_count
    c0 = window.power_manager.transition_count
    for _ in range(10):
        window.power_manager.evaluate_now()
    assert window.power_manager.transition_count == c0

    tracemalloc.start()
    snap_before = tracemalloc.take_snapshot()

    for _ in range(60):
        state["obscured"] = True
        window.power_manager._on_obscured_debounce_timeout()
        assert window.operating_state == WidgetOperatingState.IDLE

        state["obscured"] = False
        window.power_manager.evaluate_now()
        assert window.operating_state == WidgetOperatingState.ACTIVE

        window.power_manager.set_dormant(True)
        assert window.operating_state == WidgetOperatingState.DORMANT

        window.power_manager.set_dormant(False)
        assert window.operating_state == WidgetOperatingState.ACTIVE

    snap_after = tracemalloc.take_snapshot()
    tracemalloc.stop()

    stats = snap_after.compare_to(snap_before, "lineno")
    net_growth_bytes = sum(s.size_diff for s in stats)
    # Net memory growth across 240 state transitions must remain negligible (< 128 KB)
    assert net_growth_bytes < 128 * 1024

    # Clean shutdown stops all timers
    window.power_manager.stop()
    assert window.power_manager.is_polling_active is False
    assert window.power_manager.is_debounce_pending is False
    window.close()


def test_debug_logging_does_not_leak_sensitive_data(qapp, tmp_path: Path, caplog):
    """State transition debug logs must record state transitions without logging API keys."""
    secret_key = "SUPER_SECRET_NAVASAN_KEY_999"
    state = {"obscured": False}
    window, _, _, _ = _build_window(tmp_path, state, debounce_ms=20)
    window.settings_manager.update(api_key=secret_key)
    window.show()

    with caplog.at_level(logging.DEBUG, logger="chande.power"):
        state["obscured"] = True
        window.power_manager._on_obscured_debounce_timeout()
        state["obscured"] = False
        window.power_manager.evaluate_now()

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "active -> idle" in log_text
    assert "idle -> active" in log_text
    assert secret_key not in log_text
    window.close()
