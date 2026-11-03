"""Price parsing, unit verification, Persian numeral conversion, and formatting utilities."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.constants import (
    ALL_INSTRUMENTS,
    INSTRUMENT_BY_SYMBOL,
    PRIMARY_INSTRUMENTS,
    InstrumentSpec,
)
from services.i18n import get_current_language, tr

_EN_TO_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_FA_TO_EN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_UNIT_KEY_MAP: Dict[str, str] = {
    "تومان": "units.toman",
    "toman": "units.toman",
    "تومان / گرم": "units.toman_per_gram",
    "toman / g": "units.toman_per_gram",
    "toman_per_gram": "units.toman_per_gram",
    "ریال": "units.rial",
    "rial": "units.rial",
    "ریال / گرم": "units.rial_per_gram",
    "rial / g": "units.rial_per_gram",
    "rial_per_gram": "units.rial_per_gram",
    "دلار": "units.usd",
    "usd": "units.usd",
}


def localize_unit(unit_label: str, lang: str = "fa") -> str:
    """Translate a unit label (e.g. 'تومان', 'تومان / گرم', 'دلار') into the requested language."""
    clean = (unit_label or "").strip()
    key = _UNIT_KEY_MAP.get(clean.lower()) or _UNIT_KEY_MAP.get(clean)
    if key:
        return tr(key, lang=lang, default=clean)
    return clean


def to_persian_numerals(text: str) -> str:
    """Convert ASCII digits in `text` to Persian numerals."""
    return str(text).translate(_EN_TO_FA_DIGITS)


def to_english_numerals(text: str) -> str:
    """Convert Persian/Arabic digits in `text` to ASCII digits."""
    return str(text).translate(_FA_TO_EN_DIGITS)


def localize_digits(text: str, use_persian: bool = True) -> str:
    """Format digits in `text` according to user numeral preference."""
    if use_persian:
        return to_persian_numerals(to_english_numerals(text))
    return to_english_numerals(text)


def parse_numeric(raw: Any) -> Optional[float]:
    """Safely parse a string or numeric value into a float, returning None on invalid input."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        import math
        if math.isnan(raw) or math.isinf(raw):
            return None
        return float(raw)
    if isinstance(raw, str):
        cleaned = to_english_numerals(raw).strip().replace(",", "").replace("_", "")
        if not cleaned or cleaned in ("-", "+", ".", "null", "None", "nan"):
            return None
        try:
            import math
            val = float(cleaned)
            if math.isnan(val) or math.isinf(val):
                return None
            return val
        except ValueError:
            return None
    return None


def determine_instrument_unit(
    symbol: str, value: float, item_data: Dict[str, Any], lang: str = "fa"
) -> str:
    """Determine the accurate unit label for an instrument without silently scaling its value.

    Rules:
    - Never silently multiply or divide `value` by 10 or 1,000,000.
    - `usd_sell`: 'تومان' / 'Toman'
    - `18ayar`: 'تومان / گرم' / 'Toman / g'
    - `btc` and `usdt`: Determine from explicit metadata or value denomination in API response.
      In Navasan's Iranian market table, values are in Toman unless USD is returned.
    """
    explicit_currency = str(item_data.get("currency", "")).upper().strip()
    if explicit_currency == "USD":
        unit_fa = "دلار"
    elif explicit_currency in ("IRT", "TMN", "TOMAN"):
        unit_fa = "تومان / گرم" if symbol == "18ayar" else "تومان"
    elif explicit_currency == "IRR":
        unit_fa = "ریال / گرم" if symbol == "18ayar" else "ریال"
    elif symbol == "usd_sell":
        unit_fa = "تومان"
    elif symbol == "18ayar":
        unit_fa = "تومان / گرم"
    elif symbol == "usdt":
        # If Tether value is around ~1.0 (e.g. < 50), it is denominated in USD; otherwise Toman
        unit_fa = "دلار" if (0 < value < 50) else "تومان"
    elif symbol == "btc":
        # Check if dollar_rate is provided alongside value
        dollar_rate = parse_numeric(item_data.get("dollar_rate"))
        if dollar_rate is not None and dollar_rate > 0:
            if abs(value - dollar_rate) / max(dollar_rate, 1.0) < 0.01:
                unit_fa = "دلار"
            else:
                unit_fa = "تومان"
        elif 0 < value < 1_000_000:
            unit_fa = "دلار"
        else:
            unit_fa = "تومان"
    else:
        spec = INSTRUMENT_BY_SYMBOL.get(symbol)
        unit_fa = spec.default_unit_fa if spec else "تومان"

    if lang == "fa":
        return unit_fa
    return localize_unit(unit_fa, lang=lang)


