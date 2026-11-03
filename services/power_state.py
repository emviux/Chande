"""Centralized desktop visibility and operating state management for Chande.

Manages transitions between three operating states:
- ACTIVE: Widget is visible (fully or partially) on the desktop and responds immediately.
- IDLE: Widget is 100% obscured by other windows after a short debounce period.
- DORMANT: Widget is intentionally hidden or minimized by the user; all visibility
  polling and non-essential UI timers are stopped.
"""

from __future__ import annotations
from collections import deque
import ctypes
from ctypes import wintypes
from enum import Enum
import logging
import sys
import time
from typing import Callable, Deque, Optional, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger("chande.power")

# WinEvent constants for low-overhead reactive desktop window change notifications
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0


class WidgetOperatingState(str, Enum):
    """Operating power/visibility states for the Chande desktop widget."""

    ACTIVE = "active"
    IDLE = "idle"
    DORMANT = "dormant"


class _WinEventHookBridge(QObject):
    """Optional Win32 SetWinEventHook bridge for instant wake when foreground/window positions change."""

    window_environment_changed = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._hooks: list[int] = []
        self._proc_ref = None
        self._installed = False

    def install(self) -> bool:
        if self._installed or sys.platform != "win32":
            return self._installed
        try:
            user32 = ctypes.windll.user32
            WINEVENTPROC = ctypes.WINFUNCTYPE(
                None,
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.HWND,
                wintypes.LONG,
                wintypes.LONG,
                wintypes.DWORD,
                wintypes.DWORD,
            )

            def _callback(
                hWinEventHook,
                event,
                hwnd,
                idObject,
                idChild,
                dwEventThread,
                dwmsEventTime,
            ) -> None:
                if idObject != OBJID_WINDOW or not hwnd:
                    return
                try:
                    self.window_environment_changed.emit()
                except RuntimeError:
                    pass

            self._proc_ref = WINEVENTPROC(_callback)
            user32.SetWinEventHook.argtypes = [
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HMODULE,
                WINEVENTPROC,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            user32.SetWinEventHook.restype = wintypes.HANDLE

            flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
            # Hook system foreground, move/size end, and minimize/restore events
            h1 = user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND,
                EVENT_SYSTEM_MINIMIZEEND,
                None,
                self._proc_ref,
                0,
                0,
                flags,
            )
            if h1:
                self._hooks.append(int(h1))

            # Hook top-level window show/hide/location changes
            h2 = user32.SetWinEventHook(
                EVENT_OBJECT_DESTROY,
                EVENT_OBJECT_LOCATIONCHANGE,
                None,
                self._proc_ref,
                0,
                0,
                flags,
            )
            if h2:
                self._hooks.append(int(h2))

            self._installed = len(self._hooks) > 0
            return self._installed
        except Exception:
            self.uninstall()
            return False

    def uninstall(self) -> None:
        if sys.platform == "win32" and self._hooks:
            try:
                user32 = ctypes.windll.user32
                for h in self._hooks:
                    if h:
                        user32.UnhookWinEvent(wintypes.HANDLE(h))
            except Exception:
                pass
        self._hooks.clear()
        self._proc_ref = None
        self._installed = False


