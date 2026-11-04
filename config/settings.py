"""Application settings and secret management with Windows startup integration."""

from __future__ import annotations
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from config.constants import (
    ALLOWED_REFRESH_INTERVALS_HOURS,
    APP_NAME,
    CONFIG_FILE,
    DEFAULT_LANGUAGE,
    DEFAULT_REFRESH_INTERVAL_HOURS,
    DEFAULT_UPDATE_CHECK_INTERVAL_HOURS,
    DEFAULT_UPDATE_STARTUP_DELAY_SECONDS,
    MIN_REFRESH_INTERVAL_HOURS,
    ROOT_DIR,
    SUPPORTED_LANGUAGES,
    SUPPORTED_UPDATE_CHANNELS,
)
from services.i18n import set_current_language
from storage.atomic_json import atomic_write_json, safe_read_json

# Load .env from project root if present (without overriding explicit environment variables)
load_dotenv(ROOT_DIR / ".env", override=False)

_PLACEHOLDER_REPOS = {
    "",
    "owner/repo",
    "your_username/chande",
    "your-username/chande",
}


def detect_git_remote_repo(root_dir: Path = ROOT_DIR) -> str:
    """Inspect .git/config if present to extract an actual GitHub 'OWNER/REPO' slug."""
    git_config = Path(root_dir) / ".git" / "config"
    if not git_config.is_file():
        return ""
    try:
        import re
        content = git_config.read_text(encoding="utf-8", errors="ignore")
        match = re.search(
            r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?\s*$",
            content,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        if match:
            slug = match.group(1).strip().strip("/")
            if slug.lower() not in _PLACEHOLDER_REPOS:
                return slug
    except OSError:
        pass
    return ""


def normalize_github_repo(value: Optional[str]) -> str:
    """Normalize a GitHub repository URL or 'OWNER/REPO' string into 'OWNER/REPO'."""
    if not value:
        return ""
    import re
    clean = value.strip()
    url_match = re.search(
        r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
        clean,
        flags=re.IGNORECASE,
    )
    if url_match:
        clean = url_match.group(1)
    clean = clean.strip("/")
    if clean.lower() in _PLACEHOLDER_REPOS:
        return ""
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", clean):
        return clean
    return ""


def mask_api_key(key: Optional[str]) -> str:
    """Return a masked representation of an API key safe for UI hints or logs."""
    if not key:
        return ""
    clean = key.strip()
    if len(clean) <= 6:
        return "*" * len(clean)
    return f"{clean[:2]}{'*' * (len(clean) - 4)}{clean[-2:]}"


def redact_secrets(text: str, api_key: Optional[str] = None) -> str:
    """Redact any occurrence of an API key or api_key query param from logs/error strings."""
    if not text:
        return ""
    import re
    result = re.sub(r"(api_key=)[^&\s\"']+", r"\1***REDACTED***", text, flags=re.IGNORECASE)
    if api_key and api_key.strip():
        result = result.replace(api_key.strip(), "***REDACTED***")
    return result


@dataclass
class AppSettings:
    """User preferences persisted locally in user_config.json."""
    api_key: str = ""
    api_provider: str = "navasan"  # "navasan" or "free_iran_market"
    auto_refresh_enabled: bool = True
    refresh_interval_hours: int = DEFAULT_REFRESH_INTERVAL_HOURS
    language: str = DEFAULT_LANGUAGE  # "fa" (Persian) or "en" (English)
    theme: str = "dark"  # "dark" or "light"
    opacity: float = 0.96
    always_on_top: bool = True
    start_with_windows: bool = True
    use_persian_numerals: Optional[bool] = None  # Defaults to True for "fa", False for "en"
    confirm_manual_refresh: bool = True
    display_mode: str = "expanded"  # "expanded", "compact", or "ultra_compact"
    show_chart: bool = True
    show_secondary_cards: bool = True
    window_x: Optional[int] = None
    window_y: Optional[int] = None
    window_width: Optional[int] = None
    window_height: Optional[int] = None
    github_repo: str = ""
    auto_check_updates: bool = True
    auto_download_updates: bool = False
    update_channel: str = "stable"  # "stable" or "beta"
    update_startup_delay_seconds: int = DEFAULT_UPDATE_STARTUP_DELAY_SECONDS
    update_check_interval_hours: int = DEFAULT_UPDATE_CHECK_INTERVAL_HOURS

    def __post_init__(self) -> None:
        self.validate_and_clamp()

    def validate_and_clamp(self) -> None:
        """Enforce budget and UI constraints on all settings fields."""
        try:
            interval = int(self.refresh_interval_hours)
        except (TypeError, ValueError):
            interval = DEFAULT_REFRESH_INTERVAL_HOURS
        if interval < MIN_REFRESH_INTERVAL_HOURS:
            interval = MIN_REFRESH_INTERVAL_HOURS
        self.refresh_interval_hours = interval

        try:
            op = float(self.opacity)
        except (TypeError, ValueError):
            op = 0.96
        self.opacity = max(0.35, min(1.0, op))

        lang_norm = str(self.language or DEFAULT_LANGUAGE).strip().lower()
        if lang_norm not in SUPPORTED_LANGUAGES:
            lang_norm = DEFAULT_LANGUAGE
        self.language = lang_norm
        set_current_language(self.language)

        if self.use_persian_numerals is None:
            self.use_persian_numerals = self.language == "fa"
        else:
            self.use_persian_numerals = bool(self.use_persian_numerals)

        if self.theme not in ("dark", "light"):
            self.theme = "dark"

        if self.display_mode not in ("expanded", "compact", "ultra_compact"):
            self.display_mode = "expanded"

        if self.api_provider not in ("navasan", "free_iran_market"):
            self.api_provider = "navasan"

        self.api_key = (self.api_key or "").strip()

        self.github_repo = normalize_github_repo(self.github_repo)
        self.auto_check_updates = bool(self.auto_check_updates)
        self.auto_download_updates = bool(self.auto_download_updates)
        channel_norm = str(self.update_channel or "stable").strip().lower()
        if channel_norm not in SUPPORTED_UPDATE_CHANNELS:
            channel_norm = "stable"
        self.update_channel = channel_norm

        try:
            delay_s = int(self.update_startup_delay_seconds)
        except (TypeError, ValueError):
            delay_s = DEFAULT_UPDATE_STARTUP_DELAY_SECONDS
        self.update_startup_delay_seconds = max(1, min(3600, delay_s))

        try:
            upd_hours = int(self.update_check_interval_hours)
        except (TypeError, ValueError):
            upd_hours = DEFAULT_UPDATE_CHECK_INTERVAL_HOURS
        self.update_check_interval_hours = max(1, min(168, upd_hours))

        for coord_attr in ("window_x", "window_y"):
            raw_coord = getattr(self, coord_attr, None)
            if raw_coord is None or isinstance(raw_coord, bool):
                setattr(self, coord_attr, None)
            else:
                try:
                    setattr(self, coord_attr, int(round(float(raw_coord))))
                except (TypeError, ValueError):
                    setattr(self, coord_attr, None)

        for dim_attr in ("window_width", "window_height"):
            raw_dim = getattr(self, dim_attr, None)
            if raw_dim is None or isinstance(raw_dim, bool):
                setattr(self, dim_attr, None)
            else:
                try:
                    val_dim = int(round(float(raw_dim)))
                    setattr(self, dim_attr, val_dim if val_dim > 0 else None)
                except (TypeError, ValueError):
                    setattr(self, dim_attr, None)

    def get_effective_api_key(self) -> str:
        """Return the configured API key, prioritizing user settings then environment variable."""
        if self.api_key and self.api_key.strip():
            return self.api_key.strip()
        env_key = os.environ.get("NAVASAN_API_KEY", "").strip()
        if env_key and env_key != "your_navasan_api_key_here":
            return env_key
        return ""

    def get_effective_github_repo(self) -> str:
        """Return the configured GitHub 'OWNER/REPO', prioritizing settings, env, then .git/config."""
        norm = normalize_github_repo(self.github_repo)
        if norm:
            return norm
        env_repo = normalize_github_repo(os.environ.get("CHANDE_GITHUB_REPO", ""))
        if env_repo and env_repo.lower() != "owner/chande":
            return env_repo
        return detect_git_remote_repo() or "emviux/Chande"


class SettingsManager:
    """Loads, validates, and atomically persists AppSettings."""

    def __init__(self, config_path: Path = CONFIG_FILE) -> None:
        self.config_path = Path(config_path)
        self.settings = self.load()

    def load(self) -> AppSettings:
        raw = safe_read_json(self.config_path, default={})
        valid_fields = {f.name for f in AppSettings.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered: Dict[str, Any] = {k: v for k, v in raw.items() if k in valid_fields}
        settings = AppSettings(**filtered)
        # If api_key is not in config file, populate in-memory from environment if present
        if not settings.api_key:
            env_key = os.environ.get("NAVASAN_API_KEY", "").strip()
            if env_key and env_key != "your_navasan_api_key_here":
                settings.api_key = env_key
        env_provider = os.environ.get("CHANDE_API_PROVIDER", "").strip()
        if "api_provider" not in filtered and env_provider in ("navasan", "free_iran_market"):
            settings.api_provider = env_provider
        settings.validate_and_clamp()
        return settings

    def save(self, settings: Optional[AppSettings] = None) -> None:
        if settings is not None:
            self.settings = settings
        self.settings.validate_and_clamp()
        atomic_write_json(self.config_path, asdict(self.settings))

    def update(self, **kwargs: Any) -> AppSettings:
        for k, v in kwargs.items():
            if hasattr(self.settings, k):
                setattr(self.settings, k, v)
        self.settings.validate_and_clamp()
        self.save()
        return self.settings

    @staticmethod
    def apply_windows_startup(enable: bool) -> bool:
        """Configure or remove the current user's Windows Run registry entry."""
        from services.windows_gadget import WindowsStartupManager
        return WindowsStartupManager.set_startup(enable)
