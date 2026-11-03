"""Comprehensive tests for smooth edge/corner resizing and 3-state adaptive layout (Expanded, Compact, Ultra-Compact)."""

from __future__ import annotations
from pathlib import Path
import time
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from config.settings import AppSettings, SettingsManager
from services.api_client import MarketApiClient
from services.budget_manager import RequestBudgetManager
from services.windows_gadget import (
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    get_window_ex_style,
    is_window_in_alt_tab,
    is_window_topmost,
)
from storage.cache_store import CacheStore
from ui.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _sample_payload():
    return {
        "usd_sell": {
            "value": "88500",
            "change": 1200,
            "timestamp": 1730000000,
            "date": "1403-08-08 12:00:00",
        },
        "18ayar": {
            "value": "4520000",
            "change": -25000,
            "timestamp": 1730000000,
            "date": "1403-08-08 12:00:00",
        },
        "btc": {
            "value": "6150000000",
            "change": 45000000,
            "dollar_rate": "69500",
            "timestamp": 1730000000,
            "date": "1403-08-08 12:00:00",
        },
        "usdt": {
            "value": "88950",
            "change": 450,
            "timestamp": 1730000000,
            "date": "1403-08-08 12:00:00",
        },
    }


def _create_window(tmp_path: Path):
    cfg_file = tmp_path / "user_config.json"
    cache_file = tmp_path / "market_cache.json"
    budget_file = tmp_path / "request_budget.json"

    sm = SettingsManager(config_path=cfg_file)
    sm.save(
        AppSettings(
            api_key="test_resize_key_1234",
            api_provider="navasan",
            auto_refresh_enabled=False,
            refresh_interval_hours=8,
            confirm_manual_refresh=False,
            use_persian_numerals=False,
            show_chart=True,
            show_secondary_cards=True,
        )
    )
    cs = CacheStore(cache_path=cache_file)
    # Seed 3 historical snapshots so 24h sparklines have multi-point trend data
    cs.update_success(_sample_payload(), fetched_at=1730000000.0)
    payload_2 = _sample_payload()
    payload_2["usd_sell"]["value"] = "88750"
    payload_2["18ayar"]["value"] = "4535000"
    cs.update_success(payload_2, fetched_at=1730028800.0)
    payload_3 = _sample_payload()
    payload_3["usd_sell"]["value"] = "89100"
    payload_3["18ayar"]["value"] = "4550000"
    cs.update_success(payload_3, fetched_at=1730057600.0)

    bm = RequestBudgetManager(budget_path=budget_file, time_fn=lambda: 1730060000.0)
    mock_session = MagicMock()
    client = MarketApiClient(
        budget_manager=bm,
        cache_store=cs,
        session=mock_session,
        time_fn=lambda: 1730060000.0,
    )
    win = MainWindow(
        settings_manager=sm,
        budget_manager=bm,
        cache_store=cs,
        api_client=client,
        enable_tray=True,
        auto_start_schedule=False,
    )
    return win, sm, cs, bm, mock_session, cfg_file


