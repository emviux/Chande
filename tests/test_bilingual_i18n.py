"""Comprehensive tests for Full Persian (Farsi) and English Bilingual UI Support."""

from __future__ import annotations
import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from config.constants import (
    REQUIRED_SYMBOLS,
    TRANSLATIONS_DIR,
)
from config.settings import AppSettings, SettingsManager
from services.api_client import MarketApiClient
from services.budget_manager import RequestBudgetManager
from services.formatter import (
    format_cache_age,
    format_change_text,
    format_localized_datetime,
    format_number,
    localize_unit,
    parse_market_response,
    to_english_numerals,
    to_persian_numerals,
)
from services.i18n import (
    LRI,
    LRM,
    PDI,
    RLM,
    TranslationManager,
    format_bidi_text,
    get_current_language,
    get_layout_direction,
    get_leading_alignment,
    get_trailing_alignment,
    is_rtl,
    set_current_language,
    tr,
    wrap_ltr_token,
)
from storage.cache_store import CacheStore
from ui.main_window import MainWindow
from ui.settings_dialog import SettingsDialog

MODE_EXPANDED = "expanded"
MODE_COMPACT = "compact"
MODE_ULTRA_COMPACT = "ultra_compact"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def reset_default_language():
    """Ensure the global translation language starts and ends in 'fa' for test isolation."""
    set_current_language("fa")
    yield
    set_current_language("fa")


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


def _create_bilingual_env(
    tmp_path: Path,
    language: str = "fa",
    use_persian_numerals: bool | None = None,
    enable_tray: bool = False,
):
    cfg_file = tmp_path / "user_config.json"
    cache_file = tmp_path / "market_cache.json"
    budget_file = tmp_path / "request_budget.json"

    sm = SettingsManager(config_path=cfg_file)
    kwargs = {
        "api_key": "secret_test_key_9999",
        "api_provider": "navasan",
        "auto_refresh_enabled": False,
        "refresh_interval_hours": 8,
        "confirm_manual_refresh": False,
        "language": language,
    }
    if use_persian_numerals is not None:
        kwargs["use_persian_numerals"] = use_persian_numerals
    else:
        kwargs["use_persian_numerals"] = language == "fa"

    sm.save(AppSettings(**kwargs))

    cs = CacheStore(cache_path=cache_file)
    cs.update_success(_sample_payload(), fetched_at=1730000000.0)
    # Seed a second snapshot so mini-graphs have >=2 points in Expanded mode
    cs.update_success(_sample_payload(), fetched_at=1730000300.0)

    bm = RequestBudgetManager(budget_path=budget_file, time_fn=lambda: 1730000600.0)
    mock_session = MagicMock()
    client = MarketApiClient(
        budget_manager=bm,
        cache_store=cs,
        session=mock_session,
        time_fn=lambda: 1730000600.0,
    )
    win = MainWindow(
        settings_manager=sm,
        budget_manager=bm,
        cache_store=cs,
        api_client=client,
        enable_tray=enable_tray,
        auto_start_schedule=False,
    )
    return win, sm, cs, bm, mock_session


def test_1_default_language_initialization(tmp_path: Path):
    """1. Default language initialization must be Persian ('fa') with Persian numerals for new installs."""
    cfg_file = tmp_path / "fresh_config.json"
    sm = SettingsManager(config_path=cfg_file)
    settings = sm.load()

    assert settings.language == "fa"
    assert settings.use_persian_numerals is True

    tm = TranslationManager()
    assert tm.current_language == "fa"


