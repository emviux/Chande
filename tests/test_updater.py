"""Comprehensive automated tests for the Chande GitHub Releases Auto-Update System.

Covers all 21 required scenarios from the specification using mocked GitHub responses:
1. Current version equals latest version (1.0.0 == 1.0.0)
2. Current version is older (1.0.0 < 1.1.0)
3. Current version is newer (1.2.0 > 1.1.0, never downgrade)
4. Patch version comparison (1.0.1 < 1.0.2)
5. Minor version comparison (1.0.9 < 1.1.0)
6. Major version comparison (1.9.9 < 2.0.0)
7. Pre-release version handling (1.1.0-beta.1 on stable vs beta channel)
8. Missing internet connection
9. GitHub API rate limit or error response
10. Missing release asset
11. Download progress updates
12. Valid SHA-256 checksum verification
13. Invalid SHA-256 checksum rejection
14. Corrupted or incomplete archive rejection
15. Staging directory preparation & Zip-Slip protection
16. Preserving user configuration and API key
17. Preserving request-budget state and cached prices
18. Automatic rollback when update installation fails
19. Manual update check from Settings (About & Updates tab)
20. Postponed update ("Later")
21. Persian and English update UI & zero Navasan API budget consumption
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import zipfile

import pytest
import requests
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from config.settings import AppSettings, SettingsManager, detect_git_remote_repo, normalize_github_repo
from services.api_client import MarketApiClient
from services.budget_manager import RequestBudgetManager
from services.i18n import is_rtl
from storage.cache_store import CacheStore
from ui.main_window import MainWindow
from ui.settings_dialog import SettingsDialog
from ui.update_dialog import UpdateNotificationDialog
from updater.github_updater import (
    GitHubUpdateService,
    ReleaseAsset,
    ReleaseManifest,
    UpdateStateStore,
)
from updater.installer import UpdateInstaller
from updater.version import compare_semver, is_newer_version, parse_semver


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class FakeGitHubResponse:
    """Mock HTTP response for GitHub API and release asset downloads."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        text_data: str = "",
        raw_bytes: bytes = b"",
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text_data
        self._raw_bytes = raw_bytes
        self.headers = headers or {"Content-Length": str(len(raw_bytes))}

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("Invalid JSON")
        return self._json_data

    def iter_content(self, chunk_size: int = 65536):
        stream = io.BytesIO(self._raw_bytes)
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class FakeGitHubSession:
    """Mock requests.Session that records all GET calls and never touches the network."""

    def __init__(self, routes: Optional[Dict[str, Any]] = None) -> None:
        self.routes = routes or {}
        self.calls: List[str] = []

    def get(self, url: str, headers: Optional[Dict[str, str]] = None, timeout: Any = None, stream: bool = False):
        self.calls.append(url)
        handler = self.routes.get(url)
        if callable(handler):
            return handler(url=url, headers=headers, timeout=timeout, stream=stream)
        if isinstance(handler, Exception):
            raise handler
        if handler is not None:
            return handler
        return FakeGitHubResponse(status_code=404, json_data={"message": "Not Found"})


def _build_valid_release_zip(version: str = "1.3.0", nested_root: bool = True) -> bytes:
    """Create an in-memory valid Chande release ZIP containing Chande.exe and VERSION."""
    buf = io.BytesIO()
    prefix = "Chande/" if nested_root else ""
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{prefix}Chande.exe", f"MZ-FAKE-CHANDE-BINARY-{version}".encode("utf-8"))
        zf.writestr(f"{prefix}VERSION", f"{version}\n".encode("utf-8"))
        zf.writestr(f"{prefix}.env.example", b"NAVASAN_API_KEY=your_navasan_api_key_here\n")
        zf.writestr(f"{prefix}translations/fa.json", b'{"app": {"title": "Chande"}}')
        zf.writestr(f"{prefix}translations/en.json", b'{"app": {"title": "Chande"}}')
    return buf.getvalue()


