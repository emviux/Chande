"""Smooth sparkline chart widget with gradient area fill matching preferred UI."""

from __future__ import annotations
from typing import List, Optional, Tuple

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget


class SparklineWidget(QWidget):
    """Paints a smooth mini trend line with a vertical gradient fill underneath.

    Performs purely in-memory drawing during paintEvent; never triggers network calls.
    Suspends path computation and painting while hidden or explicitly suspended.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._values: List[float] = []
        self._line_color: QColor = QColor("#19D889")
        self._rendering_suspended: bool = False
        self._cached_key: Optional[Tuple[int, int, Tuple[float, ...]]] = None
        self._cached_paths: Optional[Tuple[QPainterPath, QPainterPath]] = None

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setMinimumSize(64, 28)
        self.setMaximumSize(115, 46)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(94, 40)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(64, 28)

    def set_rendering_suspended(self, suspended: bool) -> None:
        """Suspend or resume painting when the sparkline is hidden in compact or idle modes."""
        if self._rendering_suspended == suspended:
            return
        self._rendering_suspended = suspended
        if suspended:
            self.release_cached_buffers()
        elif self.isVisible():
            self.update()

    def release_cached_buffers(self) -> None:
        """Release temporary cached QPainterPath buffers while keeping series values in memory."""
        self._cached_key = None
        self._cached_paths = None

    @property
    def has_cached_buffers(self) -> bool:
        return self._cached_paths is not None

    @property
    def is_rendering_suspended(self) -> bool:
        return self._rendering_suspended

    def set_series(self, values: List[float], color_hex: str) -> None:
        new_vals = list(values) if values else []
        new_color = QColor(color_hex)
        if new_vals != self._values:
            self._values = new_vals
            self._cached_key = None
            self._cached_paths = None
        self._line_color = new_color
        if self.isVisible() and not self._rendering_suspended:
            self.update()

    def _build_paths(self, w: int, h: int) -> Tuple[QPainterPath, QPainterPath]:
        key = (w, h, tuple(self._values))
        if self._cached_key == key and self._cached_paths is not None:
            return self._cached_paths

        pad_x = 3.0
        pad_top = 5.0
        pad_bottom = 5.0
        usable_w = max(8.0, float(w) - 2.0 * pad_x)
        usable_h = max(8.0, float(h) - pad_top - pad_bottom)

        if len(self._values) < 2:
            # Graceful flat baseline when historical series has fewer than 2 points
            mid_y = pad_top + 0.5 * usable_h
            points = [QPointF(pad_x, mid_y), QPointF(pad_x + usable_w, mid_y)]
        else:
            min_v = min(self._values)
            max_v = max(self._values)
            span = max_v - min_v
            n = len(self._values)
            points = []
            for i, val in enumerate(self._values):
                x = pad_x + (i / (n - 1)) * usable_w
                norm_y = 0.5 if span <= 1e-9 else (val - min_v) / span
                y = pad_top + (1.0 - norm_y) * usable_h
                points.append(QPointF(x, y))

        line_path = QPainterPath()
        line_path.moveTo(points[0])
        for pt in points[1:]:
            line_path.lineTo(pt)

        fill_path = QPainterPath(line_path)
        fill_path.lineTo(QPointF(points[-1].x(), float(h) - 2.0))
        fill_path.lineTo(QPointF(points[0].x(), float(h) - 2.0))
        fill_path.closeSubpath()

        self._cached_key = key
        self._cached_paths = (line_path, fill_path)
        return line_path, fill_path

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if self._rendering_suspended or not self.isVisible():
            return
        w = self.width()
        h = self.height()
        if w < 10 or h < 10 or not self._values:
            return

        line_path, fill_path = self._build_paths(w, h)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        grad = QLinearGradient(0.0, 4.0, 0.0, float(h))
        top_fill = QColor(self._line_color)
        top_fill.setAlpha(68 if len(self._values) >= 2 else 28)
        bottom_fill = QColor(self._line_color)
        bottom_fill.setAlpha(0)
        grad.setColorAt(0.0, top_fill)
        grad.setColorAt(1.0, bottom_fill)

        painter.fillPath(fill_path, grad)

        pen = QPen(self._line_color, 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if len(self._values) < 2:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawPath(line_path)
        painter.end()