def test_2_switching_between_persian_and_english_immediately(qapp, tmp_path: Path):
    """2. Switching from Persian to English and English to Persian updates UI immediately without restart."""
    win, sm, _, _, _ = _create_bilingual_env(tmp_path, language="fa")
    try:
        # Initially Persian
        assert get_current_language() == "fa"
        assert win.title_label.text() == tr("app.header_title", lang="fa")
        assert win.primary_cards["usd_sell"].name_label.text() == tr("instruments.usd_sell.name", lang="fa")
        assert win.primary_cards["18ayar"].name_label.text() == tr("instruments.18ayar.name", lang="fa")
        assert "بروزرسانی:" in win.last_update_label.text()
        assert "تومان" in win.primary_cards["usd_sell"].unit_label.text()

        # Switch to English via set_language("en")
        win.set_language("en")
        assert get_current_language() == "en"
        assert sm.load().language == "en"
        assert win.title_label.text() == tr("app.header_title", lang="en")
        assert win.primary_cards["usd_sell"].name_label.text() == "US Dollar (Tehran)"
        assert win.primary_cards["18ayar"].name_label.text() == "18K Gold"
        assert win.primary_cards["btc"].name_label.text() == "Bitcoin"
        assert win.primary_cards["usdt"].name_label.text() == "Tether (USDT)"
        assert "Updated:" in win.last_update_label.text()
        assert "Toman" in win.primary_cards["usd_sell"].unit_label.text()

        # Switch back to Persian via toggle_language()
        win.toggle_language()
        assert get_current_language() == "fa"
        assert sm.load().language == "fa"
        assert win.title_label.text() == tr("app.header_title", lang="fa")
        assert win.primary_cards["usd_sell"].name_label.text() == "دلار تهران"
        assert win.primary_cards["18ayar"].name_label.text() == "طلای ۱۸ عیار"
        assert "بروزرسانی:" in win.last_update_label.text()
        assert "تومان" in win.primary_cards["usd_sell"].unit_label.text()
    finally:
        win.close()


def test_3_persist_and_restore_language_across_restarts(qapp, tmp_path: Path):
    """3. Selected language is persisted to disk and restored when a new MainWindow instance starts."""
    win1, sm1, cs, bm, mock_session = _create_bilingual_env(tmp_path, language="fa")
    try:
        win1.set_language("en")
        assert sm1.load().language == "en"
    finally:
        win1.close()

    # Simulate application restart with new SettingsManager and MainWindow reading the same config file
    sm2 = SettingsManager(config_path=tmp_path / "user_config.json")
    loaded = sm2.load()
    assert loaded.language == "en"
    assert loaded.use_persian_numerals is False

    client2 = MarketApiClient(
        budget_manager=bm,
        cache_store=cs,
        session=mock_session,
        time_fn=lambda: 1730000600.0,
    )
    win2 = MainWindow(
        settings_manager=sm2,
        budget_manager=bm,
        cache_store=cs,
        api_client=client2,
        enable_tray=False,
        auto_start_schedule=False,
    )
    try:
        assert get_current_language() == "en"
        assert win2.title_label.text() == tr("app.header_title", lang="en")
        assert win2.primary_cards["usd_sell"].name_label.text() == "US Dollar (Tehran)"
        assert win2.layoutDirection() == Qt.LayoutDirection.LeftToRight
    finally:
        win2.close()


def test_4_translation_completeness_and_codebase_key_coverage():
    """4. Verify fa.json and en.json have identical non-empty keys and cover all tr() calls in code."""
    tm = TranslationManager(translations_dir=TRANSLATIONS_DIR)
    fa_keys = tm.get_all_keys("fa")
    en_keys = tm.get_all_keys("en")

    assert len(fa_keys) > 100
    assert fa_keys == en_keys, f"Key mismatch: fa-en={fa_keys - en_keys}, en-fa={en_keys - fa_keys}"

    for key in sorted(fa_keys):
        fa_val = tm.translate(key, lang="fa")
        en_val = tm.translate(key, lang="en")
        assert isinstance(fa_val, str) and fa_val.strip(), f"Empty fa translation for {key}"
        assert isinstance(en_val, str) and en_val.strip(), f"Empty en translation for {key}"

    # Scan all Python source files for tr("...") literal keys and ensure every one exists in fa.json/en.json
    project_root = Path(__file__).resolve().parent.parent
    py_dirs = ["config", "services", "storage", "ui"]
    referenced_keys: set[str] = set()

    for d in py_dirs:
        for py_file in (project_root / d).glob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "tr"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    referenced_keys.add(node.args[0].value)

    missing_in_catalog = referenced_keys - fa_keys
    assert not missing_in_catalog, f"Code references missing translation keys: {missing_in_catalog}"


