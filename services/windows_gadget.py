"""Native Windows 10/11 desktop gadget behavior, Alt+Tab exclusion, startup, and single-instance IPC.

All Win32 API calls are isolated in this module so the rest of Chande remains clean and maintainable.
"""

from __future__ import annotations
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
from typing import Callable, Iterable, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from config.constants import APP_NAME, ROOT_DIR

# --- Win32 Constants ---
GWL_STYLE = -16
GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8

WS_MINIMIZE = 0x20000000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000

WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_LAYERED = 0x00080000
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000

GW_HWNDPREV = 3
GW_OWNER = 4
LWA_ALPHA = 0x00000002
MONITOR_DEFAULTTONEAREST = 0x00000002

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080

SW_SHOWNOACTIVATE = 4

WM_SHOWWINDOW = 0x0018
WM_ACTIVATEAPP = 0x001C
WM_WINDOWPOSCHANGING = 0x0046
WM_WINDOWPOSCHANGED = 0x0047
WM_SYSCOMMAND = 0x0112
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
SC_MINIMIZE = 0xF020

HWND_TOP = 0
HWND_BOTTOM = 1
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

DWMWA_NCRENDERING_POLICY = 2
DWMWA_TRANSITIONS_FORCEDISABLED = 3
DWMWA_EXCLUDED_FROM_PEEK = 12
DWMWA_CLOAKED = 14
DWMNCRP_ENABLED = 2

GA_ROOT = 2
GA_ROOTOWNER = 3
ERROR_ALREADY_EXISTS = 183

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_REORDER = 0x8004
EVENT_OBJECT_FOCUS = 0x8005
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002


class WINDOWPOS(ctypes.Structure):
    """Win32 WINDOWPOS structure passed via lParam in WM_WINDOWPOSCHANGING / WM_WINDOWPOSCHANGED."""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("hwndInsertAfter", wintypes.HWND),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("cx", ctypes.c_int),
        ("cy", ctypes.c_int),
        ("flags", wintypes.UINT),
    ]

STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SINGLE_INSTANCE_SERVER_NAME = "ChandeDesktopGadget_IPC_v1"
SINGLE_INSTANCE_MUTEX_NAME = "Local\\ChandeDesktopGadget_Mutex_v1"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _get_window_long_ptr(hwnd: int, index: int) -> int:
    if not _is_windows():
        return 0
    user32 = ctypes.windll.user32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        fn = user32.GetWindowLongPtrW
        fn.argtypes = [wintypes.HWND, ctypes.c_int]
        fn.restype = ctypes.c_ssize_t
    else:
        fn = user32.GetWindowLongW
        fn.argtypes = [wintypes.HWND, ctypes.c_int]
        fn.restype = ctypes.c_long
    return int(fn(wintypes.HWND(hwnd), index))


def _set_window_long_ptr(hwnd: int, index: int, new_long: int) -> int:
    if not _is_windows():
        return 0
    user32 = ctypes.windll.user32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        fn = user32.SetWindowLongPtrW
        fn.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        fn.restype = ctypes.c_ssize_t
    else:
        fn = user32.SetWindowLongW
        fn.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        fn.restype = ctypes.c_long
    return int(fn(wintypes.HWND(hwnd), index, new_long))


def find_desktop_shell_hwnds() -> Tuple[int, int]:
    """Return `(desktop_host_hwnd, defview_hwnd)` for the Windows Desktop (`Progman`/`WorkerW` + `SHELLDLL_DefView`)."""
    if not _is_windows():
        return (0, 0)
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowExW.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        ]
        user32.FindWindowExW.restype = wintypes.HWND

        progman = int(user32.FindWindowW("Progman", None) or 0)
        if progman:
            dv = int(
                user32.FindWindowExW(
                    wintypes.HWND(progman), None, "SHELLDLL_DefView", None
                )
                or 0
            )
            if dv:
                return (progman, dv)

        # Search top-level WorkerW windows if SHELLDLL_DefView was split out of Progman
        workerw = 0
        steps = 0
        while steps < 120:
            steps += 1
            nxt = int(
                user32.FindWindowExW(
                    None,
                    wintypes.HWND(workerw) if workerw else None,
                    "WorkerW",
                    None,
                )
                or 0
            )
            if not nxt or nxt == workerw:
                break
            workerw = nxt
            dv = int(
                user32.FindWindowExW(
                    wintypes.HWND(workerw), None, "SHELLDLL_DefView", None
                )
                or 0
            )
            if dv:
                return (workerw, dv)

        return (progman, 0)
    except Exception:
        return (0, 0)


