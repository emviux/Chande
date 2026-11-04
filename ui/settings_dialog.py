"""Settings dialog for Chande with strict budget controls and masked API key handling."""

from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config.constants import (
    ALLOWED_REFRESH_INTERVALS_HOURS,
    APP_VERSION,
    DAILY_SCHEDULED_LIMIT,
    MONTHLY_HARD_LIMIT,
    MONTHLY_RESERVE_BUDGET,
    MONTHLY_SCHEDULED_BUDGET,
)
from config.settings import AppSettings, SettingsManager, mask_api_key, normalize_github_repo
from services.api_client import ApiFetchResult, MarketApiClient
from services.budget_manager import RequestBudgetManager
from services.formatter import format_localized_datetime, localize_digits
from services.i18n import (
    get_layout_direction,
    is_rtl,
    tr,
)
from storage.cache_store import CacheStore
from ui.theme import FONT_FAMILY_STACK, get_palette
from ui.update_dialog import UpdateNotificationDialog
from ui.worker import MarketFetchWorker, UpdateCheckWorker, UsageCheckWorker
from updater.github_updater import GitHubUpdateService, UpdateCheckResult, is_trusted_https_github_url


class SettingsDialog(QDialog):
    """Modal configuration dialog for Chande.

    Invariants:
    - Opening or closing this dialog NEVER triggers an automatic API request.
    - The raw API key is always masked (`EchoMode.Password`) and never shown in plain text.
    - Testing API connection or querying `/usage/` requires explicit user confirmation
      because each consumes 1 request from the 120/month budget.
    """

    settings_saved = Signal(object)  # emits updated AppSettings
    cache_cleared = Signal()
    budget_updated = Signal()
    test_fetch_completed = Signal(object)  # emits ApiFetchResult if test fetch was run
    update_install_requested = Signal(object)  # emits Path to verified staging_dir

    def __init__(
        self,
        settings_manager: SettingsManager,
        budget_manager: RequestBudgetManager,
        cache_store: CacheStore,
        api_client: MarketApiClient,
        parent: Optional[QWidget] = None,
        update_service: Optional[GitHubUpdateService] = None,
    ) -> None:
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.budget_manager = budget_manager
        self.cache_store = cache_store
        self.api_client = api_client
        self.update_service = update_service or GitHubUpdateService()
        self._test_worker: Optional[MarketFetchWorker] = None
        self._usage_worker: Optional[UsageCheckWorker] = None
        self._update_check_worker: Optional[UpdateCheckWorker] = None
        self._last_update_check_result: Optional[UpdateCheckResult] = None
        self._loading_settings = False
        self._lang = "en" if self.settings_manager.settings.language == "en" else "fa"

        self.setWindowTitle(tr("settings.window_title", lang=self._lang))
        self.setLayoutDirection(get_layout_direction(self._lang))
        self.setMinimumWidth(480)
        self.setModal(True)

        self._build_ui()
        self._load_from_settings()
        self.retranslate_ui(self._lang)
        self.refresh_stats_display()
        self.refresh_about_display()
        self.apply_theme(self.settings_manager.settings.theme)

    @property
    def active_language(self) -> str:
        if hasattr(self, "language_combo"):
            val = self.language_combo.currentData()
            if val in ("fa", "en"):
                return str(val)
        return self._lang

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        self.tabs = QTabWidget()
        self.general_tab = QWidget()
        general_layout = QVBoxLayout(self.general_tab)
        general_layout.setContentsMargins(10, 10, 10, 10)
        general_layout.setSpacing(10)

        # 1. API & Connection Group
        self.api_group = QGroupBox()
        api_layout = QVBoxLayout(self.api_group)
        api_layout.setSpacing(8)

        self.form_api = QFormLayout()
        self.form_api.setSpacing(8)

        self.provider_row_label = QLabel()
        self.provider_combo = QComboBox()
        self.provider_combo.addItem("", "navasan")
        self.provider_combo.addItem("", "free_iran_market")
        self.form_api.addRow(self.provider_row_label, self.provider_combo)

        self.api_key_row_label = QLabel()
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.form_api.addRow(self.api_key_row_label, self.api_key_input)

        self.key_status_row_label = QLabel()
        self.masked_key_hint = QLabel("")
        self.masked_key_hint.setWordWrap(True)
        self.form_api.addRow(self.key_status_row_label, self.masked_key_hint)

        api_layout.addLayout(self.form_api)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.test_conn_btn = QPushButton()
        self.test_conn_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_conn_btn.clicked.connect(self._on_test_connection)
        btn_row.addWidget(self.test_conn_btn)

        self.check_usage_btn = QPushButton()
        self.check_usage_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check_usage_btn.clicked.connect(self._on_check_remote_usage)
        btn_row.addWidget(self.check_usage_btn)

        api_layout.addLayout(btn_row)

        self.api_feedback_label = QLabel("")
        self.api_feedback_label.setWordWrap(True)
        self.api_feedback_label.hide()
        api_layout.addWidget(self.api_feedback_label)

        general_layout.addWidget(self.api_group)

        # 2. Schedule & Request Budget Safeguards Group
        self.sched_group = QGroupBox()
        sched_layout = QVBoxLayout(self.sched_group)
        sched_layout.setSpacing(8)

        self.auto_refresh_cb = QCheckBox()
        sched_layout.addWidget(self.auto_refresh_cb)

        self.form_sched = QFormLayout()
        self.form_sched.setSpacing(8)

        self.interval_row_label = QLabel()
        self.interval_combo = QComboBox()
        for hours in ALLOWED_REFRESH_INTERVALS_HOURS:
            self.interval_combo.addItem("", hours)
        self.form_sched.addRow(self.interval_row_label, self.interval_combo)

        sched_layout.addLayout(self.form_sched)

        self.confirm_manual_cb = QCheckBox()
        sched_layout.addWidget(self.confirm_manual_cb)

        general_layout.addWidget(self.sched_group)

        # 3. Appearance & Window Behavior Group
        self.ui_group = QGroupBox()
        ui_layout = QVBoxLayout(self.ui_group)
        ui_layout.setSpacing(8)

        self.form_ui = QFormLayout()
        self.form_ui.setSpacing(8)

        self.language_row_label = QLabel()
        self.language_combo = QComboBox()
        self.language_combo.addItem("فارسی (Persian)", "fa")
        self.language_combo.addItem("English", "en")
        self.language_combo.currentIndexChanged.connect(self._on_language_combo_changed)
        self.form_ui.addRow(self.language_row_label, self.language_combo)

        self.theme_row_label = QLabel()
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("", "dark")
        self.theme_combo.addItem("", "light")
        self.form_ui.addRow(self.theme_row_label, self.theme_combo)

        self.mode_row_label = QLabel()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("", "expanded")
        self.mode_combo.addItem("", "compact")
        self.mode_combo.addItem("", "ultra_compact")
        self.form_ui.addRow(self.mode_row_label, self.mode_combo)

        self.numerals_row_label = QLabel()
        self.numerals_combo = QComboBox()
        self.numerals_combo.addItem("", True)
        self.numerals_combo.addItem("", False)
        self.numerals_combo.currentIndexChanged.connect(self._on_numerals_combo_changed)
        self.form_ui.addRow(self.numerals_row_label, self.numerals_combo)

        self.opacity_row_label = QLabel()
        opacity_row = QHBoxLayout()
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(40, 100)
        self.opacity_slider.valueChanged.connect(self._on_opacity_slider_changed)
        self.opacity_value_label = QLabel("96%")
        self.opacity_value_label.setMinimumWidth(42)
        opacity_row.addWidget(self.opacity_slider, 1)
        opacity_row.addWidget(self.opacity_value_label)
        self.form_ui.addRow(self.opacity_row_label, opacity_row)

        ui_layout.addLayout(self.form_ui)

        checks_row1 = QHBoxLayout()
        self.always_on_top_cb = QCheckBox()
        self.start_windows_cb = QCheckBox()
        checks_row1.addWidget(self.always_on_top_cb)
        checks_row1.addWidget(self.start_windows_cb)
        ui_layout.addLayout(checks_row1)

        checks_row2 = QHBoxLayout()
        self.show_chart_cb = QCheckBox()
        self.show_secondary_cb = QCheckBox()
        checks_row2.addWidget(self.show_chart_cb)
        checks_row2.addWidget(self.show_secondary_cb)
        ui_layout.addLayout(checks_row2)

        general_layout.addWidget(self.ui_group)

        # 4. Local Budget & Cache Statistics Group
        self.stats_group = QGroupBox()
        stats_layout = QVBoxLayout(self.stats_group)
        stats_layout.setSpacing(6)

        self.budget_stats_label = QLabel("")
        self.budget_stats_label.setWordWrap(True)
        stats_layout.addWidget(self.budget_stats_label)

        self.last_update_label = QLabel("")
        self.last_update_label.setWordWrap(True)
        stats_layout.addWidget(self.last_update_label)

        self.disclaimer_label = QLabel("")
        self.disclaimer_label.setWordWrap(True)
        stats_layout.addWidget(self.disclaimer_label)

        maint_row = QHBoxLayout()
        maint_row.setSpacing(8)

        self.clear_cache_btn = QPushButton()
        self.clear_cache_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_cache_btn.clicked.connect(self._on_clear_cache)
        maint_row.addWidget(self.clear_cache_btn)

        self.reset_stats_btn = QPushButton()
        self.reset_stats_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_stats_btn.clicked.connect(self._on_reset_local_stats)
        maint_row.addWidget(self.reset_stats_btn)

        stats_layout.addLayout(maint_row)
        general_layout.addWidget(self.stats_group)

        # Tab 2: About & Automatic Updates
        self.about_tab = QWidget()
        about_tab_layout = QVBoxLayout(self.about_tab)
        about_tab_layout.setContentsMargins(10, 10, 10, 10)
        about_tab_layout.setSpacing(12)

        self.about_group = QGroupBox()
        about_group_layout = QVBoxLayout(self.about_group)
        about_group_layout.setSpacing(8)

        self.form_about = QFormLayout()
        self.form_about.setSpacing(8)

        self.about_app_name_label = QLabel()
        self.about_app_name_value = QLabel()
        self.about_app_name_val = self.about_app_name_value
        self.form_about.addRow(self.about_app_name_label, self.about_app_name_value)

        self.about_installed_version_label = QLabel()
        self.about_installed_version_value = QLabel(APP_VERSION)
        self.about_installed_version_val = self.about_installed_version_value
        self.form_about.addRow(self.about_installed_version_label, self.about_installed_version_value)

        self.about_latest_version_label = QLabel()
        self.about_latest_version_value = QLabel()
        self.about_latest_version_val = self.about_latest_version_value
        self.form_about.addRow(self.about_latest_version_label, self.about_latest_version_value)

        self.about_repo_label = QLabel()
        self.github_repo_input = QLineEdit()
        self.form_about.addRow(self.about_repo_label, self.github_repo_input)

        about_group_layout.addLayout(self.form_about)

        links_row = QHBoxLayout()
        links_row.setSpacing(8)

        self.open_repo_btn = QPushButton()
        self.open_repo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_repo_btn.clicked.connect(self._on_open_github_repo)
        links_row.addWidget(self.open_repo_btn)

        self.open_releases_btn = QPushButton()
        self.open_releases_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_releases_btn.clicked.connect(self._on_open_release_notes)
        links_row.addWidget(self.open_releases_btn)

        about_group_layout.addLayout(links_row)
        about_tab_layout.addWidget(self.about_group)

        self.updates_group = QGroupBox()
        updates_group_layout = QVBoxLayout(self.updates_group)
        updates_group_layout.setSpacing(8)

        self.auto_check_updates_cb = QCheckBox()
        updates_group_layout.addWidget(self.auto_check_updates_cb)

        self.auto_download_updates_cb = QCheckBox()
        updates_group_layout.addWidget(self.auto_download_updates_cb)

        self.form_updates = QFormLayout()
        self.form_updates.setSpacing(8)

        self.update_channel_label = QLabel()
        self.update_channel_combo = QComboBox()
        self.update_channel_combo.addItem("", "stable")
        self.update_channel_combo.addItem("", "beta")
        self.form_updates.addRow(self.update_channel_label, self.update_channel_combo)

        self.about_last_check_label = QLabel()
        self.about_last_check_value = QLabel()
        self.about_last_check_value.setWordWrap(True)
        self.form_updates.addRow(self.about_last_check_label, self.about_last_check_value)

        self.about_update_status_label = QLabel()
        self.about_update_status_value = QLabel()
        self.about_update_status_value.setWordWrap(True)
        self.form_updates.addRow(self.about_update_status_label, self.about_update_status_value)

        updates_group_layout.addLayout(self.form_updates)

        self.check_updates_btn = QPushButton()
        self.check_updates_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check_updates_btn.clicked.connect(self._on_check_for_updates)
        updates_group_layout.addWidget(self.check_updates_btn)

        about_tab_layout.addWidget(self.updates_group)
        about_tab_layout.addStretch(1)

        self.tabs.addTab(self.general_tab, "")
        self.tabs.addTab(self.about_tab, "")
        root.addWidget(self.tabs)

        # Bottom Dialog Action Buttons
        footer_line = QFrame()
        footer_line.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(footer_line)

        action_row = QHBoxLayout()
        action_row.addStretch(1)

        self.cancel_btn = QPushButton()
        self.cancel_btn.setMinimumWidth(95)
        self.cancel_btn.clicked.connect(self.reject)
        action_row.addWidget(self.cancel_btn)

        self.save_btn = QPushButton()
        self.save_btn.setMinimumWidth(125)
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        action_row.addWidget(self.save_btn)

        root.addLayout(action_row)

    def retranslate_ui(self, lang: Optional[str] = None) -> None:
        """Retranslate all dialog labels, combos, checkboxes, and buttons in-place."""
        prev_lang = getattr(self, "_lang", None)
        norm_lang = "en" if str(lang or self.active_language).lower().startswith("en") else "fa"
        self._lang = norm_lang
        if lang is not None and not getattr(self, "_loading_settings", False) and prev_lang != norm_lang:
            if hasattr(self, "language_combo"):
                idx_lang = self.language_combo.findData(norm_lang)
                if idx_lang >= 0:
                    self.language_combo.blockSignals(True)
                    self.language_combo.setCurrentIndex(idx_lang)
                    self.language_combo.blockSignals(False)
            if hasattr(self, "numerals_combo"):
                idx_num = self.numerals_combo.findData(norm_lang == "fa")
                if idx_num >= 0:
                    self.numerals_combo.blockSignals(True)
                    self.numerals_combo.setCurrentIndex(idx_num)
                    self.numerals_combo.blockSignals(False)
        use_fa = bool(self.numerals_combo.currentData()) if hasattr(self, "numerals_combo") else (norm_lang == "fa")

        self.setWindowTitle(tr("settings.window_title", lang=norm_lang))
        self.setLayoutDirection(get_layout_direction(norm_lang))

        label_align = Qt.AlignmentFlag.AlignRight if is_rtl(norm_lang) else Qt.AlignmentFlag.AlignLeft
        self.form_api.setLabelAlignment(label_align)
        self.form_sched.setLabelAlignment(label_align)
        self.form_ui.setLabelAlignment(label_align)
        self.form_about.setLabelAlignment(label_align)
        self.form_updates.setLabelAlignment(label_align)

        # Tab titles
        self.tabs.setTabText(0, tr("settings.tab_general", lang=norm_lang))
        self.tabs.setTabText(1, tr("settings.tab_about", lang=norm_lang))

        # Group titles
        self.api_group.setTitle(tr("settings.api_group", lang=norm_lang))
        self.sched_group.setTitle(tr("settings.sched_group", lang=norm_lang))
        self.ui_group.setTitle(tr("settings.ui_group", lang=norm_lang))
        self.stats_group.setTitle(tr("settings.stats_group", lang=norm_lang))
        self.about_group.setTitle(tr("settings.about_group", lang=norm_lang))
        self.updates_group.setTitle(tr("settings.updates_group", lang=norm_lang))

        # Form row labels
        self.provider_row_label.setText(tr("settings.provider_label", lang=norm_lang))
        self.api_key_row_label.setText(tr("settings.api_key_label", lang=norm_lang))
        self.key_status_row_label.setText(tr("settings.key_status_label", lang=norm_lang))
        self.interval_row_label.setText(tr("settings.interval_label", lang=norm_lang))
        self.language_row_label.setText(tr("settings.language_label", lang=norm_lang))
        self.theme_row_label.setText(tr("settings.theme_label", lang=norm_lang))
        self.mode_row_label.setText(tr("settings.mode_label", lang=norm_lang))
        self.numerals_row_label.setText(tr("settings.numerals_label", lang=norm_lang))
        self.opacity_row_label.setText(tr("settings.opacity_label", lang=norm_lang))

        # Provider combo items
        self.provider_combo.setItemText(0, tr("settings.provider_navasan", lang=norm_lang))
        self.provider_combo.setItemText(1, tr("settings.provider_free", lang=norm_lang))

        self.api_key_input.setPlaceholderText(tr("settings.api_key_placeholder", lang=norm_lang))
        effective_key = (
            self.api_key_input.text().strip()
            or self.settings_manager.settings.get_effective_api_key()
        )
        self._update_masked_key_hint(effective_key)

        self.test_conn_btn.setText(tr("settings.test_conn_btn", lang=norm_lang))
        self.check_usage_btn.setText(tr("settings.check_usage_btn", lang=norm_lang))

        # Schedule controls
        self.auto_refresh_cb.setText(tr("settings.auto_refresh_cb", lang=norm_lang))
        for idx, hours in enumerate(ALLOWED_REFRESH_INTERVALS_HOURS):
            per_day = max(1, 24 // hours)
            label = tr("settings.interval_item", lang=norm_lang, hours=hours, per_day=per_day)
            if hours == 8:
                label += tr("settings.interval_default_suffix", lang=norm_lang)
            self.interval_combo.setItemText(idx, localize_digits(label, use_persian=use_fa))
        self.confirm_manual_cb.setText(tr("settings.confirm_manual_cb", lang=norm_lang))

        # Language, Theme, Mode, Numerals combos
        self.language_combo.setItemText(0, tr("language.fa", lang=norm_lang))
        self.language_combo.setItemText(1, tr("language.en", lang=norm_lang))

        self.theme_combo.setItemText(0, tr("settings.theme_dark", lang=norm_lang))
        self.theme_combo.setItemText(1, tr("settings.theme_light", lang=norm_lang))

        self.mode_combo.setItemText(0, tr("modes.expanded", lang=norm_lang))
        self.mode_combo.setItemText(1, tr("modes.compact", lang=norm_lang))
        self.mode_combo.setItemText(2, tr("modes.ultra_compact", lang=norm_lang))

        self.numerals_combo.setItemText(0, tr("settings.numerals_persian", lang=norm_lang))
        self.numerals_combo.setItemText(1, tr("settings.numerals_western", lang=norm_lang))

        # Checkboxes
        self.always_on_top_cb.setText(tr("settings.always_on_top_cb", lang=norm_lang))
        self.start_windows_cb.setText(tr("settings.start_windows_cb", lang=norm_lang))
        self.show_chart_cb.setText(tr("settings.show_chart_cb", lang=norm_lang))
        self.show_secondary_cb.setText(tr("settings.show_secondary_cb", lang=norm_lang))

        # Stats & action buttons
        self.disclaimer_label.setText(tr("settings.disclaimer", lang=norm_lang))
        self.clear_cache_btn.setText(tr("settings.clear_cache_btn", lang=norm_lang))
        self.reset_stats_btn.setText(tr("settings.reset_stats_btn", lang=norm_lang))
        self.cancel_btn.setText(tr("settings.cancel_btn", lang=norm_lang))
        self.save_btn.setText(tr("settings.save_btn", lang=norm_lang))

        # About & Updates controls
        self.about_app_name_label.setText(tr("settings.app_name_label", lang=norm_lang))
        self.about_app_name_value.setText(tr("app.title_with_en", lang=norm_lang))
        self.about_installed_version_label.setText(tr("settings.installed_version_label", lang=norm_lang))
        self.about_latest_version_label.setText(tr("settings.latest_version_label", lang=norm_lang))
        self.about_repo_label.setText(tr("settings.repo_label", lang=norm_lang))
        self.github_repo_input.setPlaceholderText(tr("settings.repo_placeholder", lang=norm_lang))
        self.open_repo_btn.setText(tr("settings.open_repo_btn", lang=norm_lang))
        self.open_releases_btn.setText(tr("settings.open_releases_btn", lang=norm_lang))

        self.auto_check_updates_cb.setText(tr("settings.auto_check_updates_cb", lang=norm_lang))
        self.auto_download_updates_cb.setText(tr("settings.auto_download_updates_cb", lang=norm_lang))
        self.update_channel_label.setText(tr("settings.update_channel_label", lang=norm_lang))
        self.update_channel_combo.setItemText(0, tr("settings.channel_stable", lang=norm_lang))
        self.update_channel_combo.setItemText(1, tr("settings.channel_beta", lang=norm_lang))
        self.about_last_check_label.setText(tr("settings.last_check_label", lang=norm_lang))
        self.about_update_status_label.setText(tr("settings.update_status_label", lang=norm_lang))
        self.check_updates_btn.setText(tr("settings.check_updates_btn", lang=norm_lang))

        self._on_opacity_slider_changed(self.opacity_slider.value())
        self.refresh_stats_display()
        self.refresh_about_display()
        self.apply_theme(str(self.theme_combo.currentData() or self.settings_manager.settings.theme))

    def _on_language_combo_changed(self, _index: int) -> None:
        if self._loading_settings:
            return
        lang = str(self.language_combo.currentData() or "fa")
        # Default numerals to Persian for 'fa' and Western for 'en' when switching language,
        # while still letting the user change numerals_combo independently afterward.
        idx_num = self.numerals_combo.findData(lang == "fa")
        if idx_num >= 0:
            self.numerals_combo.blockSignals(True)
            self.numerals_combo.setCurrentIndex(idx_num)
            self.numerals_combo.blockSignals(False)
        self.retranslate_ui(lang)

    def _on_numerals_combo_changed(self, _index: int) -> None:
        if self._loading_settings:
            return
        self.retranslate_ui(self.active_language)

    def _load_from_settings(self) -> None:
        self._loading_settings = True
        try:
            s = self.settings_manager.settings
            self._initial_display_mode = s.display_mode
            self._initial_show_secondary = s.show_secondary_cards
            idx_prov = self.provider_combo.findData(s.api_provider)
            if idx_prov >= 0:
                self.provider_combo.setCurrentIndex(idx_prov)

            # Keep existing API key in masked input; never show it in plain text
            self.api_key_input.setText(s.api_key)
            self._update_masked_key_hint(s.get_effective_api_key())

            self.auto_refresh_cb.setChecked(s.auto_refresh_enabled)
            idx_int = self.interval_combo.findData(s.refresh_interval_hours)
            if idx_int >= 0:
                self.interval_combo.setCurrentIndex(idx_int)

            self.confirm_manual_cb.setChecked(s.confirm_manual_refresh)

            idx_lang = self.language_combo.findData(s.language)
            if idx_lang >= 0:
                self.language_combo.setCurrentIndex(idx_lang)

            idx_theme = self.theme_combo.findData(s.theme)
            if idx_theme >= 0:
                self.theme_combo.setCurrentIndex(idx_theme)

            idx_mode = self.mode_combo.findData(s.display_mode)
            if idx_mode >= 0:
                self.mode_combo.setCurrentIndex(idx_mode)

            idx_num = self.numerals_combo.findData(s.use_persian_numerals)
            if idx_num >= 0:
                self.numerals_combo.setCurrentIndex(idx_num)

            pct = int(round(s.opacity * 100))
            self.opacity_slider.setValue(pct)
            self._on_opacity_slider_changed(pct)

            self.always_on_top_cb.setChecked(s.always_on_top)
            self.start_windows_cb.setChecked(s.start_with_windows)
            self.show_chart_cb.setChecked(s.show_chart)
            self.show_secondary_cb.setChecked(s.show_secondary_cards)

            self.github_repo_input.setText(s.get_effective_github_repo())
            self.auto_check_updates_cb.setChecked(s.auto_check_updates)
            self.auto_download_updates_cb.setChecked(s.auto_download_updates)
            idx_chan = self.update_channel_combo.findData(s.update_channel)
            if idx_chan >= 0:
                self.update_channel_combo.setCurrentIndex(idx_chan)
        finally:
            self._loading_settings = False

    def _update_masked_key_hint(self, effective_key: str) -> None:
        lang = self.active_language
        if effective_key:
            masked = mask_api_key(effective_key)
            self.masked_key_hint.setText(tr("settings.key_status_set", lang=lang, masked=masked))
        else:
            self.masked_key_hint.setText(tr("settings.key_status_empty", lang=lang))

    def _on_opacity_slider_changed(self, value: int) -> None:
        use_fa = bool(self.numerals_combo.currentData())
        txt = f"{value}%"
        self.opacity_value_label.setText(localize_digits(txt, use_persian=use_fa))

    def refresh_stats_display(self) -> None:
        lang = self.active_language
        use_fa = (
            bool(self.numerals_combo.currentData())
            if hasattr(self, "numerals_combo") and self.numerals_combo.count() > 0
            else self.settings_manager.settings.use_persian_numerals
        )
        st = self.budget_manager.get_status()

        stats_text = tr(
            "settings.stats_summary",
            lang=lang,
            month=st.month_key,
            total_used=st.monthly_total_used,
            hard_limit=MONTHLY_HARD_LIMIT,
            remaining=st.monthly_remaining,
            sched_used=st.monthly_scheduled_used,
            sched_budget=MONTHLY_SCHEDULED_BUDGET,
            reserve_remaining=st.reserve_remaining,
            reserve_budget=MONTHLY_RESERVE_BUDGET,
            daily_sched=st.daily_scheduled_used,
            daily_limit=DAILY_SCHEDULED_LIMIT,
            failed_count=st.failed_count,
        )
        self.budget_stats_label.setText(localize_digits(stats_text, use_persian=use_fa))

        last_ts = self.cache_store.get_last_fetch_timestamp()
        src = self.cache_store.get_source()
        if last_ts:
            dt_str = format_localized_datetime(last_ts, lang=lang, use_persian=use_fa)
            if src == "navasan":
                src_str = tr("settings.source_navasan", lang=lang)
            elif src == "free_iran_market":
                src_str = tr("settings.source_free", lang=lang)
            else:
                src_str = tr("settings.source_unknown", lang=lang)
            self.last_update_label.setText(
                tr("settings.last_update_with_source", lang=lang, datetime=dt_str, source=src_str)
            )
        else:
            self.last_update_label.setText(tr("settings.last_update_never", lang=lang))

    def _on_test_connection(self) -> None:
        lang = self.active_language
        use_fa = bool(self.numerals_combo.currentData())
        if self.budget_manager.is_in_flight:
            QMessageBox.information(
                self,
                tr("dialogs.request_in_progress_title", lang=lang),
                tr("dialogs.request_in_progress_body", lang=lang),
            )
            return

        st = self.budget_manager.get_status()
        if st.is_exhausted:
            QMessageBox.warning(
                self,
                tr("dialogs.quota_exhausted_title", lang=lang),
                tr("dialogs.quota_exhausted_test_body", lang=lang),
            )
            return

        provider = str(self.provider_combo.currentData() or "navasan")
        entered_key = self.api_key_input.text().strip() or self.settings_manager.settings.get_effective_api_key()
        if provider == "navasan" and not entered_key:
            QMessageBox.warning(
                self,
                tr("dialogs.missing_key_title", lang=lang),
                tr("dialogs.missing_key_test_body", lang=lang),
            )
            return

        warn_msg = localize_digits(
            tr(
                "dialogs.confirm_test_body",
                lang=lang,
                remaining=st.monthly_remaining,
                limit=MONTHLY_HARD_LIMIT,
                reserve_remaining=st.reserve_remaining,
                reserve_limit=MONTHLY_RESERVE_BUDGET,
            ),
            use_persian=use_fa,
        )
        reply = QMessageBox.question(
            self,
            tr("dialogs.confirm_test_title", lang=lang),
            warn_msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.test_conn_btn.setEnabled(False)
        self.check_usage_btn.setEnabled(False)
        self.api_feedback_label.setText(tr("settings.testing_conn", lang=lang))
        self.api_feedback_label.show()

        self._test_worker = MarketFetchWorker(
            client=self.api_client,
            api_key=entered_key,
            kind="test",
            provider=provider,
            refresh_interval_hours=int(self.interval_combo.currentData() or 8),
        )
        self._test_worker.finished_result.connect(self._on_test_worker_finished)
        self._test_worker.start()

    def _on_test_worker_finished(self, result: ApiFetchResult) -> None:
        lang = self.active_language
        use_fa = bool(self.numerals_combo.currentData())
        self.test_conn_btn.setEnabled(True)
        self.check_usage_btn.setEnabled(True)
        self.refresh_stats_display()
        self.budget_updated.emit()

        msg = result.get_message(lang)
        if result.success:
            txt = tr("settings.test_success", lang=lang, message=msg)
            self.api_feedback_label.setText(localize_digits(txt, use_persian=use_fa))
            self.test_fetch_completed.emit(result)
        else:
            txt = tr("settings.test_error", lang=lang, message=msg)
            self.api_feedback_label.setText(localize_digits(txt, use_persian=use_fa))

    def _on_check_remote_usage(self) -> None:
        lang = self.active_language
        use_fa = bool(self.numerals_combo.currentData())
        if self.budget_manager.is_in_flight:
            QMessageBox.information(
                self,
                tr("dialogs.request_in_progress_title", lang=lang),
                tr("dialogs.request_in_progress_body", lang=lang),
            )
            return

        entered_key = self.api_key_input.text().strip() or self.settings_manager.settings.get_effective_api_key()
        if not entered_key:
            QMessageBox.warning(
                self,
                tr("dialogs.missing_key_usage_title", lang=lang),
                tr("dialogs.missing_key_usage_body", lang=lang),
            )
            return

        st = self.budget_manager.get_status()
        if st.is_exhausted:
            QMessageBox.warning(
                self,
                tr("dialogs.quota_exhausted_title", lang=lang),
                tr("dialogs.quota_exhausted_body", lang=lang),
            )
            return

        warn_msg = localize_digits(
            tr(
                "dialogs.confirm_usage_body",
                lang=lang,
                remaining=st.monthly_remaining,
                limit=MONTHLY_HARD_LIMIT,
            ),
            use_persian=use_fa,
        )
        reply = QMessageBox.question(
            self,
            tr("dialogs.confirm_usage_title", lang=lang),
            warn_msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.test_conn_btn.setEnabled(False)
        self.check_usage_btn.setEnabled(False)
        self.api_feedback_label.setText(tr("settings.checking_usage", lang=lang))
        self.api_feedback_label.show()

        self._usage_worker = UsageCheckWorker(
            client=self.api_client,
            api_key=entered_key,
        )
        self._usage_worker.finished_result.connect(self._on_usage_worker_finished)
        self._usage_worker.start()

    def _on_usage_worker_finished(self, result: ApiFetchResult) -> None:
        lang = self.active_language
        use_fa = bool(self.numerals_combo.currentData())
        self.test_conn_btn.setEnabled(True)
        self.check_usage_btn.setEnabled(True)
        self.refresh_stats_display()
        self.budget_updated.emit()

        if result.success and result.usage_data:
            u = result.usage_data
            msg = localize_digits(
                tr(
                    "settings.usage_report",
                    lang=lang,
                    monthly=u.get("monthly_usage"),
                    daily=u.get("daily_usage"),
                    hourly=u.get("hourly_usage"),
                ),
                use_persian=use_fa,
            )
            self.api_feedback_label.setText(msg)
        else:
            self.api_feedback_label.setText(f"✖ {result.get_message(lang)}")

    def _on_clear_cache(self) -> None:
        lang = self.active_language
        reply = QMessageBox.question(
            self,
            tr("dialogs.clear_cache_title", lang=lang),
            tr("dialogs.clear_cache_body", lang=lang),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.cache_store.clear()
            self.refresh_stats_display()
            self.cache_cleared.emit()
            QMessageBox.information(
                self,
                tr("dialogs.clear_cache_done_title", lang=lang),
                tr("dialogs.clear_cache_done_body", lang=lang),
            )

    def _on_reset_local_stats(self) -> None:
        lang = self.active_language
        reply = QMessageBox.warning(
            self,
            tr("dialogs.reset_stats_title", lang=lang),
            tr("dialogs.reset_stats_body", lang=lang),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.budget_manager.reset_local_statistics()
            self.refresh_stats_display()
            self.budget_updated.emit()
            QMessageBox.information(
                self,
                tr("dialogs.reset_stats_done_title", lang=lang),
                tr("dialogs.reset_stats_done_body", lang=lang),
            )

    def _get_effective_dialog_repo(self) -> str:
        raw = self.github_repo_input.text().strip()
        if raw:
            return normalize_github_repo(raw)
        return self.settings_manager.settings.get_effective_github_repo()

    def refresh_about_display(self) -> None:
        lang = self.active_language
        use_fa = (
            bool(self.numerals_combo.currentData())
            if hasattr(self, "numerals_combo") and self.numerals_combo.count() > 0
            else self.settings_manager.settings.use_persian_numerals
        )

        installed_txt = f"v{self.update_service.current_version}"
        self.about_installed_version_value.setText(localize_digits(installed_txt, use_persian=use_fa))

        latest_known = self.update_service.state_store.get_latest_known_version()
        if latest_known:
            self.about_latest_version_value.setText(
                localize_digits(f"v{latest_known}", use_persian=use_fa)
            )
        else:
            self.about_latest_version_value.setText(tr("settings.latest_version_unknown", lang=lang))

        last_check_ts = self.update_service.state_store.get_last_check_timestamp()
        if last_check_ts:
            dt_str = format_localized_datetime(last_check_ts, lang=lang, use_persian=use_fa)
            self.about_last_check_value.setText(dt_str)
        else:
            self.about_last_check_value.setText(tr("settings.last_check_never", lang=lang))

        status = self.update_service.state_store.get_last_check_status()
        if status == "update_available" and latest_known:
            status_txt = tr("updater.status.update_available", lang=lang, version=latest_known)
            self.about_update_status_value.setText(localize_digits(status_txt, use_persian=use_fa))
        elif status == "up_to_date":
            status_txt = tr(
                "updater.status.up_to_date",
                lang=lang,
                version=self.update_service.current_version,
            )
            self.about_update_status_value.setText(localize_digits(status_txt, use_persian=use_fa))
        elif status == "error":
            err = self.update_service.state_store.get_last_error() or ""
            self.about_update_status_value.setText(
                tr("updater.errors.network_error", lang=lang, detail=err or "Error")
            )
        else:
            self.about_update_status_value.setText(tr("updater.status.not_checked", lang=lang))

    def _on_open_github_repo(self) -> None:
        lang = self.active_language
        repo = self._get_effective_dialog_repo()
        url = self.update_service.get_repository_html_url(repo)
        if not url:
            QMessageBox.information(
                self,
                tr("settings.about_group", lang=lang),
                tr("updater.errors.repo_not_configured", lang=lang),
            )
            return
        QDesktopServices.openUrl(QUrl(url))

    def _on_open_release_notes(self) -> None:
        lang = self.active_language
        repo = self._get_effective_dialog_repo()
        url = self.update_service.get_releases_html_url(repo)
        if not url:
            QMessageBox.information(
                self,
                tr("settings.about_group", lang=lang),
                tr("updater.errors.repo_not_configured", lang=lang),
            )
            return
        QDesktopServices.openUrl(QUrl(url))

    def _on_check_for_updates(self) -> None:
        lang = self.active_language
        if self._update_check_worker is not None and self._update_check_worker.isRunning():
            return
        repo = self._get_effective_dialog_repo()
        channel = str(self.update_channel_combo.currentData() or "stable")
        self.check_updates_btn.setEnabled(False)
        self.about_update_status_value.setText(tr("updater.status.checking", lang=lang))

        self._update_check_worker = UpdateCheckWorker(
            service=self.update_service,
            repo=repo,
            channel=channel,
            force=True,
        )
        self._update_check_worker.finished_result.connect(self._on_update_check_finished)
        self._update_check_worker.start()

    def _on_update_check_finished(self, result: UpdateCheckResult) -> None:
        lang = self.active_language
        use_fa = bool(self.numerals_combo.currentData())
        self.check_updates_btn.setEnabled(True)
        self.refresh_about_display()

        msg = localize_digits(result.get_message(lang), use_persian=use_fa)
        self.about_update_status_value.setText(msg)

        if result.checked and result.update_available and result.manifest is not None:
            dlg = UpdateNotificationDialog(
                manifest=result.manifest,
                update_service=self.update_service,
                language=lang,
                theme=str(self.theme_combo.currentData() or "dark"),
                use_persian_numerals=use_fa,
                parent=self,
            )
            dlg.install_confirmed.connect(self.update_install_requested.emit)
            dlg.exec()

    def _on_save(self) -> None:
        chosen_mode = str(self.mode_combo.currentData() or "expanded")
        show_secondary = self.show_secondary_cb.isChecked()
        cur = self.settings_manager.settings
        target_w = cur.window_width
        target_h = cur.window_height

        mode_changed = chosen_mode != getattr(self, "_initial_display_mode", cur.display_mode)
        secondary_changed_at_default_size = (
            chosen_mode == "expanded"
            and show_secondary != getattr(self, "_initial_show_secondary", cur.show_secondary_cards)
            and cur.window_width in (None, 540)
            and cur.window_height in (None, 390, 480)
        )
        if mode_changed or secondary_changed_at_default_size:
            if chosen_mode == "ultra_compact":
                target_w, target_h = 250, 200
            elif chosen_mode == "compact":
                target_w, target_h = 360, 290
            else:
                target_w = 540
                target_h = 480 if show_secondary else 390

        new_settings = AppSettings(
            api_key=self.api_key_input.text().strip(),
            api_provider=str(self.provider_combo.currentData() or "navasan"),
            auto_refresh_enabled=self.auto_refresh_cb.isChecked(),
            refresh_interval_hours=int(self.interval_combo.currentData() or 8),
            language=str(self.language_combo.currentData() or "fa"),
            theme=str(self.theme_combo.currentData() or "dark"),
            opacity=round(self.opacity_slider.value() / 100.0, 2),
            always_on_top=self.always_on_top_cb.isChecked(),
            start_with_windows=self.start_windows_cb.isChecked(),
            use_persian_numerals=bool(self.numerals_combo.currentData()),
            confirm_manual_refresh=self.confirm_manual_cb.isChecked(),
            display_mode=chosen_mode,
            show_chart=self.show_chart_cb.isChecked(),
            show_secondary_cards=show_secondary,
            window_x=cur.window_x,
            window_y=cur.window_y,
            window_width=target_w,
            window_height=target_h,
            github_repo=normalize_github_repo(self.github_repo_input.text()),
            auto_check_updates=self.auto_check_updates_cb.isChecked(),
            auto_download_updates=self.auto_download_updates_cb.isChecked(),
            update_channel=str(self.update_channel_combo.currentData() or "stable"),
            update_startup_delay_seconds=cur.update_startup_delay_seconds,
            update_check_interval_hours=cur.update_check_interval_hours,
        )
        new_settings.validate_and_clamp()
        self.settings_manager.save(new_settings)
        SettingsManager.apply_windows_startup(new_settings.start_with_windows)
        self.settings_saved.emit(new_settings)
        self.accept()

    def apply_theme(self, theme: str) -> None:
        pal = get_palette(theme)
        title_pos = "top right" if is_rtl(self.active_language) else "top left"
        accent = pal.accent_gold if theme == "dark" else pal.accent_blue
        self.setStyleSheet(
            f"""
            QDialog {{
                background-color: {pal.window_bg_start};
                color: {pal.text_primary};
                font-family: {FONT_FAMILY_STACK};
                font-size: 12px;
            }}
            QTabWidget::pane {{
                border: 1px solid {pal.card_border};
                border-radius: 10px;
                background-color: {pal.window_bg_start};
                padding: 4px;
            }}
            QTabBar::tab {{
                background-color: {pal.card_bg_end};
                color: {pal.text_secondary};
                border: 1px solid {pal.card_border};
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                padding: 6px 14px;
                margin-right: 4px;
                font-weight: 600;
            }}
            QTabBar::tab:selected {{
                background-color: {pal.card_bg_start};
                color: {accent};
            }}
            QGroupBox {{
                background-color: {pal.card_bg_end};
                border: 1px solid {pal.card_border};
                border-radius: 10px;
                margin-top: 12px;
                padding: 12px 10px 10px 10px;
                color: {pal.text_primary};
                font-weight: 700;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: {title_pos};
                padding: 0 8px;
                color: {accent};
            }}
            QLabel {{
                color: {pal.text_primary};
                font-family: {FONT_FAMILY_STACK};
            }}
            QCheckBox {{
                color: {pal.text_primary};
                spacing: 6px;
            }}
            QLineEdit, QComboBox {{
                background-color: {pal.window_bg_end};
                color: {pal.text_primary};
                border: 1px solid {pal.card_border};
                border-radius: 6px;
                padding: 5px 8px;
                font-family: {FONT_FAMILY_STACK};
            }}
            QPushButton {{
                background-color: {pal.card_bg_start};
                color: {pal.text_primary};
                border: 1px solid {pal.card_border};
                border-radius: 8px;
                padding: 6px 12px;
                font-weight: 600;
                font-family: {FONT_FAMILY_STACK};
            }}
            QPushButton:hover {{
                border: 1px solid {pal.card_hover_border};
                background-color: {pal.badge_bg};
            }}
            QPushButton:disabled {{
                color: {pal.text_muted};
            }}
            """
        )
        self.disclaimer_label.setStyleSheet(
            f"color: {pal.text_secondary}; font-size: 11px;"
        )
        self.masked_key_hint.setStyleSheet(
            f"color: {pal.text_secondary}; font-size: 11px;"
        )
        self.api_feedback_label.setStyleSheet(
            f"color: {accent}; font-weight: 600;"
        )
        self.about_update_status_value.setStyleSheet(
            f"color: {accent}; font-weight: 600;"
        )
