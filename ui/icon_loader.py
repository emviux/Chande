"""SVG icon loader and renderer with dynamic currentColor support and in-memory caching."""

from __future__ import annotations
from pathlib import Path
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from config.constants import ICONS_DIR

_PIXMAP_CACHE: Dict[Tuple[str, int, str], QPixmap] = {}


def load_svg_pixmap(
    filename: str,
    size: int = 24,
    color: Optional[str] = None,
    icons_dir: Path = ICONS_DIR,
) -> QPixmap:
    """Render an SVG file from `assets/icons` into a crisp high-DPI QPixmap.

    Caches rendered pixmaps in memory so UI repaints perform zero disk I/O.
    """
    cache_key = (filename, size, color or "")
    if cache_key in _PIXMAP_CACHE:
        return _PIXMAP_CACHE[cache_key]

    svg_path = Path(icons_dir) / filename
    if not svg_path.exists():
        empty = QPixmap(size, size)
        empty.fill(Qt.GlobalColor.transparent)
        return empty

    try:
        svg_text = svg_path.read_text(encoding="utf-8")
    except OSError:
        empty = QPixmap(size, size)
        empty.fill(Qt.GlobalColor.transparent)
        return empty

    if color:
        svg_text = svg_text.replace("currentColor", color)

    renderer = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
    scale = 2  # Render at 2x for crisp HiDPI display
    img = QImage(size * scale, size * scale, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)

    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(0, 0, size * scale, size * scale))
    painter.end()

    pixmap = QPixmap.fromImage(img)
    pixmap.setDevicePixelRatio(float(scale))
    _PIXMAP_CACHE[cache_key] = pixmap
    return pixmap


def load_svg_icon(
    filename: str,
    size: int = 24,
    color: Optional[str] = None,
    icons_dir: Path = ICONS_DIR,
) -> QIcon:
    """Return a QIcon backed by the rendered SVG pixmap."""
    return QIcon(load_svg_pixmap(filename, size=size, color=color, icons_dir=icons_dir))