def format_number(value: Optional[float], use_persian: bool = True, max_decimals: int = 2) -> str:
    """Format a number with thousands separators, preserving decimals only when non-integer."""
    if value is None:
        return "—"
    if abs(value - round(value)) < 1e-6:
        formatted = f"{int(round(value)):,}"
    else:
        formatted = f"{value:,.{max_decimals}f}".rstrip("0").rstrip(".")
    return localize_digits(formatted, use_persian=use_persian)


def calculate_percentage_change(
    current_value: Optional[float],
    change: Optional[float] = None,
    previous_value: Optional[float] = None,
    explicit_pct: Optional[float] = None,
) -> Optional[float]:
    """Calculate percentage change only when valid previous-price data is available.

    Priority:
    1. If `explicit_pct` is provided by the API response, return it.
    2. If `previous_value` is explicitly known and > 0:
       - If `change` is provided, use `(change / previous_value) * 100`.
       - Otherwise use `((current_value - previous_value) / previous_value) * 100`.
    3. If `change` is provided and `current_value` is known, Navasan defines `change` as
       change relative to yesterday's end-of-day price (`prev = current_value - change`).
       When `prev > 0` and `change != 0`, `(change / prev) * 100` is validly calculable.
    """
    if explicit_pct is not None:
        return round(float(explicit_pct), 2)

    if current_value is None or current_value <= 0:
        return None

    if previous_value is not None and previous_value > 0:
        diff = change if change is not None else (current_value - previous_value)
        return round((diff / previous_value) * 100.0, 2)

    if change is not None:
        derived_prev = current_value - change
        if derived_prev > 0:
            return round((change / derived_prev) * 100.0, 2)

    return None


def format_change_text(
    change: Optional[float],
    pct_change: Optional[float] = None,
    use_persian: bool = True,
    include_pct: bool = True,
    lang: Optional[str] = None,
) -> str:
    """Format daily change and optional percentage into a clean RTL/LTR indicator string."""
    effective_lang = lang or get_current_language()
    if change is None:
        return localize_digits(tr("status.no_daily_change", lang=effective_lang), use_persian)

    abs_change = abs(change)
    num_str = format_number(abs_change, use_persian=use_persian)

    if change > 0:
        prefix = "▲ +"
    elif change < 0:
        prefix = "▼ -"
    else:
        prefix = "• "

    base = f"{prefix}{num_str}"
    if include_pct and pct_change is not None:
        abs_pct = abs(pct_change)
        pct_str = localize_digits(f"{abs_pct:.2f}".rstrip("0").rstrip("."), use_persian=use_persian)
        pct_symbol = "٪" if use_persian else "%"
        return f"{base} ({pct_str}{pct_symbol})"
    return base


def format_cache_age(
    age_seconds: Optional[float],
    use_persian: bool = True,
    lang: Optional[str] = None,
) -> str:
    """Format cache age in seconds into a human-friendly localized relative time string."""
    effective_lang = lang or get_current_language()
    if age_seconds is None:
        return tr("time.unknown", lang=effective_lang)
    sec = max(0, int(age_seconds))
    if sec < 60:
        text = tr("time.just_now", lang=effective_lang)
    elif sec < 3600:
        mins = sec // 60
        text = tr("time.minutes_ago", lang=effective_lang, minutes=mins)
    elif sec < 86400:
        hours = sec // 3600
        mins = (sec % 3600) // 60
        if mins > 0:
            text = tr("time.hours_minutes_ago", lang=effective_lang, hours=hours, minutes=mins)
        else:
            text = tr("time.hours_ago", lang=effective_lang, hours=hours)
    else:
        days = sec // 86400
        hours = (sec % 86400) // 3600
        if hours > 0:
            text = tr("time.days_hours_ago", lang=effective_lang, days=days, hours=hours)
        else:
            text = tr("time.days_ago", lang=effective_lang, days=days)
    return localize_digits(text, use_persian=use_persian)