def _make_github_release_payload(
    tag_name: str = "v1.3.0",
    prerelease: bool = False,
    draft: bool = False,
    zip_bytes: Optional[bytes] = None,
    include_sha256_asset: bool = True,
    include_digest_field: bool = False,
    include_body_sha: bool = False,
    override_sha256: Optional[str] = None,
    include_windows_zip: bool = True,
    asset_url: str = "https://github.com/OWNER/Chande/releases/download/v1.3.0/Chande-v1.3.0-Windows-x64.zip",
) -> Dict[str, Any]:
    ver_clean = tag_name.lstrip("v")
    raw_zip = zip_bytes if zip_bytes is not None else _build_valid_release_zip(ver_clean)
    actual_sha = override_sha256 or hashlib.sha256(raw_zip).hexdigest()
    zip_name = f"Chande-{tag_name}-Windows-x64.zip"
    sha_url = f"{asset_url}.sha256"

    assets: List[Dict[str, Any]] = []
    if include_windows_zip:
        asset_entry: Dict[str, Any] = {
            "name": zip_name,
            "browser_download_url": asset_url,
            "size": len(raw_zip),
            "content_type": "application/zip",
        }
        if include_digest_field:
            asset_entry["digest"] = f"sha256:{actual_sha}"
        assets.append(asset_entry)

    if include_sha256_asset:
        assets.append(
            {
                "name": f"{zip_name}.sha256",
                "browser_download_url": sha_url,
                "size": 96,
                "content_type": "text/plain",
            }
        )

    body = f"## Chande {tag_name}\n- Improved performance\n- Added automatic update system"
    if include_body_sha:
        body += f"\n\nSHA256: `{actual_sha}`"

    return {
        "tag_name": tag_name,
        "name": f"Chande {tag_name}",
        "body": body,
        "html_url": f"https://github.com/OWNER/Chande/releases/tag/{tag_name}",
        "published_at": "2026-11-03T12:00:00Z",
        "prerelease": prerelease,
        "draft": draft,
        "assets": assets,
        "_raw_zip": raw_zip,
        "_actual_sha": actual_sha,
        "_sha_url": sha_url,
        "_asset_url": asset_url,
        "_zip_name": zip_name,
    }


# ---------------------------------------------------------------------------
# Scenarios 1–7: Semantic Versioning & Channel Comparison
# ---------------------------------------------------------------------------


def test_1_to_6_semver_comparisons_and_no_downgrade(tmp_path: Path):
    """Scenarios 1-6: SemVer equality, older, newer (no downgrade), patch, minor, and major comparisons."""
    # Scenario 1: Current version equals latest version (1.0.0 == 1.0.0)
    assert compare_semver("1.0.0", "1.0.0") == 0
    assert compare_semver("v1.0.0", "1.0.0") == 0
    assert is_newer_version("1.0.0", "1.0.0") is False

    # Scenario 2: Current version is older (1.0.0 < 1.1.0)
    assert compare_semver("1.0.0", "1.1.0") == -1
    assert is_newer_version("1.0.0", "1.1.0") is True

    # Scenario 3: Current version is newer (1.2.0 > 1.1.0) -> never downgrade
    assert compare_semver("1.2.0", "1.1.0") == 1
    assert is_newer_version("1.2.0", "1.1.0") is False

    # Scenario 4: Patch version comparison (1.0.1 < 1.0.2)
    assert compare_semver("1.0.1", "1.0.2") == -1
    assert is_newer_version("1.0.1", "1.0.2") is True

    # Scenario 5: Minor version comparison (1.0.9 < 1.1.0)
    assert compare_semver("1.0.9", "1.1.0") == -1
    assert is_newer_version("1.0.9", "1.1.0") is True
    # Multi-digit numeric comparison (1.2.0 < 1.10.0, not lexicographical)
    assert is_newer_version("1.2.0", "1.10.0") is True

    # Scenario 6: Major version comparison (1.9.9 < 2.0.0)
    assert compare_semver("1.9.9", "2.0.0") == -1
    assert is_newer_version("1.9.9", "2.0.0") is True

    # Verify via GitHubUpdateService for equal & older remote versions
    payload_same = _make_github_release_payload(tag_name="v1.2.0")
    session = FakeGitHubSession(
        {
            "https://api.github.com/repos/OWNER/Chande/releases/latest": FakeGitHubResponse(
                200, json_data=payload_same
            )
        }
    )
    svc = GitHubUpdateService(
        current_version="1.2.0",
        state_store=UpdateStateStore(tmp_path / "update_state.json"),
        updates_dir=tmp_path / "updates",
        session=session,
    )
    res_same = svc.check_for_updates("OWNER/Chande", force=True)
    assert res_same.checked is True
    assert res_same.update_available is False

    # Remote version is older (1.1.0) than installed (1.2.0) -> no downgrade
    payload_older = _make_github_release_payload(tag_name="v1.1.0")
    session.routes["https://api.github.com/repos/OWNER/Chande/releases/latest"] = FakeGitHubResponse(
        200, json_data=payload_older
    )
    res_older = svc.check_for_updates("OWNER/Chande", force=True)
    assert res_older.checked is True
    assert res_older.update_available is False


