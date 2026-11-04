"""Main desktop widget window for Chande (Windows 10/11 frameless glass UI)."""

from __future__ import annotations
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import time
from typing import Callable, Dict, Optional

from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QGuiApplication,
    QHideEvent,
    QIcon,
    QMouseEvent,
    QMoveEvent,
    QPaintEvent,
    QPainter,
    QPen,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizeGrip,
    QStackedWidget,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from config.constants import (
    MONTHLY_HARD_LIMIT,
    MONTHLY_RESERVE_BUDGET,
    PRIMARY_INSTRUMENTS,
    SECONDARY_INSTRUMENTS,
)
from config.settings import AppSettings, SettingsManager
from services.api_client import ApiFetchResult, MarketApiClient
from services.budget_manager import RequestBudgetManager
from services.formatter import (
    InstrumentQuote,
    format_cache_age,
    format_localized_datetime,
    localize_digits,
)
from services.i18n import (
    get_layout_direction,
    get_leading_alignment,
    get_trailing_alignment,
    is_rtl,
    set_current_language,
    tr,
)
from services.power_state import WidgetOperatingState, WidgetStateManager
from storage.cache_store import CacheStore
from services.windows_gadget import (
    HWND_TOPMOST,
    SC_MINIMIZE,
    SWP_HIDEWINDOW,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_NOZORDER,
    WINDOWPOS,
    WM_ACTIVATEAPP,
    WM_EXITSIZEMOVE,
    WM_SHOWWINDOW,
    WM_SYSCOMMAND,
    WM_WINDOWPOSCHANGED,
    WM_WINDOWPOSCHANGING,
    DesktopShellEventMonitor,
    apply_desktop_gadget_style,
    ensure_gadget_visible_on_desktop,
    is_desktop_shell_above_window,
    is_window_obscured,
    raise_gadget_without_focus,
)
from ui.icon_loader import load_svg_icon, load_svg_pixmap
from ui.price_card import CompactPriceRow, PrimaryPriceCard, SecondaryMiniCard
from ui.settings_dialog import SettingsDialog
from ui.theme import FONT_FAMILY_STACK, ThemePalette, get_palette
from ui.update_dialog import UpdateNotificationDialog
from ui.worker import MarketFetchWorker, UpdateCheckWorker, UpdateDownloadWorker
from updater.github_updater import (
    DownloadStageResult,
    GitHubUpdateService,
    ReleaseManifest,
    UpdateCheckResult,
)
from updater.installer import UpdateInstaller