format_age_fa = format_cache_age


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert Gregorian date (gy, gm, gd) to Jalali/Solar Hijri (jy, jm, jd)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        (365 * gy)
        + ((gy2 + 3) // 4)
        - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400)
        - 80
        + gd
        + g_d_m[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def format_jalali_datetime(timestamp: Optional[float], use_persian: bool = True) -> str:
    """Format a Unix timestamp as Jalali date and time `HH:MM - YYYY/MM/DD`."""
    if timestamp is None:
        return "—"
    dt = datetime.fromtimestamp(timestamp)
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    raw = f"{dt.hour:02d}:{dt.minute:02d} - {jy:04d}/{jm:02d}/{jd:02d}"
    return localize_digits(raw, use_persian=use_persian)


def format_gregorian_datetime(timestamp: Optional[float], use_persian: bool = False) -> str:
    """Format a Unix timestamp as Gregorian date and time `YYYY-MM-DD HH:MM` in local timezone."""
    if timestamp is None:
        return "—"
    dt = datetime.fromtimestamp(timestamp)
    raw = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"
    return localize_digits(raw, use_persian=use_persian)


def format_localized_datetime(
    timestamp: Optional[float],
    lang: Optional[str] = None,
    use_persian: Optional[bool] = None,
) -> str:
    """Format a Unix timestamp in Jalali calendar for Persian (`fa`) or Gregorian for English (`en`)."""
    effective_lang = (lang or get_current_language()).strip().lower()
    effective_numerals = (effective_lang == "fa") if use_persian is None else bool(use_persian)
    if effective_lang == "en":
        return format_gregorian_datetime(timestamp, use_persian=effective_numerals)
    return format_jalali_datetime(timestamp, use_persian=effective_numerals)


@dataclass
class InstrumentQuote:
    """Normalized representation of an instrument's market quote for the UI."""
    symbol: str
    name_fa: str
    unit_fa: str
    icon_filename: str
    accent_color: str
    is_primary: bool = True
    available: bool = False
    value: Optional[float] = None
    change: Optional[float] = None
    pct_change: Optional[float] = None
    timestamp: Optional[int] = None
    date_str: str = ""
    dollar_rate: Optional[float] = None
    sparkline: List[float] = field(default_factory=list)
    error_note: str = ""
    name_en: str = ""
    unit_en: str = ""
    error_code: str = ""

    @property
    def direction(self) -> int:
        """Return +1 for positive change, -1 for negative change, 0 for flat/unknown."""
        if self.change is None or self.change == 0:
            return 0
        return 1 if self.change > 0 else -1

    def get_name(self, lang: Optional[str] = None) -> str:
        effective_lang = (lang or get_current_language()).strip().lower()
        translated = tr(f"instruments.{self.symbol}.name", lang=effective_lang, default="")
        if translated:
            return translated
        if effective_lang == "en" and self.name_en:
            return self.name_en
        return self.name_fa

    def get_unit(self, lang: Optional[str] = None) -> str:
        effective_lang = (lang or get_current_language()).strip().lower()
        if effective_lang == "en":
            return self.unit_en or localize_unit(self.unit_fa, lang="en")
        return self.unit_fa

    def get_error_note(self, lang: Optional[str] = None) -> str:
        effective_lang = (lang or get_current_language()).strip().lower()
        if self.error_code:
            return tr(f"status.{self.error_code}", lang=effective_lang, default=self.error_note)
        if effective_lang == "en":
            if self.error_note == "داده ناموجود":
                return tr("status.data_unavailable", lang="en")
            if self.error_note == "مقدار نامعتبر":
                return tr("status.invalid_value", lang="en")
            return tr("status.not_available_note", lang="en")
        return self.error_note or tr("status.not_available_note", lang="fa")


def parse_market_response(
    raw_response: Dict[str, Any],
    previous_prices: Optional[Dict[str, float]] = None,
    price_history: Optional[Dict[str, List[float]]] = None,
    include_secondary: bool = True,
) -> Dict[str, InstrumentQuote]:
    """Parse a raw Navasan-compatible response dict into normalized InstrumentQuote objects.

    Handles missing symbols and malformed values gracefully without raising exceptions.
    """
    prev_map = previous_prices or {}
    hist_map = price_history or {}
    quotes: Dict[str, InstrumentQuote] = {}

    specs: List[InstrumentSpec] = (
        ALL_INSTRUMENTS if include_secondary else PRIMARY_INSTRUMENTS
    )

    if not isinstance(raw_response, dict):
        raw_response = {}

    for spec in specs:
        item = raw_response.get(spec.symbol)
        if not isinstance(item, dict):
            quotes[spec.symbol] = InstrumentQuote(
                symbol=spec.symbol,
                name_fa=spec.name_fa,
                name_en=spec.name_en,
                unit_fa=spec.default_unit_fa,
                unit_en=spec.default_unit_en,
                icon_filename=spec.icon_filename,
                accent_color=spec.accent_color,
                is_primary=spec.is_primary,
                available=False,
                error_note="داده ناموجود",
                error_code="data_unavailable",
            )
            continue

        val = parse_numeric(item.get("value"))
        if val is None or val <= 0:
            quotes[spec.symbol] = InstrumentQuote(
                symbol=spec.symbol,
                name_fa=spec.name_fa,
                name_en=spec.name_en,
                unit_fa=spec.default_unit_fa,
                unit_en=spec.default_unit_en,
                icon_filename=spec.icon_filename,
                accent_color=spec.accent_color,
                is_primary=spec.is_primary,
                available=False,
                error_note="مقدار نامعتبر",
                error_code="invalid_value",
            )
            continue

        chg = parse_numeric(item.get("change"))
        explicit_pct = parse_numeric(item.get("change_pct"))
        prev_val = parse_numeric(item.get("prev_close")) or prev_map.get(spec.symbol)
        pct = calculate_percentage_change(
            current_value=val,
            change=chg,
            previous_value=prev_val,
            explicit_pct=explicit_pct,
        )

        unit_label_fa = determine_instrument_unit(spec.symbol, val, item, lang="fa")
        unit_label_en = localize_unit(unit_label_fa, lang="en")
        ts_val = parse_numeric(item.get("timestamp"))
        ts_int = int(ts_val) if ts_val is not None else None
        date_str = str(item.get("date") or "").strip()
        dollar_rate = parse_numeric(item.get("dollar_rate"))

        # Build sparkline series for visual chart
        hist = list(hist_map.get(spec.symbol) or [])
        if len(hist) < 2:
            # Construct a realistic 6-point curve from previous/change if available
            if chg is not None and chg != 0 and (val - chg) > 0:
                start_v = val - chg
                hist = [
                    start_v,
                    start_v + chg * 0.25,
                    start_v + chg * 0.15,
                    start_v + chg * 0.65,
                    start_v + chg * 0.55,
                    val,
                ]
            else:
                delta = max(val * 0.002, 1.0)
                hist = [
                    val - delta * 0.4,
                    val + delta * 0.2,
                    val - delta * 0.15,
                    val + delta * 0.35,
                    val - delta * 0.05,
                    val,
                ]

        quotes[spec.symbol] = InstrumentQuote(
            symbol=spec.symbol,
            name_fa=spec.name_fa,
            name_en=spec.name_en,
            unit_fa=unit_label_fa,
            unit_en=unit_label_en,
            icon_filename=spec.icon_filename,
            accent_color=spec.accent_color,
            is_primary=spec.is_primary,
            available=True,
            value=val,
            change=chg,
            pct_change=pct,
            timestamp=ts_int,
            date_str=date_str,
            dollar_rate=dollar_rate,
            sparkline=hist,
        )

    return quotes
