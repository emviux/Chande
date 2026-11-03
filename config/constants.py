"""Application-wide constants, instrument definitions, and budget safeguards."""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Dict, List

APP_NAME = "Chande"
APP_NAME_FA = "چنده"
APP_DISPLAY_FA = APP_NAME_FA
APP_HEADER_FA = "قیمت ارز و طلا"
APP_TITLE_FA = APP_HEADER_FA
APP_HEADER_EN = "Currency & Gold"
APP_TITLE_EN = APP_HEADER_EN

SUPPORTED_LANGUAGES = ("fa", "en")
DEFAULT_LANGUAGE = "fa"

# Root paths (supports both source execution and PyInstaller frozen executable)
if getattr(sys, "frozen", False):
    _exe_dir = Path(sys.executable).resolve().parent
    # If running from dist/Chande/Chande.exe or dist/Chande.exe inside the project workspace,
    # share the workspace root so cache, settings, and the 120/month budget counter are unified.
    if _exe_dir.name == APP_NAME and _exe_dir.parent.name == "dist" and (_exe_dir.parent.parent / "main.py").is_file():
        ROOT_DIR = _exe_dir.parent.parent
    elif _exe_dir.name == "dist" and (_exe_dir.parent / "main.py").is_file():
        ROOT_DIR = _exe_dir.parent
    else:
        ROOT_DIR = _exe_dir
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", _exe_dir)).resolve()
else:
    ROOT_DIR = Path(__file__).resolve().parent.parent
    BUNDLE_DIR = ROOT_DIR


def load_app_version(default: str = "1.2.0") -> str:
    """Read the central application version from the VERSION file."""
    for candidate in (BUNDLE_DIR / "VERSION", ROOT_DIR / "VERSION"):
        try:
            if candidate.is_file():
                raw = candidate.read_text(encoding="utf-8").strip()
                if raw:
                    return raw.lstrip("vV")
        except OSError:
            continue
    return default


VERSION_FILE = ROOT_DIR / "VERSION"
APP_VERSION = load_app_version("1.2.0")

ASSETS_DIR = BUNDLE_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
IMAGES_DIR = ASSETS_DIR / "images"
TRANSLATIONS_DIR = BUNDLE_DIR / "translations"
DATA_DIR = ROOT_DIR / "data"

# Persistence files (stored in DATA_DIR, excluded from git)
CONFIG_FILE = DATA_DIR / "user_config.json"
CACHE_FILE = DATA_DIR / "market_cache.json"
BUDGET_FILE = DATA_DIR / "request_budget.json"
UPDATE_STATE_FILE = DATA_DIR / "update_state.json"
UPDATES_DIR = DATA_DIR / "updates"

# GitHub Releases Automatic Updater Constants
GITHUB_API_BASE_URL = "https://api.github.com"
DEFAULT_UPDATE_STARTUP_DELAY_SECONDS = 15
DEFAULT_UPDATE_CHECK_INTERVAL_HOURS = 24
UPDATE_TIMEOUT = (5.0, 15.0)
SUPPORTED_UPDATE_CHANNELS = ("stable", "beta")

# API Endpoints (HTTPS verified on api.navasan.tech with TLSv1.3)
NAVASAN_BASE_URL = "https://api.navasan.tech"
NAVASAN_LATEST_URL = f"{NAVASAN_BASE_URL}/latest/"
NAVASAN_USAGE_URL = f"{NAVASAN_BASE_URL}/usage/"

# Optional Free Public API from Hameds/APIs-made-in-Iran (iran-market.github.io)
FREE_IRAN_MARKET_URL = (
    "https://raw.githubusercontent.com/iran-market/iran-market.github.io/main/data/latest-toman.json"
)

# Network timeouts (connect_timeout, read_timeout) in seconds
DEFAULT_TIMEOUT = (5.0, 10.0)

# CRITICAL: Monthly API Request Budget Constants
MONTHLY_HARD_LIMIT = 120          # Hard monthly cap across all request types
MONTHLY_SCHEDULED_BUDGET = 90     # Max scheduled requests per month
MONTHLY_RESERVE_BUDGET = 30       # Reserved for manual refreshes & user actions (120 - 90)
DAILY_SCHEDULED_LIMIT = 3         # Max 3 scheduled API requests per calendar day
DEFAULT_REFRESH_INTERVAL_HOURS = 8
MIN_REFRESH_INTERVAL_HOURS = 8    # Never configurable below 8h (24h / 3 daily = 8h)
ALLOWED_REFRESH_INTERVALS_HOURS = [8, 12, 24]