def test_5_missing_key_fallback_behavior():
    """5. Missing translation keys never crash the UI and return a clean human-readable fallback."""
    tm = TranslationManager(translations_dir=TRANSLATIONS_DIR)

    # Non-existent key returns humanized tail segment instead of crashing
    fallback = tm.translate("nonexistent.section.custom_warning_label", lang="fa")
    assert fallback == "custom warning label"

    # Formatting error in kwargs falls back safely without raising KeyError
    safe_fmt = tm.translate("footer.last_update", lang="en")  # missing 'age' kwarg
    assert isinstance(safe_fmt, str) and len(safe_fmt) > 0


def test_6_rtl_layout_direction_and_right_alignment_in_persian(qapp, tmp_path: Path):
    """6. Persian mode applies Right-to-Left layout direction and right alignment across UI components."""
    win, sm, cs, bm, _ = _create_bilingual_env(tmp_path, language="fa")
    try:
        assert is_rtl("fa") is True
        assert get_layout_direction("fa") == Qt.LayoutDirection.RightToLeft
        assert bool(get_leading_alignment("fa") & Qt.AlignmentFlag.AlignRight)
        assert bool(get_trailing_alignment("fa") & Qt.AlignmentFlag.AlignLeft)

        assert win.layoutDirection() == Qt.LayoutDirection.RightToLeft
        for card in win.primary_cards.values():
            assert card.layoutDirection() == Qt.LayoutDirection.RightToLeft
            assert bool(card.name_label.alignment() & Qt.AlignmentFlag.AlignRight)
            assert bool(card.price_label.alignment() & Qt.AlignmentFlag.AlignRight)

        dlg = SettingsDialog(
            settings_manager=sm,
            budget_manager=bm,
            cache_store=cs,
            api_client=win.api_client,
        )
        try:
            assert dlg.layoutDirection() == Qt.LayoutDirection.RightToLeft
            assert "subcontrol-position: top right" in dlg.styleSheet()
        finally:
            dlg.close()
    finally:
        win.close()


def test_7_ltr_layout_direction_and_left_alignment_in_english(qapp, tmp_path: Path):
    """7. English mode applies Left-to-Right layout direction and left alignment across UI components."""
    win, sm, cs, bm, _ = _create_bilingual_env(tmp_path, language="en")
    try:
        assert is_rtl("en") is False
        assert get_layout_direction("en") == Qt.LayoutDirection.LeftToRight
        assert bool(get_leading_alignment("en") & Qt.AlignmentFlag.AlignLeft)
        assert bool(get_trailing_alignment("en") & Qt.AlignmentFlag.AlignRight)

        assert win.layoutDirection() == Qt.LayoutDirection.LeftToRight
        for card in win.primary_cards.values():
            assert card.layoutDirection() == Qt.LayoutDirection.LeftToRight
            assert bool(card.name_label.alignment() & Qt.AlignmentFlag.AlignLeft)
            assert bool(card.price_label.alignment() & Qt.AlignmentFlag.AlignLeft)

        dlg = SettingsDialog(
            settings_manager=sm,
            budget_manager=bm,
            cache_store=cs,
            api_client=win.api_client,
        )
        try:
            assert dlg.layoutDirection() == Qt.LayoutDirection.LeftToRight
            assert "subcontrol-position: top left" in dlg.styleSheet()
        finally:
            dlg.close()
    finally:
        win.close()


