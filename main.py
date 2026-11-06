"""Entry point for Chande — Lightweight Windows Currency & Gold Desktop Widget."""

from __future__ import annotations
import argparse
import ctypes
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from config.constants import APP_DISPLAY_FA, APP_NAME, APP_VERSION
from config.settings import SettingsManager
from services.api_client import MarketApiClient
from services.budget_manager import RequestBudgetManager
from services.i18n import get_layout_direction, set_current_language, tr
from services.windows_gadget import (
    SingleInstanceController,
    WindowsDesktopContextMenuManager,
    WindowsStartupManager,
)
from storage.cache_store import CacheStore
from ui.icon_loader import load_svg_icon
from ui.main_window import MainWindow


def _set_windows_app_user_model_id() -> None:
    """Register explicit Windows AppUserModelID so taskbar & tray icons group properly."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"{APP_NAME}.DesktopWidget.{APP_VERSION}"
            )
        except Exception:
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Chande — Lightweight Windows Currency & Gold Widget",
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Start minimized to the Windows system tray.",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove Chande per-user Windows startup and desktop context-menu registry entries.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {APP_VERSION}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.uninstall:
        WindowsStartupManager.set_startup(False)
        WindowsDesktopContextMenuManager.set_context_menu(False)
        return 0
    _set_windows_app_user_model_id()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_FA)
    app.setApplicationVersion(APP_VERSION)
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    app.setWindowIcon(load_svg_icon("gold-coin.svg", size=32))

    if QSystemTrayIcon.isSystemTrayAvailable():
        app.setQuitOnLastWindowClosed(False)

    # Enforce single-instance protection: if Chande is already running, notify primary and exit cleanly
    instance_controller = SingleInstanceController()
    if not instance_controller.try_acquire_primary():
        return 0

    app.aboutToQuit.connect(instance_controller.release)

    settings_manager = SettingsManager()
    set_current_language(settings_manager.settings.language)
    app.setLayoutDirection(get_layout_direction(settings_manager.settings.language))
    app.setApplicationDisplayName(tr("app.name", lang=settings_manager.settings.language))
    # Synchronize and self-heal Windows per-user sign-in startup and desktop context-menu registrations
    WindowsStartupManager.sync_on_startup(settings_manager.settings.start_with_windows)
    WindowsDesktopContextMenuManager.sync_on_startup(settings_manager.settings.desktop_context_menu)

    cache_store = CacheStore()
    budget_manager = RequestBudgetManager()
    api_client = MarketApiClient(
        budget_manager=budget_manager,
        cache_store=cache_store,
    )

    window = MainWindow(
        settings_manager=settings_manager,
        budget_manager=budget_manager,
        cache_store=cache_store,
        api_client=api_client,
        enable_tray=True,
        auto_start_schedule=True,
    )
    instance_controller.activation_requested.connect(window.on_second_instance_activated)

    if not args.minimized:
        window.show_gadget_without_focus()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