@dataclass(frozen=True)
class InstrumentSpec:
    """Metadata specification for a market instrument."""
    symbol: str
    name_fa: str
    name_en: str
    default_unit_fa: str
    icon_filename: str
    accent_color: str
    is_primary: bool = True
    free_api_symbol: str = ""
    default_unit_en: str = "Toman"

    def get_name(self, lang: str = "fa") -> str:
        return self.name_en if lang == "en" else self.name_fa

    def get_default_unit(self, lang: str = "fa") -> str:
        return self.default_unit_en if lang == "en" else self.default_unit_fa


# The 4 mandatory primary instruments
PRIMARY_INSTRUMENTS: List[InstrumentSpec] = [
    InstrumentSpec(
        symbol="usd_sell",
        name_fa="دلار تهران",
        name_en="US Dollar (Tehran)",
        default_unit_fa="تومان",
        default_unit_en="Toman",
        icon_filename="usd.svg",
        accent_color="#19D889",
        is_primary=True,
        free_api_symbol="USD_IRR_FREE",
    ),
    InstrumentSpec(
        symbol="18ayar",
        name_fa="طلای ۱۸ عیار",
        name_en="18K Gold",
        default_unit_fa="تومان / گرم",
        default_unit_en="Toman / g",
        icon_filename="gold-bars.svg",
        accent_color="#FFD65A",
        is_primary=True,
        free_api_symbol="GOLD_18K_IRR",
    ),
    InstrumentSpec(
        symbol="btc",
        name_fa="بیتکوین",
        name_en="Bitcoin",
        default_unit_fa="تومان",
        default_unit_en="Toman",
        icon_filename="btc.svg",
        accent_color="#F7931A",
        is_primary=True,
        free_api_symbol="BTC_IRR",
    ),
    InstrumentSpec(
        symbol="usdt",
        name_fa="تتر",
        name_en="Tether (USDT)",
        default_unit_fa="تومان",
        default_unit_en="Toman",
        icon_filename="usdt.svg",
        accent_color="#26A17B",
        is_primary=True,
        free_api_symbol="USDT_IRR",
    ),
]

# Secondary instruments returned in the same single /latest/ call (zero extra API cost)
SECONDARY_INSTRUMENTS: List[InstrumentSpec] = [
    InstrumentSpec(
        symbol="eur",
        name_fa="یورو",
        name_en="Euro",
        default_unit_fa="تومان",
        default_unit_en="Toman",
        icon_filename="euro.svg",
        accent_color="#3B82F6",
        is_primary=False,
        free_api_symbol="EUR_IRR_FREE",
    ),
    InstrumentSpec(
        symbol="gbp",
        name_fa="پوند انگلیس",
        name_en="British Pound",
        default_unit_fa="تومان",
        default_unit_en="Toman",
        icon_filename="gbp.svg",
        accent_color="#60A5FA",
        is_primary=False,
        free_api_symbol="GBP_IRR_FREE",
    ),
    InstrumentSpec(
        symbol="sekkeh",
        name_fa="سکه امامی",
        name_en="Emami Gold Coin",
        default_unit_fa="تومان",
        default_unit_en="Toman",
        icon_filename="gold-coin.svg",
        accent_color="#FFD65A",
        is_primary=False,
        free_api_symbol="COIN_EMAMI_IRR",
    ),
    InstrumentSpec(
        symbol="nim",
        name_fa="نیم سکه",
        name_en="Half Gold Coin",
        default_unit_fa="تومان",
        default_unit_en="Toman",
        icon_filename="gold-coin.svg",
        accent_color="#FFD65A",
        is_primary=False,
        free_api_symbol="COIN_HALF_IRR",
    ),
]

ALL_INSTRUMENTS: List[InstrumentSpec] = PRIMARY_INSTRUMENTS + SECONDARY_INSTRUMENTS
INSTRUMENT_BY_SYMBOL: Dict[str, InstrumentSpec] = {
    spec.symbol: spec for spec in ALL_INSTRUMENTS
}
REQUIRED_SYMBOLS: List[str] = [spec.symbol for spec in PRIMARY_INSTRUMENTS]
