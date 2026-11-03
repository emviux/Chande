"""Bilingual (Persian/English) non-intrusive Update Notification & Download dialog for Chande."""

from __future__ import annotations
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from config.constants import APP_VERSION
from services.formatter import localize_digits
from services.i18n import get_layout_direction, is_rtl, tr
from ui.theme import FONT_FAMILY_STACK, get_palette
from ui.worker import UpdateDownloadWorker
from updater.github_updater import (
    DownloadStageResult,
    GitHubUpdateService,
    ReleaseManifest,
    is_trusted_https_github_url,
)


def _format_bytes(num_bytes: int, use_persian: bool = False) -> str:
    if num_bytes <= 0:
        txt = "0 KB"
    elif num_bytes < 1024 * 1024:
        txt = f"{num_bytes / 1024:.1f} KB"
    else:
        txt = f"{num_bytes / (1024 * 1024):.2f} MB"
    return localize_digits(txt, use_persian=use_persian)


class UpdateNotificationDialog(QDialog):
    """Bilingual notification dialog for downloading, verifying, and installing Chande updates."""

    install_confirmed = Signal(object)  # emits Path to verified staging_dir
    update_postponed = Signal(str)      # emits postponed version string

    def __init__(
        self,
        manifest: ReleaseManifest,
        update_service: GitHubUpdateService,
        installed_version: Optional[str] = None,
        lang: Optional[str] = None,
        language: Optional[str] = None,
        theme: str = "dark",
        use_persian_numerals: Optional[bool] = None,
        staged_dir: Optional[Path] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Tool)
        self.manifest = manifest
        self.update_service = update_service
        self.installed_version = installed_version or update_service.current_version or APP_VERSION
        chosen_lang = language or lang or "fa"
        self._lang = "en" if str(chosen_lang).lower().startswith("en") else "fa"
        self._theme = theme
        self._use_persian = (
            bool(use_persian_numerals)
            if use_persian_numerals is not None
            else (self._lang == "fa")
        )
        self._staged_dir: Optional[Path] = staged_dir
        self._download_worker: Optional[UpdateDownloadWorker] = None

        self.setMinimumWidth(440)
        self.setMinimumHeight(310)
        self._build_ui()
        self.retranslate_ui(self._lang, self._use_persian)
        self.apply_theme(self._theme)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        self.title_label = QLabel()
        self.title_label.setObjectName("updateTitleLabel")
        self.title_label.setWordWrap(True)
        self.header_label = self.title_label
        root.addWidget(self.title_label)

        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("updateSubtitleLabel")
        self.subtitle_label.setWordWrap(True)
        root.addWidget(self.subtitle_label)

        ver_frame = QFrame()
        ver_frame.setObjectName("versionInfoFrame")
        ver_layout = QVBoxLayout(ver_frame)
        ver_layout.setContentsMargins(12, 8, 12, 8)
        ver_layout.setSpacing(4)

        self.installed_ver_label = QLabel()
        self.latest_ver_label = QLabel()
        self.current_ver_val = self.installed_ver_label
        self.new_ver_val = self.latest_ver_label
        ver_layout.addWidget(self.installed_ver_label)
        ver_layout.addWidget(self.latest_ver_label)
        root.addWidget(ver_frame)

        self.whats_new_label = QLabel()
        root.addWidget(self.whats_new_label)

        self.notes_browser = QTextBrowser()
        self.notes_browser.setOpenExternalLinks(True)
        self.notes_browser.setMinimumHeight(100)
        root.addWidget(self.notes_browser, 1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.release_notes_btn = QPushButton()
        self.release_notes_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.release_notes_btn.clicked.connect(self._on_open_release_notes)
        btn_row.addWidget(self.release_notes_btn)

        btn_row.addStretch(1)

        self.later_btn = QPushButton()
        self.later_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.later_btn.clicked.connect(self._on_later_clicked)
        btn_row.addWidget(self.later_btn)

        self.download_install_btn = QPushButton()
        self.download_install_btn.setObjectName("primaryActionBtn")
        self.download_install_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_install_btn.setDefault(True)
        self.download_install_btn.clicked.connect(self._on_download_or_install_clicked)
        btn_row.addWidget(self.download_install_btn)

        root.addLayout(btn_row)

    def retranslate_ui(self, lang: str, use_persian: Optional[bool] = None) -> None:
        norm_lang = "en" if str(lang).lower().startswith("en") else "fa"
        self._lang = norm_lang
        if use_persian is not None:
            self._use_persian = bool(use_persian)

        self.setWindowTitle(tr("updater.notification_title", lang=norm_lang))
        self.setLayoutDirection(get_layout_direction(norm_lang))

        self.title_label.setText(tr("updater.notification_title", lang=norm_lang))
        self.subtitle_label.setText(tr("updater.notification_subtitle", lang=norm_lang))

        inst_txt = tr("updater.installed_version", lang=norm_lang, version=self.installed_version)
        lat_txt = tr("updater.latest_version", lang=norm_lang, version=self.manifest.version)
        self.installed_ver_label.setText(localize_digits(inst_txt, use_persian=self._use_persian))
        self.latest_ver_label.setText(localize_digits(lat_txt, use_persian=self._use_persian))

        self.whats_new_label.setText(tr("updater.whats_new", lang=norm_lang))
        notes_text = (self.manifest.body or "").strip()
        if not notes_text:
            notes_text = tr("updater.no_release_notes", lang=norm_lang)
        self.notes_browser.setPlainText(notes_text)

        self.release_notes_btn.setText(tr("updater.release_notes_btn", lang=norm_lang))
        self.release_notes_btn.setEnabled(bool(self.manifest.html_url))
        self.later_btn.setText(tr("updater.later_btn", lang=norm_lang))

        if self._staged_dir and self._staged_dir.is_dir():
            self.download_install_btn.setText(tr("updater.install_now_btn", lang=norm_lang))
        else:
            self.download_install_btn.setText(tr("updater.download_and_install_btn", lang=norm_lang))

    def show_without_focus(self) -> None:
        """Show the update notification without stealing focus from the user's active application."""
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setModal(False)
        self.show()

    def _on_open_release_notes(self) -> None:
        if self.manifest.html_url and is_trusted_https_github_url(self.manifest.html_url):
            QDesktopServices.openUrl(QUrl(self.manifest.html_url))

    def _on_later_clicked(self) -> None:
        self.update_service.state_store.postpone_version(self.manifest.version, hours=24.0)
        self.update_postponed.emit(self.manifest.version)
        self.reject()

    def _on_download_or_install_clicked(self) -> None:
        if self._staged_dir and self._staged_dir.is_dir():
            self._prompt_restart_and_install(self._staged_dir)
            return

        self.download_install_btn.setEnabled(False)
        self.later_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.status_label.setText(
            localize_digits(
                tr("updater.status.downloading", lang=self._lang, version=self.manifest.version),
                use_persian=self._use_persian,
            )
        )
        self.status_label.show()

        self._download_worker = UpdateDownloadWorker(
            update_service=self.update_service,
            manifest=self.manifest,
        )
        self._download_worker.progress_changed.connect(self._on_download_progress)
        self._download_worker.finished_result.connect(self._on_download_finished)
        self._download_worker.start()

    def _on_download_progress(self, downloaded: int, total: int, percent: int) -> None:
        self.progress_bar.setValue(percent)
        if percent >= 100:
            self.status_label.setText(tr("updater.verifying_checksum", lang=self._lang))
        else:
            msg = tr(
                "updater.downloading_progress",
                lang=self._lang,
                percent=localize_digits(str(percent), use_persian=self._use_persian),
                downloaded=_format_bytes(downloaded, use_persian=self._use_persian),
                total=_format_bytes(total, use_persian=self._use_persian),
            )
            self.status_label.setText(msg)

    def _on_download_finished(self, result: DownloadStageResult) -> None:
        self.later_btn.setEnabled(True)
        self.download_install_btn.setEnabled(True)

        if not result.success or result.staging_dir is None:
            self.status_label.setText(f"✖ {result.get_message(self._lang)}")
            self.status_label.show()
            return

        self._staged_dir = result.staging_dir
        self.progress_bar.setValue(100)
        self.status_label.setText(
            localize_digits(
                f"✔ {result.get_message(self._lang)}",
                use_persian=self._use_persian,
            )
        )
        self.download_install_btn.setText(tr("updater.install_now_btn", lang=self._lang))
        self._prompt_restart_and_install(result.staging_dir)

    def _prompt_restart_and_install(self, staging_dir: Path) -> None:
        title = tr("updater.confirm_restart_title", lang=self._lang)
        body = localize_digits(
            tr("updater.confirm_restart_body", lang=self._lang, version=self.manifest.version),
            use_persian=self._use_persian,
        )
        reply = QMessageBox.question(
            self,
            title,
            body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.install_confirmed.emit(staging_dir)
            self.accept()

    def apply_theme(self, theme: str) -> None:
        self._theme = theme
        pal = get_palette(theme)
        accent = pal.accent_gold if theme == "dark" else pal.accent_blue
        align = "right" if is_rtl(self._lang) else "left"
        self.setStyleSheet(
            f"""
            QDialog {{
                background-color: {pal.window_bg_start};
                color: {pal.text_primary};
                font-family: {FONT_FAMILY_STACK};
                font-size: 12px;
            }}
            QLabel {{
                color: {pal.text_primary};
                text-align: {align};
            }}
            QLabel#updateTitleLabel {{
                font-size: 15px;
                font-weight: 700;
                color: {accent};
            }}
            QLabel#updateSubtitleLabel {{
                font-size: 12px;
                color: {pal.text_secondary};
            }}
            QFrame#versionInfoFrame {{
                background-color: {pal.card_bg_end};
                border: 1px solid {pal.card_border};
                border-radius: 8px;
            }}
            QTextBrowser {{
                background-color: {pal.card_bg_end};
                color: {pal.text_primary};
                border: 1px solid {pal.card_border};
                border-radius: 6px;
                padding: 8px;
            }}
            QProgressBar {{
                background-color: {pal.card_bg_end};
                color: {pal.text_primary};
                border: 1px solid {pal.card_border};
                border-radius: 6px;
                text-align: center;
                min-height: 18px;
            }}
            QProgressBar::chunk {{
                background-color: {accent};
                border-radius: 5px;
            }}
            QPushButton {{
                background-color: {pal.card_bg_start};
                color: {pal.text_primary};
                border: 1px solid {pal.card_border};
                border-radius: 6px;
                padding: 6px 14px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background-color: {pal.badge_bg};
                border-color: {accent};
            }}
            QPushButton#primaryActionBtn {{
                background-color: {accent};
                color: #09131C;
                border: none;
            }}
            """
        )