def test_hit_test_and_smooth_resizing_all_edges_and_corners(qapp, tmp_path: Path):
    """Verify hit-testing, resize cursors, and smooth dragging from all 4 edges and 4 corners."""
    win, _, _, bm, mock_session, _ = _create_window(tmp_path)
    win.show()
    win.setGeometry(200, 150, 540, 480)
    qapp.processEvents()

    w, h = win.width(), win.height()

    # 1. Verify edge and corner hit-testing and cursors
    assert win.hit_test_resize_edges(QPoint(2, h // 2)) == Qt.Edge.LeftEdge
    assert win.cursor_for_edges(Qt.Edge.LeftEdge) == Qt.CursorShape.SizeHorCursor

    assert win.hit_test_resize_edges(QPoint(w - 2, h // 2)) == Qt.Edge.RightEdge
    assert win.cursor_for_edges(Qt.Edge.RightEdge) == Qt.CursorShape.SizeHorCursor

    assert win.hit_test_resize_edges(QPoint(w // 2, 2)) == Qt.Edge.TopEdge
    assert win.cursor_for_edges(Qt.Edge.TopEdge) == Qt.CursorShape.SizeVerCursor

    assert win.hit_test_resize_edges(QPoint(w // 2, h - 2)) == Qt.Edge.BottomEdge
    assert win.cursor_for_edges(Qt.Edge.BottomEdge) == Qt.CursorShape.SizeVerCursor

    tl = Qt.Edge.TopEdge | Qt.Edge.LeftEdge
    assert win.hit_test_resize_edges(QPoint(2, 2)) == tl
    assert win.cursor_for_edges(tl) == Qt.CursorShape.SizeFDiagCursor

    br = Qt.Edge.BottomEdge | Qt.Edge.RightEdge
    assert win.hit_test_resize_edges(QPoint(w - 2, h - 2)) == br
    assert win.cursor_for_edges(br) == Qt.CursorShape.SizeFDiagCursor

    tr = Qt.Edge.TopEdge | Qt.Edge.RightEdge
    assert win.hit_test_resize_edges(QPoint(w - 2, 2)) == tr
    assert win.cursor_for_edges(tr) == Qt.CursorShape.SizeBDiagCursor

    bl = Qt.Edge.BottomEdge | Qt.Edge.LeftEdge
    assert win.hit_test_resize_edges(QPoint(2, h - 2)) == bl
    assert win.cursor_for_edges(bl) == Qt.CursorShape.SizeBDiagCursor

    # Interior point must not trigger edge resize
    assert win.hit_test_resize_edges(QPoint(w // 2, h // 2)) == Qt.Edge(0)

    # 2. Simulate interactive mouse-drag resize from Bottom-Right corner
    start_local = QPoint(win.width() - 2, win.height() - 2)
    start_global = win.mapToGlobal(start_local)
    press_ev = QMouseEvent(
        QEvent_MouseButtonPress := QMouseEvent.Type.MouseButtonPress,
        QPointF(start_local),
        QPointF(start_global),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    win.mousePressEvent(press_ev)

    target_global = start_global + QPoint(-160, -170)
    move_ev = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        QPointF(win.mapFromGlobal(target_global)),
        QPointF(target_global),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    win.mouseMoveEvent(move_ev)

    rel_ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(win.mapFromGlobal(target_global)),
        QPointF(target_global),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    win.mouseReleaseEvent(rel_ev)
    qapp.processEvents()

    assert win.width() == 380
    assert win.height() == 310
    assert win.responsive_mode == "compact"

    # 3. Verify Top-Left corner resize shifts origin and clamps to MIN_WINDOW_SIZE / MAX_WINDOW_SIZE
    base_geom = QRect(300, 250, 400, 320)
    win.setGeometry(base_geom)
    win.perform_edge_resize(tl, QPoint(50, 40), start_geom=base_geom)
    assert win.width() == 350
    assert win.height() == 280
    assert win.x() == 350
    assert win.y() == 290

    # Clamp to minimum dimensions when dragging excessively inward
    win.perform_edge_resize(br, QPoint(-2000, -2000), start_geom=win.geometry())
    assert win.width() == MainWindow.MIN_WINDOW_SIZE.width()
    assert win.height() == MainWindow.MIN_WINDOW_SIZE.height()

    # Clamp to maximum dimensions when dragging excessively outward
    win.perform_edge_resize(br, QPoint(2000, 2000), start_geom=win.geometry())
    assert win.width() == MainWindow.MAX_WINDOW_SIZE.width()
    assert win.height() == MainWindow.MAX_WINDOW_SIZE.height()

    assert mock_session.get.call_count == 0
    assert bm.get_status().monthly_total_used == 0
    win._is_quitting = True
    win.close()


def test_three_responsive_modes_and_automatic_graph_visibility(qapp, tmp_path: Path):
    """Verify Expanded, Compact, and Ultra-Compact mode transitions, graph hiding/restoration, and zero empty space."""
    win, _, _, bm, mock_session, _ = _create_window(tmp_path)
    win.show()

    # --- State 1: Expanded Mode (540x480) ---
    win.resize(540, 480)
    qapp.processEvents()

    assert win.responsive_mode == "expanded"
    assert win.footer_frame.isVisible()
    assert win.secondary_container.isVisible()
    for sym, card in win.primary_cards.items():
        assert card.responsive_mode == "expanded"
        assert card.is_graph_effectively_visible, f"{sym} graph should be visible in Expanded mode"
        assert card.sparkline.isVisible()
        assert not card.sparkline.is_rendering_suspended
        assert card.bottom_row_widget.isVisible()
        assert card.unit_label.isVisible()

    # --- State 2: Compact Mode (360x290) ---
    win.resize(360, 290)
    qapp.processEvents()

    assert win.responsive_mode == "compact"
    assert not win.secondary_container.isVisible()
    assert win.overflow_btn.isVisible()
    for sym, card in win.primary_cards.items():
        assert card.responsive_mode == "compact"
        assert not card.is_graph_effectively_visible, f"{sym} graph must hide in Compact mode"
        assert not card.sparkline.isVisible()
        assert card.sparkline.is_rendering_suspended
        # Sparkline container spacing/margins collapse to 0 so hidden graph leaves zero empty space
        assert card.mid_row_layout.spacing() == 0
        assert not card.unit_label.isVisible()
        assert not card.freshness_label.isVisible()
        assert card.name_label.isVisible()
        assert card.price_label.isVisible()
        assert card.change_label.isVisible()

    # --- State 3: Ultra-Compact Mode (235x190) ---
    win.resize(235, 190)
    qapp.processEvents()

    assert win.responsive_mode == "ultra_compact"
    assert win._current_grid_cols == 1  # Reflowed into 1x4 vertical stack
    assert not win.footer_frame.isVisible()
    assert not win.banner_frame.isVisible()
    assert not win.secondary_container.isVisible()
    assert win.overflow_btn.isVisible()
    assert win.overflow_refresh_action.isEnabled()
    assert win.overflow_settings_action.isEnabled()

    for sym, card in win.primary_cards.items():
        assert card.responsive_mode == "ultra_compact"
        assert not card.is_graph_effectively_visible
        assert not card.sparkline.isVisible()
        assert card.sparkline.is_rendering_suspended
        assert not card.bottom_row_widget.isVisible()
        assert not card.mid_row_widget.isVisible()  # Inline single-row mode
        assert card.name_label.isVisible()
        assert card.price_label.isVisible()
        assert card.price_label.text() != ""
        # Ensure cards do not overflow window width
        assert card.width() <= win.width()

    # --- State 4: Enlarge back to Expanded Mode (540x480) ---
    win.resize(540, 480)
    qapp.processEvents()

    assert win.responsive_mode == "expanded"
    assert win._current_grid_cols == 2
    assert win.footer_frame.isVisible()
    for sym, card in win.primary_cards.items():
        assert card.responsive_mode == "expanded"
        assert card.is_graph_effectively_visible, f"{sym} graph must automatically reappear in Expanded mode"
        assert card.sparkline.isVisible()
        assert not card.sparkline.is_rendering_suspended
        assert card.bottom_row_widget.isVisible()

    assert mock_session.get.call_count == 0
    assert bm.get_status().monthly_total_used == 0
    win._is_quitting = True
    win.close()


def test_size_persistence_across_restarts(qapp, tmp_path: Path):
    """Selected window size and responsive state must be saved and restored on next startup."""
    win, sm, cs, bm, mock_session, cfg_file = _create_window(tmp_path)
    win.show()
    win.resize(355, 275)
    win.move(180, 140)
    qapp.processEvents()
    win._is_quitting = True
    win.close()  # triggers _persist_geometry

    # Reload SettingsManager from disk and launch a fresh MainWindow instance
    sm2 = SettingsManager(config_path=cfg_file)
    assert sm2.settings.window_width == 355
    assert sm2.settings.window_height == 275

    win2 = MainWindow(
        settings_manager=sm2,
        budget_manager=bm,
        cache_store=cs,
        api_client=win.api_client,
        enable_tray=False,
        auto_start_schedule=False,
    )
    win2.show()
    qapp.processEvents()

    assert win2.width() == 355
    assert win2.height() == 275
    assert win2.responsive_mode == "compact"
    for card in win2.primary_cards.values():
        assert not card.is_graph_effectively_visible

    assert mock_session.get.call_count == 0
    win2._is_quitting = True
    win2.close()


def test_exact_location_and_size_persistence_across_restarts_and_settings(qapp, tmp_path: Path):
    """Exact window location (window_x, window_y) and size must persist across moves, Settings saves, tray toggles, and restarts."""
    from ui.settings_dialog import SettingsDialog

    win, sm, cs, bm, mock_session, cfg_file = _create_window(tmp_path)
    win.show_gadget_without_focus()
    qapp.processEvents()

    # 1. Move and resize to a custom ultra-compact location (e.g. top-right corner area)
    avail = qapp.primaryScreen().availableGeometry()
    target_w, target_h = 210, 175
    target_x = max(avail.left() + 10, min(avail.left() + 320, avail.right() - target_w - 10))
    target_y = avail.top() + 15

    win.resize(target_w, target_h)
    win.move(target_x, target_y)
    qapp.processEvents()

    # Verify moveEvent and resizeEvent immediately updated in-memory settings
    assert sm.settings.window_x == target_x
    assert sm.settings.window_y == target_y
    assert sm.settings.window_width == target_w
    assert sm.settings.window_height == target_h
    assert sm.settings.display_mode == "ultra_compact"

    # 2. Open SettingsDialog and save an unrelated setting (e.g. opacity/theme) -> must NOT reset size or shift position
    dlg = SettingsDialog(
        settings_manager=sm,
        budget_manager=bm,
        cache_store=cs,
        api_client=win.api_client,
        parent=win,
    )
    dlg.settings_saved.connect(win._on_settings_saved)
    dlg.opacity_slider.setValue(90)
    dlg._on_save()
    qapp.processEvents()

    assert win.x() == target_x
    assert win.y() == target_y
    assert win.width() == target_w
    assert win.height() == target_h

    # 3. Toggle Always-on-Top and hide/restore from tray -> must preserve exact location
    win.pin_btn.setChecked(not win.pin_btn.isChecked())
    win.toggle_always_on_top()
    qapp.processEvents()
    assert win.x() == target_x
    assert win.y() == target_y
    assert win.width() == target_w
    assert win.height() == target_h

    win.hide()
    qapp.processEvents()
    win.show_gadget_without_focus()
    qapp.processEvents()
    assert win.x() == target_x
    assert win.y() == target_y
    assert win.width() == target_w
    assert win.height() == target_h

    # 4. Close and restart a new MainWindow instance -> must start at the exact same location and size
    win._is_quitting = True
    win.close()

    sm_restarted = SettingsManager(config_path=cfg_file)
    assert sm_restarted.settings.window_x == target_x
    assert sm_restarted.settings.window_y == target_y
    assert sm_restarted.settings.window_width == target_w
    assert sm_restarted.settings.window_height == target_h

    win_restarted = MainWindow(
        settings_manager=sm_restarted,
        budget_manager=bm,
        cache_store=cs,
        api_client=win.api_client,
        enable_tray=False,
        auto_start_schedule=False,
    )
    win_restarted.show_gadget_without_focus()
    qapp.processEvents()

    assert win_restarted.x() == target_x
    assert win_restarted.y() == target_y
    assert win_restarted.width() == target_w
    assert win_restarted.height() == target_h
    assert win_restarted.responsive_mode == "ultra_compact"
    assert mock_session.get.call_count == 0
    win_restarted._is_quitting = True
    win_restarted.close()

    win_restarted._is_quitting = True
    win_restarted.close()


def test_continuous_resizing_zero_api_calls_and_preserve_gadget_behavior(qapp, tmp_path: Path):
    """Continuous live resizing across 45 steps must issue 0 API calls, stay fast, and keep gadget styles intact."""
    win, _, _, bm, mock_session, _ = _create_window(tmp_path)
    win.show()
    qapp.processEvents()

    # Sweep smoothly from 620x520 down to 210x175 and back up to 540x480
    sizes = [
        (620 - i * 20, 520 - i * 17) for i in range(21)
    ] + [
        (210 + i * 16, 175 + i * 15) for i in range(22)
    ]

    t0 = time.perf_counter()
    for w, h in sizes:
        win.resize(w, h)
        win.repaint()
    qapp.processEvents()
    elapsed = time.perf_counter() - t0

    # Must complete 43 full resize+repaint frames in well under 1.5s with 0 API calls
    assert elapsed < 1.5
    assert mock_session.get.call_count == 0
    assert bm.get_status().monthly_total_used == 0

    # Verify Windows 7 desktop gadget native behavior is preserved after resizing
    hwnd = int(win.winId())
    ex_style = get_window_ex_style(hwnd)
    assert is_window_in_alt_tab(hwnd) is False
    assert is_window_topmost(hwnd) is True
    assert bool(ex_style & WS_EX_TOOLWINDOW) is True
    assert bool(ex_style & WS_EX_NOACTIVATE) is True

    win._is_quitting = True
    win.close()
