"""Acceptance tests for Windows 7 Desktop Gadget behavior, Alt+Tab exclusion, Startup, and Single-Instance."""

from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMainWindow

from config.settings import AppSettings, SettingsManager
from services.api_client import MarketApiClient
from services.budget_manager import RequestBudgetManager
from services.windows_gadget import (
    HWND_BOTTOM,
    HWND_TOPMOST,
    SC_MINIMIZE,
    SWP_HIDEWINDOW,
    SWP_NOACTIVATE,
    WM_SHOWWINDOW,
    WM_SYSCOMMAND,
    WS_EX_APPWINDOW,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    SingleInstanceController,
    WindowsStartupManager,
    ensure_gadget_visible_on_desktop,
    get_window_ex_style,
    is_window_in_alt_tab,
    is_window_minimized_or_hidden,
    is_window_stealing_foreground,
    is_window_topmost,
)
from storage.cache_store import CacheStore
from ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _make_gadget(tmp_path: Path, enable_tray: bool = False) -> MainWindow:
    sm = SettingsManager(config_path=tmp_path / "user_config.json")
    sm.update(
        api_key="test_key_123456",
        always_on_top=True,
        start_with_windows=True,
        confirm_manual_refresh=False,
    )
    bm = RequestBudgetManager(budget_path=tmp_path / "request_budget.json")
    cs = CacheStore(cache_path=tmp_path / "market_cache.json")
    client = MarketApiClient(budget_manager=bm, cache_store=cs)
    win = MainWindow(
        settings_manager=sm,
        budget_manager=bm,
        cache_store=cs,
        api_client=client,
        enable_tray=enable_tray,
        auto_start_schedule=False,
    )
    return win


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-native HWND styles test")
def test_gadget_native_window_styles_and_alt_tab_exclusion(qapp, tmp_path: Path) -> None:
    """Verify Chande is visible on the desktop, has WS_EX_TOOLWINDOW, no WS_EX_APPWINDOW, and is excluded from Alt+Tab."""
    win = _make_gadget(tmp_path)
    win.show_gadget_without_focus()
    qapp.processEvents()

    hwnd = int(win.winId())
    assert win.isVisible() is True

    ex_style = get_window_ex_style(hwnd)
    assert bool(ex_style & WS_EX_TOOLWINDOW) is True, "Must have WS_EX_TOOLWINDOW"
    assert bool(ex_style & WS_EX_APPWINDOW) is False, "Must NOT have WS_EX_APPWINDOW"
    assert bool(ex_style & WS_EX_NOACTIVATE) is True, "Must have WS_EX_NOACTIVATE"
    assert is_window_topmost(hwnd) is True, "Must have WS_EX_TOPMOST"
    assert is_window_in_alt_tab(hwnd) is False, "Must NOT appear in Windows Alt+Tab switcher"

    win.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-native z-order & focus test")
def test_gadget_stays_above_maximized_window_without_stealing_focus(qapp, tmp_path: Path) -> None:
    """Verify Chande stays topmost when another normal window is opened/maximized and never steals focus."""
    win = _make_gadget(tmp_path)
    win.show_gadget_without_focus()
    qapp.processEvents()
    hwnd = int(win.winId())

    # Open and maximize a standard application window
    other_app = QMainWindow()
    other_app.setWindowTitle("Other Normal Application")
    other_app.resize(800, 600)
    other_app.showMaximized()
    other_app.raise_()
    other_app.activateWindow()
    qapp.processEvents()

    # Chande must remain visible and topmost while not stealing foreground focus
    assert win.isVisible() is True
    assert is_window_topmost(hwnd) is True
    assert is_window_stealing_foreground(hwnd) is False
    assert is_window_in_alt_tab(hwnd) is False

    other_app.close()
    win.close()


