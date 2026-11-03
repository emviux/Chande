"""Centralized bilingual translation manager and RTL/LTR direction utilities for Chande."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, Optional, Set

from PySide6.QtCore import Qt

from config.constants import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, TRANSLATIONS_DIR

# Unicode directional isolate/mark characters for mixed-direction (BiDi) text
LRM = "\u200e"  # Left-to-Right Mark
RLM = "\u200f"  # Right-to-Left Mark
LRI = "\u2066"  # Left-to-Right Isolate
RLI = "\u2067"  # Right-to-Left Isolate
PDI = "\u2069"  # Pop Directional Isolate


class TranslationManager:
    """Loads and resolves localized strings from JSON translation bundles (`fa.json`, `en.json`)."""

    def __init__(self, translations_dir: Path = TRANSLATIONS_DIR) -> None:
        self.translations_dir = Path(translations_dir)
        self._current_language: str = DEFAULT_LANGUAGE
        self._catalogs: Dict[str, Dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        """Load or reload all supported language JSON files from disk."""
        self._catalogs.clear()
        for lang in SUPPORTED_LANGUAGES:
            path = self.translations_dir / f"{lang}.json"
            if path.is_file():
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        data = json.load(fh)
                        if isinstance(data, dict):
                            self._catalogs[lang] = data
                        else:
                            self._catalogs[lang] = {}
                except Exception:
                    self._catalogs[lang] = {}
            else:
                self._catalogs[lang] = {}

    @property
    def current_language(self) -> str:
        return self._current_language

    def set_language(self, lang: str) -> str:
        norm = (lang or "").strip().lower()
        if norm not in SUPPORTED_LANGUAGES:
            norm = DEFAULT_LANGUAGE
        self._current_language = norm
        return self._current_language

    def get_catalog(self, lang: str) -> Dict[str, Any]:
        norm = (lang or "").strip().lower()
        return self._catalogs.get(norm, {})

    @staticmethod
    def _lookup_nested(catalog: Dict[str, Any], key: str) -> Optional[str]:
        if not key or not isinstance(catalog, dict):
            return None
        node: Any = catalog
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node if isinstance(node, str) else None

    def translate(
        self,
        key: str,
        lang: Optional[str] = None,
        default: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Resolve `key` in `lang` (or current language), falling back gracefully if missing."""
        target_lang = (lang or self._current_language or DEFAULT_LANGUAGE).strip().lower()
        if target_lang not in SUPPORTED_LANGUAGES:
            target_lang = DEFAULT_LANGUAGE

        raw = self._lookup_nested(self._catalogs.get(target_lang, {}), key)
        if raw is None:
            # Fallback to the other supported language before using `default` or key name
            for fallback_lang in SUPPORTED_LANGUAGES:
                if fallback_lang != target_lang:
                    raw = self._lookup_nested(self._catalogs.get(fallback_lang, {}), key)
                    if raw is not None:
                        break

        if raw is None:
            if default is not None:
                raw = default
            else:
                # Safe human-readable fallback from key tail
                tail = key.rsplit(".", 1)[-1].replace("_", " ").strip()
                raw = tail if tail else key

        if kwargs:
            try:
                return raw.format(**kwargs)
            except Exception:
                return raw
        return raw

    def get_all_keys(self, lang: str) -> Set[str]:
        """Return a flat set of all dot-separated leaf keys in the specified language catalog."""
        catalog = self.get_catalog(lang)
        keys: Set[str] = set()

        def _walk(prefix: str, obj: Any) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    next_key = f"{prefix}.{k}" if prefix else str(k)
                    _walk(next_key, v)
            elif isinstance(obj, str):
                keys.add(prefix)

        _walk("", catalog)
        return keys


# Module-level singleton instance
_MANAGER = TranslationManager()


def get_translation_manager() -> TranslationManager:
    return _MANAGER


def get_current_language() -> str:
    return _MANAGER.current_language


def set_current_language(lang: str) -> str:
    return _MANAGER.set_language(lang)


def tr(
    key: str,
    lang: Optional[str] = None,
    default: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """Translate dot-separated `key` using the active or specified language."""
    return _MANAGER.translate(key, lang=lang, default=default, **kwargs)


def is_rtl(lang: Optional[str] = None) -> bool:
    """Return True if the specified or active language uses Right-to-Left layout."""
    target = (lang or _MANAGER.current_language).strip().lower()
    return target == "fa"


def get_layout_direction(lang: Optional[str] = None) -> Qt.LayoutDirection:
    """Return the Qt layout direction (`RightToLeft` for Persian, `LeftToRight` for English)."""
    return (
        Qt.LayoutDirection.RightToLeft
        if is_rtl(lang)
        else Qt.LayoutDirection.LeftToRight
    )


def get_leading_alignment(lang: Optional[str] = None) -> Qt.AlignmentFlag:
    """Return natural reading-start alignment (`AlignRight` in Persian RTL, `AlignLeft` in English LTR)."""
    horiz = (
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute
        if is_rtl(lang)
        else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignAbsolute
    )
    return horiz | Qt.AlignmentFlag.AlignVCenter


def get_trailing_alignment(lang: Optional[str] = None) -> Qt.AlignmentFlag:
    """Return natural reading-end alignment (`AlignLeft` in Persian RTL, `AlignRight` in English LTR)."""
    horiz = (
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignAbsolute
        if is_rtl(lang)
        else Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute
    )
    return horiz | Qt.AlignmentFlag.AlignVCenter


def wrap_ltr_token(token: str) -> str:
    """Wrap an inline LTR token (e.g. ticker, +/- number, URL, HTTP code) so it stays LTR inside RTL text."""
    if not token:
        return ""
    return f"{LRI}{token}{PDI}"


def format_bidi_text(text: str, lang: Optional[str] = None) -> str:
    """Ensure mixed-direction text (Persian + USD/BTC/USDT/numbers) renders with proper directional context."""
    if not text:
        return ""
    mark = RLM if is_rtl(lang) else LRM
    if text.startswith((RLM, LRM)):
        return text
    return f"{mark}{text}"