class WidgetStateManager(QObject):
    """Centralized controller for `ACTIVE`, `IDLE`, and `DORMANT` widget operating states.

    - Prevents duplicate/conflicting transitions (strictly idempotent).
    - Debounces `ACTIVE -> IDLE` when obscured so brief window switches do not cause state thrashing.
    - Wakes `IDLE -> ACTIVE` with 0 ms debounce as soon as any part of the widget is visible.
    - Enters `DORMANT` immediately when the widget is hidden or minimized by the user,
      halting all visibility polling and timers until shown again.
    """

    state_changed = Signal(str, str)  # (old_state_value, new_state_value)

    def __init__(
        self,
        visibility_checker: Callable[[], bool],
        occlusion_checker: Callable[[], bool],
        obscured_debounce_ms: int = 1200,
        active_poll_interval_ms: int = 750,
        idle_poll_interval_ms: int = 400,
        enable_winevent_hook: bool = True,
        wake_completion_hook: Optional[Callable[[], None]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._visibility_checker = visibility_checker
        self._occlusion_checker = occlusion_checker
        self._obscured_debounce_ms = max(10, int(obscured_debounce_ms))
        self._active_poll_interval_ms = max(40, int(active_poll_interval_ms))
        self._idle_poll_interval_ms = max(40, int(idle_poll_interval_ms))
        self._wake_completion_hook = wake_completion_hook

        self._state: WidgetOperatingState = WidgetOperatingState.ACTIVE
        self._transition_count: int = 0
        self._last_wake_latency_ms: float = 0.0
        self._history: Deque[Tuple[float, str, str]] = deque(maxlen=64)
        self._stopped: bool = False

        # Debounce timer for ACTIVE -> IDLE when obscured
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._on_obscured_debounce_timeout)

        # Coalescing timer for WinEvent notifications (prevents flood during window drag)
        self._event_coalesce_timer = QTimer(self)
        self._event_coalesce_timer.setSingleShot(True)
        self._event_coalesce_timer.setInterval(45)
        self._event_coalesce_timer.timeout.connect(self.evaluate_now)

        # Fallback low-overhead visibility poll timer (paused completely in DORMANT)
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self.evaluate_now)

        self._winevent_bridge: Optional[_WinEventHookBridge] = None
        if enable_winevent_hook and sys.platform == "win32":
            self._winevent_bridge = _WinEventHookBridge(self)
            self._winevent_bridge.window_environment_changed.connect(
                self._on_window_environment_event
            )

    @property
    def state(self) -> WidgetOperatingState:
        """Current operating state enum (`ACTIVE`, `IDLE`, or `DORMANT`)."""
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state == WidgetOperatingState.ACTIVE

    @property
    def is_idle(self) -> bool:
        return self._state == WidgetOperatingState.IDLE

    @property
    def is_dormant(self) -> bool:
        return self._state == WidgetOperatingState.DORMANT

    @property
    def transition_count(self) -> int:
        return self._transition_count

    @property
    def last_wake_latency_ms(self) -> float:
        return self._last_wake_latency_ms

    @property
    def is_polling_active(self) -> bool:
        return self._poll_timer.isActive()

    @property
    def is_debounce_pending(self) -> bool:
        return self._debounce_timer.isActive()

    def set_obscured_debounce_ms(self, debounce_ms: int) -> None:
        self._obscured_debounce_ms = max(10, int(debounce_ms))

    def start(self) -> None:
        """Start monitoring desktop visibility."""
        self._stopped = False
        if self._winevent_bridge is not None:
            self._winevent_bridge.install()
        self.evaluate_now()

    def stop(self) -> None:
        """Cleanly stop all timers and native hooks on shutdown."""
        self._stopped = True
        self._debounce_timer.stop()
        self._event_coalesce_timer.stop()
        self._poll_timer.stop()
        if self._winevent_bridge is not None:
            self._winevent_bridge.uninstall()

    def set_dormant(self, dormant: bool) -> None:
        """Explicitly enter or leave `DORMANT` state (e.g., when hidden/shown via tray or close button)."""
        if self._stopped:
            return
        if dormant:
            self._debounce_timer.stop()
            self._event_coalesce_timer.stop()
            self._poll_timer.stop()
            self._transition_to(WidgetOperatingState.DORMANT, reason="explicit_hide_or_minimize")
        else:
            # Immediately evaluate visibility when shown again
            self.evaluate_now()

    def _on_window_environment_event(self) -> None:
        if self._stopped or self._state == WidgetOperatingState.DORMANT:
            return
        if not self._event_coalesce_timer.isActive():
            self._event_coalesce_timer.start()

    def evaluate_now(self) -> WidgetOperatingState:
        """Immediately inspect visibility and occlusion and update state accordingly."""
        if self._stopped:
            return self._state

        t0 = time.perf_counter()
        try:
            visible = bool(self._visibility_checker())
        except Exception:
            visible = True

        if not visible:
            self._debounce_timer.stop()
            self._event_coalesce_timer.stop()
            self._poll_timer.stop()
            self._transition_to(WidgetOperatingState.DORMANT, reason="widget_not_visible")
            return self._state

        try:
            obscured = bool(self._occlusion_checker())
        except Exception:
            obscured = False

        if not obscured:
            # Widget is at least partially visible on the desktop -> wake immediately!
            if self._debounce_timer.isActive():
                self._debounce_timer.stop()
                logger.debug("Cancelled pending IDLE debounce (widget uncovered before timeout)")

            if self._state != WidgetOperatingState.ACTIVE:
                self._transition_to(
                    WidgetOperatingState.ACTIVE,
                    reason="desktop_visible",
                    wake_start_perf=t0,
                )
            self._ensure_poll_timer(self._active_poll_interval_ms)
        else:
            # Widget is 100% obscured by other windows
            if self._state == WidgetOperatingState.ACTIVE:
                if not self._debounce_timer.isActive():
                    logger.debug(
                        "Widget obscured; starting %d ms IDLE debounce timer",
                        self._obscured_debounce_ms,
                    )
                    self._debounce_timer.start(self._obscured_debounce_ms)
                self._ensure_poll_timer(self._active_poll_interval_ms)
            elif self._state == WidgetOperatingState.DORMANT:
                # Shown while covered -> enter IDLE
                self._transition_to(WidgetOperatingState.IDLE, reason="shown_while_obscured")
                self._ensure_poll_timer(self._idle_poll_interval_ms)
            else:
                # Already IDLE
                self._ensure_poll_timer(self._idle_poll_interval_ms)

        return self._state

    def _on_obscured_debounce_timeout(self) -> None:
        if self._stopped:
            return
        try:
            visible = bool(self._visibility_checker())
        except Exception:
            visible = True

        if not visible:
            self._poll_timer.stop()
            self._transition_to(WidgetOperatingState.DORMANT, reason="hidden_during_debounce")
            return

        try:
            still_obscured = bool(self._occlusion_checker())
        except Exception:
            still_obscured = False

        if still_obscured:
            self._transition_to(WidgetOperatingState.IDLE, reason="obscured_debounce_elapsed")
            self._ensure_poll_timer(self._idle_poll_interval_ms)
        else:
            logger.debug("Widget became visible before debounce completion; remaining ACTIVE")
            self._ensure_poll_timer(self._active_poll_interval_ms)

    def _ensure_poll_timer(self, interval_ms: int) -> None:
        if self._stopped or self._state == WidgetOperatingState.DORMANT:
            self._poll_timer.stop()
            return
        if not self._poll_timer.isActive() or self._poll_timer.interval() != interval_ms:
            self._poll_timer.start(interval_ms)

    def _transition_to(
        self,
        new_state: WidgetOperatingState,
        reason: str = "",
        wake_start_perf: Optional[float] = None,
    ) -> bool:
        """Perform an idempotent state transition and emit `state_changed` once."""
        old_state = self._state
        if old_state == new_state:
            return False

        t_start = wake_start_perf if wake_start_perf is not None else time.perf_counter()
        self._state = new_state
        self._transition_count += 1
        self._history.append((time.time(), old_state.value, new_state.value))

        self.state_changed.emit(old_state.value, new_state.value)

        if new_state == WidgetOperatingState.ACTIVE:
            if self._wake_completion_hook is not None:
                try:
                    self._wake_completion_hook()
                except Exception:
                    pass
            self._last_wake_latency_ms = (time.perf_counter() - t_start) * 1000.0
            logger.debug(
                "State transition: %s -> %s (reason=%s, wake_latency=%.3f ms)",
                old_state.value,
                new_state.value,
                reason,
                self._last_wake_latency_ms,
            )
        else:
            logger.debug(
                "State transition: %s -> %s (reason=%s)",
                old_state.value,
                new_state.value,
                reason,
            )
        return True