def test_7_prerelease_version_handling_stable_vs_beta(tmp_path: Path):
    """Scenario 7: Pre-release versions (e.g. 1.3.0-beta.1) ignored on stable channel, accepted on beta."""
    # SemVer precedence: 1.3.0-beta.1 < 1.3.0, and 1.3.0-beta.1 < 1.3.0-beta.2
    v_beta1 = parse_semver("v1.3.0-beta.1")
    assert v_beta1 is not None and v_beta1.is_prerelease is True
    assert is_newer_version("1.2.0", "1.3.0-beta.1", allow_prerelease=False) is False
    assert is_newer_version("1.2.0", "1.3.0-beta.1", allow_prerelease=True) is True
    assert is_newer_version("1.3.0-beta.1", "1.3.0-beta.2", allow_prerelease=True) is True
    assert is_newer_version("1.3.0-beta.2", "1.3.0", allow_prerelease=True) is True

    beta_payload = _make_github_release_payload(tag_name="v1.3.0-beta.1", prerelease=True)
    stable_payload = _make_github_release_payload(tag_name="v1.2.0", prerelease=False)

    session = FakeGitHubSession(
        {
            "https://api.github.com/repos/OWNER/Chande/releases/latest": FakeGitHubResponse(
                200, json_data=beta_payload
            ),
            "https://api.github.com/repos/OWNER/Chande/releases?per_page=10": FakeGitHubResponse(
                200, json_data=[beta_payload, stable_payload]
            ),
        }
    )
    svc = GitHubUpdateService(
        current_version="1.2.0",
        state_store=UpdateStateStore(tmp_path / "update_state.json"),
        updates_dir=tmp_path / "updates",
        session=session,
    )

    # On stable channel: beta is skipped, falls back to stable 1.2.0 -> up to date
    res_stable = svc.check_for_updates("OWNER/Chande", channel="stable", force=True)
    assert res_stable.update_available is False

    # On beta channel: 1.3.0-beta.1 is offered as an update
    res_beta = svc.check_for_updates("OWNER/Chande", channel="beta", force=True)
    assert res_beta.update_available is True
    assert res_beta.manifest is not None
    assert res_beta.manifest.version == "1.3.0-beta.1"
    assert res_beta.manifest.prerelease is True


# ---------------------------------------------------------------------------
# Scenarios 8–10: Network Errors, Rate Limits, and Missing Release Assets
# ---------------------------------------------------------------------------