def test_8_mixed_rtl_ltr_bidirectional_text_rendering():
    """8. Mixed RTL/LTR strings (Persian sentences containing tickers/numbers/units) isolate cleanly."""
    token = wrap_ltr_token("+1,200 (+1.35%)")
    assert token.startswith(LRI) and token.endswith(PDI)

    rtl_mixed = format_bidi_text("نرخ لحظه‌ای USD در بازار", lang="fa")
    assert rtl_mixed.startswith(RLM) and "USD" in rtl_mixed

    ltr_mixed = format_bidi_text("Current USD market rate", lang="en")
    assert ltr_mixed.startswith(LRM) and "USD" in ltr_mixed


def test_9_number_currency_percentage_and_date_formatting():
    """9. Verify numerals, currency units, percentage changes, and Jalali/Gregorian dates in both languages."""
    # Numerals
    assert format_number(88500, use_persian=True) == "۸۸,۵۰۰"
    assert format_number(88500, use_persian=False) == "88,500"
    assert to_persian_numerals("1234567890") == "۱۲۳۴۵۶۷۸۹۰"
    assert to_english_numerals("۱۲۳۴۵۶۷۸۹۰") == "1234567890"

    # Currency units
    assert localize_unit("تومان", "fa") == "تومان"
    assert localize_unit("تومان", "en") == "Toman"
    assert localize_unit("تومان / گرم", "fa") == "تومان / گرم"
    assert localize_unit("تومان / گرم", "en") == "Toman / g"
    assert localize_unit("ریال", "en") == "Rial"
    assert localize_unit("دلار", "en") == "USD"

    # Change & percentage formatting
    pos_fa = format_change_text(1200, pct_change=1.25, use_persian=True, lang="fa")
    assert "۱,۲۰۰" in pos_fa and "+" in pos_fa and "(۱.۲۵٪)" in pos_fa
    neg_en = format_change_text(-250, pct_change=-0.5, use_persian=False, lang="en")
    assert "-250" in neg_en and "(0.5%)" in neg_en

    # Cache age formatting
    age_fa = format_cache_age(300, use_persian=True, lang="fa")
    assert "۵ دقیقه پیش" in age_fa
    age_en = format_cache_age(300, use_persian=False, lang="en")
    assert "5 min ago" in age_en

    # Date formatting: Jalali in Persian, Gregorian in English
    jalali_str = format_localized_datetime(
        timestamp=1730000000,
        lang="fa",
        use_persian=True,
    )
    assert "۱۴۰۳" in jalali_str

    gregorian_str = format_localized_datetime(
        timestamp=1730000000,
        lang="en",
        use_persian=False,
    )
    assert "2024-" in gregorian_str and "1403" not in gregorian_str

    # Quotes preserve raw numerical price across languages
    quotes = parse_market_response(_sample_payload())
    usd_q = quotes["usd_sell"]
    assert usd_q.value == 88500.0
    assert usd_q.get_name("fa") == "دلار تهران"
    assert usd_q.get_name("en") == "US Dollar (Tehran)"
    assert usd_q.get_unit("fa") == "تومان"
    assert usd_q.get_unit("en") == "Toman"


def test_10_expanded_compact_and_ultra_compact_layouts_both_languages(qapp, tmp_path: Path):
    """10. Expanded, compact, and ultra-compact layouts work cleanly in both Persian and English."""
    win, _, _, _, _ = _create_bilingual_env(tmp_path, language="fa")
    try:
        win.show()
        qapp.processEvents()

        for lang in ("fa", "en"):
            win.set_language(lang)
            qapp.processEvents()

            # Expanded mode (540x480)
            win.resize(540, 480)
            qapp.processEvents()
            assert win.responsive_mode == MODE_EXPANDED
            for sym in REQUIRED_SYMBOLS:
                card = win.primary_cards[sym]
                assert card.name_label.text() != ""
                assert card.price_label.text() != ""
                assert card.sparkline.isVisible() is True

            # Compact mode (360x290)
            win.resize(360, 290)
            qapp.processEvents()
            assert win.responsive_mode == MODE_COMPACT
            for sym in REQUIRED_SYMBOLS:
                card = win.primary_cards[sym]
                assert card.name_label.isVisible() is True
                assert card.price_label.isVisible() is True
                assert card.sparkline.isVisible() is False
                assert card.sparkline.is_rendering_suspended is True
                assert card.mid_row_layout.spacing() == 0

            # Ultra-compact mode (235x190)
            win.resize(235, 190)
            qapp.processEvents()
            assert win.responsive_mode == MODE_ULTRA_COMPACT
            assert win.footer_frame.isVisible() is False
            for sym in REQUIRED_SYMBOLS:
                card = win.primary_cards[sym]
                assert card.name_label.isVisible() is True
                assert card.price_label.isVisible() is True
                assert card.sparkline.isVisible() is False
                assert card.sparkline.is_rendering_suspended is True
                assert card.mid_row_layout.spacing() == 0
    finally:
        win.close()