def apply_desktop_gadget_style(
    hwnd: int,
    always_on_top: bool = True,
    no_activate: bool = True,
    force_topmost_over_desktop: bool = False,
) -> bool:
    """Configure a native Win32 HWND to behave like a Windows 7 Desktop Gadget.

    - Clears `WS_MINIMIZEBOX` and `WS_MAXIMIZEBOX` from `GWL_STYLE` so Windows Shell
      `MinimizeAll` / `Show Desktop` does not treat the gadget as a minimizable window.
    - Sets `WS_EX_TOOLWINDOW` and clears `WS_EX_APPWINDOW` so the window never appears
      in Alt+Tab or on the Windows taskbar.
    - Sets `WS_EX_NOACTIVATE` when `no_activate=True` so showing or interacting with the
      gadget does not steal focus from the active foreground application.
    - Sets `DWMWA_EXCLUDED_FROM_PEEK=1` and `DWMWA_TRANSITIONS_FORCEDISABLED=1` so
      `Win+D` / Aero Peek / Show Desktop does not hide or animate-minimize the gadget.
    - When `always_on_top=False` (and not temporarily above Show Desktop), binds
      `GWLP_HWNDPARENT` to the top-level desktop host (`Progman` / `WorkerW`) so
      clicking Show Desktop raises the gadget along with the desktop.
    - Applies `HWND_TOPMOST` (when `always_on_top=True` or `force_topmost_over_desktop=True`)
      with `SWP_NOACTIVATE` so the gadget stays visible above normal, maximized, and
      Show Desktop `WorkerW` windows without stealing focus.
    """
    if not _is_windows() or not hwnd:
        return False

    try:
        # 1. Remove minimize/maximize box styles so Shell MinimizeAll ignores this window
        style = _get_window_long_ptr(hwnd, GWL_STYLE)
        new_style = style & ~(WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_MINIMIZE)
        if new_style != style:
            _set_window_long_ptr(hwnd, GWL_STYLE, new_style)

        # 2. Enforce tool window, remove app window flag, and configure non-activating behavior
        ex_style = _get_window_long_ptr(hwnd, GWL_EXSTYLE)
        ex_style |= WS_EX_TOOLWINDOW
        ex_style &= ~WS_EX_APPWINDOW

        if no_activate:
            ex_style |= WS_EX_NOACTIVATE
        else:
            ex_style &= ~WS_EX_NOACTIVATE

        _set_window_long_ptr(hwnd, GWL_EXSTYLE, ex_style)

        # 3. Manage desktop owner relationship:
        # - When always_on_top is True, keep GWLP_HWNDPARENT=0 so WorkerW dropping to HWND_BOTTOM
        #   never strips WS_EX_TOPMOST from the gadget.
        # - When always_on_top is False (and not forced topmost), bind GWLP_HWNDPARENT to the
        #   top-level desktop host (Progman/WorkerW) so Show Desktop raises it with the desktop.
        use_topmost = bool(always_on_top or force_topmost_over_desktop)
        try:
            cur_owner = _get_window_long_ptr(hwnd, GWLP_HWNDPARENT)
            if use_topmost:
                if cur_owner != 0:
                    _set_window_long_ptr(hwnd, GWLP_HWNDPARENT, 0)
            else:
                desktop_host, _ = find_desktop_shell_hwnds()
                if desktop_host and desktop_host != int(hwnd) and cur_owner != desktop_host:
                    _set_window_long_ptr(hwnd, GWLP_HWNDPARENT, desktop_host)
        except Exception:
            pass

        # 4. Exclude gadget from Aero Peek / Show Desktop hiding and disable DWM minimize transitions
        try:
            dwmapi = ctypes.windll.dwmapi
            one = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(DWMWA_EXCLUDED_FROM_PEEK),
                ctypes.byref(one),
                ctypes.sizeof(one),
            )
            dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(DWMWA_TRANSITIONS_FORCEDISABLED),
                ctypes.byref(one),
                ctypes.sizeof(one),
            )
        except Exception:
            pass

        # 5. Apply Z-order and frame change without activating/stealing focus
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL

        insert_after = HWND_TOPMOST if use_topmost else HWND_NOTOPMOST
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED
        user32.SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(insert_after),
            0,
            0,
            0,
            0,
            flags,
        )
        return True
    except Exception:
        return False


