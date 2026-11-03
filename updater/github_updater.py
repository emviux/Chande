"""GitHub Releases update checker, downloader, SHA-256 verifier, and staging manager."""

from __future__ import annotations
from dataclasses import asdict, dataclass, field
import hashlib
import hmac
from pathlib import Path
import re
import shutil
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse
import zipfile

import requests

from config.constants import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_UPDATE_CHECK_INTERVAL_HOURS,
    GITHUB_API_BASE_URL,
    UPDATES_DIR,
    UPDATE_STATE_FILE,
    UPDATE_TIMEOUT,
)
from config.settings import normalize_github_repo
from services.i18n import tr
from storage.atomic_json import atomic_write_json, safe_read_json
from updater.version import is_newer_version, parse_semver

_SHA256_HEX_RE = re.compile(r"\b([a-fA-F0-9]{64})\b")
_TRUSTED_GITHUB_HOSTS = {
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "codeload.github.com",
}


def is_trusted_https_github_url(url: str) -> bool:
    """Validate that an asset or release URL uses HTTPS and belongs to GitHub."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() != "https":
            return False
        host = (parsed.hostname or "").lower()
        return host in _TRUSTED_GITHUB_HOSTS or host.endswith(".githubusercontent.com") or host.endswith(".github.com")
    except ValueError:
        return False


@dataclass
class ReleaseAsset:
    """Metadata for a downloadable GitHub release asset."""

    name: str
    download_url: str
    size: int = 0
    sha256: str = ""
    checksum_url: str = ""
    signature_url: str = ""


@dataclass
class ReleaseManifest:
    """Normalized metadata for a GitHub Release."""

    tag_name: str
    version: str
    name: str
    body: str
    html_url: str
    published_at: str
    prerelease: bool = False
    draft: bool = False
    asset: Optional[ReleaseAsset] = None
    public_key_id: str = ""

    @property
    def windows_asset(self) -> Optional[ReleaseAsset]:
        return self.asset

    @property
    def expected_sha256(self) -> str:
        return self.asset.sha256 if self.asset else ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["ReleaseManifest"]:
        if not isinstance(data, dict) or not data.get("version"):
            return None
        asset_raw = data.get("asset")
        asset = None
        if isinstance(asset_raw, dict) and asset_raw.get("name") and asset_raw.get("download_url"):
            asset = ReleaseAsset(
                name=str(asset_raw.get("name", "")),
                download_url=str(asset_raw.get("download_url", "")),
                size=int(asset_raw.get("size", 0) or 0),
                sha256=str(asset_raw.get("sha256", "") or ""),
                checksum_url=str(asset_raw.get("checksum_url", "") or ""),
                signature_url=str(asset_raw.get("signature_url", "") or ""),
            )
        return cls(
            tag_name=str(data.get("tag_name", "")),
            version=str(data.get("version", "")),
            name=str(data.get("name", "")),
            body=str(data.get("body", "")),
            html_url=str(data.get("html_url", "")),
            published_at=str(data.get("published_at", "")),
            prerelease=bool(data.get("prerelease", False)),
            draft=bool(data.get("draft", False)),
            asset=asset,
            public_key_id=str(data.get("public_key_id", "") or ""),
        )


@dataclass
class UpdateCheckResult:
    """Result of checking GitHub Releases for a newer version of Chande."""

    checked_ok: bool
    update_available: bool
    installed_version: str
    latest_version: str = ""
    manifest: Optional[ReleaseManifest] = None
    error_code: str = ""
    message_fa: str = ""
    message_en: str = ""
    checked_at: float = field(default_factory=time.time)
    from_cache: bool = False

    @property
    def checked(self) -> bool:
        return bool(self.checked_ok and not self.from_cache)

    def get_message(self, lang: str = "fa") -> str:
        if str(lang).lower().startswith("en"):
            return self.message_en or self.message_fa
        return self.message_fa or self.message_en


@dataclass
class DownloadStageResult:
    """Result of downloading, verifying SHA-256, and staging a release package."""

    success: bool
    version: str
    zip_path: Optional[Path] = None
    staging_dir: Optional[Path] = None
    sha256_verified: bool = False
    computed_sha256: str = ""
    expected_sha256: str = ""
    error_code: str = ""
    message_fa: str = ""
    message_en: str = ""

    @property
    def sha256(self) -> str:
        return self.computed_sha256

    def get_message(self, lang: str = "fa") -> str:
        if str(lang).lower().startswith("en"):
            return self.message_en or self.message_fa
        return self.message_fa or self.message_en


class UpdateStateStore:
    """Persistent state store for update check timestamps, cached manifest, and postponed versions."""

    def __init__(self, state_file: Path = UPDATE_STATE_FILE) -> None:
        self.state_file = Path(state_file)
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        raw = safe_read_json(self.state_file, default={})
        return raw if isinstance(raw, dict) else {}

    def save(self) -> None:
        atomic_write_json(self.state_file, self._data)

    @property
    def last_check_timestamp(self) -> Optional[float]:
        val = self._data.get("last_check_timestamp")
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def last_successful_check_timestamp(self) -> Optional[float]:
        val = self._data.get("last_successful_check_timestamp")
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def latest_known_version(self) -> str:
        manifest = self.get_cached_manifest()
        if manifest and manifest.version:
            return manifest.version
        return str(self._data.get("latest_known_version", "") or "")

    def get_latest_known_version(self) -> str:
        return self.latest_known_version

    def get_last_check_timestamp(self) -> Optional[float]:
        return self.last_check_timestamp

    def get_last_check_status(self) -> str:
        return str(self._data.get("last_check_status", "") or "")

    def get_last_error(self) -> str:
        return str(self._data.get("last_error", "") or "")

    def get_cached_manifest(self) -> Optional[ReleaseManifest]:
        raw = self._data.get("last_manifest")
        if isinstance(raw, dict):
            return ReleaseManifest.from_dict(raw)
        return None

    def record_check_success(
        self,
        manifest: Optional[ReleaseManifest],
        etag: str = "",
        now: Optional[float] = None,
        update_available: bool = False,
    ) -> None:
        ts = float(now if now is not None else time.time())
        self._data["last_check_timestamp"] = ts
        self._data["last_successful_check_timestamp"] = ts
        self._data["last_check_status"] = "update_available" if update_available else "up_to_date"
        self._data["last_error"] = ""
        if etag:
            self._data["etag"] = etag
        if manifest is not None:
            self._data["last_manifest"] = manifest.to_dict()
            self._data["latest_known_version"] = manifest.version
        self.save()

    def record_check_error(self, error_code: str, now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        self._data["last_check_timestamp"] = ts
        self._data["last_check_status"] = "error"
        self._data["last_error"] = str(error_code or "error")
        self.save()

    def record_check_attempt(self, now: Optional[float] = None) -> None:
        self._data["last_check_timestamp"] = float(now if now is not None else time.time())
        self.save()

    def postpone_version(self, version: str, hours: float = 24.0, now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        self._data["postponed_version"] = str(version).strip()
        self._data["postponed_until"] = ts + (hours * 3600.0)
        self.save()

    def is_version_postponed(self, version: str, now: Optional[float] = None) -> bool:
        ts = float(now if now is not None else time.time())
        postponed_ver = str(self._data.get("postponed_version", "") or "").strip()
        try:
            until = float(self._data.get("postponed_until", 0.0) or 0.0)
        except (TypeError, ValueError):
            until = 0.0
        return bool(postponed_ver and postponed_ver == str(version).strip() and ts < until)

    def clear_postponed(self) -> None:
        self._data.pop("postponed_version", None)
        self._data.pop("postponed_until", None)
        self.save()

    def record_staged_update(self, version: str, staging_dir: Path) -> None:
        self._data["staged_version"] = str(version)
        self._data["staged_dir"] = str(staging_dir)
        self.save()

    def get_staged_update(self) -> Tuple[str, Optional[Path]]:
        ver = str(self._data.get("staged_version", "") or "")
        raw_dir = self._data.get("staged_dir")
        if ver and raw_dir:
            p = Path(str(raw_dir))
            if p.is_dir():
                return ver, p
        return "", None


def _extract_sha256_for_asset(asset_name: str, text: str) -> str:
    """Extract a 64-character SHA-256 hex digest matching asset_name (or a single digest) from text."""
    if not text:
        return ""
    # 1. Look for a line containing both the asset filename and a 64-hex digest
    for line in text.splitlines():
        if asset_name.lower() in line.lower():
            m = _SHA256_HEX_RE.search(line)
            if m:
                return m.group(1).lower()
    # 2. If the text is a dedicated .sha256 file or has a single SHA-256 digest
    matches = _SHA256_HEX_RE.findall(text)
    if len(matches) == 1:
        return matches[0].lower()
    return ""


def _select_windows_asset(assets: List[Dict[str, Any]], release_body: str) -> Optional[ReleaseAsset]:
    """Identify the Windows x64 ZIP package and its companion checksum/signature assets."""
    if not isinstance(assets, list):
        return None

    checksum_assets: Dict[str, str] = {}
    signature_assets: Dict[str, str] = {}
    candidates: List[Dict[str, Any]] = []

    for item in assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        url = str(item.get("browser_download_url", "") or "").strip()
        if not name or not is_trusted_https_github_url(url):
            continue
        lower = name.lower()
        if lower.endswith(".sha256") or lower in ("sha256sums.txt", "sha256sums", "checksums.txt"):
            checksum_assets[lower] = url
        elif lower.endswith(".sig") or lower.endswith(".asc") or lower.endswith(".minisig"):
            signature_assets[lower] = url
        elif lower.endswith(".zip"):
            candidates.append(item)

    if not candidates:
        return None

    # Prioritize Windows x64 ZIP assets (e.g. Chande-v1.2.0-Windows-x64.zip)
    def _score(asset_dict: Dict[str, Any]) -> Tuple[int, int]:
        n = str(asset_dict.get("name", "")).lower()
        has_win = 1 if ("windows" in n or "win" in n or "x64" in n) else 0
        has_chande = 1 if "chande" in n else 0
        return (has_win + has_chande, len(n))

    candidates.sort(key=_score, reverse=True)
    chosen = candidates[0]
    chosen_name = str(chosen.get("name", "")).strip()
    chosen_url = str(chosen.get("browser_download_url", "")).strip()
    chosen_size = int(chosen.get("size", 0) or 0)

    # Check if GitHub API provided `digest: "sha256:..."` directly on the asset object
    sha256_hex = ""
    raw_digest = str(chosen.get("digest", "") or "").strip()
    if raw_digest.lower().startswith("sha256:"):
        candidate_hash = raw_digest.split(":", 1)[1].strip()
        if _SHA256_HEX_RE.fullmatch(candidate_hash):
            sha256_hex = candidate_hash.lower()

    if not sha256_hex:
        sha256_hex = _extract_sha256_for_asset(chosen_name, release_body)

    # Match companion checksum URL (.zip.sha256 or SHA256SUMS.txt)
    checksum_url = (
        checksum_assets.get(f"{chosen_name.lower()}.sha256")
        or checksum_assets.get("sha256sums.txt")
        or checksum_assets.get("checksums.txt")
        or ""
    )
    signature_url = (
        signature_assets.get(f"{chosen_name.lower()}.sig")
        or signature_assets.get(f"{chosen_name.lower()}.minisig")
        or signature_assets.get(f"{chosen_name.lower()}.asc")
        or ""
    )

    return ReleaseAsset(
        name=chosen_name,
        download_url=chosen_url,
        size=chosen_size,
        sha256=sha256_hex,
        checksum_url=checksum_url,
        signature_url=signature_url,
    )


def parse_github_release_payload(payload: Dict[str, Any]) -> Optional[ReleaseManifest]:
    """Parse and validate a GitHub Release JSON object into a ReleaseManifest."""
    if not isinstance(payload, dict):
        return None
    tag_name = str(payload.get("tag_name", "") or "").strip()
    semver = parse_semver(tag_name)
    if semver is None:
        # Fallback to release name if tag_name wasn't a SemVer string
        semver = parse_semver(str(payload.get("name", "") or ""))
    if semver is None:
        return None

    body = str(payload.get("body", "") or "")
    html_url = str(payload.get("html_url", "") or "").strip()
    if html_url and not is_trusted_https_github_url(html_url):
        html_url = ""

    assets_raw = payload.get("assets", [])
    asset = _select_windows_asset(assets_raw if isinstance(assets_raw, list) else [], body)

    return ReleaseManifest(
        tag_name=tag_name or f"v{semver}",
        version=str(semver),
        name=str(payload.get("name", "") or tag_name or f"v{semver}"),
        body=body,
        html_url=html_url,
        published_at=str(payload.get("published_at", "") or ""),
        prerelease=bool(payload.get("prerelease", False)),
        draft=bool(payload.get("draft", False)),
        asset=asset,
    )


class GitHubUpdateService:
    """Automatic update checker and verified package downloader using GitHub Releases.

    Invariants:
    - Never touches Navasan API or RequestBudgetManager.
    - Enforces Semantic Versioning comparison (never downgrades, ignores drafts, ignores
      prereleases unless channel == 'beta').
    - Requires HTTPS and SHA-256 verification before staging any downloaded release ZIP.
    """

    def __init__(
        self,
        state_store: Optional[UpdateStateStore] = None,
        updates_dir: Path = UPDATES_DIR,
        session: Optional[requests.Session] = None,
        installed_version: str = APP_VERSION,
        current_version: Optional[str] = None,
        timeout: Tuple[float, float] = UPDATE_TIMEOUT,
    ) -> None:
        self.state_store = state_store or UpdateStateStore()
        self.updates_dir = Path(updates_dir)
        self.installed_version = current_version or installed_version
        self.timeout = timeout
        self._session = session or requests.Session()
        self._in_progress = False

    @property
    def current_version(self) -> str:
        return self.installed_version

    @staticmethod
    def is_allowed_download_url(url: str) -> bool:
        return is_trusted_https_github_url(url)

    @staticmethod
    def get_repository_html_url(repo_slug: str) -> str:
        norm = normalize_github_repo(repo_slug)
        return f"https://github.com/{norm}" if norm else ""

    @staticmethod
    def get_releases_html_url(repo_slug: str) -> str:
        norm = normalize_github_repo(repo_slug)
        return f"https://github.com/{norm}/releases" if norm else ""

    def postpone_version(self, version: str, hours: float = 24.0, now: Optional[float] = None) -> None:
        self.state_store.postpone_version(version, hours=hours, now=now)

    @property
    def is_in_progress(self) -> bool:
        return self._in_progress

    def should_auto_check(
        self,
        interval_hours: int = DEFAULT_UPDATE_CHECK_INTERVAL_HOURS,
        now: Optional[float] = None,
    ) -> bool:
        """Return True if at least `interval_hours` have elapsed since the last update check."""
        last_ts = self.state_store.last_check_timestamp
        if last_ts is None:
            return True
        current_ts = float(now if now is not None else time.time())
        return (current_ts - last_ts) >= max(1, int(interval_hours)) * 3600.0

    def should_notify_user(self, version: str, now: Optional[float] = None) -> bool:
        """Return True if the given version has not been postponed via 'Later'."""
        return not self.state_store.is_version_postponed(version, now=now)

    def check_for_updates(
        self,
        repo_slug: str,
        channel: str = "stable",
        force: bool = False,
        now: Optional[float] = None,
    ) -> UpdateCheckResult:
        """Query GitHub Releases for a newer version of Chande."""
        current_ts = float(now if now is not None else time.time())
        norm_repo = normalize_github_repo(repo_slug)
        if not norm_repo:
            return UpdateCheckResult(
                checked_ok=False,
                update_available=False,
                installed_version=self.installed_version,
                error_code="repo_not_configured",
                message_fa=tr("updater.errors.repo_not_configured", lang="fa"),
                message_en=tr("updater.errors.repo_not_configured", lang="en"),
                checked_at=current_ts,
            )

        allow_prerelease = str(channel or "stable").strip().lower() == "beta"

        if not force and not self.should_auto_check(now=current_ts):
            cached = self.state_store.get_cached_manifest()
            if cached is not None:
                is_newer = is_newer_version(
                    self.installed_version,
                    cached.version,
                    allow_prerelease=allow_prerelease,
                )
                return UpdateCheckResult(
                    checked_ok=True,
                    update_available=is_newer,
                    installed_version=self.installed_version,
                    latest_version=cached.version,
                    manifest=cached,
                    message_fa=tr(
                        "updater.status.update_available" if is_newer else "updater.status.up_to_date",
                        lang="fa",
                        version=cached.version,
                    ),
                    message_en=tr(
                        "updater.status.update_available" if is_newer else "updater.status.up_to_date",
                        lang="en",
                        version=cached.version,
                    ),
                    checked_at=self.state_store.last_successful_check_timestamp or current_ts,
                    from_cache=True,
                )

        if self._in_progress:
            return UpdateCheckResult(
                checked_ok=False,
                update_available=False,
                installed_version=self.installed_version,
                error_code="in_progress",
                message_fa=tr("updater.errors.in_progress", lang="fa"),
                message_en=tr("updater.errors.in_progress", lang="en"),
                checked_at=current_ts,
            )

        self._in_progress = True
        self.state_store.record_check_attempt(now=current_ts)
        try:
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": f"{APP_NAME}-Desktop-Widget/{self.installed_version}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if allow_prerelease:
                url = f"{GITHUB_API_BASE_URL}/repos/{norm_repo}/releases?per_page=10"
            else:
                url = f"{GITHUB_API_BASE_URL}/repos/{norm_repo}/releases/latest"

            resp = self._session.get(url, headers=headers, timeout=self.timeout)

            if resp.status_code == 404:
                self.state_store.record_check_success(None, now=current_ts, update_available=False)
                return UpdateCheckResult(
                    checked_ok=True,
                    update_available=False,
                    installed_version=self.installed_version,
                    latest_version=self.installed_version,
                    error_code="no_releases",
                    message_fa=tr("updater.status.up_to_date", lang="fa", version=self.installed_version),
                    message_en=tr("updater.status.up_to_date", lang="en", version=self.installed_version),
                    checked_at=current_ts,
                )

            if resp.status_code == 403 or resp.status_code == 429:
                self.state_store.record_check_error("rate_limit", now=current_ts)
                return UpdateCheckResult(
                    checked_ok=False,
                    update_available=False,
                    installed_version=self.installed_version,
                    error_code="rate_limit",
                    message_fa=tr("updater.errors.rate_limited", lang="fa"),
                    message_en=tr("updater.errors.rate_limited", lang="en"),
                    checked_at=current_ts,
                )

            if resp.status_code != 200:
                self.state_store.record_check_error(f"http_{resp.status_code}", now=current_ts)
                return UpdateCheckResult(
                    checked_ok=False,
                    update_available=False,
                    installed_version=self.installed_version,
                    error_code=f"http_{resp.status_code}",
                    message_fa=tr("updater.errors.http_error", lang="fa", code=resp.status_code),
                    message_en=tr("updater.errors.http_error", lang="en", code=resp.status_code),
                    checked_at=current_ts,
                )

            try:
                data = resp.json()
            except ValueError:
                self.state_store.record_check_error("malformed_json", now=current_ts)
                return UpdateCheckResult(
                    checked_ok=False,
                    update_available=False,
                    installed_version=self.installed_version,
                    error_code="malformed_json",
                    message_fa=tr("updater.errors.malformed_response", lang="fa"),
                    message_en=tr("updater.errors.malformed_response", lang="en"),
                    checked_at=current_ts,
                )

            manifest = self._select_best_release(data, allow_prerelease=allow_prerelease)
            if manifest is None and not allow_prerelease and isinstance(data, dict):
                # Fallback to /releases list if /releases/latest returned a prerelease/draft
                fallback_url = f"{GITHUB_API_BASE_URL}/repos/{norm_repo}/releases?per_page=10"
                resp_fb = self._session.get(fallback_url, headers=headers, timeout=self.timeout)
                if resp_fb.status_code == 200:
                    try:
                        manifest = self._select_best_release(resp_fb.json(), allow_prerelease=False)
                    except ValueError:
                        manifest = None

            if manifest is None:
                self.state_store.record_check_error("invalid_release_metadata", now=current_ts)
                return UpdateCheckResult(
                    checked_ok=False,
                    update_available=False,
                    installed_version=self.installed_version,
                    error_code="invalid_release_metadata",
                    message_fa=tr("updater.errors.malformed_response", lang="fa"),
                    message_en=tr("updater.errors.malformed_response", lang="en"),
                    checked_at=current_ts,
                )

            is_newer = is_newer_version(
                self.installed_version,
                manifest.version,
                allow_prerelease=allow_prerelease,
            )
            if is_newer and manifest.asset is None:
                self.state_store.record_check_error("missing_asset", now=current_ts)
                return UpdateCheckResult(
                    checked_ok=True,
                    update_available=False,
                    installed_version=self.installed_version,
                    latest_version=manifest.version,
                    manifest=manifest,
                    error_code="missing_asset",
                    message_fa=tr("updater.errors.missing_asset", lang="fa"),
                    message_en=tr("updater.errors.missing_asset", lang="en"),
                    checked_at=current_ts,
                )

            if is_newer and manifest.asset is not None and not manifest.asset.sha256:
                try:
                    self._resolve_expected_sha256(manifest.asset)
                except requests.RequestException:
                    pass

            etag = str(resp.headers.get("ETag", "") or "")
            self.state_store.record_check_success(
                manifest, etag=etag, now=current_ts, update_available=is_newer
            )

            return UpdateCheckResult(
                checked_ok=True,
                update_available=is_newer,
                installed_version=self.installed_version,
                latest_version=manifest.version,
                manifest=manifest,
                message_fa=tr(
                    "updater.status.update_available" if is_newer else "updater.status.up_to_date",
                    lang="fa",
                    version=manifest.version,
                ),
                message_en=tr(
                    "updater.status.update_available" if is_newer else "updater.status.up_to_date",
                    lang="en",
                    version=manifest.version,
                ),
                checked_at=current_ts,
                from_cache=False,
            )
        except requests.Timeout:
            self.state_store.record_check_error("timeout", now=current_ts)
            return UpdateCheckResult(
                checked_ok=False,
                update_available=False,
                installed_version=self.installed_version,
                error_code="timeout",
                message_fa=tr("updater.errors.network_timeout", lang="fa"),
                message_en=tr("updater.errors.network_timeout", lang="en"),
                checked_at=current_ts,
            )
        except requests.RequestException as exc:
            self.state_store.record_check_error("network_error", now=current_ts)
            return UpdateCheckResult(
                checked_ok=False,
                update_available=False,
                installed_version=self.installed_version,
                error_code="network_error",
                message_fa=tr("updater.errors.network_error", lang="fa", detail=type(exc).__name__),
                message_en=tr("updater.errors.network_error", lang="en", detail=type(exc).__name__),
                checked_at=current_ts,
            )
        finally:
            self._in_progress = False

    def _select_best_release(
        self,
        payload: Any,
        allow_prerelease: bool,
    ) -> Optional[ReleaseManifest]:
        """Select the highest SemVer non-draft release from a single object or list of releases."""
        items: List[Dict[str, Any]]
        if isinstance(payload, dict):
            items = [payload]
        elif isinstance(payload, list):
            items = [x for x in payload if isinstance(x, dict)]
        else:
            return None

        valid_manifests: List[Tuple[Any, ReleaseManifest]] = []
        for raw in items:
            if bool(raw.get("draft", False)):
                continue
            if bool(raw.get("prerelease", False)) and not allow_prerelease:
                continue
            manifest = parse_github_release_payload(raw)
            if manifest is None:
                continue
            parsed_ver = parse_semver(manifest.version)
            if parsed_ver is None:
                continue
            if parsed_ver.is_prerelease and not allow_prerelease:
                continue
            valid_manifests.append((parsed_ver, manifest))

        if not valid_manifests:
            return None

        valid_manifests.sort(key=lambda pair: pair[0], reverse=True)
        return valid_manifests[0][1]

    def _resolve_expected_sha256(self, asset: ReleaseAsset) -> str:
        """Return expected SHA-256 hex digest, fetching companion checksum asset over HTTPS if needed."""
        if asset.sha256 and _SHA256_HEX_RE.fullmatch(asset.sha256.strip()):
            return asset.sha256.strip().lower()

        if asset.checksum_url and is_trusted_https_github_url(asset.checksum_url):
            resp = self._session.get(
                asset.checksum_url,
                headers={"User-Agent": f"{APP_NAME}-Desktop-Widget/{self.installed_version}"},
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                extracted = _extract_sha256_for_asset(asset.name, resp.text)
                if extracted:
                    asset.sha256 = extracted
                    return extracted
        return ""

    def download_and_stage_update(
        self,
        manifest: ReleaseManifest,
        progress_callback: Optional[Callable[..., None]] = None,
        signature_verifier: Optional[Callable[[Path, ReleaseManifest], bool]] = None,
    ) -> DownloadStageResult:
        return self.download_and_stage(
            manifest=manifest,
            progress_callback=progress_callback,
            signature_verifier=signature_verifier,
        )

    def download_and_stage(
        self,
        manifest: ReleaseManifest,
        progress_callback: Optional[Callable[..., None]] = None,
        signature_verifier: Optional[Callable[[Path, ReleaseManifest], bool]] = None,
    ) -> DownloadStageResult:
        """Download release ZIP via HTTPS, verify SHA-256 checksum, extract, and validate staged files."""
        if manifest.asset is None or not manifest.asset.download_url:
            return DownloadStageResult(
                success=False,
                version=manifest.version,
                error_code="missing_asset",
                message_fa=tr("updater.errors.missing_asset", lang="fa"),
                message_en=tr("updater.errors.missing_asset", lang="en"),
            )

        asset = manifest.asset
        if not is_trusted_https_github_url(asset.download_url):
            return DownloadStageResult(
                success=False,
                version=manifest.version,
                error_code="untrusted_url",
                message_fa=tr("updater.errors.untrusted_url", lang="fa"),
                message_en=tr("updater.errors.untrusted_url", lang="en"),
            )

        downloads_dir = self.updates_dir / "downloads"
        staging_root = self.updates_dir / "staging"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        staging_root.mkdir(parents=True, exist_ok=True)

        safe_filename = re.sub(r"[^A-Za-z0-9_.-]", "_", asset.name)
        part_path = downloads_dir / f"{safe_filename}.part"
        final_zip_path = downloads_dir / safe_filename

        try:
            expected_sha256 = self._resolve_expected_sha256(asset)
            if not expected_sha256:
                return DownloadStageResult(
                    success=False,
                    version=manifest.version,
                    error_code="missing_checksum",
                    message_fa=tr("updater.errors.missing_checksum", lang="fa"),
                    message_en=tr("updater.errors.missing_checksum", lang="en"),
                )

            headers = {"User-Agent": f"{APP_NAME}-Desktop-Widget/{self.installed_version}"}
            with self._session.get(asset.download_url, headers=headers, stream=True, timeout=self.timeout) as resp:
                if resp.status_code != 200:
                    return DownloadStageResult(
                        success=False,
                        version=manifest.version,
                        error_code=f"download_http_{resp.status_code}",
                        message_fa=tr("updater.errors.download_failed", lang="fa"),
                        message_en=tr("updater.errors.download_failed", lang="en"),
                    )

                total_bytes = int(resp.headers.get("Content-Length", 0) or asset.size or 0)
                downloaded = 0
                hasher = hashlib.sha256()

                with open(part_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        if progress_callback is not None:
                            pct = int(min(100, round((downloaded * 100) / total_bytes))) if total_bytes > 0 else 0
                            try:
                                progress_callback(downloaded, total_bytes, pct)
                            except TypeError:
                                progress_callback(downloaded, total_bytes)

            if total_bytes > 0 and downloaded < total_bytes:
                part_path.unlink(missing_ok=True)
                return DownloadStageResult(
                    success=False,
                    version=manifest.version,
                    error_code="incomplete_download",
                    message_fa=tr("updater.errors.incomplete_download", lang="fa"),
                    message_en=tr("updater.errors.incomplete_download", lang="en"),
                )

            computed_sha256 = hasher.hexdigest().lower()
            if not hmac.compare_digest(computed_sha256, expected_sha256.lower()):
                part_path.unlink(missing_ok=True)
                return DownloadStageResult(
                    success=False,
                    version=manifest.version,
                    sha256_verified=False,
                    computed_sha256=computed_sha256,
                    expected_sha256=expected_sha256,
                    error_code="checksum_mismatch",
                    message_fa=tr("updater.errors.checksum_mismatch", lang="fa"),
                    message_en=tr("updater.errors.checksum_mismatch", lang="en"),
                )

            if final_zip_path.exists():
                final_zip_path.unlink()
            part_path.replace(final_zip_path)

            # Optional cryptographic signature verification hook for future publisher authentication
            if signature_verifier is not None and not signature_verifier(final_zip_path, manifest):
                final_zip_path.unlink(missing_ok=True)
                return DownloadStageResult(
                    success=False,
                    version=manifest.version,
                    error_code="signature_invalid",
                    message_fa=tr("updater.errors.signature_invalid", lang="fa"),
                    message_en=tr("updater.errors.signature_invalid", lang="en"),
                )

            # Extract into clean staging directory
            target_stage_dir = staging_root / f"v{manifest.version}"
            if target_stage_dir.exists():
                shutil.rmtree(target_stage_dir, ignore_errors=True)
            target_stage_dir.mkdir(parents=True, exist_ok=True)

            normalized_stage_dir = self._extract_and_validate_zip(final_zip_path, target_stage_dir)
            self.state_store.record_staged_update(manifest.version, normalized_stage_dir)

            return DownloadStageResult(
                success=True,
                version=manifest.version,
                zip_path=final_zip_path,
                staging_dir=normalized_stage_dir,
                sha256_verified=True,
                computed_sha256=computed_sha256,
                expected_sha256=expected_sha256,
                message_fa=tr("updater.status.ready_to_install", lang="fa", version=manifest.version),
                message_en=tr("updater.status.ready_to_install", lang="en", version=manifest.version),
            )

        except (zipfile.BadZipFile, ValueError) as exc:
            part_path.unlink(missing_ok=True)
            final_zip_path.unlink(missing_ok=True)
            return DownloadStageResult(
                success=False,
                version=manifest.version,
                error_code="corrupt_archive",
                message_fa=tr("updater.errors.corrupted_archive", lang="fa", detail=str(exc)),
                message_en=tr("updater.errors.corrupted_archive", lang="en", detail=str(exc)),
            )
        except (requests.RequestException, OSError) as exc:
            part_path.unlink(missing_ok=True)
            return DownloadStageResult(
                success=False,
                version=manifest.version,
                error_code="download_interrupted",
                message_fa=tr("updater.errors.download_interrupted", lang="fa", detail=type(exc).__name__),
                message_en=tr("updater.errors.download_interrupted", lang="en", detail=type(exc).__name__),
            )

    def _extract_and_validate_zip(self, zip_path: Path, target_dir: Path) -> Path:
        """Safely extract zip_path into target_dir (preventing Zip-Slip) and validate structure."""
        resolved_root = target_dir.resolve()
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                member_path = (resolved_root / member.filename).resolve()
                if resolved_root not in member_path.parents and member_path != resolved_root:
                    raise ValueError(f"Unsafe path in update archive: {member.filename!r}")
            zf.extractall(resolved_root)

        # If archive wrapped everything inside a single top-level folder (e.g. Chande/), unwrap it
        entries = [p for p in resolved_root.iterdir()]
        app_root = resolved_root
        if len(entries) == 1 and entries[0].is_dir():
            app_root = entries[0]

        has_exe = (app_root / f"{APP_NAME}.exe").is_file()
        has_source = (app_root / "main.py").is_file()
        if not (has_exe or has_source):
            raise ValueError("Staged update is missing Chande.exe or main.py")

        has_assets = (
            (app_root / "assets").is_dir()
            or (app_root / "_internal" / "assets").is_dir()
        )
        has_translations = (
            (app_root / "translations").is_dir()
            or (app_root / "_internal" / "translations").is_dir()
        )
        has_version = (app_root / "VERSION").is_file()
        if not (has_translations or has_assets or has_version):
            raise ValueError("Staged update is missing required application metadata/translations")

        return app_root
