"""Atomic JSON read/write utility to prevent corrupted configuration or cache files."""

from __future__ import annotations
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict

_FILE_LOCK = threading.RLock()


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write a dictionary to a JSON file using a temporary file and os.replace."""
    target = Path(path)
    with _FILE_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=f".{target.stem}_",
            suffix=".tmp",
            dir=str(target.parent),
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp_path), str(target))
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise


def safe_read_json(path: Path, default: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Read a JSON file safely, returning a copy of `default` on missing or corrupted files."""
    target = Path(path)
    fallback = dict(default) if default is not None else {}
    with _FILE_LOCK:
        if not target.exists():
            return fallback
        try:
            with open(target, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return fallback
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    return parsed
                return fallback
        except (OSError, json.JSONDecodeError, ValueError):
            return fallback
