"""Price card widgets with in-place responsive adaptation for Expanded, Compact, and Ultra-Compact modes."""

from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config.constants import InstrumentSpec
from services.formatter import (
    InstrumentQuote,
    format_change_text,
    format_number,
    localize_digits,
)
from services.i18n import (
    get_layout_direction,
    get_leading_alignment,
    get_trailing_alignment,
    is_rtl,
    tr,
)
from ui.icon_loader import load_svg_pixmap
from ui.sparkline import SparklineWidget
from ui.theme import FONT_FAMILY_STACK, ThemePalette, get_palette


class PrimaryPriceCard(QFrame):
    """Main instrument card that adapts in-place across Expanded, Compact, and Ultra-Compact modes.

    - Expanded mode: Full padding, 36px icon, name + unit subtitle, USD badge (for BTC),
      large price, sparkline graph, full daily change + percentage, and freshness age.
    - Compact mode: Reduced padding/gaps/typography, 24px icon, name, price, and concise change
      indicator. Sparkline graph, unit subtitle, USD badge, and freshness label are hidden
      with zero reserved layout space.
    - Ultra-Compact mode: Minimal padding, 18px icon, instrument name and price prioritized
      (arranged on a single horizontal row when stacked in 1 column or short height, or a tight
      2-line card in 2x2). All graphs, containers, secondary labels, and timestamps are hidden
      with zero reserved space.
    """

    def __init__(
        self,
        spec: InstrumentSpec,
        theme: str = "dark",
        lang: str = "fa",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self._theme = theme
        self._lang = "en" if lang == "en" else "fa"
        self._user_wants_chart = True
        self._layout_mode = "expanded"  # "expanded", "compact", "ultra_compact"
        self._single_row_inline = False
        self._power_idle = False
        self._last_quote: Optional[InstrumentQuote] = None
        self._use_persian = self._lang == "fa"
        self._freshness_badge_text = ""

        self.setObjectName(f"primaryCard_{spec.symbol}")
        self.setLayoutDirection(get_layout_direction(self._lang))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(92, 28)

        self._build_ui()
        self._apply_language_direction()
        self.apply_theme(theme)

    @property
    def language(self) -> str:
        return self._lang

    def set_language(self, lang: str, use_persian: Optional[bool] = None) -> None:
        """Switch card language and bidirectional layout direction in-place."""
        norm_lang = "en" if str(lang).lower().startswith("en") else "fa"
        self._lang = norm_lang
        if use_persian is not None:
            self._use_persian = bool(use_persian)
        self._apply_language_direction()
        self._sync_visibility_and_metrics()

    def _apply_language_direction(self) -> None:
        lead_vcenter = get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        trail_vcenter = get_trailing_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        trail_top = get_trailing_alignment(self._lang) | Qt.AlignmentFlag.AlignTop

        self.setLayoutDirection(get_layout_direction(self._lang))
        self.mid_row_widget.setLayoutDirection(get_layout_direction(self._lang))
        self.bottom_row_widget.setLayoutDirection(get_layout_direction(self._lang))

        self.name_label.setText(self.spec.get_name(self._lang))
        self.name_label.setAlignment(lead_vcenter)

        if self._last_quote is None or not self._last_quote.available:
            self.unit_label.setText(self.spec.get_default_unit(self._lang))
        else:
            self.unit_label.setText(self._last_quote.get_unit(self._lang))
        self.unit_label.setAlignment(lead_vcenter)

        self.badge_label.setAlignment(trail_top)
        self.change_label.setAlignment(lead_vcenter)
        self.freshness_label.setAlignment(trail_vcenter)

        desc = tr(f"instruments.{self.spec.symbol}.description", lang=self._lang, default=self.spec.get_name(self._lang))
        self.setToolTip(desc)

    def _build_ui(self) -> None:
        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(14, 12, 14, 10)
        self.root_layout.setSpacing(5)

        # Top row: Icon + Title/Unit on leading side, optional badge or inline price on trailing side
        self.top_row_layout = QHBoxLayout()
        self.top_row_layout.setContentsMargins(0, 0, 0, 0)
        self.top_row_layout.setSpacing(8)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(36, 36)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setPixmap(load_svg_pixmap(self.spec.icon_filename, size=34))
        self.top_row_layout.addWidget(self.icon_label)

        self.title_col = QVBoxLayout()
        self.title_col.setContentsMargins(0, 0, 0, 0)
        self.title_col.setSpacing(1)

        self.name_label = QLabel(self.spec.get_name(self._lang))
        self.name_label.setAlignment(
            get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        )
        self.title_col.addWidget(self.name_label)

        self.unit_label = QLabel(self.spec.get_default_unit(self._lang))
        self.unit_label.setAlignment(
            get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        )
        self.title_col.addWidget(self.unit_label)

        self.top_row_layout.addLayout(self.title_col)
        self.top_row_layout.addStretch(1)

        self.badge_label = QLabel("")
        self.badge_label.setAlignment(
            get_trailing_alignment(self._lang) | Qt.AlignmentFlag.AlignTop
        )
        self.badge_label.hide()
        self.top_row_layout.addWidget(self.badge_label)

        self.root_layout.addLayout(self.top_row_layout)

        # Middle row container: Price on leading side, Sparkline chart on trailing side
        self.mid_row_widget = QWidget(self)
        self.mid_row_layout = QHBoxLayout(self.mid_row_widget)
        self.mid_row_layout.setContentsMargins(0, 1, 0, 1)
        self.mid_row_layout.setSpacing(6)

        self.price_label = QLabel("—")
        self.price_label.setAlignment(
            get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        )
        self.mid_row_layout.addWidget(self.price_label, 1)

        self.sparkline = SparklineWidget(self.mid_row_widget)
        self.mid_row_layout.addWidget(self.sparkline)

        self.root_layout.addWidget(self.mid_row_widget)

        # Bottom row container: Daily change indicator + Freshness timestamp
        self.bottom_row_widget = QWidget(self)
        self.bottom_row_layout = QHBoxLayout(self.bottom_row_widget)
        self.bottom_row_layout.setContentsMargins(0, 0, 0, 0)
        self.bottom_row_layout.setSpacing(4)

        self.change_label = QLabel(tr("status.awaiting_data", lang=self._lang))
        self.change_label.setAlignment(
            get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        )
        self.bottom_row_layout.addWidget(self.change_label, 1)

        self.freshness_label = QLabel("")
        self.freshness_label.setAlignment(
            get_trailing_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        )
        self.bottom_row_layout.addWidget(self.freshness_label)

        self.root_layout.addWidget(self.bottom_row_widget)

    def set_chart_visible(self, visible: bool) -> None:
        """Set user preference for showing the chart (only shown when in expanded mode)."""
        self._user_wants_chart = bool(visible)
        self._sync_visibility_and_metrics()

    def set_responsive_mode(self, mode: str, single_row: bool = False) -> None:
        """Update card layout density in-place without recreating widgets."""
        if mode not in ("expanded", "compact", "ultra_compact"):
            mode = "expanded"
        if self._layout_mode == mode and self._single_row_inline == single_row:
            return
        self._layout_mode = mode
        self._single_row_inline = single_row
        self._sync_visibility_and_metrics()

    @property
    def responsive_mode(self) -> str:
        """Current responsive mode of the card ('expanded', 'compact', or 'ultra_compact')."""
        return self._layout_mode

    @property
    def is_graph_effectively_visible(self) -> bool:
        """Return True if the sparkline graph is currently active and visible in the layout."""
        return self._layout_mode == "expanded" and self._user_wants_chart and not self._single_row_inline

    def set_power_idle(self, idle: bool) -> None:
        """Suspend or resume sparkline rendering and temporary path caches during IDLE/DORMANT states.

        Preserves widget geometry and displayed values so waking is instantaneous with zero layout shift.
        """
        idle_bool = bool(idle)
        if self._power_idle == idle_bool:
            return
        self._power_idle = idle_bool
        if self._power_idle:
            self.sparkline.set_rendering_suspended(True)
        else:
            self.sparkline.set_rendering_suspended(not self.is_graph_effectively_visible)

    def _sync_visibility_and_metrics(self) -> None:
        mode = self._layout_mode
        show_graph = self.is_graph_effectively_visible

        # Move price_label between top_row (inline single-row ultra-compact) and mid_row
        if self._single_row_inline:
            self.mid_row_layout.removeWidget(self.price_label)
            self.top_row_layout.addWidget(
                self.price_label,
                0,
                get_trailing_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter,
            )
            self.price_label.setAlignment(
                get_trailing_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
            )
            self.mid_row_widget.hide()
        else:
            self.top_row_layout.removeWidget(self.price_label)
            if self.mid_row_layout.indexOf(self.price_label) == -1:
                self.mid_row_layout.insertWidget(0, self.price_label, 1)
            self.price_label.setAlignment(
                get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
            )
            self.mid_row_widget.show()

        # Sparkline visibility & rendering suspension (zero space when hidden)
        self.sparkline.set_rendering_suspended((not show_graph) or self._power_idle)
        self.sparkline.setVisible(show_graph)
        if show_graph:
            self.mid_row_layout.setContentsMargins(0, 1, 0, 1)
            self.mid_row_layout.setSpacing(6)
        else:
            self.mid_row_layout.setContentsMargins(0, 0, 0, 0)
            self.mid_row_layout.setSpacing(0)

        if mode == "expanded":
            self.root_layout.setContentsMargins(14, 11, 14, 10)
            self.root_layout.setSpacing(4)
            self.top_row_layout.setSpacing(8)
            self.icon_label.setFixedSize(34, 34)
            self.icon_label.setPixmap(load_svg_pixmap(self.spec.icon_filename, size=32))
            self.unit_label.setVisible(True)
            self.freshness_label.setVisible(True)
            self.bottom_row_widget.setVisible(True)
            self.setMinimumSize(175, 102)
        elif mode == "compact":
            self.root_layout.setContentsMargins(10, 7, 10, 6)
            self.root_layout.setSpacing(2)
            self.top_row_layout.setSpacing(6)
            self.icon_label.setFixedSize(22, 22)
            self.icon_label.setPixmap(load_svg_pixmap(self.spec.icon_filename, size=20))
            self.unit_label.setVisible(False)
            self.badge_label.setVisible(False)
            self.freshness_label.setVisible(False)
            self.bottom_row_widget.setVisible(True)
            self.setMinimumSize(120, 62)
        else:  # "ultra_compact"
            if self._single_row_inline:
                self.root_layout.setContentsMargins(8, 3, 8, 3)
                self.root_layout.setSpacing(0)
                self.top_row_layout.setSpacing(5)
                self.icon_label.setFixedSize(16, 16)
                self.icon_label.setPixmap(load_svg_pixmap(self.spec.icon_filename, size=15))
                self.setMinimumSize(90, 24)
            else:
                self.root_layout.setContentsMargins(7, 4, 7, 4)
                self.root_layout.setSpacing(1)
                self.top_row_layout.setSpacing(4)
                self.icon_label.setFixedSize(16, 16)
                self.icon_label.setPixmap(load_svg_pixmap(self.spec.icon_filename, size=15))
                self.setMinimumSize(88, 40)
            self.unit_label.setVisible(False)
            self.badge_label.setVisible(False)
            self.freshness_label.setVisible(False)
            self.bottom_row_widget.setVisible(False)

        self._apply_typography_and_quote()

    def apply_theme(self, theme: str) -> None:
        self._theme = theme
        self._apply_typography_and_quote()

    def _apply_typography_and_quote(self) -> None:
        pal: ThemePalette = get_palette(self._theme)
        is_gold = self.spec.symbol == "18ayar"
        mode = self._layout_mode

        bg_start = pal.gold_card_bg_start if is_gold else pal.card_bg_start
        bg_end = pal.gold_card_bg_end if is_gold else pal.card_bg_end
        border_col = pal.gold_card_border if is_gold else pal.card_border
        radius = 15 if mode == "expanded" else (11 if mode == "compact" else 8)

        self.setStyleSheet(
            f"""
            QFrame#primaryCard_{self.spec.symbol} {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {bg_start}, stop:1 {bg_end});
                border: 1px solid {border_col};
                border-radius: {radius}px;
            }}
            QFrame#primaryCard_{self.spec.symbol}:hover {{
                border: 1px solid {pal.accent_gold if is_gold else pal.card_hover_border};
            }}
            QWidget {{
                background: transparent;
                border: none;
            }}
            QLabel {{
                background: transparent;
                border: none;
                font-family: {FONT_FAMILY_STACK};
            }}
            """
        )

        name_color = pal.accent_gold if (is_gold and self._theme == "dark") else pal.text_primary
        name_px = 14 if mode == "expanded" else (12 if mode == "compact" else 11)
        self.name_label.setStyleSheet(
            f"color: {name_color}; font-size: {name_px}px; font-weight: 700;"
        )
        self.unit_label.setStyleSheet(
            f"color: {pal.text_secondary}; font-size: 10px; font-weight: 500;"
        )
        self.badge_label.setStyleSheet(
            f"color: {pal.text_secondary}; background-color: {pal.badge_bg}; "
            f"border-radius: 6px; padding: 1px 5px; font-size: 9px; font-weight: 600;"
        )
        self.freshness_label.setStyleSheet(
            f"color: {pal.text_muted}; font-size: 9px;"
        )

        if self._last_quote is not None:
            self.update_quote(
                self._last_quote,
                use_persian=self._use_persian,
                freshness_text=self._freshness_badge_text,
            )
        else:
            base_price_px = 20 if mode == "expanded" else (15 if mode == "compact" else 12)
            self.price_label.setStyleSheet(
                f"color: {pal.text_primary}; font-size: {base_price_px}px; font-weight: 800;"
            )
            self.change_label.setText(tr("status.awaiting_data", lang=self._lang))
            self.change_label.setStyleSheet(
                f"color: {pal.text_muted}; font-size: 11px; font-weight: 600;"
            )

    def update_quote(
        self,
        quote: Optional[InstrumentQuote],
        use_persian: bool = True,
        freshness_text: str = "",
    ) -> None:
        self._last_quote = quote
        self._use_persian = use_persian
        self._freshness_badge_text = freshness_text
        pal = get_palette(self._theme)
        mode = self._layout_mode

        self.name_label.setText(self.spec.get_name(self._lang))
        self.freshness_label.setText(localize_digits(freshness_text, use_persian))

        if quote is None or not quote.available or quote.value is None:
            self.unit_label.setText(self.spec.get_default_unit(self._lang))
            self.price_label.setText(tr("status.unavailable", lang=self._lang))
            na_px = 16 if mode == "expanded" else (13 if mode == "compact" else 11)
            self.price_label.setStyleSheet(
                f"color: {pal.text_muted}; font-size: {na_px}px; font-weight: 700;"
            )
            note = (
                quote.get_error_note(self._lang)
                if quote and quote.get_error_note(self._lang)
                else tr("status.data_unavailable", lang=self._lang)
            )
            self.change_label.setText(note)
            self.change_label.setStyleSheet(
                f"color: {pal.text_muted}; font-size: 10px; font-weight: 500;"
            )
            self.badge_label.hide()
            self.sparkline.set_series([], pal.text_muted)
            return

        self.unit_label.setText(quote.get_unit(self._lang))
        formatted_price = format_number(quote.value, use_persian=use_persian)
        is_long_price = len(formatted_price) >= 12

        if mode == "expanded":
            font_px = 17 if is_long_price else 21
        elif mode == "compact":
            font_px = 13 if is_long_price else 15
        else:  # "ultra_compact"
            font_px = 11 if is_long_price else 13

        self.price_label.setStyleSheet(
            f"color: {pal.text_primary}; font-size: {font_px}px; font-weight: 800;"
        )
        self.price_label.setText(formatted_price)

        # Optional secondary USD badge for BTC when dollar_rate is provided (only in expanded mode)
        if mode == "expanded" and quote.dollar_rate is not None and quote.unit_fa == "تومان":
            usd_str = format_number(quote.dollar_rate, use_persian=use_persian)
            self.badge_label.setText(f"${usd_str}")
            self.badge_label.show()
        else:
            self.badge_label.hide()

        # Daily change formatting (full in expanded, concise in compact)
        if mode == "compact" and quote.pct_change is not None:
            abs_pct = abs(quote.pct_change)
            pct_str = localize_digits(
                f"{abs_pct:.2f}".rstrip("0").rstrip("."), use_persian=use_persian
            )
            sym = "٪" if use_persian else "%"
            arrow = "▲ " if quote.direction > 0 else ("▼ " if quote.direction < 0 else "• ")
            chg_str = f"{arrow}{pct_str}{sym}"
        else:
            include_pct = quote.pct_change is not None
            chg_str = format_change_text(
                quote.change,
                quote.pct_change,
                use_persian=use_persian,
                include_pct=include_pct,
                lang=self._lang,
            )
        self.change_label.setText(chg_str)

        if quote.direction > 0:
            chg_color = pal.accent_green
            chart_color = pal.accent_gold if self.spec.symbol == "18ayar" else pal.accent_green
        elif quote.direction < 0:
            chg_color = pal.accent_red
            chart_color = pal.accent_red
        else:
            chg_color = pal.text_secondary
            chart_color = pal.accent_gold if self.spec.symbol == "18ayar" else pal.accent_green

        chg_px = 11 if mode == "expanded" else 10
        self.change_label.setStyleSheet(
            f"color: {chg_color}; font-size: {chg_px}px; font-weight: 700;"
        )
        self.sparkline.set_series(quote.sparkline, chart_color)


class SecondaryMiniCard(QFrame):
    """Compact mini-card for the bottom 4-instrument row in Expanded mode."""

    def __init__(
        self,
        spec: InstrumentSpec,
        theme: str = "dark",
        lang: str = "fa",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self._theme = theme
        self._lang = "en" if lang == "en" else "fa"
        self._last_quote: Optional[InstrumentQuote] = None
        self._use_persian = self._lang == "fa"

        self.setObjectName(f"miniCard_{spec.symbol}")
        self.setLayoutDirection(get_layout_direction(self._lang))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumSize(85, 62)

        root = QVBoxLayout(self)
        root.setContentsMargins(9, 6, 9, 6)
        root.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(5)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(18, 18)
        self.icon_label.setPixmap(load_svg_pixmap(spec.icon_filename, size=18))
        top.addWidget(self.icon_label)

        self.name_label = QLabel(spec.get_name(self._lang))
        self.name_label.setAlignment(
            get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        )
        top.addWidget(self.name_label, 1)
        root.addLayout(top)

        self.price_label = QLabel("—")
        self.price_label.setAlignment(
            get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        )
        root.addWidget(self.price_label)

        self.change_label = QLabel("—")
        self.change_label.setAlignment(
            get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        )
        root.addWidget(self.change_label)

        self._apply_language_direction()
        self.apply_theme(theme)

    @property
    def language(self) -> str:
        return self._lang

    def set_language(self, lang: str, use_persian: Optional[bool] = None) -> None:
        norm_lang = "en" if str(lang).lower().startswith("en") else "fa"
        self._lang = norm_lang
        if use_persian is not None:
            self._use_persian = bool(use_persian)
        self._apply_language_direction()
        if self._last_quote is not None:
            self.update_quote(self._last_quote, use_persian=self._use_persian)

    def _apply_language_direction(self) -> None:
        lead_vcenter = get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        self.setLayoutDirection(get_layout_direction(self._lang))
        self.name_label.setText(self.spec.get_name(self._lang))
        self.name_label.setAlignment(lead_vcenter)
        self.price_label.setAlignment(lead_vcenter)
        self.change_label.setAlignment(lead_vcenter)
        desc = tr(f"instruments.{self.spec.symbol}.description", lang=self._lang, default=self.spec.get_name(self._lang))
        self.setToolTip(desc)

    def apply_theme(self, theme: str) -> None:
        self._theme = theme
        pal = get_palette(theme)
        self.setStyleSheet(
            f"""
            QFrame#miniCard_{self.spec.symbol} {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {pal.card_bg_start}, stop:1 {pal.card_bg_end});
                border: 1px solid {pal.card_border};
                border-radius: 11px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                font-family: {FONT_FAMILY_STACK};
            }}
            """
        )
        self.name_label.setStyleSheet(
            f"color: {pal.text_secondary}; font-size: 11px; font-weight: 600;"
        )
        self.price_label.setStyleSheet(
            f"color: {pal.text_primary}; font-size: 13px; font-weight: 800;"
        )
        if self._last_quote is not None:
            self.update_quote(self._last_quote, use_persian=self._use_persian)
        else:
            self.change_label.setStyleSheet(
                f"color: {pal.text_muted}; font-size: 10px; font-weight: 600;"
            )

    def update_quote(
        self, quote: Optional[InstrumentQuote], use_persian: bool = True
    ) -> None:
        self._last_quote = quote
        self._use_persian = use_persian
        pal = get_palette(self._theme)
        self.name_label.setText(self.spec.get_name(self._lang))

        if quote is None or not quote.available or quote.value is None:
            self.price_label.setText("—")
            self.change_label.setText(tr("status.unavailable", lang=self._lang))
            self.change_label.setStyleSheet(
                f"color: {pal.text_muted}; font-size: 10px;"
            )
            return

        self.price_label.setText(format_number(quote.value, use_persian=use_persian))

        if quote.pct_change is not None:
            abs_pct = abs(quote.pct_change)
            pct_str = localize_digits(
                f"{abs_pct:.1f}".rstrip("0").rstrip("."), use_persian=use_persian
            )
            sym = "٪" if use_persian else "%"
            arrow = "▲ " if quote.direction > 0 else ("▼ " if quote.direction < 0 else "• ")
            self.change_label.setText(f"{arrow}{pct_str}{sym}")
        else:
            self.change_label.setText(
                format_change_text(
                    quote.change,
                    None,
                    use_persian=use_persian,
                    include_pct=False,
                    lang=self._lang,
                )
            )

        color = (
            pal.accent_green
            if quote.direction > 0
            else (pal.accent_red if quote.direction < 0 else pal.text_secondary)
        )
        self.change_label.setStyleSheet(
            f"color: {color}; font-size: 10px; font-weight: 700;"
        )


class CompactPriceRow(QFrame):
    """Single horizontal row retained for compatibility."""

    def __init__(
        self,
        spec: InstrumentSpec,
        theme: str = "dark",
        lang: str = "fa",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self._theme = theme
        self._lang = "en" if lang == "en" else "fa"
        self._last_quote: Optional[InstrumentQuote] = None
        self._use_persian = self._lang == "fa"

        self.setObjectName(f"compactRow_{spec.symbol}")
        self.setLayoutDirection(get_layout_direction(self._lang))
        self.setMinimumHeight(28)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 2, 8, 2)
        row.setSpacing(6)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(20, 20)
        self.icon_label.setPixmap(load_svg_pixmap(spec.icon_filename, size=20))
        row.addWidget(self.icon_label)

        self.name_label = QLabel(spec.get_name(self._lang))
        self.name_label.setAlignment(
            get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.name_label, 1)

        self.price_label = QLabel("—")
        self.price_label.setAlignment(
            get_trailing_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.price_label)

        self.change_label = QLabel("—")
        self.change_label.setMinimumWidth(52)
        self.change_label.setAlignment(
            get_trailing_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.change_label)

        self._apply_language_direction()
        self.apply_theme(theme)

    @property
    def language(self) -> str:
        return self._lang

    def set_language(self, lang: str, use_persian: Optional[bool] = None) -> None:
        norm_lang = "en" if str(lang).lower().startswith("en") else "fa"
        self._lang = norm_lang
        if use_persian is not None:
            self._use_persian = bool(use_persian)
        self._apply_language_direction()
        if self._last_quote is not None:
            self.update_quote(self._last_quote, use_persian=self._use_persian)

    def _apply_language_direction(self) -> None:
        lead_vcenter = get_leading_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        trail_vcenter = get_trailing_alignment(self._lang) | Qt.AlignmentFlag.AlignVCenter
        self.setLayoutDirection(get_layout_direction(self._lang))
        self.name_label.setText(self.spec.get_name(self._lang))
        self.name_label.setAlignment(lead_vcenter)
        self.price_label.setAlignment(trail_vcenter)
        self.change_label.setAlignment(trail_vcenter)

    def apply_theme(self, theme: str) -> None:
        self._theme = theme
        pal = get_palette(theme)
        self.setStyleSheet(
            f"""
            QFrame#compactRow_{self.spec.symbol} {{
                background: transparent;
                border-bottom: 1px solid {pal.divider};
            }}
            QLabel {{
                background: transparent;
                border: none;
                font-family: {FONT_FAMILY_STACK};
            }}
            """
        )
        self.name_label.setStyleSheet(
            f"color: {pal.text_primary}; font-size: 12px; font-weight: 600;"
        )
        self.price_label.setStyleSheet(
            f"color: {pal.text_primary}; font-size: 13px; font-weight: 800;"
        )
        if self._last_quote is not None:
            self.update_quote(self._last_quote, use_persian=self._use_persian)
        else:
            self.change_label.setStyleSheet(
                f"color: {pal.text_muted}; font-size: 10px;"
            )

    def update_quote(
        self, quote: Optional[InstrumentQuote], use_persian: bool = True
    ) -> None:
        self._last_quote = quote
        self._use_persian = use_persian
        pal = get_palette(self._theme)
        self.name_label.setText(self.spec.get_name(self._lang))

        if quote is None or not quote.available or quote.value is None:
            self.price_label.setText(tr("status.unavailable", lang=self._lang))
            self.change_label.setText("—")
            self.change_label.setStyleSheet(f"color: {pal.text_muted}; font-size: 10px;")
            return

        self.price_label.setText(format_number(quote.value, use_persian=use_persian))

        if quote.pct_change is not None:
            abs_pct = abs(quote.pct_change)
            pct_str = localize_digits(
                f"{abs_pct:.1f}".rstrip("0").rstrip("."), use_persian=use_persian
            )
            sym = "٪" if use_persian else "%"
            arrow = "▲ " if quote.direction > 0 else ("▼ " if quote.direction < 0 else "• ")
            self.change_label.setText(f"{arrow}{pct_str}{sym}")
        else:
            self.change_label.setText(
                format_change_text(
                    quote.change,
                    None,
                    use_persian=use_persian,
                    include_pct=False,
                    lang=self._lang,
                )
            )

        color = (
            pal.accent_green
            if quote.direction > 0
            else (pal.accent_red if quote.direction < 0 else pal.text_secondary)
        )
        self.change_label.setStyleSheet(
            f"color: {color}; font-size: 10px; font-weight: 700;"
        )