def test_8_to_10_network_failure_rate_limit_and_missing_asset(tmp_path: Path):
    """Scenarios 8-10: Offline/network failure, GitHub 403 rate limit, and missing Windows asset."""
    state_store = UpdateStateStore(tmp_path / "update_state.json")

    # Scenario 8: Missing internet connection
    offline_session = FakeGitHubSession(
        {
            "https://api.github.com/repos/OWNER/Chande/releases/latest": requests.RequestException(
                "Connection aborted"
            )
        }
    )
    svc_offline = GitHubUpdateService(
        current_version="1.2.0",
        state_store=state_store,
        updates_dir=tmp_path / "updates",
        session=offline_session,
    )
    res_offline = svc_offline.check_for_updates("OWNER/Chande", force=True)
    assert res_offline.checked is False
    assert res_offline.update_available is False
    assert res_offline.error_code == "network_error"
    assert state_store.get_last_check_status() == "error"

    # Scenario 9: GitHub API rate limit (403) or error response (500)
    rate_limit_session = FakeGitHubSession(
        {
            "https://api.github.com/repos/OWNER/Chande/releases/latest": FakeGitHubResponse(
                status_code=403, json_data={"message": "API rate limit exceeded"}
            )
        }
    )
    svc_rl = GitHubUpdateService(
        current_version="1.2.0",
        state_store=state_store,
        updates_dir=tmp_path / "updates",
        session=rate_limit_session,
    )
    res_rl = svc_rl.check_for_updates("OWNER/Chande", force=True)
    assert res_rl.checked is False
    assert res_rl.update_available is False
    assert res_rl.error_code == "rate_limit"

    # Scenario 10: Release exists (v1.3.0) but has no Windows release asset
    no_asset_payload = _make_github_release_payload(
        tag_name="v1.3.0",
        include_windows_zip=False,
        include_sha256_asset=False,
    )
    no_asset_session = FakeGitHubSession(
        {
            "https://api.github.com/repos/OWNER/Chande/releases/latest": FakeGitHubResponse(
                status_code=200, json_data=no_asset_payload
            )
        }
    )
    svc_no_asset = GitHubUpdateService(
        current_version="1.2.0",
        state_store=state_store,
        updates_dir=tmp_path / "updates",
        session=no_asset_session,
    )
    res_no_asset = svc_no_asset.check_for_updates("OWNER/Chande", force=True)
    assert res_no_asset.checked is True
    assert res_no_asset.update_available is False
    assert res_no_asset.error_code == "missing_asset"


# ---------------------------------------------------------------------------
# Scenarios 11–15: Download Progress, SHA-256 Verification, Corrupt Archive, Staging & Zip-Slip
# ---------------------------------------------------------------------------


def test_11_and_12_download_progress_and_valid_sha256_verification(tmp_path: Path):
    """Scenarios 11 & 12: Download progress callback updates and valid SHA-256 checksum verification."""
    payload = _make_github_release_payload(tag_name="v1.3.0")
    raw_zip = payload["_raw_zip"]
    actual_sha = payload["_actual_sha"]
    asset_url = payload["_asset_url"]
    sha_url = payload["_sha_url"]
    zip_name = payload["_zip_name"]

    session = FakeGitHubSession(
        {
            "https://api.github.com/repos/OWNER/Chande/releases/latest": FakeGitHubResponse(
                200, json_data=payload
            ),
            sha_url: FakeGitHubResponse(
                200, text_data=f"{actual_sha}  {zip_name}\n"
            ),
            asset_url: FakeGitHubResponse(
                200, raw_bytes=raw_zip
            ),
        }
    )
    svc = GitHubUpdateService(
        current_version="1.2.0",
        state_store=UpdateStateStore(tmp_path / "update_state.json"),
        updates_dir=tmp_path / "updates",
        session=session,
    )

    check_res = svc.check_for_updates("OWNER/Chande", force=True)
    assert check_res.update_available is True
    assert check_res.manifest is not None
    assert check_res.manifest.expected_sha256 == actual_sha

    progress_events: List[tuple[int, int]] = []
    stage_res = svc.download_and_stage_update(
        check_res.manifest,
        progress_callback=lambda dl, total: progress_events.append((dl, total)),
    )
    assert stage_res.success is True
    assert stage_res.sha256 == actual_sha
    assert len(progress_events) >= 1
    assert progress_events[-1][0] == len(raw_zip)
    assert progress_events[-1][1] == len(raw_zip)
    assert stage_res.staging_dir is not None
    assert (stage_res.staging_dir / "Chande.exe").exists()
    assert (stage_res.staging_dir / "VERSION").read_text(encoding="utf-8").strip() == "1.3.0"


