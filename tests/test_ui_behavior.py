"""UI and integration tests verifying zero-unintended-API-call guarantees and widget states."""

from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

from config.settings import AppSettings, SettingsManager
from services.api_client import MarketApiClient
from services.budget_manager import RequestBudgetManager
from storage.cache_store import CacheStore
from ui.main_window import MainWindow
from ui.settings_dialog import SettingsDialog


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


def _create_window_env(tmp_path: Path, auto_refresh: bool = False, pre_seed_cache: bool = True):
    cfg_file = tmp_path / "user_config.json"
    cache_file = tmp_path / "market_cache.json"
    budget_file = tmp_path / "request_budget.json"

    sm = SettingsManager(config_path=cfg_file)
    sm.save(
        AppSettings(
            api_key="secret_test_key_9999",
            api_provider="navasan",
            auto_refresh_enabled=auto_refresh,
            refresh_interval_hours=8,
            confirm_manual_refresh=False,
            use_persian_numerals=False,
        )
    )
    cs = CacheStore(cache_path=cache_file)
    if pre_seed_cache:
        cs.update_success(_sample_payload(), fetched_at=1730000000.0)

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
        enable_tray=False,
        auto_start_schedule=False,
    )
    return win, sm, cs, bm, mock_session


def test_offline_startup_displays_cached_prices_immediately(qapp, tmp_path: Path):
    """Startup must load and render cached prices immediately with zero network requests."""
    win, _, _, bm, mock_session = _create_window_env(
        tmp_path, auto_refresh=False, pre_seed_cache=True
    )
    assert mock_session.get.call_count == 0
    assert bm.get_status().monthly_total_used == 0

    assert win.primary_cards["usd_sell"].price_label.text() == "88,500"
    assert win.primary_cards["18ayar"].price_label.text() == "4,520,000"
    assert win.primary_cards["btc"].price_label.text() == "6,150,000,000"
    assert win.primary_cards["usdt"].price_label.text() == "88,950"
    win.close()


def test_ui_events_never_trigger_api_requests(qapp, tmp_path: Path):
    """Resizing, repainting, changing themes, toggling compact mode, tray restore, and settings must issue 0 API calls."""
    win, sm, cs, bm, mock_session = _create_window_env(
        tmp_path, auto_refresh=True, pre_seed_cache=True
    )
    win.show()

    # 1. Resize window multiple times
    win.resize(QSize(600, 520))
    win.resize(QSize(480, 420))

    # 2. Repaint widget
    win.repaint()
    for card in win.primary_cards.values():
        card.repaint()

    # 3. Change theme (dark -> light -> dark)
    win.apply_theme("light")
    win.apply_theme("dark")

    # 4. Toggle display mode (expanded -> compact -> expanded)
    win.toggle_display_mode()
    assert win.settings_manager.settings.display_mode == "compact"
    win.toggle_display_mode()
    assert win.settings_manager.settings.display_mode == "expanded"

    # 5. Toggle chart visibility
    win.chart_toggle_btn.click()
    win.chart_toggle_btn.click()

    # 6. Simulate hide and restore from system tray
    win._toggle_visibility_from_tray()
    win._toggle_visibility_from_tray()

    # 7. Instantiate and save SettingsDialog
    dlg = SettingsDialog(
        settings_manager=sm,
        budget_manager=bm,
        cache_store=cs,
        api_client=win.api_client,
        parent=win,
    )
    dlg._on_save()

    assert mock_session.get.call_count == 0
    assert bm.get_status().monthly_total_used == 0
    win.close()


def test_settings_dialog_masks_api_key(qapp, tmp_path: Path):
    """SettingsDialog must keep the API key masked (`Password` echo mode) and never expose it in labels."""
    win, sm, cs, bm, _ = _create_window_env(tmp_path)
    raw_secret = "secret_test_key_9999"
    dlg = SettingsDialog(
        settings_manager=sm,
        budget_manager=bm,
        cache_store=cs,
        api_client=win.api_client,
        parent=win,
    )
    assert dlg.api_key_input.echoMode() == QLineEdit.EchoMode.Password
    assert raw_secret not in dlg.masked_key_hint.text()
    assert "***" in dlg.masked_key_hint.text()
    dlg.close()
    win.close()


def test_manual_refresh_confirmation_and_reserve_warning(qapp, tmp_path: Path):
    """When confirm_manual_refresh is enabled or in reserve, cancelling the dialog issues 0 requests."""
    win, sm, _, bm, mock_session = _create_window_env(tmp_path)
    sm.update(confirm_manual_refresh=True)

    with patch.object(
        QMessageBox, "question", return_value=QMessageBox.StandardButton.No
    ) as mock_q:
        win.on_manual_refresh_clicked()
        assert mock_q.call_count == 1
        assert mock_session.get.call_count == 0
        assert bm.get_status().monthly_total_used == 0

    win.close()


def test_budget_exhausted_blocks_ui_refresh_and_preserves_cache(qapp, tmp_path: Path):
    """When 120 monthly requests are exhausted, the UI disables refresh and preserves cached prices."""
    win, _, _, bm, mock_session = _create_window_env(
        tmp_path, auto_refresh=False, pre_seed_cache=True
    )
    # Exhaust all 120 requests
    for _ in range(120):
        ok, _, idx = bm.acquire_and_record_dispatch(kind="manual")
        assert ok
        bm.complete_dispatch(idx, success=True, status_code=200)

    win._update_footer_and_freshness()
    assert not win.refresh_btn.isEnabled()

    # Even calling handler directly must not issue a request
    win.on_manual_refresh_clicked()
    assert mock_session.get.call_count == 0
    assert win._current_state == "exhausted"
    # Cached prices remain intact
    assert win.primary_cards["usd_sell"].price_label.text() == "88,500"
    win.close()