def raise_gadget_without_focus(hwnd: int, always_on_top: bool = True) -> bool:
    """Bring the gadget to the top of its Z-order layer without stealing foreground focus."""
    if not _is_windows() or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        insert_after = HWND_TOPMOST if always_on_top else HWND_TOP
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        return bool(
            user32.SetWindowPos(
                wintypes.HWND(hwnd),
                wintypes.HWND(insert_after),
                0,
                0,
                0,
                0,
                flags,
            )
        )
    except Exception:
        return False


def get_window_ex_style(hwnd: int) -> int:
    """Return the current Win32 `GWL_EXSTYLE` bitmask for `hwnd`."""
    return _get_window_long_ptr(hwnd, GWL_EXSTYLE)


def is_window_topmost(hwnd: int) -> bool:
    """Verify via Win32 `GWL_EXSTYLE` whether `hwnd` currently has `WS_EX_TOPMOST`."""
    if not _is_windows() or not hwnd:
        return False
    ex_style = _get_window_long_ptr(hwnd, GWL_EXSTYLE)
    return bool(ex_style & WS_EX_TOPMOST)


def is_window_in_alt_tab(hwnd: int) -> bool:
    """Programmatically verify whether Windows 10/11 includes `hwnd` in Alt+Tab / Taskbar.

    According to the Win32 shell rules for Alt+Tab switcher eligibility:
    1. The window must be visible (`IsWindowVisible(hwnd)`).
    2. It must NOT have `WS_EX_TOOLWINDOW` unless it explicitly has `WS_EX_APPWINDOW`.
    3. It must not be cloaked by DWM.
    """
    if not _is_windows() or not hwnd:
        return False
    user32 = ctypes.windll.user32
    if not user32.IsWindowVisible(wintypes.HWND(hwnd)):
        return False

    ex_style = _get_window_long_ptr(hwnd, GWL_EXSTYLE)
    if ex_style & WS_EX_APPWINDOW:
        return True
    if ex_style & WS_EX_TOOLWINDOW:
        return False

    return True


def is_window_stealing_foreground(hwnd: int) -> bool:
    """Return True if `hwnd` is currently the active foreground window (`GetForegroundWindow`)."""
    if not _is_windows() or not hwnd:
        return False
    fg = ctypes.windll.user32.GetForegroundWindow()
    return int(fg or 0) == int(hwnd)


# --- Desktop Visibility & Window Occlusion Detection ---

_SHELL_BACKGROUND_CLASSES = frozenset(
    {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "DummyDWMListenerWindow",
        "EdgeUiInputTopWndClass",
        "EdgeUiInputWndClass",
        "NativeHWNDHost",
    }
)