def test_13_invalid_sha256_checksum_rejected(tmp_path: Path):
    """Scenario 13: Downloaded archive with mismatched SHA-256 is rejected and deleted."""
    wrong_sha = "a" * 64
    payload = _make_github_release_payload(tag_name="v1.3.0", override_sha256=wrong_sha)
    raw_zip = payload["_raw_zip"]
    asset_url = payload["_asset_url"]
    sha_url = payload["_sha_url"]
    zip_name = payload["_zip_name"]

    session = FakeGitHubSession(
        {
            "https://api.github.com/repos/OWNER/Chande/releases/latest": FakeGitHubResponse(
                200, json_data=payload
            ),
            sha_url: FakeGitHubResponse(200, text_data=f"{wrong_sha} *{zip_name}\n"),
            asset_url: FakeGitHubResponse(200, raw_bytes=raw_zip),
        }
    )
    svc = GitHubUpdateService(
        current_version="1.2.0",
        state_store=UpdateStateStore(tmp_path / "update_state.json"),
        updates_dir=tmp_path / "updates",
        session=session,
    )
    check_res = svc.check_for_updates("OWNER/Chande", force=True)
    assert check_res.manifest is not None

    stage_res = svc.download_and_stage_update(check_res.manifest)
    assert stage_res.success is False
    assert stage_res.error_code == "checksum_mismatch"
    # Corrupted/unverified file must be removed
    assert not (tmp_path / "updates" / zip_name).exists()


def test_14_corrupted_or_incomplete_archive_rejected(tmp_path: Path):
    """Scenario 14: Corrupted ZIP or archive missing Chande.exe is rejected before installation."""
    corrupt_bytes = b"NOT-A-VALID-ZIP-ARCHIVE-BYTES"
    corrupt_sha = hashlib.sha256(corrupt_bytes).hexdigest()
    payload = _make_github_release_payload(
        tag_name="v1.3.0",
        zip_bytes=corrupt_bytes,
        override_sha256=corrupt_sha,
    )
    session = FakeGitHubSession(
        {
            "https://api.github.com/repos/OWNER/Chande/releases/latest": FakeGitHubResponse(
                200, json_data=payload
            ),
            payload["_sha_url"]: FakeGitHubResponse(200, text_data=corrupt_sha),
            payload["_asset_url"]: FakeGitHubResponse(200, raw_bytes=corrupt_bytes),
        }
    )
    svc = GitHubUpdateService(
        current_version="1.2.0",
        state_store=UpdateStateStore(tmp_path / "update_state.json"),
        updates_dir=tmp_path / "updates",
        session=session,
    )
    check_res = svc.check_for_updates("OWNER/Chande", force=True)
    assert check_res.manifest is not None

    stage_res = svc.download_and_stage_update(check_res.manifest)
    assert stage_res.success is False
    assert stage_res.error_code == "corrupt_archive"

    # Also test a valid ZIP that does NOT contain Chande.exe (incomplete package)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.txt", "incomplete package without executable")
    incomplete_zip = buf.getvalue()
    incomplete_sha = hashlib.sha256(incomplete_zip).hexdigest()
    payload2 = _make_github_release_payload(
        tag_name="v1.3.1",
        zip_bytes=incomplete_zip,
        override_sha256=incomplete_sha,
        asset_url="https://github.com/OWNER/Chande/releases/download/v1.3.1/Chande-v1.3.1-Windows-x64.zip",
    )
    session.routes["https://api.github.com/repos/OWNER/Chande/releases/latest"] = FakeGitHubResponse(
        200, json_data=payload2
    )
    session.routes[payload2["_sha_url"]] = FakeGitHubResponse(200, text_data=incomplete_sha)
    session.routes[payload2["_asset_url"]] = FakeGitHubResponse(200, raw_bytes=incomplete_zip)

    check_res2 = svc.check_for_updates("OWNER/Chande", force=True)
    stage_res2 = svc.download_and_stage_update(check_res2.manifest)
    assert stage_res2.success is False
    assert stage_res2.error_code == "corrupt_archive"


