"""Theme palettes and stylesheet definitions inspired by Windows 11 and preferred UI."""

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class ThemePalette:
    name: str
    window_bg_start: str
    window_bg_end: str
    window_border: str
    card_bg_start: str
    card_bg_end: str
    card_border: str
    card_hover_border: str
    gold_card_bg_start: str
    gold_card_bg_end: str
    gold_card_border: str
    text_primary: str
    text_secondary: str
    text_muted: str
    accent_green: str
    accent_red: str
    accent_gold: str
    accent_blue: str
    icon_color: str
    badge_bg: str
    banner_warn_bg: str
    banner_warn_border: str
    banner_warn_text: str
    banner_err_bg: str
    banner_err_border: str
    banner_err_text: str
    divider: str


DARK_PALETTE = ThemePalette(
    name="dark",
    window_bg_start="#0D213A",
    window_bg_end="#081526",
    window_border="#28466C",
    card_bg_start="#152E4D",
    card_bg_end="#10233B",
    card_border="#264368",
    card_hover_border="#3B6496",
    gold_card_bg_start="#233246",
    gold_card_bg_end="#16253A",
    gold_card_border="#5C4E2C",
    text_primary="#F8FAFC",
    text_secondary="#94A3B8",
    text_muted="#64748B",
    accent_green="#19D889",
    accent_red="#FF4058",
    accent_gold="#FFD65A",
    accent_blue="#38BDF8",
    icon_color="#CBD5E1",
    badge_bg="#1E3A5F",
    banner_warn_bg="#332711",
    banner_warn_border="#855E17",
    banner_warn_text="#FDE68A",
    banner_err_bg="#38141E",
    banner_err_border="#881D36",
    banner_err_text="#FECDD3",
    divider="#1E3654",
)

LIGHT_PALETTE = ThemePalette(
    name="light",
    window_bg_start="#F8FAFC",
    window_bg_end="#EEF2F6",
    window_border="#CBD5E1",
    card_bg_start="#FFFFFF",
    card_bg_end="#F8FAFC",
    card_border="#E2E8F0",
    card_hover_border="#94A3B8",
    gold_card_bg_start="#FFFDF7",
    gold_card_bg_end="#FEF9E7",
    gold_card_border="#FCD34D",
    text_primary="#0F172A",
    text_secondary="#475569",
    text_muted="#64748B",
    accent_green="#059669",
    accent_red="#E11D48",
    accent_gold="#D97706",
    accent_blue="#0284C7",
    icon_color="#334155",
    badge_bg="#E2E8F0",
    banner_warn_bg="#FEF3C7",
    banner_warn_border="#F59E0B",
    banner_warn_text="#92400E",
    banner_err_bg="#FFE4E6",
    banner_err_border="#F43F5E",
    banner_err_text="#9F1239",
    divider="#E2E8F0",
)


def get_palette(theme: str) -> ThemePalette:
    return LIGHT_PALETTE if theme == "light" else DARK_PALETTE


FONT_FAMILY_STACK = '"Vazirmatn", "IRANSans", "Segoe UI", "Tahoma", sans-serif'