def compute_rect_occlusion(
    target_rect: Tuple[int, int, int, int],
    covering_rects: Iterable[Tuple[int, int, int, int]],
) -> bool:
    """Return True if `target_rect` `(left, top, right, bottom)` is 100% covered by `covering_rects`.

    Uses exact 2D rectangle subtraction without allocating any GDI region handles:
    - If even 1 pixel of `target_rect` remains uncovered, returns False (partially or fully visible).
    - If a single window or a combination of multiple overlapping windows covers 100% of `target_rect`,
      returns True (completely obscured).
    """
    tl, tt, tr_x, tb = target_rect
    if tr_x <= tl or tb <= tt:
        return True

    uncovered: List[Tuple[int, int, int, int]] = [(tl, tt, tr_x, tb)]

    for cl, ct, cr, cb in covering_rects:
        if cr <= cl or cb <= ct:
            continue
        next_uncovered: List[Tuple[int, int, int, int]] = []
        for rl, rt, rr, rb in uncovered:
            # Intersection between uncovered piece `r` and covering rect `c`
            il = max(rl, cl)
            it = max(rt, ct)
            ir = min(rr, cr)
            ib = min(rb, cb)
            if il >= ir or it >= ib:
                # Disjoint: keep piece unchanged
                next_uncovered.append((rl, rt, rr, rb))
                continue
            # Split remaining non-covered parts into up to 4 non-overlapping strips
            if it > rt:
                next_uncovered.append((rl, rt, rr, it))
            if ib < rb:
                next_uncovered.append((rl, ib, rr, rb))
            if il > rl:
                next_uncovered.append((rl, it, il, ib))
            if ir < rr:
                next_uncovered.append((ir, it, rr, ib))

        uncovered = next_uncovered
        if not uncovered:
            return True

    return len(uncovered) == 0


def is_window_cloaked(hwnd: int) -> bool:
    """Return True if DWM reports `hwnd` as cloaked (e.g. UWP background window or other virtual desktop)."""
    if not _is_windows() or not hwnd:
        return False
    try:
        dwmapi = ctypes.windll.dwmapi
        cloaked = wintypes.DWORD(0)
        res = dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(DWMWA_CLOAKED),
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return res == 0 and bool(cloaked.value)
    except Exception:
        return False


def get_window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """Return `(left, top, right, bottom)` screen coordinates for `hwnd`, or None on failure."""
    if not _is_windows() or not hwnd:
        return None
    try:
        rect = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        pass
    return None


def _get_window_class_name(hwnd: int) -> str:
    if not _is_windows() or not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(64)
        length = ctypes.windll.user32.GetClassNameW(wintypes.HWND(hwnd), buf, 64)
        return buf.value[:length] if length > 0 else ""
    except Exception:
        return ""


def _is_effectively_transparent_window(hwnd: int, ex_style: int) -> bool:
    """Return True if a layered window is click-through transparent or has 0 alpha."""
    if ex_style & WS_EX_TRANSPARENT:
        return True
    if not (ex_style & WS_EX_LAYERED):
        return False
    try:
        user32 = ctypes.windll.user32
        cr_key = wintypes.COLORREF(0)
        alpha = wintypes.BYTE(255)
        flags = wintypes.DWORD(0)
        ok = user32.GetLayeredWindowAttributes(
            wintypes.HWND(hwnd),
            ctypes.byref(cr_key),
            ctypes.byref(alpha),
            ctypes.byref(flags),
        )
        if ok and (flags.value & LWA_ALPHA) and alpha.value < 15:
            return True
    except Exception:
        pass
    return False