def test_15_staging_directory_and_zip_slip_protection(tmp_path: Path):
    """Scenario 15: Zip-Slip path traversal entries (../) and non-GitHub URLs are rejected."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.exe", b"MALICIOUS")
        zf.writestr("Chande/Chande.exe", b"MZ")
    evil_zip = buf.getvalue()
    evil_sha = hashlib.sha256(evil_zip).hexdigest()

    payload = _make_github_release_payload(
        tag_name="v1.3.0",
        zip_bytes=evil_zip,
        override_sha256=evil_sha,
    )
    session = FakeGitHubSession(
        {
            "https://api.github.com/repos/OWNER/Chande/releases/latest": FakeGitHubResponse(
                200, json_data=payload
            ),
            payload["_sha_url"]: FakeGitHubResponse(200, text_data=evil_sha),
            payload["_asset_url"]: FakeGitHubResponse(200, raw_bytes=evil_zip),
        }
    )
    svc = GitHubUpdateService(
        current_version="1.2.0",
        state_store=UpdateStateStore(tmp_path / "update_state.json"),
        updates_dir=tmp_path / "updates",
        session=session,
    )
    check_res = svc.check_for_updates("OWNER/Chande", force=True)
    stage_res = svc.download_and_stage_update(check_res.manifest)
    assert stage_res.success is False
    assert stage_res.error_code == "corrupt_archive"
    assert not (tmp_path / "evil.exe").exists()

    # Also verify non-HTTPS or non-GitHub download URLs are rejected
    assert GitHubUpdateService.is_allowed_download_url("http://github.com/OWNER/Chande/a.zip") is False
    assert GitHubUpdateService.is_allowed_download_url("https://evil.example.com/Chande.zip") is False
    assert (
        GitHubUpdateService.is_allowed_download_url(
            "https://github.com/OWNER/Chande/releases/download/v1.2.0/Chande.zip"
        )
        is True
    )


# ---------------------------------------------------------------------------
# Scenarios 16–18: Safe Installation, Preserving User Data/Budget/Cache & Automatic Rollback
# ---------------------------------------------------------------------------


def test_16_and_17_preserve_user_config_api_key_budget_and_cache_across_update(tmp_path: Path):
    """Scenarios 16 & 17: Updating replaces application files while preserving API key, settings, budget, and cache."""
    install_dir = tmp_path / "ChandeInstall"
    install_dir.mkdir(parents=True)

    # Existing v1.2.0 installation
    (install_dir / "Chande.exe").write_bytes(b"OLD-BINARY-1.2.0")
    (install_dir / "VERSION").write_text("1.2.0\n", encoding="utf-8")
    (install_dir / ".env").write_text("NAVASAN_API_KEY=env_secret_key_999\n", encoding="utf-8")

    data_dir = install_dir / "data"
    data_dir.mkdir(parents=True)
    user_cfg_path = data_dir / "user_config.json"
    budget_path = data_dir / "request_budget.json"
    cache_path = data_dir / "market_cache.json"

    user_cfg_content = {
        "api_key": "user_saved_navasan_key_12345",
        "language": "fa",
        "theme": "dark",
        "window_x": 420,
        "window_y": 180,
        "window_width": 360,
        "window_height": 290,
        "display_mode": "compact",
    }
    budget_content = {
        "current_month": "2026-11",
        "monthly_total_used": 42,
        "monthly_scheduled_used": 30,
        "monthly_manual_used": 12,
    }
    cache_content = {
        "last_fetch_ts": 1762160000,
        "source": "navasan",
        "items": {"usd_sell": {"value": "685000", "change": 1200}},
    }
    user_cfg_path.write_text(json.dumps(user_cfg_content), encoding="utf-8")
    budget_path.write_text(json.dumps(budget_content), encoding="utf-8")
    cache_path.write_text(json.dumps(cache_content), encoding="utf-8")

    # Stage new v1.3.0 update (even if staged directory accidentally has a data/ folder, installer must not overwrite user data)
    staging_dir = tmp_path / "staging_v1.3.0"
    staging_dir.mkdir(parents=True)
    (staging_dir / "Chande.exe").write_bytes(b"NEW-BINARY-1.3.0")
    (staging_dir / "VERSION").write_text("1.3.0\n", encoding="utf-8")
    (staging_dir / "data").mkdir()
    (staging_dir / "data" / "user_config.json").write_text('{"api_key": "OVERWRITTEN"}', encoding="utf-8")

    installer = UpdateInstaller(
        install_dir=install_dir,
        updates_dir=tmp_path / "updates",
    )
    res = installer.apply_staged_update(staging_dir)
    assert res.success is True
    assert res.rolled_back is False

    # Application binary and VERSION were updated to 1.3.0
    assert (install_dir / "Chande.exe").read_bytes() == b"NEW-BINARY-1.3.0"
    assert (install_dir / "VERSION").read_text(encoding="utf-8").strip() == "1.3.0"

    # User configuration, API key, .env, request budget, and cached prices are 100% intact
    assert (install_dir / ".env").read_text(encoding="utf-8") == "NAVASAN_API_KEY=env_secret_key_999\n"
    saved_cfg = json.loads(user_cfg_path.read_text(encoding="utf-8"))
    assert saved_cfg["api_key"] == "user_saved_navasan_key_12345"
    assert saved_cfg["window_x"] == 420
    assert saved_cfg["window_y"] == 180
    assert saved_cfg["display_mode"] == "compact"

    saved_budget = json.loads(budget_path.read_text(encoding="utf-8"))
    assert saved_budget["monthly_total_used"] == 42
    assert saved_budget["monthly_scheduled_used"] == 30

    saved_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved_cache["items"]["usd_sell"]["value"] == "685000"


def test_18_rollback_when_update_installation_fails(tmp_path: Path):
    """Scenario 18: If copying a staged file fails mid-update, installer rolls back to the previous working version."""
    install_dir = tmp_path / "ChandeInstall"
    install_dir.mkdir(parents=True)
    (install_dir / "Chande.exe").write_bytes(b"WORKING-BINARY-1.2.0")
    (install_dir / "VERSION").write_text("1.2.0\n", encoding="utf-8")

    staging_dir = tmp_path / "staging_v1.3.0"
    staging_dir.mkdir(parents=True)
    (staging_dir / "Chande.exe").write_bytes(b"PARTIAL-BINARY-1.3.0")
    (staging_dir / "VERSION").write_text("1.3.0\n", encoding="utf-8")
    (staging_dir / "crash_file.dll").write_bytes(b"CRASH")

    real_copy2 = __import__("shutil").copy2

    def failing_copy_fn(src: Path, dst: Path) -> None:
        if Path(src).name == "crash_file.dll":
            raise OSError("Simulated disk write failure during update")
        real_copy2(src, dst)

    installer = UpdateInstaller(
        install_dir=install_dir,
        updates_dir=tmp_path / "updates",
        copy_fn=failing_copy_fn,
    )
    res = installer.apply_staged_update(staging_dir)
    assert res.success is False
    assert res.rolled_back is True
    assert res.error_code == "rolled_back"

    # Previous working version is restored and partial file is cleaned up
    assert (install_dir / "Chande.exe").read_bytes() == b"WORKING-BINARY-1.2.0"
    assert (install_dir / "VERSION").read_text(encoding="utf-8").strip() == "1.2.0"
    assert not (install_dir / "crash_file.dll").exists()


# ---------------------------------------------------------------------------
# Scenarios 19–21: Settings Manual Check, "Later" Postponement, Bilingual UI & Zero Budget Impact
# ---------------------------------------------------------------------------


def test_19_and_20_postponed_update_and_24h_interval_throttling(tmp_path: Path):
    """Scenarios 19 & 20: 'Later' postpones automatic notifications for that version while manual check still works."""
    payload = _make_github_release_payload(tag_name="v1.3.0")
    session = FakeGitHubSession(
        {
            "https://api.github.com/repos/OWNER/Chande/releases/latest": FakeGitHubResponse(
                200, json_data=payload
            )
        }
    )
    state_store = UpdateStateStore(tmp_path / "update_state.json")
    svc = GitHubUpdateService(
        current_version="1.2.0",
        state_store=state_store,
        updates_dir=tmp_path / "updates",
        session=session,
    )

    # First check: should_auto_check is True
    assert svc.should_auto_check(interval_hours=24) is True
    res1 = svc.check_for_updates("OWNER/Chande", force=False)
    assert res1.checked is True
    assert res1.update_available is True
    api_calls_1 = [u for u in session.calls if "/releases/latest" in u]
    assert len(api_calls_1) == 1

    # Immediate second automatic check within 24h uses cached state without hitting GitHub API again
    assert svc.should_auto_check(interval_hours=24) is False
    res_cached = svc.check_for_updates("OWNER/Chande", force=False)
    assert res_cached.checked is False
    assert res_cached.update_available is True
    api_calls_cached = [u for u in session.calls if "/releases/latest" in u]
    assert len(api_calls_cached) == 1

    # User clicks "Later" for v1.3.0 -> automatic notification suppressed for 1.3.0, but allowed for 1.4.0
    assert svc.should_notify_user("1.3.0") is True
    svc.postpone_version("1.3.0")
    assert svc.should_notify_user("1.3.0") is False
    assert svc.should_notify_user("1.4.0") is True

    # Manual check (force=True) always queries GitHub even within 24h
    res_manual = svc.check_for_updates("OWNER/Chande", force=True)
    assert res_manual.checked is True
    assert res_manual.update_available is True
    api_calls_manual = [u for u in session.calls if "/releases/latest" in u]
    assert len(api_calls_manual) == 2


def test_21_persian_and_english_update_ui_and_zero_navasan_budget_usage(qapp, tmp_path: Path):
    """Scenario 21: Update dialog & Settings About tab work in both Persian (RTL) and English (LTR) with zero Navasan API calls."""
    payload = _make_github_release_payload(tag_name="v1.3.0")
    session = FakeGitHubSession(
        {
            "https://api.github.com/repos/OWNER/Chande/releases/latest": FakeGitHubResponse(
                200, json_data=payload
            )
        }
    )
    state_store = UpdateStateStore(tmp_path / "update_state.json")
    svc = GitHubUpdateService(
        current_version="1.2.0",
        state_store=state_store,
        updates_dir=tmp_path / "updates",
        session=session,
    )
    check_res = svc.check_for_updates("OWNER/Chande", force=True)
    assert check_res.manifest is not None

    # Test UpdateNotificationDialog in Persian (RTL) and English (LTR)
    dlg_fa = UpdateNotificationDialog(
        manifest=check_res.manifest,
        update_service=svc,
        language="fa",
        theme="dark",
        use_persian_numerals=True,
    )
    assert dlg_fa.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert "نسخه جدید" in dlg_fa.header_label.text()
    assert "۱.۲.۰" in dlg_fa.current_ver_val.text()
    assert "۱.۳.۰" in dlg_fa.new_ver_val.text()
    assert bool(dlg_fa.windowFlags() & Qt.WindowType.Tool)
    dlg_fa.close()

    dlg_en = UpdateNotificationDialog(
        manifest=check_res.manifest,
        update_service=svc,
        language="en",
        theme="light",
        use_persian_numerals=False,
    )
    assert dlg_en.layoutDirection() == Qt.LayoutDirection.LeftToRight
    assert "Update Available" in dlg_en.header_label.text()
    assert "1.2.0" in dlg_en.current_ver_val.text()
    assert "1.3.0" in dlg_en.new_ver_val.text()
    dlg_en.close()

    # Verify SettingsDialog About & Updates tab in both languages and zero Navasan API budget consumption
    settings_mgr = SettingsManager(config_path=tmp_path / "user_config.json")
    settings_mgr.update(github_repo="OWNER/Chande", language="fa")
    budget_mgr = RequestBudgetManager(budget_path=tmp_path / "request_budget.json")
    cache_store = CacheStore(cache_path=tmp_path / "market_cache.json")
    api_client = MarketApiClient(budget_manager=budget_mgr, cache_store=cache_store)

    settings_dlg = SettingsDialog(
        settings_manager=settings_mgr,
        budget_manager=budget_mgr,
        cache_store=cache_store,
        api_client=api_client,
        update_service=svc,
    )
    assert settings_dlg.tabs.count() == 2
    assert "درباره" in settings_dlg.tabs.tabText(1)
    assert "۱.۲.۰" in settings_dlg.about_installed_version_val.text()
    assert "۱.۳.۰" in settings_dlg.about_latest_version_val.text()

    # Switch Settings dialog language to English immediately
    settings_dlg.retranslate_ui("en")
    assert "About" in settings_dlg.tabs.tabText(1)
    assert "v1.2.0" in settings_dlg.about_installed_version_val.text()
    assert "v1.3.0" in settings_dlg.about_latest_version_val.text()
    settings_dlg.close()

    # Navasan request budget must remain at 0 used
    st = budget_mgr.get_status()
    assert st.monthly_total_used == 0
    assert st.monthly_remaining == 120