def test_offscreen_position_clamped_to_visible_desktop(qapp, tmp_path: Path) -> None:
    """Verify invalid off-screen saved coordinates are clamped inside available screen geometry."""
    sm = SettingsManager(config_path=tmp_path / "user_config.json")
    sm.update(
        window_x=-99999,
        window_y=99999,
        window_width=540,
        window_height=480,
    )
    bm = RequestBudgetManager(budget_path=tmp_path / "request_budget.json")
    cs = CacheStore(cache_path=tmp_path / "market_cache.json")
    client = MarketApiClient(budget_manager=bm, cache_store=cs)

    win = MainWindow(
        settings_manager=sm,
        budget_manager=bm,
        cache_store=cs,
        api_client=client,
        enable_tray=False,
        auto_start_schedule=False,
    )
    qapp.processEvents()

    screens = QGuiApplication.screens()
    assert len(screens) >= 1
    # Verify the widget's center lies inside at least one screen's availableGeometry
    center = win.frameGeometry().center()
    assert any(scr.availableGeometry().contains(center) for scr in screens)
    win.close()


def test_single_instance_controller_blocks_duplicate_and_signals_primary(qapp) -> None:
    """Verify launching a second instance signals the primary instance and returns False."""
    server_name = "ChandeTest_IPC_Unique_v1"
    mutex_name = "Local\\ChandeTest_Mutex_Unique_v1"

    primary = SingleInstanceController(server_name=server_name, mutex_name=mutex_name)
    secondary = SingleInstanceController(server_name=server_name, mutex_name=mutex_name)
    activated_count = 0

    def _on_activated() -> None:
        nonlocal activated_count
        activated_count += 1

    primary.activation_requested.connect(_on_activated)

    try:
        assert primary.try_acquire_primary() is True
        assert secondary.try_acquire_primary() is False
        qapp.processEvents()
        assert activated_count == 1
    finally:
        secondary.release()
        primary.release()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows HKCU Run registry test")
def test_windows_startup_registration_idempotent_and_valid() -> None:
    """Verify startup registration writes a valid executable path to HKCU Run without duplicates."""
    # Enable multiple times to verify idempotency
    assert WindowsStartupManager.set_startup(True) is True
    assert WindowsStartupManager.set_startup(True) is True
    assert WindowsStartupManager.is_registered_and_valid() is True

    cmd = WindowsStartupManager.get_registered_command()
    assert cmd is not None and len(cmd) > 0

    # Disable and verify removal, then restore default enabled state
    assert WindowsStartupManager.set_startup(False) is True
    assert WindowsStartupManager.get_registered_command() is None

    assert WindowsStartupManager.set_startup(True) is True
    assert WindowsStartupManager.is_registered_and_valid() is True


def test_tray_menu_actions_and_zero_extra_api_calls(qapp, tmp_path: Path) -> None:
    """Verify tray menu items exist and Show/Hide or second-instance activation makes 0 API calls."""
    win = _make_gadget(tmp_path, enable_tray=True)
    with patch.object(win.api_client, "fetch_latest") as mock_fetch:
        win.show_gadget_without_focus()
        qapp.processEvents()

        if win.tray_icon is not None:
            assert hasattr(win, "tray_show_action")
            assert hasattr(win, "tray_refresh_action")
            assert hasattr(win, "tray_settings_action")
            assert hasattr(win, "tray_startup_action")
            assert hasattr(win, "tray_exit_action")
            assert win.tray_startup_action.isCheckable() is True

        # Hide and restore via tray handler -> 0 API calls
        win._toggle_visibility_from_tray()
        assert win.isVisible() is False
        win._toggle_visibility_from_tray()
        assert win.isVisible() is True

        # Second instance activation -> 0 API calls
        win.hide()
        win.on_second_instance_activated()
        assert win.isVisible() is True

        assert mock_fetch.call_count == 0
    if win.tray_icon is not None:
        win.tray_icon.hide()
    win._is_quitting = True
    win.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Show Desktop shell behavior test")