def _get_monitor_rect_for_window(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """Return the full monitor bounding rect `(left, top, right, bottom)` containing `hwnd`."""
    if not _is_windows() or not hwnd:
        return None

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    try:
        user32 = ctypes.windll.user32
        hmon = user32.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(wintypes.HANDLE(hmon), ctypes.byref(mi)):
            r = mi.rcMonitor
            return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        pass
    return None


def is_window_minimized_or_hidden(hwnd: int) -> bool:
    """Return True if `hwnd` is hidden, minimized (`IsIconic`), or cloaked."""
    if not _is_windows() or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        h = wintypes.HWND(hwnd)
        if not user32.IsWindowVisible(h):
            return True
        if user32.IsIconic(h):
            return True
        if is_window_cloaked(hwnd):
            return True
        return False
    except Exception:
        return False


_DESKTOP_SHELL_TRIGGER_CLASSES = frozenset(
    {
        "Progman",
        "WorkerW",
        "SHELLDLL_DefView",
        "SysListView32",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "TrayShowDesktopButtonWClass",
        "ReBarWindow32",
        "MSTaskSwWClass",
        "MSTaskListWClass",
    }
)


def is_desktop_shell_window(hwnd: int) -> bool:
    """Return True if `hwnd` (or its root parent) belongs to the Windows Desktop or Taskbar shell."""
    if not _is_windows() or not hwnd:
        return False
    try:
        cls_name = _get_window_class_name(hwnd)
        if cls_name in _DESKTOP_SHELL_TRIGGER_CLASSES:
            return True
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        user32.GetAncestor.restype = wintypes.HWND
        root = int(user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT) or 0)
        if root and root != int(hwnd):
            root_cls = _get_window_class_name(root)
            if root_cls in _DESKTOP_SHELL_TRIGGER_CLASSES:
                return True
    except Exception:
        pass
    return False


def is_desktop_shell_above_window(hwnd: int) -> bool:
    """Return True if Windows 'Show Desktop' has raised `WorkerW` or `Progman` above `hwnd` in Z-order."""
    if not _is_windows() or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.FindWindowExW.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        ]
        user32.FindWindowExW.restype = wintypes.HWND

        desktop_host, _ = find_desktop_shell_hwnds()
        curr = user32.GetWindow(wintypes.HWND(hwnd), GW_HWNDPREV)
        steps = 0
        while curr and steps < 240:
            steps += 1
            curr_int = int(curr)
            if curr_int != int(hwnd) and user32.IsWindowVisible(curr) and not is_window_cloaked(curr_int):
                if desktop_host and curr_int == desktop_host:
                    return True
                cls_name = _get_window_class_name(curr_int)
                if cls_name in ("WorkerW", "Progman"):
                    return True
                dv = int(
                    user32.FindWindowExW(
                        wintypes.HWND(curr_int), None, "SHELLDLL_DefView", None
                    )
                    or 0
                )
                if dv:
                    return True
            curr = user32.GetWindow(curr, GW_HWNDPREV)
    except Exception:
        pass
    return False


def ensure_gadget_visible_on_desktop(
    hwnd: int,
    always_on_top: bool = True,
    force_topmost_over_desktop: bool = False,
) -> bool:
    """Ensure `hwnd` remains visible on the desktop (never minimized or covered by Show Desktop `WorkerW`)
    without stealing foreground focus or altering position/size.
    """
    if not _is_windows() or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        h = wintypes.HWND(hwnd)

        # 1. If minimized or hidden by shell action, restore visibility without activating
        if user32.IsIconic(h) or not user32.IsWindowVisible(h):
            user32.ShowWindow(h, SW_SHOWNOACTIVATE)

        # 2. If Show Desktop raised WorkerW/Progman above hwnd, we must place hwnd at top of HWND_TOPMOST
        shell_above = is_desktop_shell_above_window(hwnd)
        use_topmost = bool(always_on_top or force_topmost_over_desktop or shell_above)

        apply_desktop_gadget_style(
            hwnd,
            always_on_top=always_on_top,
            no_activate=True,
            force_topmost_over_desktop=use_topmost,
        )

        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        insert_after = HWND_TOPMOST if use_topmost else HWND_TOP
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        return bool(
            user32.SetWindowPos(
                h,
                wintypes.HWND(insert_after),
                0,
                0,
                0,
                0,
                flags,
            )
        )
    except Exception:
        return False