def test_11_settings_tray_tooltips_and_error_states_in_both_languages(qapp, tmp_path: Path):
    """11. Settings dialog, tray menu, tooltips, and error/stale states localize completely in fa and en."""
    win, sm, cs, bm, _ = _create_bilingual_env(tmp_path, language="fa", enable_tray=True)
    try:
        # Check Persian tooltips and tray actions
        assert "بروزرسانی" in win.refresh_btn.toolTip()
        assert "تنظیمات" in win.settings_btn.toolTip()
        if getattr(win, "tray_menu", None) is not None:
            assert win.tray_refresh_action.text() == tr("menu.refresh", lang="fa")
            assert win.tray_exit_action.text() == tr("menu.tray_exit", lang="fa")

        # Trigger an error banner in Persian and then switch to English
        win._show_banner(
            tr("errors.network_error", lang="fa", detail="DNS"),
            level="error",
            tr_key="errors.network_error",
            tr_kwargs={"detail": "DNS"},
        )
        assert win.banner_frame.isHidden() is False
        assert "شبکه" in win.banner_label.text()

        # Switch to English and verify banner, tooltips, tray menu, and settings dialog update
        win.set_language("en")
        assert "Network" in win.banner_label.text()
        assert "Refresh prices manually" in win.refresh_btn.toolTip()
        assert "Settings" in win.settings_btn.toolTip()
        if getattr(win, "tray_menu", None) is not None:
            assert win.tray_refresh_action.text() == tr("menu.refresh", lang="en")
            assert win.tray_exit_action.text() == tr("menu.tray_exit", lang="en")

        # Open SettingsDialog in English and switch language inside dialog
        dlg = SettingsDialog(
            settings_manager=sm,
            budget_manager=bm,
            cache_store=cs,
            api_client=win.api_client,
        )
        try:
            assert "Chande Settings" in dlg.windowTitle()
            assert dlg.save_btn.text() == tr("settings.save_btn", lang="en")
            assert dlg.cancel_btn.text() == tr("settings.cancel_btn", lang="en")

            # Switch combo to Persian ("fa") inside SettingsDialog
            idx_fa = dlg.language_combo.findData("fa")
            dlg.language_combo.setCurrentIndex(idx_fa)
            assert dlg.active_language == "fa"
            assert "تنظیمات چنده" in dlg.windowTitle()
            assert dlg.save_btn.text() == tr("settings.save_btn", lang="fa")
            # Changing language to 'fa' in SettingsDialog also syncs numeral default to True
            assert dlg.numerals_combo.currentData() is True
        finally:
            dlg.close()
    finally:
        win.close()


def test_12_language_switching_never_triggers_api_requests(qapp, tmp_path: Path):
    """12. Switching languages repeatedly must use cached data and trigger zero API calls."""
    win, _, _, bm, mock_session = _create_bilingual_env(tmp_path, language="fa")
    try:
        assert bm.get_status().monthly_total_used == 0
        assert mock_session.get.call_count == 0

        for _ in range(5):
            win.set_language("en")
            qapp.processEvents()
            win.set_language("fa")
            qapp.processEvents()
            win.toggle_language()
            qapp.processEvents()

        assert bm.get_status().monthly_total_used == 0
        assert mock_session.get.call_count == 0
    finally:
        win.close()