def test_windows_show_desktop_keeps_gadget_visible_and_unminimized(qapp, tmp_path: Path) -> None:
    """Verify clicking the Windows Show Desktop button (or Win+D) never minimizes, hides, or covers Chande."""
    import ctypes

    user32 = ctypes.windll.user32
    win = _make_gadget(tmp_path, enable_tray=True)
    win.resize(420, 580)
    win.move(220, 140)
    win.show_gadget_without_focus()
    qapp.processEvents()

    hwnd = int(win.winId())
    initial_pos = win.pos()
    initial_size = win.size()

    # Open a normal maximized application window alongside Chande
    normal_app = QMainWindow()
    normal_app.setWindowTitle("Maximized Normal App")
    normal_app.resize(900, 650)
    normal_app.showMaximized()
    normal_app.raise_()
    normal_app.activateWindow()
    qapp.processEvents()

    # Create a mock desktop shell surface window representing Explorer's WorkerW / Progman
    shell_surface = QMainWindow()
    shell_surface.setWindowTitle("Program Manager")
    shell_surface.setGeometry(0, 0, 1280, 720)
    shell_surface.show()
    qapp.processEvents()
    shell_hwnd = int(shell_surface.winId())

    with patch.object(win.api_client, "fetch_latest") as mock_fetch:
        # 1. Simulate shell sending WM_SYSCOMMAND + SC_MINIMIZE (Minimize All)
        user32.SendMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
        qapp.processEvents()
        assert win.isVisible() is True
        assert win.isMinimized() is False
        assert is_window_minimized_or_hidden(hwnd) is False
        assert win.size() == initial_size
        assert win.pos() == initial_pos

        # 2. Simulate shell sending WM_SHOWWINDOW with SW_PARENTCLOSING (lParam=1)
        user32.SendMessageW(hwnd, WM_SHOWWINDOW, 0, 1)
        qapp.processEvents()
        assert win.isVisible() is True
        assert win.isMinimized() is False
        assert win.size() == initial_size
        assert win.pos() == initial_pos

        # 3. Simulate shell SetWindowPos trying to hide Chande, move it to (-32000, -32000), or drop it to HWND_BOTTOM
        user32.SetWindowPos(
            hwnd,
            HWND_BOTTOM,
            -32000,
            -32000,
            160,
            28,
            SWP_HIDEWINDOW | SWP_NOACTIVATE,
        )
        qapp.processEvents()
        assert win.isVisible() is True
        assert is_window_minimized_or_hidden(hwnd) is False
        assert is_window_topmost(hwnd) is True
        assert win.size() == initial_size
        assert win.pos() == initial_pos

        # 4. Simulate Windows Show Desktop raising WorkerW above all windows into HWND_TOPMOST (multiple toggles)
        with patch("services.windows_gadget.find_desktop_shell_hwnds", return_value=[shell_hwnd]):
            for _ in range(3):
                # Explorer raises WorkerW to top of HWND_TOPMOST band on Show Desktop click
                user32.SetWindowPos(shell_hwnd, HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | SWP_NOACTIVATE)
                win._on_show_desktop_detected()
                qapp.processEvents()

                assert win.isVisible() is True
                assert win.isMinimized() is False
                assert is_window_topmost(hwnd) is True
                assert is_window_in_alt_tab(hwnd) is False
                assert is_window_stealing_foreground(hwnd) is False
                assert win.size() == initial_size
                assert win.pos() == initial_pos

                # Toggle Show Desktop off by activating the normal app window
                win._on_normal_window_activated(int(normal_app.winId()))
                qapp.processEvents()
                assert win.isVisible() is True
                assert win.size() == initial_size
                assert win.pos() == initial_pos

        # 5. Also verify Show Desktop protection when user has Always-on-Top unchecked (unpinned desktop gadget)
        win.settings_manager.update(always_on_top=False)
        with patch("services.windows_gadget.find_desktop_shell_hwnds", return_value=[shell_hwnd]):
            user32.SetWindowPos(shell_hwnd, HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | SWP_NOACTIVATE)
            win._on_show_desktop_detected()
            qapp.processEvents()
            assert win.isVisible() is True
            assert is_window_topmost(hwnd) is True
            assert win.size() == initial_size

            # When normal window is activated again, unpinned gadget returns to non-topmost cleanly
            win._on_normal_window_activated(int(normal_app.winId()))
            qapp.processEvents()
            assert win.isVisible() is True
            assert win.size() == initial_size

        assert mock_fetch.call_count == 0

    shell_surface.close()
    normal_app.close()
    if win.tray_icon is not None:
        win.tray_icon.hide()
    win._is_quitting = True
    win.close()