class MainWindow(QMainWindow):
    """Frameless Windows 11-inspired desktop widget for Iranian currency, gold, and crypto prices.

    Strict architectural guarantees:
    - Never calls the API on paintEvent, resizeEvent, moveEvent, theme change, mode toggle,
      opening Settings, or restoring from the system tray.
    - Displays cached prices immediately on startup.
    - Enforces the 120/month hard cap, 90/month scheduled budget, and 3/day scheduled cap.
    """

    MIN_WINDOW_SIZE = QSize(210, 175)
    MAX_WINDOW_SIZE = QSize(960, 820)
    EXPANDED_SIZE = QSize(540, 480)
    COMPACT_SIZE = QSize(360, 290)
    ULTRA_COMPACT_SIZE = QSize(250, 200)

    # Responsive layout breakpoint thresholds (pixels)
    EXPANDED_MIN_WIDTH = 460
    EXPANDED_MIN_HEIGHT = 360
    ULTRA_COMPACT_MAX_WIDTH = 330
    ULTRA_COMPACT_MAX_HEIGHT = 255
    SINGLE_COL_MAX_WIDTH = 340
    RESIZE_BORDER_PX = 8

    def __init__(
        self,
        settings_manager: SettingsManager,
        budget_manager: RequestBudgetManager,
        cache_store: CacheStore,
        api_client: MarketApiClient,
        enable_tray: bool = True,
        auto_start_schedule: bool = True,
        obscured_debounce_ms: int = 1200,
        occlusion_checker: Optional[Callable[[], bool]] = None,
        update_service: Optional[GitHubUpdateService] = None,
    ) -> None:
        super().__init__()
        self.settings_manager = settings_manager
        self.budget_manager = budget_manager
        self.cache_store = cache_store
        self.api_client = api_client
        self.update_service = update_service or GitHubUpdateService()
        self._enable_tray = enable_tray

        self._drag_pos: Optional[QPoint] = None
        self._resize_edges: Qt.Edge = Qt.Edge(0)
        self._resize_start_global: Optional[QPoint] = None
        self._resize_start_geom: Optional[QRect] = None
        self._responsive_mode: str = "expanded"
        self._current_grid_cols: int = 2
        self._has_active_banner: bool = False
        self._banner_state: Optional[dict] = None
        self._fetch_worker: Optional[MarketFetchWorker] = None
        self._update_check_worker: Optional[UpdateCheckWorker] = None
        self._update_download_worker: Optional[UpdateDownloadWorker] = None
        self._active_update_dialog: Optional[UpdateNotificationDialog] = None
        self._pending_update_manifest: Optional[ReleaseManifest] = None
        self._is_quitting = False
        self._explicitly_hidden: bool = False
        self._keep_above_show_desktop: bool = False
        self._restoring_geometry: bool = True
        self._current_state = "idle"  # "loading", "live", "cached", "error", "exhausted", "missing_key"
        self._dirty_while_sleeping: bool = False
        self._last_active_size: QSize = self.EXPANDED_SIZE
        self._last_valid_size: QSize = self.EXPANDED_SIZE
        self._last_valid_pos: Optional[QPoint] = None

        self.primary_cards: Dict[str, PrimaryPriceCard] = {}
        self.secondary_cards: Dict[str, SecondaryMiniCard] = {}
        self.compact_rows: Dict[str, CompactPriceRow] = {}

        # Debounce timer so continuous mouse-drag resizing does not thrash disk I/O
        self._resize_save_timer = QTimer(self)
        self._resize_save_timer.setSingleShot(True)
        self._resize_save_timer.setInterval(200)
        self._resize_save_timer.timeout.connect(self._persist_geometry)

        # Lightweight timer (every 60s) solely to refresh the "X minutes ago" cache age label
        self._age_timer = QTimer(self)
        self._age_timer.setInterval(60_000)
        self._age_timer.timeout.connect(self._update_footer_and_freshness)

        # Single-shot timer for scheduled background refreshes (8h+ interval)
        self._schedule_timer = QTimer(self)
        self._schedule_timer.setSingleShot(True)
        self._schedule_timer.timeout.connect(self._on_scheduled_timer_fired)

        # Delayed startup timer and periodic 24h check timer for application updates
        self._update_startup_timer = QTimer(self)
        self._update_startup_timer.setSingleShot(True)
        self._update_startup_timer.timeout.connect(self._on_auto_update_timer_fired)

        self._update_periodic_timer = QTimer(self)
        self._update_periodic_timer.setInterval(3_600_000)  # Check hourly if 24h interval has elapsed
        self._update_periodic_timer.timeout.connect(self._on_auto_update_timer_fired)

        # Centralized desktop visibility & operating power-state controller
        self.power_manager = WidgetStateManager(
            visibility_checker=self._is_widget_visible_on_desktop,
            occlusion_checker=occlusion_checker or self._check_desktop_occlusion,
            obscured_debounce_ms=obscured_debounce_ms,
            wake_completion_hook=self._on_wake_completed,
            parent=self,
        )
        self.power_manager.state_changed.connect(self._on_operating_state_changed)

        # Event-driven Windows Shell monitor for 'Show Desktop' / 'Minimize All'
        self.shell_event_monitor = DesktopShellEventMonitor(
            target_hwnd_getter=self._safe_hwnd,
            parent=self,
        )
        self.shell_event_monitor.show_desktop_detected.connect(self._on_show_desktop_detected)
        self.shell_event_monitor.normal_window_activated.connect(self._on_normal_window_activated)

        self._configure_window_flags()
        self.setMinimumSize(self.MIN_WINDOW_SIZE)
        self.setMaximumSize(self.MAX_WINDOW_SIZE)
        self.setMouseTracking(True)
        self._build_ui()
        if self._enable_tray:
            self._setup_tray_icon()

        # Apply persisted appearance and geometry before showing the widget
        had_saved_pos = (
            self.settings_manager.settings.window_x is not None
            and self.settings_manager.settings.window_y is not None
        )
        had_saved_size = (
            self.settings_manager.settings.window_width is not None
            and self.settings_manager.settings.window_height is not None
        )
        self._apply_all_settings(self.settings_manager.settings, initial=True)
        if self._resize_save_timer.isActive():
            self._resize_save_timer.stop()
        self._restoring_geometry = False
        self._sync_geometry_in_memory()
        if not had_saved_pos or not had_saved_size:
            self._persist_geometry()
        self._last_active_size = self.size()
        self._last_valid_size = self.size()
        self._last_valid_pos = self.pos()
        self._connect_screen_change_signals()
        self._connect_app_shutdown_signals()

        # Step 1 & 2 on startup: Load and display cached prices immediately without network I/O
        self._load_cached_data_into_ui()
        self._age_timer.start()

        if auto_start_schedule:
            QTimer.singleShot(150, self._evaluate_startup_schedule)
            delay_ms = max(5, int(self.settings_manager.settings.update_startup_delay_seconds)) * 1000
            self._update_startup_timer.start(delay_ms)
            self._update_periodic_timer.start()

    @property
    def operating_state(self) -> WidgetOperatingState:
        """Current operating power/visibility state (`ACTIVE`, `IDLE`, or `DORMANT`)."""
        return self.power_manager.state

    @property
    def is_power_active(self) -> bool:
        return self.power_manager.is_active

    @property
    def is_power_idle(self) -> bool:
        return self.power_manager.is_idle

    @property
    def is_power_dormant(self) -> bool:
        return self.power_manager.is_dormant

    def _is_widget_visible_on_desktop(self) -> bool:
        """Return True if the widget is currently shown and not minimized."""
        return self.isVisible() and not self.isMinimized()

    def _check_desktop_occlusion(self) -> bool:
        """Return True if the widget's native window is 100% covered by other windows."""
        if not self.isVisible() or self.isMinimized():
            return True
        try:
            hwnd = int(self.winId())
        except Exception:
            return False
        return is_window_obscured(hwnd)

    def _on_operating_state_changed(self, _old_state: str, new_state: str) -> None:
        """Respond imperceptibly to transitions between `ACTIVE`, `IDLE`, and `DORMANT`."""
        if new_state in (WidgetOperatingState.IDLE.value, WidgetOperatingState.DORMANT.value):
            # Pause non-essential UI freshness timer while obscured or dormant
            self._age_timer.stop()
            # Suspend sparkline rendering and release cached QPainterPath buffers while keeping values intact
            for card in self.primary_cards.values():
                card.set_power_idle(True)
            if hasattr(self, "central_container"):
                self.central_container.setUpdatesEnabled(False)
            if self._resize_edges == Qt.Edge(0):
                self.unsetCursor()

    def _on_wake_completed(self) -> None:
        """Restore active rendering immediately on wake (< 100 ms target) without any API call."""
        if hasattr(self, "central_container"):
            self.central_container.setUpdatesEnabled(True)
        for card in self.primary_cards.values():
            card.set_power_idle(False)

        if self._dirty_while_sleeping:
            self._dirty_while_sleeping = False
            self._refresh_cards_from_memory()
            self._refresh_active_banner()
            self._style_state_badge()

        if self.size() != self._last_active_size:
            self._last_active_size = self.size()
            self._apply_responsive_layout(self.width(), self.height(), force=False)

        self._update_footer_and_freshness()
        if not self._age_timer.isActive() and not self._is_quitting:
            self._age_timer.start()
        self.update()

    @property
    def responsive_mode(self) -> str:
        """Current adaptive display mode ('expanded', 'compact', or 'ultra_compact')."""
        return self._responsive_mode

    @property
    def language(self) -> str:
        """Current active UI language ('fa' or 'en')."""
        return self.settings_manager.settings.language

    def compute_responsive_mode(self, width: int, height: int) -> str:
        """Determine the responsive display state based on available window dimensions."""
        if width < self.ULTRA_COMPACT_MAX_WIDTH or height < self.ULTRA_COMPACT_MAX_HEIGHT:
            return "ultra_compact"
        if width < self.EXPANDED_MIN_WIDTH or height < self.EXPANDED_MIN_HEIGHT:
            return "compact"
        return "expanded"

    def minimumSizeHint(self) -> QSize:
        """Override QMainWindow's layout minimumSizeHint so user-saved custom sizes are never expanded."""
        return self.MIN_WINDOW_SIZE

    def _safe_hwnd(self, force_create: bool = False) -> int:
        try:
            if not force_create and not self.testAttribute(Qt.WidgetAttribute.WA_WState_Created):
                return 0
            return int(self.winId())
        except Exception:
            return 0

    def _configure_window_flags(self) -> None:
        lang = self.settings_manager.settings.language
        set_current_language(lang)
        self.setWindowTitle(tr("app.full_title", lang=lang))
        self.setWindowIcon(load_svg_icon("gold-coin.svg", size=32))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setLayoutDirection(get_layout_direction(lang))

        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self.settings_manager.settings.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)

    def showEvent(self, event: QShowEvent) -> None:
        self._explicitly_hidden = False
        super().showEvent(event)
        self._apply_native_gadget_behavior()
        self.shell_event_monitor.start()
        self.power_manager.start()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self.power_manager.set_dormant(True)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized() and not self._explicitly_hidden and not self._is_quitting:
                # Never allow Windows Show Desktop / Minimize All to minimize the desktop gadget
                QTimer.singleShot(0, self._restore_after_show_desktop)
                return
            if self.isMinimized():
                self.power_manager.set_dormant(True)
            else:
                self.power_manager.evaluate_now()

    def _apply_native_gadget_behavior(self) -> None:
        """Apply native Win32 extended styles (WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, HWND_TOPMOST)."""
        hwnd = self._safe_hwnd()
        if not hwnd:
            return
        apply_desktop_gadget_style(
            hwnd,
            always_on_top=self.settings_manager.settings.always_on_top or self._keep_above_show_desktop,
            no_activate=True,
        )

    def _on_show_desktop_detected(self) -> None:
        """Handle Windows Show Desktop / Win+D shell events immediately without stealing focus."""
        if self._explicitly_hidden or self._is_quitting:
            return
        self._keep_above_show_desktop = True
        self._restore_after_show_desktop()

    def _on_normal_window_activated(self, _foreground_hwnd: int) -> None:
        """When a normal app window is activated after Show Desktop, restore non-topmost mode if unpinned."""
        if self._explicitly_hidden or self._is_quitting:
            return
        if self._keep_above_show_desktop and not self.settings_manager.settings.always_on_top:
            self._keep_above_show_desktop = False
            hwnd = self._safe_hwnd()
            if hwnd:
                raise_gadget_without_focus(hwnd, always_on_top=False)
        self.power_manager.evaluate_now()

    def _restore_after_show_desktop(self) -> None:
        """Keep the gadget visible in place above the raised WorkerW desktop surface during Show Desktop."""
        if self._explicitly_hidden or self._is_quitting:
            return
        hwnd = self._safe_hwnd()
        if not hwnd:
            return
        prev_restoring = self._restoring_geometry
        self._restoring_geometry = True
        try:
            if self.isMinimized():
                self.setWindowState(
                    (self.windowState() & ~Qt.WindowState.WindowMinimized)
                    | Qt.WindowState.WindowNoState
                )
            ensure_gadget_visible_on_desktop(
                hwnd,
                always_on_top=self.settings_manager.settings.always_on_top or self._keep_above_show_desktop,
            )
            if not self.isVisible():
                self.showNormal()
            if self._last_valid_size.isValid() and self.size() != self._last_valid_size:
                self.resize(self._last_valid_size)
            if self._last_valid_pos is not None and self.pos() != self._last_valid_pos:
                self.move(self._last_valid_pos)
        finally:
            self._restoring_geometry = prev_restoring
        self.power_manager.evaluate_now()

    def nativeEvent(self, eventType: object, message: object) -> Tuple[bool, int]:
        """Intercept Win32 shell window-management messages so Show Desktop never hides or minimizes Chande."""
        if sys.platform == "win32" and not self._explicitly_hidden and not self._is_quitting:
            try:
                msg_addr = int(message)  # type: ignore[arg-type]
                if msg_addr:
                    msg = ctypes.wintypes.MSG.from_address(msg_addr)
                    m = msg.message
                    if m == WM_SYSCOMMAND:
                        if (msg.wParam & 0xFFF0) == SC_MINIMIZE:
                            self._on_show_desktop_detected()
                            return True, 0
                    elif m == WM_SHOWWINDOW:
                        # Block shell-initiated hide (SW_PARENTCLOSING=1 / SW_OTHERZOOM=2 / SW_OTHERUNZOOM=4)
                        if msg.wParam == 0 and msg.lParam != 0:
                            QTimer.singleShot(0, self._on_show_desktop_detected)
                            return True, 0
                    elif m == WM_WINDOWPOSCHANGING and msg.lParam:
                        wp = WINDOWPOS.from_address(msg.lParam)
                        # Block shell hide flag unless explicitly hidden by user
                        if wp.flags & SWP_HIDEWINDOW:
                            wp.flags &= ~SWP_HIDEWINDOW
                        # Block shell attempts to move the gadget to iconic offscreen coordinates (-32000, -32000)
                        if not (wp.flags & SWP_NOMOVE) and (wp.x <= -30000 or wp.y <= -30000):
                            wp.flags |= SWP_NOMOVE
                        if not (wp.flags & SWP_NOSIZE) and (
                            wp.cx < self.MIN_WINDOW_SIZE.width() or wp.cy < self.MIN_WINDOW_SIZE.height()
                        ):
                            wp.flags |= SWP_NOSIZE
                        # Prevent shell from demoting Z-order during Show Desktop or when pinned
                        if self.settings_manager.settings.always_on_top or self._keep_above_show_desktop:
                            if not (wp.flags & SWP_NOZORDER):
                                if wp.hwndInsertAfter not in (HWND_TOPMOST, 0xFFFFFFFFFFFFFFFF, -1):
                                    wp.hwndInsertAfter = HWND_TOPMOST
                    elif m == WM_WINDOWPOSCHANGED:
                        hwnd = int(msg.hWnd or 0)
                        if hwnd and self.isVisible() and is_desktop_shell_above_window(hwnd):
                            QTimer.singleShot(0, self._on_show_desktop_detected)
                    elif m == WM_ACTIVATEAPP and msg.wParam == 0:
                        hwnd = int(msg.hWnd or 0)
                        if hwnd and self.isVisible() and is_desktop_shell_above_window(hwnd):
                            QTimer.singleShot(0, self._on_show_desktop_detected)
                    elif m == WM_EXITSIZEMOVE:
                        QTimer.singleShot(0, self._persist_geometry)
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def _build_ui(self) -> None:
        lang = self.settings_manager.settings.language
        self.central_container = QFrame(self)
        self.central_container.setObjectName("centralContainer")
        self.central_container.setMouseTracking(True)
        self.central_container.installEventFilter(self)
        self.setCentralWidget(self.central_container)

        self._root_layout = QVBoxLayout(self.central_container)
        self._root_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._root_layout.setContentsMargins(16, 14, 16, 12)
        self._root_layout.setSpacing(10)

        # --- Header Bar ---
        self.header_frame = QFrame()
        self.header_frame.setObjectName("headerFrame")
        self.header_frame.setMouseTracking(True)
        self._header_layout = QHBoxLayout(self.header_frame)
        self._header_layout.setContentsMargins(2, 0, 2, 4)
        self._header_layout.setSpacing(8)

        self.app_icon_label = QLabel()
        self.app_icon_label.setFixedSize(28, 28)
        self.app_icon_label.setPixmap(load_svg_pixmap("gold-coin.svg", size=26))
        self._header_layout.addWidget(self.app_icon_label)

        self.title_label = QLabel(tr("app.header_title", lang=lang))
        self.title_label.setAlignment(
            get_leading_alignment(lang) | Qt.AlignmentFlag.AlignVCenter
        )
        self._header_layout.addWidget(self.title_label)

        self.state_badge = QLabel(tr("status.idle", lang=lang))
        self.state_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._header_layout.addWidget(self.state_badge)

        self._header_layout.addStretch(1)

        # Header action buttons (Refresh, Mode Toggle, Pin, Settings, Overflow, Close)
        self.refresh_btn = self._create_header_button("refresh.svg", tr("header.refresh_tooltip", lang=lang))
        self.refresh_btn.clicked.connect(self.on_manual_refresh_clicked)
        self._header_layout.addWidget(self.refresh_btn)

        self.mode_btn = self._create_header_button("compact.svg", tr("header.mode_tooltip", lang=lang))
        self.mode_btn.clicked.connect(self.toggle_display_mode)
        self._header_layout.addWidget(self.mode_btn)

        self.pin_btn = self._create_header_button("pin.svg", tr("header.pin_tooltip", lang=lang))
        self.pin_btn.setCheckable(True)
        self.pin_btn.clicked.connect(self.toggle_always_on_top)
        self._header_layout.addWidget(self.pin_btn)

        self.settings_btn = self._create_header_button("settings.svg", tr("header.settings_tooltip", lang=lang))
        self.settings_btn.clicked.connect(self.open_settings_dialog)
        self._header_layout.addWidget(self.settings_btn)

        # Compact/Ultra-compact Overflow Menu Button (guarantees access to Refresh & Settings at any size)
        self.overflow_btn = self._create_header_button("settings.svg", tr("header.overflow_tooltip", lang=lang))
        self.overflow_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.overflow_menu = QMenu(self)
        self.overflow_menu.setLayoutDirection(get_layout_direction(lang))

        self.overflow_refresh_action = QAction(tr("menu.refresh", lang=lang), self)
        self.overflow_refresh_action.triggered.connect(self.on_manual_refresh_clicked)
        self.overflow_menu.addAction(self.overflow_refresh_action)

        self.overflow_settings_action = QAction(tr("menu.settings", lang=lang), self)
        self.overflow_settings_action.triggered.connect(self.open_settings_dialog)
        self.overflow_menu.addAction(self.overflow_settings_action)

        self.overflow_mode_action = QAction(tr("menu.toggle_mode", lang=lang), self)
        self.overflow_mode_action.triggered.connect(self.toggle_display_mode)
        self.overflow_menu.addAction(self.overflow_mode_action)

        self.overflow_pin_action = QAction(tr("menu.always_on_top", lang=lang), self)
        self.overflow_pin_action.setCheckable(True)
        self.overflow_pin_action.triggered.connect(self._on_overflow_pin_toggled)
        self.overflow_menu.addAction(self.overflow_pin_action)

        self.overflow_chart_action = QAction(tr("menu.show_chart", lang=lang), self)
        self.overflow_chart_action.setCheckable(True)
        self.overflow_chart_action.triggered.connect(self._on_overflow_chart_toggled)
        self.overflow_menu.addAction(self.overflow_chart_action)

        self.overflow_lang_action = QAction(tr("menu.switch_language", lang=lang), self)
        self.overflow_lang_action.triggered.connect(self.toggle_language)
        self.overflow_menu.addAction(self.overflow_lang_action)

        self.overflow_btn.setMenu(self.overflow_menu)
        self.overflow_btn.hide()
        self._header_layout.addWidget(self.overflow_btn)

        self.close_btn = self._create_header_button("close.svg", tr("header.close_tooltip", lang=lang))
        self.close_btn.clicked.connect(self.close)
        self._header_layout.addWidget(self.close_btn)

        self._root_layout.addWidget(self.header_frame)

        # --- Status / Alert Banner (for errors, offline notice, missing API key, or budget exhaustion) ---
        self.banner_frame = QFrame()
        self.banner_frame.setObjectName("bannerFrame")
        banner_layout = QHBoxLayout(self.banner_frame)
        banner_layout.setContentsMargins(10, 6, 10, 6)
        banner_layout.setSpacing(8)

        self.banner_label = QLabel("")
        self.banner_label.setWordWrap(True)
        banner_layout.addWidget(self.banner_label, 1)

        self.banner_action_btn = QPushButton(tr("footer.free_api_btn", lang=lang))
        self.banner_action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.banner_action_btn.clicked.connect(self._on_switch_to_free_provider)
        self.banner_action_btn.hide()
        banner_layout.addWidget(self.banner_action_btn)

        self.banner_frame.hide()
        self._root_layout.addWidget(self.banner_frame)

        # --- Adaptive Content Area ---
        self.stack = QStackedWidget()
        self.stack.setMouseTracking(True)

        # Responsive Primary Card Grid + Optional Secondary Mini Cards
        self.expanded_page = QWidget()
        self.expanded_page.setMouseTracking(True)
        self._exp_layout = QVBoxLayout(self.expanded_page)
        self._exp_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._exp_layout.setContentsMargins(0, 0, 0, 0)
        self._exp_layout.setSpacing(10)

        self.grid_layout = QGridLayout()
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setHorizontalSpacing(10)
        self.grid_layout.setVerticalSpacing(10)

        for idx, spec in enumerate(PRIMARY_INSTRUMENTS):
            card = PrimaryPriceCard(
                spec,
                theme=self.settings_manager.settings.theme,
                lang=lang,
            )
            card.setMouseTracking(True)
            row = idx // 2
            col = idx % 2
            self.grid_layout.addWidget(card, row, col)
            self.primary_cards[spec.symbol] = card

        self.grid_layout.setColumnStretch(0, 1)
        self.grid_layout.setColumnStretch(1, 1)
        self.grid_layout.setRowStretch(0, 1)
        self.grid_layout.setRowStretch(1, 1)

        self._exp_layout.addLayout(self.grid_layout, 1)

        # Secondary Mini-Cards Row (EUR, GBP, Sekkeh, Nim) matching preferred UI.png
        self.secondary_container = QWidget()
        sec_layout = QHBoxLayout(self.secondary_container)
        sec_layout.setContentsMargins(0, 0, 0, 0)
        sec_layout.setSpacing(8)
        for spec in SECONDARY_INSTRUMENTS:
            mini = SecondaryMiniCard(
                spec,
                theme=self.settings_manager.settings.theme,
                lang=lang,
            )
            sec_layout.addWidget(mini, 1)
            self.secondary_cards[spec.symbol] = mini

        self._exp_layout.addWidget(self.secondary_container)
        self.stack.addWidget(self.expanded_page)

        # Keep compact_page / compact_rows instantiated in memory for backward compatibility
        self.compact_page = QWidget()
        comp_layout = QVBoxLayout(self.compact_page)
        comp_layout.setContentsMargins(2, 2, 2, 2)
        comp_layout.setSpacing(2)
        for spec in PRIMARY_INSTRUMENTS:
            crow = CompactPriceRow(
                spec,
                theme=self.settings_manager.settings.theme,
                lang=lang,
            )
            comp_layout.addWidget(crow)
            self.compact_rows[spec.symbol] = crow
        comp_layout.addStretch(1)

        self._root_layout.addWidget(self.stack, 1)

        # --- Footer Bar ---
        self.footer_frame = QFrame()
        self.footer_frame.setObjectName("footerFrame")
        self._footer_layout = QHBoxLayout(self.footer_frame)
        self._footer_layout.setContentsMargins(2, 4, 2, 0)
        self._footer_layout.setSpacing(6)

        self.clock_icon_label = QLabel()
        self.clock_icon_label.setFixedSize(15, 15)
        self._footer_layout.addWidget(self.clock_icon_label)

        self.last_update_label = QLabel(tr("footer.last_update_none", lang=lang))
        self._footer_layout.addWidget(self.last_update_label)

        self._footer_layout.addStretch(1)

        self.budget_pill_label = QLabel("")
        self.budget_pill_label.setToolTip(
            tr("footer.budget_pill_tooltip", lang=lang, limit=MONTHLY_HARD_LIMIT)
        )
        self._footer_layout.addWidget(self.budget_pill_label)

        self.chart_toggle_btn = QPushButton(tr("footer.chart_toggle_btn", lang=lang))
        self.chart_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chart_toggle_btn.setCheckable(True)
        self.chart_toggle_btn.clicked.connect(self._on_toggle_chart_clicked)
        self._footer_layout.addWidget(self.chart_toggle_btn)

        self.size_grip = QSizeGrip(self.footer_frame)
        self.size_grip.setFixedSize(14, 14)
        self.size_grip.installEventFilter(self)
        grip_align = Qt.AlignmentFlag.AlignBottom | (
            Qt.AlignmentFlag.AlignLeft if is_rtl(lang) else Qt.AlignmentFlag.AlignRight
        )
        self._footer_layout.addWidget(self.size_grip, 0, grip_align)

        self._root_layout.addWidget(self.footer_frame)

    def _create_header_button(self, icon_name: str, tooltip: str) -> QToolButton:
        btn = QToolButton(self)
        btn.setFixedSize(30, 30)
        btn.setIconSize(QSize(17, 17))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tooltip)
        btn.setProperty("icon_name", icon_name)
        return btn

    def _setup_tray_icon(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = None
            return

        lang = self.settings_manager.settings.language
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(load_svg_icon("gold-coin.svg", size=32))
        self.tray_icon.setToolTip(tr("app.full_title", lang=lang))

        self.tray_menu = QMenu(self)
        self.tray_menu.setLayoutDirection(get_layout_direction(lang))

        self.tray_show_action = QAction(tr("menu.tray_show_hide", lang=lang), self)
        self.tray_show_action.triggered.connect(self._toggle_visibility_from_tray)
        self.tray_menu.addAction(self.tray_show_action)

        self.tray_refresh_action = QAction(tr("menu.refresh", lang=lang), self)
        self.tray_refresh_action.triggered.connect(self.on_manual_refresh_clicked)
        self.tray_menu.addAction(self.tray_refresh_action)

        self.tray_settings_action = QAction(tr("menu.settings", lang=lang), self)
        self.tray_settings_action.triggered.connect(self.open_settings_dialog)
        self.tray_menu.addAction(self.tray_settings_action)

        self.tray_lang_action = QAction(tr("menu.switch_language", lang=lang), self)
        self.tray_lang_action.triggered.connect(self.toggle_language)
        self.tray_menu.addAction(self.tray_lang_action)

        self.tray_startup_action = QAction(tr("menu.tray_startup", lang=lang), self)
        self.tray_startup_action.setCheckable(True)
        self.tray_startup_action.setChecked(self.settings_manager.settings.start_with_windows)
        self.tray_startup_action.triggered.connect(self._on_tray_startup_toggled)
        self.tray_menu.addAction(self.tray_startup_action)

        self.tray_menu.addSeparator()

        self.tray_exit_action = QAction(tr("menu.tray_exit", lang=lang), self)
        self.tray_exit_action.triggered.connect(self.quit_application)
        self.tray_menu.addAction(self.tray_exit_action)

        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_startup_toggled(self, checked: bool) -> None:
        """Toggle Windows sign-in startup directly from the system tray menu."""
        self.settings_manager.update(start_with_windows=bool(checked))
        SettingsManager.apply_windows_startup(bool(checked))

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_visibility_from_tray()

    def _toggle_visibility_from_tray(self) -> None:
        """Restore or hide the widget without issuing any API request or stealing focus."""
        if self.isVisible() and not self.isMinimized():
            self._explicitly_hidden = True
            self.hide()
        else:
            self._explicitly_hidden = False
            self.show_gadget_without_focus()

    def show_gadget_without_focus(self) -> None:
        """Show and raise the desktop gadget above normal windows without stealing active focus."""
        self._explicitly_hidden = False
        s = self.settings_manager.settings
        target_x = s.window_x if s.window_x is not None else self.x()
        target_y = s.window_y if s.window_y is not None else self.y()
        target_w = s.window_width if s.window_width else self.width()
        target_h = s.window_height if s.window_height else self.height()

        prev_restoring = self._restoring_geometry
        self._restoring_geometry = True
        try:
            self._apply_responsive_layout(target_w, target_h, force=True)
            if self.width() != target_w or self.height() != target_h:
                self.resize(target_w, target_h)
            if self.x() != target_x or self.y() != target_y:
                self.move(target_x, target_y)
            self.clamp_to_visible_desktop()
            clamped_pos = self.pos()
            clamped_size = self.size()
            self.showNormal()
            self._apply_native_gadget_behavior()
            try:
                raise_gadget_without_focus(
                    int(self.winId()),
                    always_on_top=self.settings_manager.settings.always_on_top or self._keep_above_show_desktop,
                )
            except Exception:
                pass
            if self.size() != clamped_size:
                self.resize(clamped_size)
            if self.pos() != clamped_pos:
                self.move(clamped_pos)
            self._last_valid_size = clamped_size
            self._last_valid_pos = clamped_pos
        finally:
            self._restoring_geometry = prev_restoring
        self._sync_geometry_in_memory()

    def on_second_instance_activated(self) -> None:
        """Handle second-launch notification: show existing gadget without calling API."""
        self.show_gadget_without_focus()
        lang = self.settings_manager.settings.language
        if getattr(self, "tray_icon", None) is not None and self.tray_icon.isVisible():
            try:
                self.tray_icon.showMessage(
                    tr("app.name", lang=lang),
                    tr("app.already_running", lang=lang),
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )
            except Exception:
                pass

    # --- Multi-Monitor & Resolution Clamping ---

    def _connect_screen_change_signals(self) -> None:
        app = QGuiApplication.instance()
        if app is None:
            return
        try:
            app.screenAdded.connect(self._on_screens_changed)  # type: ignore[attr-defined]
            app.screenRemoved.connect(self._on_screens_changed)  # type: ignore[attr-defined]
            app.primaryScreenChanged.connect(self._on_screens_changed)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _connect_app_shutdown_signals(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        try:
            app.aboutToQuit.connect(self._persist_geometry)
            app.commitDataRequest.connect(lambda _mgr: self._persist_geometry())  # type: ignore[attr-defined]
        except Exception:
            pass

    def _on_screens_changed(self, *_args: object) -> None:
        prev_restoring = self._restoring_geometry
        self._restoring_geometry = True
        try:
            self.clamp_to_visible_desktop()
        finally:
            self._restoring_geometry = prev_restoring
        self._persist_geometry()

    def clamp_to_visible_desktop(self) -> None:
        """Ensure the widget's geometry lies within an active monitor's available desktop area."""
        screens = QGuiApplication.screens()
        if not screens:
            return

        w = max(self.MIN_WINDOW_SIZE.width(), min(self.MAX_WINDOW_SIZE.width(), self.width()))
        h = max(self.MIN_WINDOW_SIZE.height(), min(self.MAX_WINDOW_SIZE.height(), self.height()))
        cur_rect = QRect(self.x(), self.y(), w, h)
        center = cur_rect.center()

        # Find the screen containing the widget's center, or the screen with the largest intersection
        target_screen = None
        for scr in screens:
            if scr.availableGeometry().contains(center):
                target_screen = scr
                break

        if target_screen is None:
            best_area = 0
            for scr in screens:
                inter = scr.availableGeometry().intersected(cur_rect)
                area = max(0, inter.width()) * max(0, inter.height())
                if area > best_area:
                    best_area = area
                    target_screen = scr

        if target_screen is None:
            target_screen = QGuiApplication.primaryScreen() or screens[0]

        avail = target_screen.availableGeometry()
        # Scale proportionally if saved dimensions exceed available screen area; otherwise preserve exact size
        max_avail_w = max(self.MIN_WINDOW_SIZE.width(), avail.width())
        max_avail_h = max(self.MIN_WINDOW_SIZE.height(), avail.height())
        if w > max_avail_w or h > max_avail_h:
            scale = min(max_avail_w / float(w), max_avail_h / float(h))
            clamped_w = max(
                self.MIN_WINDOW_SIZE.width(),
                min(max_avail_w, int(round(w * scale))),
            )
            clamped_h = max(
                self.MIN_WINDOW_SIZE.height(),
                min(max_avail_h, int(round(h * scale))),
            )
        else:
            clamped_w = w
            clamped_h = h

        if clamped_w != self.width() or clamped_h != self.height():
            self.resize(clamped_w, clamped_h)

        min_x = avail.left()
        max_x = max(avail.left(), avail.right() - clamped_w + 1)
        min_y = avail.top()
        max_y = max(avail.top(), avail.bottom() - clamped_h + 1)

        clamped_x = max(min_x, min(self.x(), max_x))
        clamped_y = max(min_y, min(self.y(), max_y))
        if clamped_x != self.x() or clamped_y != self.y():
            self.move(clamped_x, clamped_y)

    # --- Responsive Layout Engine & Settings Application (Zero API Calls) ---

    def _apply_responsive_layout(self, width: int, height: int, force: bool = False) -> None:
        """Adapt layout, cards, graphs, and controls smoothly based on current window size."""
        mode = self.compute_responsive_mode(width, height)
        grid_cols = 1 if width < self.SINGLE_COL_MAX_WIDTH else 2
        single_row = (mode == "ultra_compact") and (grid_cols == 1 or height < 235)
        s = self.settings_manager.settings

        # Reflow grid between 2x2 and 1x4 only when column count changes
        if force or grid_cols != self._current_grid_cols:
            self._current_grid_cols = grid_cols
            cards_list = [self.primary_cards[spec.symbol] for spec in PRIMARY_INSTRUMENTS]
            for card in cards_list:
                self.grid_layout.removeWidget(card)

            if grid_cols == 1:
                for idx, card in enumerate(cards_list):
                    self.grid_layout.addWidget(card, idx, 0)
                self.grid_layout.setColumnStretch(0, 1)
                self.grid_layout.setColumnStretch(1, 0)
                for r in range(4):
                    self.grid_layout.setRowStretch(r, 1)
            else:
                for idx, card in enumerate(cards_list):
                    self.grid_layout.addWidget(card, idx // 2, idx % 2)
                self.grid_layout.setColumnStretch(0, 1)
                self.grid_layout.setColumnStretch(1, 1)
                self.grid_layout.setRowStretch(0, 1)
                self.grid_layout.setRowStretch(1, 1)
                self.grid_layout.setRowStretch(2, 0)
                self.grid_layout.setRowStretch(3, 0)

        self._responsive_mode = mode
        pal = get_palette(s.theme)
        lang = s.language

        if mode == "ultra_compact":
            self._root_layout.setContentsMargins(6, 5, 6, 5)
            self._root_layout.setSpacing(4)
            self._header_layout.setContentsMargins(2, 0, 2, 2)
            self._header_layout.setSpacing(4)
            self._exp_layout.setSpacing(4)
            self.grid_layout.setHorizontalSpacing(4)
            self.grid_layout.setVerticalSpacing(4)

            self.app_icon_label.setFixedSize(18, 18)
            self.app_icon_label.setPixmap(load_svg_pixmap("gold-coin.svg", size=16))
            self.title_label.setText(tr("app.name", lang=lang))
            self.title_label.setStyleSheet(
                f"color: {pal.text_primary}; font-size: 12px; font-weight: 800;"
            )
            self.state_badge.setVisible(False)

            btn_sz, icon_sz = 22, 13
            self.refresh_btn.setVisible(width >= 245)
            self.mode_btn.setVisible(False)
            self.pin_btn.setVisible(False)
            self.settings_btn.setVisible(False)
            self.overflow_btn.setVisible(True)
            self.close_btn.setVisible(True)

            self.secondary_container.setVisible(False)
            self.banner_frame.setVisible(False)
            self.footer_frame.setVisible(False)
            self.chart_toggle_btn.setVisible(False)

        elif mode == "compact":
            self._root_layout.setContentsMargins(10, 8, 10, 8)
            self._root_layout.setSpacing(6)
            self._header_layout.setContentsMargins(2, 0, 2, 2)
            self._header_layout.setSpacing(5)
            self._exp_layout.setSpacing(6)
            self.grid_layout.setHorizontalSpacing(6)
            self.grid_layout.setVerticalSpacing(6)

            self.app_icon_label.setFixedSize(22, 22)
            self.app_icon_label.setPixmap(load_svg_pixmap("gold-coin.svg", size=20))
            self.title_label.setText(
                tr("app.name", lang=lang) if width < 390 else tr("app.header_title", lang=lang)
            )
            self.title_label.setStyleSheet(
                f"color: {pal.text_primary}; font-size: 13px; font-weight: 800;"
            )
            self.state_badge.setVisible(width >= 415)

            btn_sz, icon_sz = 26, 15
            self.refresh_btn.setVisible(True)
            self.mode_btn.setVisible(width >= 390)
            self.pin_btn.setVisible(False)
            self.settings_btn.setVisible(True)
            self.overflow_btn.setVisible(True)
            self.close_btn.setVisible(True)

            self.secondary_container.setVisible(False)
            self.banner_frame.setVisible(self._has_active_banner and height >= 315)
            self.footer_frame.setVisible(height >= 285)
            self.budget_pill_label.setVisible(False)
            self.chart_toggle_btn.setVisible(False)

        else:  # "expanded"
            self._root_layout.setContentsMargins(16, 14, 16, 12)
            self._root_layout.setSpacing(10)
            self._header_layout.setContentsMargins(2, 0, 2, 4)
            self._header_layout.setSpacing(8)
            self._exp_layout.setSpacing(10)
            self.grid_layout.setHorizontalSpacing(10)
            self.grid_layout.setVerticalSpacing(10)

            self.app_icon_label.setFixedSize(28, 28)
            self.app_icon_label.setPixmap(load_svg_pixmap("gold-coin.svg", size=26))
            self.title_label.setText(tr("app.header_title", lang=lang))
            self.title_label.setStyleSheet(
                f"color: {pal.text_primary}; font-size: 16px; font-weight: 800;"
            )
            self.state_badge.setVisible(True)

            btn_sz, icon_sz = 30, 17
            self.refresh_btn.setVisible(True)
            self.mode_btn.setVisible(True)
            self.pin_btn.setVisible(True)
            self.settings_btn.setVisible(True)
            self.overflow_btn.setVisible(False)
            self.close_btn.setVisible(True)

            self.secondary_container.setVisible(s.show_secondary_cards and height >= 425)
            self.banner_frame.setVisible(self._has_active_banner)
            self.footer_frame.setVisible(True)
            self.budget_pill_label.setVisible(width >= 480)
            self.chart_toggle_btn.setVisible(True)

        for btn in (
            self.refresh_btn,
            self.mode_btn,
            self.pin_btn,
            self.settings_btn,
            self.overflow_btn,
            self.close_btn,
        ):
            btn.setFixedSize(btn_sz, btn_sz)
            btn.setIconSize(QSize(icon_sz, icon_sz))

        for card in self.primary_cards.values():
            card.set_responsive_mode(mode, single_row=single_row)

    def set_language(self, lang: str, sync_numerals: Optional[bool] = None) -> None:
        """Switch the active UI language ('fa' or 'en') immediately without restarting or calling the API."""
        norm_lang = "en" if str(lang).lower().startswith("en") else "fa"
        if sync_numerals is None:
            use_persian = norm_lang == "fa"
        else:
            use_persian = bool(sync_numerals)
        set_current_language(norm_lang)
        self.settings_manager.update(language=norm_lang, use_persian_numerals=use_persian)
        self.retranslate_ui()

    def toggle_language(self) -> None:
        """Toggle between Persian ('fa') and English ('en') immediately without calling the API."""
        new_lang = "en" if self.settings_manager.settings.language == "fa" else "fa"
        self.set_language(new_lang, sync_numerals=(new_lang == "fa"))

    def retranslate_ui(self) -> None:
        """Apply active language translations and RTL/LTR direction across the entire widget."""
        s = self.settings_manager.settings
        lang = s.language
        use_fa = s.use_persian_numerals
        set_current_language(lang)
        direction = get_layout_direction(lang)

        app = QApplication.instance()
        if app is not None and isinstance(app, QApplication):
            app.setLayoutDirection(direction)
            app.setApplicationDisplayName(tr("app.name", lang=lang))

        self.setWindowTitle(tr("app.full_title", lang=lang))
        self.setLayoutDirection(direction)
        self.central_container.setLayoutDirection(direction)
        self.header_frame.setLayoutDirection(direction)
        self.banner_frame.setLayoutDirection(direction)
        self.expanded_page.setLayoutDirection(direction)
        self.secondary_container.setLayoutDirection(direction)
        self.compact_page.setLayoutDirection(direction)
        self.footer_frame.setLayoutDirection(direction)

        self.title_label.setAlignment(
            get_leading_alignment(lang) | Qt.AlignmentFlag.AlignVCenter
        )

        # Header button tooltips
        self.refresh_btn.setToolTip(tr("header.refresh_tooltip", lang=lang))
        self.mode_btn.setToolTip(tr("header.mode_tooltip", lang=lang))
        self.pin_btn.setToolTip(tr("header.pin_tooltip", lang=lang))
        self.settings_btn.setToolTip(tr("header.settings_tooltip", lang=lang))
        self.overflow_btn.setToolTip(tr("header.overflow_tooltip", lang=lang))
        self.close_btn.setToolTip(tr("header.close_tooltip", lang=lang))

        # Overflow menu
        self.overflow_menu.setLayoutDirection(direction)
        self.overflow_refresh_action.setText(tr("menu.refresh", lang=lang))
        self.overflow_settings_action.setText(tr("menu.settings", lang=lang))
        self.overflow_mode_action.setText(tr("menu.toggle_mode", lang=lang))
        self.overflow_pin_action.setText(tr("menu.always_on_top", lang=lang))
        self.overflow_chart_action.setText(tr("menu.show_chart", lang=lang))
        self.overflow_lang_action.setText(tr("menu.switch_language", lang=lang))

        # System tray menu & tooltip
        if getattr(self, "tray_icon", None) is not None:
            self.tray_icon.setToolTip(tr("app.full_title", lang=lang))
            if hasattr(self, "tray_menu"):
                self.tray_menu.setLayoutDirection(direction)
            self.tray_show_action.setText(tr("menu.tray_show_hide", lang=lang))
            self.tray_refresh_action.setText(tr("menu.refresh", lang=lang))
            self.tray_settings_action.setText(tr("menu.settings", lang=lang))
            if hasattr(self, "tray_lang_action"):
                self.tray_lang_action.setText(tr("menu.switch_language", lang=lang))
            self.tray_startup_action.setText(tr("menu.tray_startup", lang=lang))
            self.tray_exit_action.setText(tr("menu.tray_exit", lang=lang))

        # Banner & footer controls
        self.banner_action_btn.setText(tr("footer.free_api_btn", lang=lang))
        self.budget_pill_label.setToolTip(
            localize_digits(
                tr("footer.budget_pill_tooltip", lang=lang, limit=MONTHLY_HARD_LIMIT),
                use_persian=use_fa,
            )
        )
        self.chart_toggle_btn.setText(
            localize_digits(tr("footer.chart_toggle_btn", lang=lang), use_persian=use_fa)
        )

        grip_align = Qt.AlignmentFlag.AlignBottom | (
            Qt.AlignmentFlag.AlignLeft if is_rtl(lang) else Qt.AlignmentFlag.AlignRight
        )
        self._footer_layout.setAlignment(self.size_grip, grip_align)

        # Update cards
        for card in self.primary_cards.values():
            card.set_language(lang, use_persian=use_fa)
        for mini in self.secondary_cards.values():
            mini.set_language(lang, use_persian=use_fa)
        for crow in self.compact_rows.values():
            crow.set_language(lang, use_persian=use_fa)

        self._style_state_badge()
        self._apply_responsive_layout(self.width(), self.height(), force=False)
        self._refresh_cards_from_memory()
        self._refresh_active_banner()
        self._update_footer_and_freshness()

    def _apply_all_settings(self, s: AppSettings, initial: bool = False) -> None:
        prev_restoring = self._restoring_geometry
        self._restoring_geometry = True
        try:
            self.setWindowOpacity(s.opacity)
            self.pin_btn.setChecked(s.always_on_top)
            self.overflow_pin_action.setChecked(s.always_on_top)
            self.chart_toggle_btn.setChecked(s.show_chart)
            self.overflow_chart_action.setChecked(s.show_chart)
            if hasattr(self, "tray_startup_action"):
                self.tray_startup_action.setChecked(s.start_with_windows)

            for card in self.primary_cards.values():
                card.set_chart_visible(s.show_chart)

            self.stack.setCurrentWidget(self.expanded_page)

            if s.window_width and s.window_height:
                target_w = max(
                    self.MIN_WINDOW_SIZE.width(),
                    min(self.MAX_WINDOW_SIZE.width(), s.window_width),
                )
                target_h = max(
                    self.MIN_WINDOW_SIZE.height(),
                    min(self.MAX_WINDOW_SIZE.height(), s.window_height),
                )
            else:
                if s.display_mode == "ultra_compact":
                    target_w, target_h = self.ULTRA_COMPACT_SIZE.width(), self.ULTRA_COMPACT_SIZE.height()
                elif s.display_mode == "compact":
                    target_w, target_h = self.COMPACT_SIZE.width(), self.COMPACT_SIZE.height()
                else:
                    target_w = 540
                    target_h = 480 if s.show_secondary_cards else 390

            # Adapt card minimum sizes before resizing so layout constraints never resist compact sizes
            self._apply_responsive_layout(target_w, target_h, force=True)
            if self.width() != target_w or self.height() != target_h:
                self.resize(target_w, target_h)

            if s.window_x is not None and s.window_y is not None:
                if self.x() != s.window_x or self.y() != s.window_y:
                    self.move(s.window_x, s.window_y)
            elif initial:
                # Default gadget placement: top-right corner of primary screen's available desktop
                primary = QGuiApplication.primaryScreen()
                if primary is not None:
                    avail = primary.availableGeometry()
                    default_x = max(avail.left(), avail.right() - self.width() - 24)
                    default_y = avail.top() + 24
                    self.move(default_x, default_y)

            self.clamp_to_visible_desktop()
            clamped_size = self.size()
            clamped_pos = self.pos()
            self.retranslate_ui()
            self.apply_theme(s.theme)
            self._apply_responsive_layout(clamped_size.width(), clamped_size.height(), force=True)
            if self.size() != clamped_size:
                self.resize(clamped_size)
            if self.pos() != clamped_pos:
                self.move(clamped_pos)
            self._last_valid_size = self.size()
            self._last_valid_pos = self.pos()
            self._refresh_cards_from_memory()
            self._update_footer_and_freshness()
        finally:
            self._restoring_geometry = prev_restoring
        self._sync_geometry_in_memory()

    def apply_theme(self, theme: str) -> None:
        pal: ThemePalette = get_palette(theme)

        self.central_container.setStyleSheet(
            f"""
            QFrame#centralContainer {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {pal.window_bg_start}, stop:1 {pal.window_bg_end});
                border: 1.5px solid {pal.window_border};
                border-radius: 18px;
            }}
            QLabel {{
                font-family: {FONT_FAMILY_STACK};
                background: transparent;
            }}
            """
        )

        for btn in (
            self.refresh_btn,
            self.mode_btn,
            self.pin_btn,
            self.settings_btn,
            self.overflow_btn,
            self.close_btn,
        ):
            icon_name = btn.property("icon_name")
            tint = pal.accent_gold if (btn is self.pin_btn and btn.isChecked()) else pal.icon_color
            btn.setIcon(load_svg_icon(icon_name, size=17, color=tint))
            btn.setStyleSheet(
                f"""
                QToolButton {{
                    background-color: {pal.card_bg_start};
                    border: 1px solid {pal.card_border};
                    border-radius: 8px;
                }}
                QToolButton::menu-indicator {{
                    image: none;
                    width: 0px;
                }}
                QToolButton:hover {{
                    border: 1px solid {pal.card_hover_border};
                    background-color: {pal.badge_bg};
                }}
                QToolButton:checked {{
                    border: 1px solid {pal.accent_gold};
                }}
                QToolButton:disabled {{
                    opacity: 0.5;
                }}
                """
            )

        self.clock_icon_label.setPixmap(
            load_svg_pixmap("clock.svg", size=14, color=pal.text_secondary)
        )
        self.last_update_label.setStyleSheet(
            f"color: {pal.text_secondary}; font-size: 11px; font-weight: 500;"
        )
        self.budget_pill_label.setStyleSheet(
            f"color: {pal.text_secondary}; background-color: {pal.badge_bg}; "
            f"border-radius: 7px; padding: 2px 8px; font-size: 10px; font-weight: 600;"
        )
        self.chart_toggle_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {pal.card_bg_start};
                color: {pal.text_secondary};
                border: 1px solid {pal.card_border};
                border-radius: 8px;
                padding: 3px 10px;
                font-size: 11px;
                font-weight: 600;
                font-family: {FONT_FAMILY_STACK};
            }}
            QPushButton:hover {{
                color: {pal.text_primary};
                border: 1px solid {pal.card_hover_border};
            }}
            QPushButton:checked {{
                color: {pal.accent_gold if theme == 'dark' else pal.accent_blue};
                border: 1px solid {pal.accent_gold if theme == 'dark' else pal.accent_blue};
            }}
            """
        )
        self.banner_action_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {pal.card_bg_start};
                color: {pal.text_primary};
                border: 1px solid {pal.card_hover_border};
                border-radius: 6px;
                padding: 3px 8px;
                font-size: 11px;
                font-weight: 700;
                font-family: {FONT_FAMILY_STACK};
            }}
            """
        )

        for card in self.primary_cards.values():
            card.apply_theme(theme)
        for mini in self.secondary_cards.values():
            mini.apply_theme(theme)
        for crow in self.compact_rows.values():
            crow.apply_theme(theme)

        self._style_state_badge()
        self._apply_responsive_layout(self.width(), self.height(), force=False)

    def _style_state_badge(self) -> None:
        s = self.settings_manager.settings
        pal = get_palette(s.theme)
        lang = s.language
        if self._current_state == "live":
            bg, fg, txt = pal.badge_bg, pal.accent_green, tr("status.live", lang=lang)
        elif self._current_state == "loading":
            bg, fg, txt = pal.badge_bg, pal.accent_blue, tr("status.loading", lang=lang)
        elif self._current_state == "cached":
            bg, fg, txt = pal.banner_warn_bg, pal.banner_warn_text, tr("status.cached", lang=lang)
        elif self._current_state == "exhausted":
            bg, fg, txt = pal.banner_err_bg, pal.banner_err_text, tr("status.exhausted", lang=lang)
        elif self._current_state == "missing_key":
            bg, fg, txt = pal.banner_warn_bg, pal.banner_warn_text, tr("status.missing_key", lang=lang)
        elif self._current_state == "error":
            bg, fg, txt = pal.banner_err_bg, pal.banner_err_text, tr("status.error", lang=lang)
        else:
            bg, fg, txt = pal.badge_bg, pal.text_secondary, tr("status.idle", lang=lang)

        self.state_badge.setText(txt)
        self.state_badge.setStyleSheet(
            f"background-color: {bg}; color: {fg}; border-radius: 7px; "
            f"padding: 2px 8px; font-size: 10px; font-weight: 700;"
        )

    def _show_banner(
        self,
        message: str = "",
        level: str = "warn",
        show_free_api_button: bool = False,
        *,
        tr_key: Optional[str] = None,
        tr_kwargs: Optional[dict] = None,
        message_fa: Optional[str] = None,
        message_en: Optional[str] = None,
    ) -> None:
        self._has_active_banner = True
        self._banner_state = {
            "message": message,
            "level": level,
            "show_free_api_button": show_free_api_button,
            "tr_key": tr_key,
            "tr_kwargs": tr_kwargs or {},
            "message_fa": message_fa,
            "message_en": message_en,
        }
        self._render_banner_from_state()

    def _render_banner_from_state(self) -> None:
        if not self._has_active_banner or not self._banner_state:
            return
        s = self.settings_manager.settings
        lang = s.language
        use_fa = s.use_persian_numerals
        pal = get_palette(s.theme)
        level = self._banner_state.get("level", "warn")
        show_free_api_button = bool(self._banner_state.get("show_free_api_button", False))

        if level == "error":
            bg, border, fg = pal.banner_err_bg, pal.banner_err_border, pal.banner_err_text
        else:
            bg, border, fg = pal.banner_warn_bg, pal.banner_warn_border, pal.banner_warn_text

        self.banner_frame.setStyleSheet(
            f"""
            QFrame#bannerFrame {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 10px;
            }}
            """
        )
        self.banner_label.setStyleSheet(f"color: {fg}; font-size: 11px; font-weight: 600;")
        self.banner_label.setAlignment(
            get_leading_alignment(lang) | Qt.AlignmentFlag.AlignVCenter
        )

        tr_key = self._banner_state.get("tr_key")
        tr_kwargs = dict(self._banner_state.get("tr_kwargs") or {})
        if tr_key == "banners.showing_cached":
            age_sec = self.cache_store.get_cache_age_seconds()
            tr_kwargs["age"] = format_cache_age(age_sec, use_persian=use_fa, lang=lang)
            text = tr(tr_key, lang=lang, **tr_kwargs)
        elif tr_key == "banners.partial_missing_symbols":
            missing_syms = tr_kwargs.get("missing_symbols", [])
            sep = "، " if lang == "fa" else ", "
            names = [
                spec.get_name(lang)
                for spec in PRIMARY_INSTRUMENTS
                if spec.symbol in missing_syms
            ]
            text = tr(tr_key, lang=lang, symbols=sep.join(names))
        elif tr_key:
            text = tr(tr_key, lang=lang, **tr_kwargs)
        elif lang == "en" and self._banner_state.get("message_en"):
            text = str(self._banner_state["message_en"])
        elif lang == "fa" and self._banner_state.get("message_fa"):
            text = str(self._banner_state["message_fa"])
        else:
            text = str(self._banner_state.get("message", ""))

        self.banner_label.setText(localize_digits(text, use_persian=use_fa))
        self.banner_action_btn.setText(tr("footer.free_api_btn", lang=lang))
        self.banner_action_btn.setVisible(show_free_api_button)
        show_in_mode = self._responsive_mode == "expanded" or (
            self._responsive_mode == "compact" and self.height() >= 315
        )
        self.banner_frame.setVisible(show_in_mode)

    def _refresh_active_banner(self) -> None:
        if self._has_active_banner and self._banner_state:
            self._render_banner_from_state()

    def _hide_banner(self) -> None:
        self._has_active_banner = False
        self._banner_state = None
        self.banner_frame.hide()

    # --- Data Loading & Display (Preserving Cache) ---

    def _load_cached_data_into_ui(self) -> None:
        """Immediately populate cards from local cache on startup without any API request."""
        quotes = self.api_client.get_cached_quotes(include_secondary=True)
        st = self.budget_manager.get_status()
        s = self.settings_manager.settings

        if quotes:
            self._apply_quotes_to_cards(quotes)
            self._current_state = "cached"
        else:
            self._apply_quotes_to_cards({})
            self._current_state = "idle"

        if st.is_exhausted:
            self._current_state = "exhausted"
            self._show_banner(
                tr("banners.budget_exhausted", lang=s.language),
                level="error",
                tr_key="banners.budget_exhausted",
            )
        elif s.api_provider == "navasan" and not s.get_effective_api_key():
            self._current_state = "missing_key"
            self._show_banner(
                tr("banners.missing_api_key", lang=s.language),
                level="warn",
                show_free_api_button=True,
                tr_key="banners.missing_api_key",
            )
        elif quotes:
            age_sec = self.cache_store.get_cache_age_seconds()
            age_txt = format_cache_age(age_sec, use_persian=s.use_persian_numerals, lang=s.language)
            self._show_banner(
                tr("banners.showing_cached", lang=s.language, age=age_txt),
                level="warn",
                tr_key="banners.showing_cached",
            )
        else:
            self._hide_banner()

        self._style_state_badge()
        self._update_footer_and_freshness()

    def _refresh_cards_from_memory(self) -> None:
        quotes = self.api_client.get_cached_quotes(include_secondary=True)
        if quotes:
            self._apply_quotes_to_cards(quotes)
        else:
            self._apply_quotes_to_cards({})

    def _apply_quotes_to_cards(self, quotes: Dict[str, InstrumentQuote]) -> None:
        s = self.settings_manager.settings
        use_fa = s.use_persian_numerals
        lang = s.language
        age_sec = self.cache_store.get_cache_age_seconds()
        freshness = (
            format_cache_age(age_sec, use_persian=use_fa, lang=lang)
            if age_sec is not None
            else ""
        )

        for sym, card in self.primary_cards.items():
            card.update_quote(quotes.get(sym), use_persian=use_fa, freshness_text=freshness)

        for sym, mini in self.secondary_cards.items():
            mini.update_quote(quotes.get(sym), use_persian=use_fa)

        for sym, crow in self.compact_rows.items():
            crow.update_quote(quotes.get(sym), use_persian=use_fa)

    def _update_footer_and_freshness(self) -> None:
        s = self.settings_manager.settings
        use_fa = s.use_persian_numerals
        lang = s.language
        st = self.budget_manager.get_status()

        last_ts = self.cache_store.get_last_fetch_timestamp()
        age_sec = self.cache_store.get_cache_age_seconds()
        if last_ts is not None:
            dt_str = format_localized_datetime(last_ts, lang=lang, use_persian=use_fa)
            age_str = format_cache_age(age_sec, use_persian=use_fa, lang=lang)
            self.last_update_label.setText(
                tr("footer.last_update", lang=lang, datetime=dt_str, age=age_str)
            )
            for card in self.primary_cards.values():
                card.freshness_label.setText(localize_digits(age_str, use_persian=use_fa))
        else:
            self.last_update_label.setText(tr("footer.last_update_none", lang=lang))

        budget_txt = tr(
            "footer.budget_pill",
            lang=lang,
            used=st.monthly_total_used,
            limit=MONTHLY_HARD_LIMIT,
        )
        self.budget_pill_label.setText(localize_digits(budget_txt, use_persian=use_fa))

        # Disable refresh button if monthly hard limit of 120 is reached or a request is in flight
        can_refresh = (not st.is_exhausted) and (not self.budget_manager.is_in_flight)
        self.refresh_btn.setEnabled(can_refresh)
        self.overflow_refresh_action.setEnabled(can_refresh)
        if hasattr(self, "tray_refresh_action"):
            self.tray_refresh_action.setEnabled(can_refresh)

    # --- Scheduling & Network Request Dispatch ---

    def _evaluate_startup_schedule(self) -> None:
        """Schedule next permitted refresh and fetch on startup ONLY if permitted by schedule & budget."""
        s = self.settings_manager.settings
        if not s.auto_refresh_enabled:
            self._schedule_timer.stop()
            return

        if s.api_provider == "navasan" and not s.get_effective_api_key():
            # Do not issue doomed requests without an API key
            self._schedule_timer.stop()
            return

        can_sched, _ = self.budget_manager.can_issue_scheduled_request(
            refresh_interval_hours=s.refresh_interval_hours
        )
        if can_sched:
            self._start_background_fetch(kind="scheduled")
        else:
            self._arm_next_schedule_timer()

    def _arm_next_schedule_timer(self) -> None:
        self._schedule_timer.stop()
        s = self.settings_manager.settings
        if not s.auto_refresh_enabled:
            return

        wait_sec = self.budget_manager.seconds_until_next_scheduled(
            refresh_interval_hours=s.refresh_interval_hours
        )
        if wait_sec is None:
            return

        # Clamp timer milliseconds to a safe positive integer (at least 5 seconds)
        delay_ms = max(5_000, min(int(wait_sec * 1000), 86_400_000))
        self._schedule_timer.start(delay_ms)

    def _on_scheduled_timer_fired(self) -> None:
        s = self.settings_manager.settings
        if not s.auto_refresh_enabled:
            return
        if s.api_provider == "navasan" and not s.get_effective_api_key():
            return
        can_sched, _ = self.budget_manager.can_issue_scheduled_request(
            refresh_interval_hours=s.refresh_interval_hours
        )
        if can_sched:
            self._start_background_fetch(kind="scheduled")
        else:
            self._arm_next_schedule_timer()

    def on_manual_refresh_clicked(self) -> None:
        """Handle manual Refresh button click — at most ONE API request per click, with budget confirmation."""
        if self.budget_manager.is_in_flight:
            return

        s = self.settings_manager.settings
        st = self.budget_manager.get_status()
        use_fa = s.use_persian_numerals
        lang = s.language

        if st.is_exhausted:
            self._current_state = "exhausted"
            self._style_state_badge()
            self._show_banner(
                tr("banners.budget_exhausted_short", lang=lang),
                level="error",
                tr_key="banners.budget_exhausted_short",
            )
            return

        if s.api_provider == "navasan" and not s.get_effective_api_key():
            self._current_state = "missing_key"
            self._style_state_badge()
            self._show_banner(
                tr("banners.missing_api_key_short", lang=lang),
                level="warn",
                show_free_api_button=True,
                tr_key="banners.missing_api_key_short",
            )
            return

        # Mandatory rule: Manual refresh must display the number of requests remaining in the local budget
        # and ask for confirmation if configured OR if it could exhaust the 30-request reserve.
        must_confirm = s.confirm_manual_refresh or st.is_in_reserve
        if must_confirm:
            reserve_warning = (
                tr("dialogs.reserve_warning", lang=lang)
                if st.is_in_reserve
                else ""
            )
            msg = localize_digits(
                tr(
                    "dialogs.manual_refresh_body",
                    lang=lang,
                    remaining=st.monthly_remaining,
                    limit=MONTHLY_HARD_LIMIT,
                    reserve_remaining=st.reserve_remaining,
                    reserve_limit=MONTHLY_RESERVE_BUDGET,
                    daily_used=st.daily_total_used,
                    reserve_warning=reserve_warning,
                ),
                use_persian=use_fa,
            )
            reply = QMessageBox.question(
                self,
                tr("dialogs.manual_refresh_title", lang=lang),
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._start_background_fetch(kind="manual")

    def _start_background_fetch(self, kind: str = "manual") -> None:
        if self.budget_manager.is_in_flight:
            return

        s = self.settings_manager.settings
        self._current_state = "loading"
        self._style_state_badge()
        self.refresh_btn.setEnabled(False)
        self.overflow_refresh_action.setEnabled(False)
        if hasattr(self, "tray_refresh_action"):
            self.tray_refresh_action.setEnabled(False)

        self._fetch_worker = MarketFetchWorker(
            client=self.api_client,
            api_key=s.get_effective_api_key(),
            kind=kind,
            provider=s.api_provider,
            refresh_interval_hours=s.refresh_interval_hours,
        )
        self._fetch_worker.finished_result.connect(self._on_fetch_completed)
        self._fetch_worker.start()

    def _on_fetch_completed(self, result: ApiFetchResult) -> None:
        if result.quotes:
            self._apply_quotes_to_cards(result.quotes)

        s = self.settings_manager.settings
        lang = s.language
        st = self.budget_manager.get_status()
        if result.success:
            self._current_state = "live"
            # Check if any required primary symbol was missing in the payload
            missing_syms = [
                spec.symbol
                for spec in PRIMARY_INSTRUMENTS
                if not result.quotes.get(spec.symbol) or not result.quotes[spec.symbol].available
            ]
            if missing_syms:
                sep = "، " if lang == "fa" else ", "
                missing_names = [
                    spec.get_name(lang)
                    for spec in PRIMARY_INSTRUMENTS
                    if spec.symbol in missing_syms
                ]
                self._show_banner(
                    tr("banners.partial_missing_symbols", lang=lang, symbols=sep.join(missing_names)),
                    level="warn",
                    tr_key="banners.partial_missing_symbols",
                    tr_kwargs={"missing_symbols": missing_syms},
                )
            else:
                self._hide_banner()
        else:
            msg_cur = result.get_message(lang)
            if st.is_exhausted or result.error_code == "budget_exhausted":
                self._current_state = "exhausted"
                self._show_banner(
                    msg_cur,
                    level="error",
                    message_fa=result.message_fa,
                    message_en=result.message_en,
                )
            elif result.error_code == "missing_api_key":
                self._current_state = "missing_key"
                self._show_banner(
                    msg_cur,
                    level="warn",
                    show_free_api_button=True,
                    message_fa=result.message_fa,
                    message_en=result.message_en,
                )
            else:
                self._current_state = "error" if not result.from_cache else "cached"
                self._show_banner(
                    msg_cur,
                    level="error",
                    message_fa=result.message_fa,
                    message_en=result.message_en,
                )

        if not self.power_manager.is_active:
            self._dirty_while_sleeping = True
        self._style_state_badge()
        self._update_footer_and_freshness()
        self._arm_next_schedule_timer()

    def _on_switch_to_free_provider(self) -> None:
        """Allow user to switch to the free Iran-Market API with one click when no Navasan key is set."""
        self.settings_manager.update(api_provider="free_iran_market")
        self._hide_banner()
        self._start_background_fetch(kind="manual")

    # --- UI Controls & Dialogs (Zero Automatic API Calls) ---

    def toggle_display_mode(self) -> None:
        """Toggle between Expanded and Compact widget modes without calling the API."""
        cur = self.settings_manager.settings.display_mode
        new_mode = "compact" if cur == "expanded" else "expanded"
        target_size = (
            self.COMPACT_SIZE
            if new_mode == "compact"
            else QSize(540, 480 if self.settings_manager.settings.show_secondary_cards else 390)
        )
        updated = self.settings_manager.update(
            display_mode=new_mode,
            window_width=target_size.width(),
            window_height=target_size.height(),
        )
        self._apply_all_settings(updated, initial=False)

    def _on_overflow_pin_toggled(self, checked: bool) -> None:
        self.pin_btn.setChecked(checked)
        self.toggle_always_on_top()

    def _on_overflow_chart_toggled(self, checked: bool) -> None:
        self.chart_toggle_btn.setChecked(checked)
        self._on_toggle_chart_clicked()

    def toggle_always_on_top(self) -> None:
        """Toggle Always-on-Top window flag without calling the API."""
        new_val = self.pin_btn.isChecked()
        self.overflow_pin_action.setChecked(new_val)
        self.settings_manager.update(always_on_top=new_val)
        was_visible = self.isVisible()
        saved_pos = self.pos()
        saved_size = self.size()
        prev_restoring = self._restoring_geometry
        self._restoring_geometry = True
        try:
            self._configure_window_flags()
            self.apply_theme(self.settings_manager.settings.theme)
            self.resize(saved_size)
            self.move(saved_pos)
            if was_visible:
                self.show()
                if self.size() != saved_size:
                    self.resize(saved_size)
                if self.pos() != saved_pos:
                    self.move(saved_pos)
                self._apply_native_gadget_behavior()
        finally:
            self._restoring_geometry = prev_restoring
        self._sync_geometry_in_memory()

    def _on_toggle_chart_clicked(self) -> None:
        """Toggle sparkline visibility on primary cards without calling the API."""
        show = self.chart_toggle_btn.isChecked()
        self.overflow_chart_action.setChecked(show)
        self.settings_manager.update(show_chart=show)
        for card in self.primary_cards.values():
            card.set_chart_visible(show)

    def open_settings_dialog(self) -> None:
        """Open the modal Settings window. Does NOT trigger any API call."""
        self._persist_geometry()
        dlg = SettingsDialog(
            settings_manager=self.settings_manager,
            budget_manager=self.budget_manager,
            cache_store=self.cache_store,
            api_client=self.api_client,
            update_service=self.update_service,
            parent=self,
        )
        dlg.settings_saved.connect(self._on_settings_saved)
        dlg.cache_cleared.connect(self._load_cached_data_into_ui)
        dlg.budget_updated.connect(self._update_footer_and_freshness)
        dlg.test_fetch_completed.connect(self._on_fetch_completed)
        dlg.update_install_requested.connect(self._on_apply_staged_update)
        dlg.exec()

    # --- Automatic Application Update Check & Installation ---

    def _on_auto_update_timer_fired(self) -> None:
        s = self.settings_manager.settings
        if not s.auto_check_updates:
            return
        if not self.update_service.should_auto_check(s.update_check_interval_hours):
            return
        self.check_for_application_updates(force=False)

    def check_for_application_updates(self, force: bool = False) -> bool:
        """Start an asynchronous GitHub Releases check in the background.

        Does NOT touch the Navasan market API or request budget.
        """
        s = self.settings_manager.settings
        if not force and not s.auto_check_updates:
            return False
        repo = s.get_effective_github_repo()
        if not repo:
            return False
        if self._update_check_worker is not None and self._update_check_worker.isRunning():
            return False

        self._update_check_worker = UpdateCheckWorker(
            service=self.update_service,
            repo=repo,
            channel=s.update_channel,
            force=force,
        )
        self._update_check_worker.finished_result.connect(self._on_background_update_check_finished)
        self._update_check_worker.start()
        return True

    def _on_background_update_check_finished(self, result: UpdateCheckResult) -> None:
        if not result.update_available or result.manifest is None:
            return
        manifest = result.manifest
        if not self.update_service.should_notify_user(manifest.version):
            return

        s = self.settings_manager.settings
        if s.auto_download_updates and manifest.windows_asset is not None:
            if self._update_download_worker is None or not self._update_download_worker.isRunning():
                self._pending_update_manifest = manifest
                self._update_download_worker = UpdateDownloadWorker(
                    service=self.update_service,
                    manifest=manifest,
                )
                self._update_download_worker.finished_result.connect(
                    self._on_background_update_download_finished
                )
                self._update_download_worker.start()
                return

        self._show_update_notification_dialog(manifest, staged_dir=None)

    def _on_background_update_download_finished(self, stage_res: DownloadStageResult) -> None:
        manifest = self._pending_update_manifest
        self._pending_update_manifest = None
        if manifest is None:
            return
        staged_dir = stage_res.staging_dir if stage_res.success else None
        self._show_update_notification_dialog(manifest, staged_dir=staged_dir)

    def _show_update_notification_dialog(
        self,
        manifest: ReleaseManifest,
        staged_dir: Optional[Path] = None,
    ) -> None:
        if self._active_update_dialog is not None and self._active_update_dialog.isVisible():
            return
        s = self.settings_manager.settings
        dlg = UpdateNotificationDialog(
            manifest=manifest,
            update_service=self.update_service,
            language=s.language,
            theme=s.theme,
            use_persian_numerals=s.use_persian_numerals,
            staged_dir=staged_dir,
            parent=self,
        )
        dlg.install_confirmed.connect(self._on_apply_staged_update)
        self._active_update_dialog = dlg
        dlg.show_without_focus()

    def _on_apply_staged_update(self, staging_dir: Path) -> None:
        launched = UpdateInstaller.launch_external_updater_and_restart(staging_dir)
        if launched:
            self.quit_application()

    def _on_settings_saved(self, new_settings: AppSettings) -> None:
        old_top = bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self._apply_all_settings(new_settings, initial=False)
        if old_top != new_settings.always_on_top:
            was_vis = self.isVisible()
            saved_pos = self.pos()
            saved_size = self.size()
            prev_restoring = self._restoring_geometry
            self._restoring_geometry = True
            try:
                self._configure_window_flags()
                self.resize(saved_size)
                self.move(saved_pos)
                if was_vis:
                    self.show()
                    if self.size() != saved_size:
                        self.resize(saved_size)
                    if self.pos() != saved_pos:
                        self.move(saved_pos)
            finally:
                self._restoring_geometry = prev_restoring
        if self.isVisible():
            self._apply_native_gadget_behavior()
        self._persist_geometry()

        # Re-arm schedule timer according to updated settings (without issuing immediate unrequested calls)
        self._arm_next_schedule_timer()
        if new_settings.api_provider == "free_iran_market" or new_settings.get_effective_api_key():
            if self._current_state == "missing_key":
                self._hide_banner()
                self._current_state = "cached" if self.cache_store.has_cached_data() else "idle"
                self._style_state_badge()

    # --- Smooth Edge/Corner Resizing, Window Dragging & Geometry Persistence ---

    def hit_test_resize_edges(self, pos: QPoint) -> Qt.Edge:
        """Determine which window edges/corners the local position `pos` lies on."""
        m = self.RESIZE_BORDER_PX
        w = self.width()
        h = self.height()
        edges = Qt.Edge(0)
        if pos.x() <= m:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= w - m:
            edges |= Qt.Edge.RightEdge

        if pos.y() <= m:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= h - m:
            edges |= Qt.Edge.BottomEdge

        return edges

    @staticmethod
    def cursor_for_edges(edges: Qt.Edge) -> Qt.CursorShape:
        """Return the standard resize cursor corresponding to `edges`."""
        on_left = bool(edges & Qt.Edge.LeftEdge)
        on_right = bool(edges & Qt.Edge.RightEdge)
        on_top = bool(edges & Qt.Edge.TopEdge)
        on_bottom = bool(edges & Qt.Edge.BottomEdge)

        if (on_top and on_left) or (on_bottom and on_right):
            return Qt.CursorShape.SizeFDiagCursor
        if (on_top and on_right) or (on_bottom and on_left):
            return Qt.CursorShape.SizeBDiagCursor
        if on_left or on_right:
            return Qt.CursorShape.SizeHorCursor
        if on_top or on_bottom:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def perform_edge_resize(
        self,
        edges: Qt.Edge,
        delta_global: QPoint,
        start_geom: Optional[QRect] = None,
    ) -> QRect:
        """Apply smooth edge/corner resize geometry clamped to minimum and maximum window dimensions."""
        base = start_geom if start_geom is not None else self.geometry()
        min_w = self.minimumWidth()
        max_w = self.maximumWidth()
        min_h = self.minimumHeight()
        max_h = self.maximumHeight()

        new_left = base.left()
        new_top = base.top()
        new_w = base.width()
        new_h = base.height()

        if edges & Qt.Edge.RightEdge:
            new_w = max(min_w, min(max_w, base.width() + delta_global.x()))
        elif edges & Qt.Edge.LeftEdge:
            new_w = max(min_w, min(max_w, base.width() - delta_global.x()))
            new_left = base.left() + (base.width() - new_w)

        if edges & Qt.Edge.BottomEdge:
            new_h = max(min_h, min(max_h, base.height() + delta_global.y()))
        elif edges & Qt.Edge.TopEdge:
            new_h = max(min_h, min(max_h, base.height() - delta_global.y()))
            new_top = base.top() + (base.height() - new_h)

        target_rect = QRect(new_left, new_top, new_w, new_h)
        if target_rect != self.geometry():
            self.setGeometry(target_rect)
        return target_rect

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is getattr(self, "size_grip", None) and isinstance(event, QMouseEvent):
            grip_edges = Qt.Edge.BottomEdge | (
                Qt.Edge.LeftEdge if is_rtl(self.language) else Qt.Edge.RightEdge
            )
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._resize_edges = grip_edges
                self._resize_start_global = event.globalPosition().toPoint()
                self._resize_start_geom = self.geometry()
                self._drag_pos = None
                self.setCursor(self.cursor_for_edges(grip_edges))
                event.accept()
                return True
            if (
                event.type() == QEvent.Type.MouseMove
                and self._resize_edges != Qt.Edge(0)
                and self._resize_start_global is not None
                and self._resize_start_geom is not None
                and (event.buttons() & Qt.MouseButton.LeftButton)
            ):
                delta = event.globalPosition().toPoint() - self._resize_start_global
                self.perform_edge_resize(self._resize_edges, delta, self._resize_start_geom)
                event.accept()
                return True
            if (
                event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._resize_edges = Qt.Edge(0)
                self._resize_start_global = None
                self._resize_start_geom = None
                self.unsetCursor()
                self._persist_geometry()
                event.accept()
                return True

        if watched is self.central_container and event.type() == QEvent.Type.MouseMove:
            if not self.power_manager.is_active:
                return False
            if isinstance(event, QMouseEvent) and event.buttons() == Qt.MouseButton.NoButton:
                local_pos = self.mapFromGlobal(event.globalPosition().toPoint())
                edges = self.hit_test_resize_edges(local_pos)
                if edges != Qt.Edge(0):
                    self.setCursor(self.cursor_for_edges(edges))
                else:
                    self.unsetCursor()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            local_pos = event.position().toPoint()
            edges = self.hit_test_resize_edges(local_pos)
            if edges != Qt.Edge(0):
                self._resize_edges = edges
                self._resize_start_global = event.globalPosition().toPoint()
                self._resize_start_geom = self.geometry()
                self._drag_pos = None
                self.setCursor(self.cursor_for_edges(edges))
                event.accept()
                return

            self._resize_edges = Qt.Edge(0)
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        global_pt = event.globalPosition().toPoint()
        if (
            self._resize_edges != Qt.Edge(0)
            and self._resize_start_global is not None
            and self._resize_start_geom is not None
            and (event.buttons() & Qt.MouseButton.LeftButton)
        ):
            delta = global_pt - self._resize_start_global
            self.perform_edge_resize(self._resize_edges, delta, self._resize_start_geom)
            event.accept()
            return

        if (
            self._drag_pos is not None
            and (event.buttons() & Qt.MouseButton.LeftButton)
        ):
            self.move(global_pt - self._drag_pos)
            event.accept()
            return

        if event.buttons() == Qt.MouseButton.NoButton:
            edges = self.hit_test_resize_edges(event.position().toPoint())
            if edges != Qt.Edge(0):
                self.setCursor(self.cursor_for_edges(edges))
            else:
                self.unsetCursor()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._resize_edges != Qt.Edge(0):
                self._resize_edges = Qt.Edge(0)
                self._resize_start_global = None
                self._resize_start_geom = None
                self.unsetCursor()
                self._persist_geometry()
                event.accept()
                return
            if self._drag_pos is not None:
                self._drag_pos = None
                self._persist_geometry()
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if self._resize_edges == Qt.Edge(0):
            self.unsetCursor()
        super().leaveEvent(event)

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        pos = event.pos()
        if self.isMinimized() or pos.x() <= -30000 or pos.y() <= -30000:
            return
        self._last_valid_pos = pos
        if getattr(self, "_restoring_geometry", False):
            return
        s = self.settings_manager.settings
        if s.window_x == pos.x() and s.window_y == pos.y():
            return
        s.window_x = pos.x()
        s.window_y = pos.y()
        self._resize_save_timer.start()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        sz = event.size()
        if (
            self.isMinimized()
            or sz.width() < self.MIN_WINDOW_SIZE.width()
            or sz.height() < self.MIN_WINDOW_SIZE.height()
        ):
            return
        self._last_valid_size = sz
        if self.power_manager.is_active:
            self._last_active_size = sz
            self._apply_responsive_layout(sz.width(), sz.height())
        if getattr(self, "_restoring_geometry", False):
            return
        s = self.settings_manager.settings
        if (
            s.window_width == sz.width()
            and s.window_height == sz.height()
            and s.display_mode == self._responsive_mode
        ):
            return
        # Keep in-memory settings dimensions and display_mode synchronized immediately and debounce disk flush
        s.window_width = sz.width()
        s.window_height = sz.height()
        s.display_mode = self._responsive_mode
        self._resize_save_timer.start()

    def _sync_geometry_in_memory(self) -> None:
        """Synchronize in-memory settings with the widget's current position, size, and responsive mode."""
        pos = self.pos()
        if self.isMinimized() or pos.x() <= -30000 or pos.y() <= -30000:
            if self._last_valid_pos is not None:
                pos = self._last_valid_pos
        else:
            self._last_valid_pos = pos

        sz = self.size()
        if (
            self.isMinimized()
            or sz.width() < self.MIN_WINDOW_SIZE.width()
            or sz.height() < self.MIN_WINDOW_SIZE.height()
        ):
            if self._last_valid_size.isValid():
                sz = self._last_valid_size
        else:
            self._last_valid_size = sz

        s = self.settings_manager.settings
        s.window_x = pos.x()
        s.window_y = pos.y()
        s.window_width = sz.width()
        s.window_height = sz.height()
        s.display_mode = self.compute_responsive_mode(sz.width(), sz.height())

    def _persist_geometry(self) -> None:
        if self._resize_save_timer.isActive():
            self._resize_save_timer.stop()
        pos = self.pos()
        if self.isMinimized() or pos.x() <= -30000 or pos.y() <= -30000:
            if self._last_valid_pos is not None:
                pos = self._last_valid_pos
        else:
            self._last_valid_pos = pos

        sz = self.size()
        if (
            self.isMinimized()
            or sz.width() < self.MIN_WINDOW_SIZE.width()
            or sz.height() < self.MIN_WINDOW_SIZE.height()
        ):
            if self._last_valid_size.isValid():
                sz = self._last_valid_size
        else:
            self._last_valid_size = sz

        mode = self.compute_responsive_mode(sz.width(), sz.height())
        self.settings_manager.update(
            window_x=pos.x(),
            window_y=pos.y(),
            window_width=sz.width(),
            window_height=sz.height(),
            display_mode=mode,
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self._persist_geometry()
        if not self._is_quitting and getattr(self, "tray_icon", None) is not None and self.tray_icon.isVisible():
            event.ignore()
            self._explicitly_hidden = True
            self.hide()
            self.power_manager.set_dormant(True)
            return
        self.shell_event_monitor.stop()
        self.power_manager.stop()
        super().closeEvent(event)

    def quit_application(self) -> None:
        self._is_quitting = True
        self.shell_event_monitor.stop()
        self.power_manager.stop()
        self._schedule_timer.stop()
        self._update_startup_timer.stop()
        self._update_periodic_timer.stop()
        self._age_timer.stop()
        self._persist_geometry()
        if getattr(self, "tray_icon", None) is not None:
            self.tray_icon.hide()
        QApplication.quit()