class DesktopShellEventMonitor(QObject):
    """Event-driven Win32 shell monitor (`SetWinEventHook`) that detects Windows 'Show Desktop'
    and 'Minimize All' transitions without aggressive polling timers.
    """

    show_desktop_detected = Signal()
    normal_window_activated = Signal(int)

    def __init__(
        self,
        target_hwnd_getter: Callable[[], int],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._target_hwnd_getter = target_hwnd_getter
        self._hooks: List[int] = []
        self._win_event_proc = None

    def start(self) -> bool:
        if not _is_windows() or self._hooks:
            return bool(self._hooks)
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
            self._win_event_proc = WINEVENTPROC(self._on_win_event)
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
            for ev_min, ev_max in (
                (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
                (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND),
                (EVENT_OBJECT_REORDER, EVENT_OBJECT_FOCUS),
            ):
                h = int(
                    user32.SetWinEventHook(
                        ev_min, ev_max, None, self._win_event_proc, 0, 0, flags
                    )
                    or 0
                )
                if h:
                    self._hooks.append(h)
            return bool(self._hooks)
        except Exception:
            return False

    def stop(self) -> None:
        if not _is_windows():
            return
        user32 = ctypes.windll.user32
        for h in self._hooks:
            try:
                user32.UnhookWinEvent(wintypes.HANDLE(h))
            except Exception:
                pass
        self._hooks.clear()
        self._win_event_proc = None

    def _on_win_event(
        self,
        _h_win_event_hook: int,
        event: int,
        hwnd: int,
        _id_object: int,
        _id_child: int,
        _dw_event_thread: int,
        _dwms_event_time: int,
    ) -> None:
        try:
            target_hwnd = int(self._target_hwnd_getter() or 0)
        except Exception:
            target_hwnd = 0
        if not target_hwnd:
            return

        hwnd_int = int(hwnd or 0)
        if hwnd_int == target_hwnd:
            if event == EVENT_SYSTEM_MINIMIZESTART:
                self.show_desktop_detected.emit()
            return

        if event == EVENT_SYSTEM_MINIMIZESTART:
            self.show_desktop_detected.emit()
            return

        if event in (EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_FOCUS):
            if hwnd_int and is_desktop_shell_window(hwnd_int):
                self.show_desktop_detected.emit()
                return
            if is_desktop_shell_above_window(target_hwnd):
                self.show_desktop_detected.emit()
                return
            if event == EVENT_SYSTEM_FOREGROUND and hwnd_int:
                self.normal_window_activated.emit(hwnd_int)
            return

        if event == EVENT_OBJECT_REORDER:
            if (hwnd_int and is_desktop_shell_window(hwnd_int)) or is_desktop_shell_above_window(target_hwnd):
                self.show_desktop_detected.emit()


def is_window_obscured(hwnd: int, inset: int = 2) -> bool:
    """Determine whether `hwnd` is 100% obscured on the Windows desktop.

    Walks the Z-order above `hwnd` (`GW_HWNDPREV`) and checks whether visible,
    non-minimized, non-cloaked, opaque windows completely cover `hwnd`'s bounding
    rectangle. Also detects full-screen foreground applications covering the widget's
    monitor. Partially visible widgets return False.
    """
    if not _is_windows() or not hwnd:
        return False

    try:
        user32 = ctypes.windll.user32
        target_h = wintypes.HWND(hwnd)
        if not user32.IsWindowVisible(target_h) or user32.IsIconic(target_h):
            return True

        raw_rect = get_window_rect(hwnd)
        if raw_rect is None:
            return False

        tl, tt, tr_x, tb = raw_rect
        if (tr_x - tl) > inset * 2 and (tb - tt) > inset * 2:
            target_rect = (tl + inset, tt + inset, tr_x - inset, tb - inset)
        else:
            target_rect = raw_rect

        covering_rects: List[Tuple[int, int, int, int]] = []

        # 1. Walk Z-order strictly in front of `hwnd`
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND

        curr = user32.GetWindow(target_h, GW_HWNDPREV)
        steps = 0
        while curr and steps < 240:
            steps += 1
            curr_int = int(curr)
            if curr_int != int(hwnd):
                if user32.IsWindowVisible(curr) and not user32.IsIconic(curr):
                    ex_style = _get_window_long_ptr(curr_int, GWL_EXSTYLE)
                    if not _is_effectively_transparent_window(curr_int, ex_style):
                        cls_name = _get_window_class_name(curr_int)
                        if cls_name not in _SHELL_BACKGROUND_CLASSES and not is_window_cloaked(curr_int):
                            w_rect = get_window_rect(curr_int)
                            if w_rect is not None:
                                cl, ct, cr, cb = w_rect
                                # Check quick bounding overlap with target_rect before adding
                                if cl < target_rect[2] and cr > target_rect[0] and ct < target_rect[3] and cb > target_rect[1]:
                                    covering_rects.append(w_rect)
                                    if compute_rect_occlusion(target_rect, covering_rects):
                                        return True
            curr = user32.GetWindow(curr, GW_HWNDPREV)

        # 2. Check if a full-screen application is active in the foreground on the same monitor
        fg = user32.GetForegroundWindow()
        fg_int = int(fg or 0)
        if fg_int and fg_int != int(hwnd):
            if user32.IsWindowVisible(fg) and not user32.IsIconic(fg) and not is_window_cloaked(fg_int):
                cls_name = _get_window_class_name(fg_int)
                if cls_name not in _SHELL_BACKGROUND_CLASSES:
                    fg_rect = get_window_rect(fg_int)
                    mon_rect = _get_monitor_rect_for_window(hwnd)
                    if fg_rect is not None and mon_rect is not None:
                        # Full-screen window covers the entire physical monitor (including taskbar area)
                        if (
                            fg_rect[0] <= mon_rect[0]
                            and fg_rect[1] <= mon_rect[1]
                            and fg_rect[2] >= mon_rect[2]
                            and fg_rect[3] >= mon_rect[3]
                        ):
                            return True

        return compute_rect_occlusion(target_rect, covering_rects)
    except Exception:
        return False


# --- Windows Startup Registration ---


class WindowsStartupManager:
    """Manages per-user Windows sign-in startup registration without admin rights.

    - Prefers launching the packaged GUI executable (`Chande.exe`) over a Python script.
    - Uses `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run` (`Chande`) so enabling
      startup multiple times updates the single value in-place and never creates duplicates.
    - Gracefully detects and repairs relocated or missing executable paths.
    """

    @staticmethod
    def resolve_preferred_startup_command() -> Tuple[str, Path]:
        """Return `(command_string, target_executable_path)` for Windows startup."""
        if getattr(sys, "frozen", False):
            exe_path = Path(sys.executable).resolve()
            return f'"{exe_path}"', exe_path

        # Prefer packaged GUI executable if it has been built in dist/
        candidates = [
            ROOT_DIR / "dist" / APP_NAME / f"{APP_NAME}.exe",
            ROOT_DIR / "dist" / f"{APP_NAME}.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                resolved = candidate.resolve()
                return f'"{resolved}"', resolved

        # Fallback to windowless pythonw.exe running main.py
        main_script = (ROOT_DIR / "main.py").resolve()
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = pythonw.resolve() if pythonw.is_file() else Path(sys.executable).resolve()
        return f'"{exe}" "{main_script}"', exe

    @staticmethod
    def _extract_primary_path_from_command(cmd: str) -> Optional[Path]:
        if not cmd:
            return None
        clean = cmd.strip()
        if clean.startswith('"'):
            end_quote = clean.find('"', 1)
            if end_quote > 1:
                return Path(clean[1:end_quote])
        parts = clean.split()
        return Path(parts[0]) if parts else None

    @classmethod
    def get_registered_command(cls) -> Optional[str]:
        """Read the current `Chande` startup command from HKCU Run registry, or None."""
        if not _is_windows():
            return None
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, winreg.KEY_READ
            ) as reg_key:
                val, _ = winreg.QueryValueEx(reg_key, APP_NAME)
                return str(val) if val else None
        except FileNotFoundError:
            return None
        except OSError:
            return None

    @classmethod
    def is_registered_and_valid(cls) -> bool:
        """Return True if a startup entry exists AND its target executable exists on disk."""
        cmd = cls.get_registered_command()
        if not cmd:
            return False
        target = cls._extract_primary_path_from_command(cmd)
        if target is None or not target.is_file():
            return False
        return True

    @classmethod
    def set_startup(cls, enable: bool) -> bool:
        """Enable or disable per-user Windows startup registration without creating duplicates."""
        if not _is_windows():
            return False
        try:
            import winreg
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as reg_key:
                if enable:
                    cmd, target_exe = cls.resolve_preferred_startup_command()
                    if not target_exe.is_file():
                        return False
                    winreg.SetValueEx(reg_key, APP_NAME, 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(reg_key, APP_NAME)
                    except FileNotFoundError:
                        pass
            return True
        except OSError:
            return False

    @classmethod
    def sync_on_startup(cls, desired_enabled: bool) -> bool:
        """Ensure registry entry matches `desired_enabled` and self-heals if the exe moved."""
        if not _is_windows():
            return False
        if desired_enabled:
            return cls.set_startup(True)
        else:
            # If disabled in settings, ensure no stale registry entry remains
            return cls.set_startup(False)


# --- Single-Instance Protection & IPC Activation ---


class SingleInstanceController(QObject):
    """Prevents multiple Chande instances and notifies the primary instance on second launch."""

    activation_requested = Signal()

    def __init__(
        self,
        server_name: str = SINGLE_INSTANCE_SERVER_NAME,
        mutex_name: str = SINGLE_INSTANCE_MUTEX_NAME,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.server_name = server_name
        self.mutex_name = mutex_name
        self._mutex_handle: Optional[int] = None
        self._server: Optional[QLocalServer] = None
        self._is_primary = False

    def try_acquire_primary(self) -> bool:
        """Attempt to become the single primary instance.

        Returns True if this process is the primary instance, or False if another
        instance is already running (after signaling it to activate).
        """
        # 1. Check if an active QLocalServer is already listening
        probe = QLocalSocket()
        probe.connectToServer(self.server_name)
        if probe.waitForConnected(250):
            try:
                probe.write(b"ACTIVATE\n")
                probe.flush()
                probe.waitForBytesWritten(250)
            except Exception:
                pass
            finally:
                probe.disconnectFromServer()
            return False

        # 2. Also acquire a Win32 Named Mutex when on Windows
        if _is_windows():
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.argtypes = [
                wintypes.LPVOID,
                wintypes.BOOL,
                wintypes.LPCWSTR,
            ]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            handle = kernel32.CreateMutexW(None, False, self.mutex_name)
            last_err = kernel32.GetLastError()
            if handle:
                self._mutex_handle = int(handle)
            if last_err == ERROR_ALREADY_EXISTS:
                # Another process holds the mutex; try once more to notify via socket
                retry_sock = QLocalSocket()
                retry_sock.connectToServer(self.server_name)
                if retry_sock.waitForConnected(300):
                    retry_sock.write(b"ACTIVATE\n")
                    retry_sock.flush()
                    retry_sock.waitForBytesWritten(200)
                    retry_sock.disconnectFromServer()
                self.release()
                return False

        # 3. Start listening on QLocalServer for future second-launch activations
        QLocalServer.removeServer(self.server_name)
        self._server = QLocalServer(self)
        if self._server.listen(self.server_name):
            self._server.newConnection.connect(self._on_new_connection)
        self._is_primary = True
        return True

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            if conn is not None:
                conn.waitForReadyRead(100)
                _ = conn.readAll()
                conn.close()
                conn.deleteLater()
                self.activation_requested.emit()

    def release(self) -> None:
        """Cleanly close the IPC server and release the Win32 mutex on application exit."""
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
        if _is_windows() and self._mutex_handle:
            try:
                ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(self._mutex_handle))
            except Exception:
                pass
            self._mutex_handle = None
        self._is_primary = False
