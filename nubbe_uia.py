"""Windows UI Automation for NUBB-E's hands: the real names of the buttons, links, tabs and fields in a
window, where they are, and what's typed in them. Much more reliable than guessing from a screenshot.

    import nubbe_uia as uia
    hwnd, title = uia.front_window()             # the window in front (NUBB-E's own are skipped)
    found = uia.read(hwnd)                       # [Control(name="Export", kind="button", rect=(l, t, r, b)), ...]
    best = uia.find(found, "the export button")  # the control that best matches Erfan's words, or None
    uia.ring(best.rect)                          # a ring around it on screen for a few seconds, no click

Every app that supports Windows accessibility works (Windows apps, Office, Edge/Chrome/Firefox web pages,
Electron apps). Apps that don't, return nothing, and the hands fall back to the screenshot method.
Password boxes are flagged and their text is never read. Uses comtypes (MIT) for the COM calls.
"""
import difflib
import re
import sys
import threading
import time
from dataclasses import dataclass, field

IS_WINDOWS = sys.platform == "win32"
KINDS = {50000: "button", 50002: "checkbox", 50003: "dropdown", 50004: "edit", 50005: "link", 50006: "image",
         50007: "list item", 50011: "menu item", 50013: "radio button", 50015: "slider", 50019: "tab",
         50020: "text", 50024: "tree item", 50025: "custom", 50029: "item", 50030: "document", 50031: "button",
         50034: "header", 50035: "header item", 50018: "tab list", 50021: "toolbar", 50026: "group", 50033: "pane"}
CLICKABLE = {"button", "link", "tab", "menu item", "checkbox", "radio button", "list item", "tree item", "item",
             "dropdown", "image", "header item", "custom"}
NOUNS = {"button": {"button"}, "link": {"link"}, "tab": {"tab"}, "menu": {"menu item"}, "icon": {"button", "image"},
         "field": {"edit"}, "box": {"edit", "checkbox"}, "checkbox": {"checkbox"}, "dropdown": {"dropdown"},
         "option": {"list item", "radio button", "menu item"}, "item": {"list item", "tree item", "item"}}
OWN_TITLES = ("NUBB-E", "NUBB-E Quick")
MAX_CONTROLS = 2000


@dataclass
class Control:
    name: str
    kind: str
    rect: tuple[int, int, int, int]       # screen pixels: left, top, right, bottom
    enabled: bool = True
    password: bool = False
    value: str = ""                       # what's typed in it (never read for passwords)
    element: object = field(default=None, repr=False, compare=False)

    @property
    def center(self) -> tuple[int, int]:
        return (self.rect[0] + self.rect[2]) // 2, (self.rect[1] + self.rect[3]) // 2

    @property
    def size(self) -> int:
        return max(0, self.rect[2] - self.rect[0]) * max(0, self.rect[3] - self.rect[1])


# ---------------------------------------------------------------------------
# matching Erfan's words to a control (pure, no Windows needed)
# ---------------------------------------------------------------------------
FILLER_WORDS = re.compile(r"\b(?:the|a|an|that|this|on|in|at|screen|page|window|please|thing|one|called|named|"
                          r"says|labeled|labelled|with|text|where|it|is|button|link|tab|icon|menu|field|box|option|"
                          r"item|checkbox|dropdown|click|press|tap|hit|select|where's|wheres|are|show|me|find|can|"
                          r"you|could|point|to|highlight|go|switch|for|my)\b", re.IGNORECASE)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w+#&@.-]+", " ", str(text).lower()).split())


def wanted_kinds(target: str) -> set[str]:
    words = set(_norm(target).split())
    return set().union(*(kinds for noun, kinds in NOUNS.items() if noun in words or noun + "s" in words))


def score(c: Control, target: str) -> float:
    """0 to 1: how well this control matches what Erfan said ("the export button" -> Export)."""
    want = _norm(FILLER_WORDS.sub(" ", target.strip("\"'")))
    name = _norm(c.name)
    if not want or not name or len(name) > 200:
        return 0.0
    if name == want:
        s = 1.0
    elif name.startswith(want + " ") or name.endswith(" " + want) or re.search(rf"\b{re.escape(want)}\b", name):
        s = 0.85 if len(want) >= 3 else 0.6
    elif all(re.search(rf"\b{re.escape(w)}", name) for w in want.split()):
        s = 0.75
    else:
        ratio = difflib.SequenceMatcher(None, want, name[:len(want) + 10]).ratio()
        s = ratio * 0.8 if ratio >= 0.8 else ratio * 0.4     # a typo counts ("exprot"), another word doesn't ("import")
    kinds = wanted_kinds(target)
    if kinds:
        s += 0.1 if c.kind in kinds else -0.15
    if c.kind in CLICKABLE or c.kind == "edit":
        s += 0.05
    if not c.enabled:
        s -= 0.2
    return max(0.0, min(1.0, s))


def find(controls: list[Control], target: str, min_score: float = 0.6) -> Control | None:
    """The control that best matches. Ties go to the one Erfan is most likely looking at (the bigger one,
    then the one nearer the top). None if nothing is close enough."""
    best = sorted(((score(c, target), -c.size, c.rect[1], i, c) for i, c in enumerate(controls) if c.size > 0),
                  key=lambda t: (-t[0], t[1], t[2], t[3]))
    return best[0][4] if best and best[0][0] >= min_score else None


def fields(controls: list[Control]) -> list[Control]:
    """The boxes you can type in, top to bottom."""
    return sorted((c for c in controls if c.kind == "edit" and c.enabled and c.size > 0),
                  key=lambda c: (c.rect[1] // 8, c.rect[0]))


# ---------------------------------------------------------------------------
# reading a window (Windows)
# ---------------------------------------------------------------------------
_local = threading.local()


def _uia():
    """(the UIAutomationClient module, the IUIAutomation object) for this thread."""
    if getattr(_local, "auto", None) is None:
        import comtypes
        import comtypes.client as cc
        comtypes.CoInitialize()
        mod = cc.GetModule("UIAutomationCore.dll")
        _local.mod = mod
        _local.auto = cc.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation)
    return _local.mod, _local.auto


def front_window() -> tuple[int, str]:
    """The top visible window that isn't NUBB-E's own (they're in front while Erfan asks)."""
    import ctypes
    from ctypes import wintypes
    u32, dwm, found = ctypes.windll.user32, ctypes.windll.dwmapi, []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
            return True
        cloaked = wintypes.DWORD()
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
        title = ctypes.create_unicode_buffer(256)
        u32.GetWindowTextW(hwnd, title, 256)
        ex = u32.GetWindowLongW(hwnd, -20)
        if cloaked.value or not title.value or title.value in OWN_TITLES or ex & 0x80:   # WS_EX_TOOLWINDOW
            return True
        found.append((hwnd, title.value))
        return False
    u32.EnumWindows(each, 0)
    return found[0] if found else (0, "")


def read(hwnd: int, limit: int = MAX_CONTROLS) -> list[Control]:
    """Every control on screen in this window, in one quick cross-app call (about 10 to 300 ms)."""
    mod, auto = _uia()
    req = auto.CreateCacheRequest()
    props = (mod.UIA_NamePropertyId, mod.UIA_ControlTypePropertyId, mod.UIA_BoundingRectanglePropertyId,
             mod.UIA_IsEnabledPropertyId, mod.UIA_IsPasswordPropertyId, mod.UIA_ValueValuePropertyId,
             mod.UIA_IsOffscreenPropertyId)
    for pid in props:
        req.AddProperty(pid)
    root = auto.ElementFromHandle(hwnd)
    cond = auto.CreatePropertyCondition(mod.UIA_IsOffscreenPropertyId, False)
    found = root.FindAllBuildCache(mod.TreeScope_Descendants, cond, req)
    out = []
    for i in range(min(found.Length, limit)):
        e = found.GetElement(i)
        try:
            kind = KINDS.get(e.CachedControlType, "other")
            r = e.CachedBoundingRectangle
            password = bool(e.CachedIsPassword)
            value = ""
            if kind == "edit" and not password:
                value = str(e.GetCachedPropertyValue(mod.UIA_ValueValuePropertyId) or "")
            out.append(Control(" ".join(str(e.CachedName or "").split())[:200], kind,
                               (int(r.left), int(r.top), int(r.right), int(r.bottom)),
                               bool(e.CachedIsEnabled), password, value[:2000], e))
        except Exception:      # one odd control never spoils the rest
            continue
    return out


def value_of(c: Control) -> str | None:
    """What's typed in a field now (None if it can't tell, "" for passwords)."""
    if c.password:
        return ""
    try:
        mod, _ = _uia()
        p = c.element.GetCurrentPattern(mod.UIA_ValuePatternId)
        return str(p.QueryInterface(mod.IUIAutomationValuePattern).CurrentValue or "") if p else None
    except Exception:
        return None


def still_at(c: Control) -> bool:
    """The control is still there, at the same place (the page didn't change under it)."""
    try:
        r = c.element.CurrentBoundingRectangle
        return (int(r.left), int(r.top), int(r.right), int(r.bottom)) == c.rect and not c.element.CurrentIsOffscreen
    except Exception:
        return False


def focus(c: Control) -> bool:
    try:
        c.element.SetFocus()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# the ring: "where's the export button?"
# ---------------------------------------------------------------------------
RING_COLOR = 0x0040A0FF      # orange (BGR)
KEY_COLOR = 0x00FF00FF       # magenta: see-through


def ring_rect(rect: tuple[int, int, int, int], pad: int = 14, screen=None) -> tuple[int, int, int, int]:
    """Where the ring window goes: around the control with some room, kept on screen."""
    l, t, r, b = rect
    l, t, r, b = l - pad, t - pad, r + pad, b + pad
    if screen:
        sl, st, sr, sb = screen
        l, t, r, b = max(sl, l), max(st, t), min(sr, r), min(sb, b)
    return l, t, r, b


def ring(rect: tuple[int, int, int, int], seconds: float = 4.0, wait: bool = False) -> bool:
    """Draws a ring around rect (screen pixels) on top of everything for a few seconds. It can't be clicked
    and never takes focus. Runs on its own thread."""
    if not IS_WINDOWS:
        return False
    done = threading.Event()
    t = threading.Thread(target=_ring_window, args=(rect, seconds, done), daemon=True)
    t.start()
    if wait:
        t.join(seconds + 2)
    return True


_procs = []   # window procedures must stay alive while their windows exist


def _ring_window(rect, seconds, done) -> None:
    import ctypes
    from ctypes import wintypes
    u32, gdi, k32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
    set_ctx = u32.SetThreadDpiAwarenessContext
    set_ctx.restype, set_ctx.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
    set_ctx(ctypes.c_void_p(-4))          # real pixels, like UI Automation's rectangles
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    u32.DefWindowProcW.restype, u32.DefWindowProcW.argtypes = LRESULT, [wintypes.HWND, wintypes.UINT,
                                                                        wintypes.WPARAM, wintypes.LPARAM]
    screen = (u32.GetSystemMetrics(76), u32.GetSystemMetrics(77),
              u32.GetSystemMetrics(76) + u32.GetSystemMetrics(78), u32.GetSystemMetrics(77) + u32.GetSystemMetrics(79))
    l, t, r, b = ring_rect(rect, screen=screen if rect[0] > -30000 else None)
    w, h = max(8, r - l), max(8, b - t)

    class PAINTSTRUCT(ctypes.Structure):
        _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", wintypes.RECT),
                    ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]

    def proc(hwnd, msg, wp, lp):
        if msg == 0x000F:                 # WM_PAINT
            ps = PAINTSTRUCT()
            hdc = u32.BeginPaint(hwnd, ctypes.byref(ps))
            bg = gdi.CreateSolidBrush(KEY_COLOR)
            rc = wintypes.RECT(0, 0, w, h)
            u32.FillRect(hdc, ctypes.byref(rc), bg)
            pen = gdi.CreatePen(0, 5, RING_COLOR)
            old_pen, old_brush = gdi.SelectObject(hdc, pen), gdi.SelectObject(hdc, gdi.GetStockObject(5))   # NULL_BRUSH
            gdi.RoundRect(hdc, 3, 3, w - 3, h - 3, 18, 18)
            gdi.SelectObject(hdc, old_pen)
            gdi.SelectObject(hdc, old_brush)
            gdi.DeleteObject(pen)
            gdi.DeleteObject(bg)
            u32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == 0x0113:                 # WM_TIMER: time's up
            u32.DestroyWindow(hwnd)
            return 0
        if msg == 0x0002:                 # WM_DESTROY
            u32.PostQuitMessage(0)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    wndproc = WNDPROC(proc)
    _procs.append(wndproc)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]
    name = f"NUBBE_Ring_{threading.get_ident()}_{time.time_ns()}"
    k32.GetModuleHandleW.restype = wintypes.HMODULE
    wc = WNDCLASSW(0, wndproc, 0, 0, k32.GetModuleHandleW(None), None, None, None, None, name)
    u32.RegisterClassW(ctypes.byref(wc))
    u32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    u32.CreateWindowExW.restype = wintypes.HWND
    u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                    wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    # layered + click-through + always on top + no taskbar button + never takes focus
    ex = 0x00080000 | 0x00000020 | 0x00000008 | 0x00000080 | 0x08000000
    hwnd = u32.CreateWindowExW(ex, name, "NUBB-E ring", 0x80000000, l, t, w, h, None, None, wc.hInstance, None)
    try:
        if not hwnd:
            return
        u32.SetLayeredWindowAttributes(hwnd, KEY_COLOR, 255, 0x1)   # LWA_COLORKEY: magenta is see-through
        u32.ShowWindow(hwnd, 4)                                      # SW_SHOWNOACTIVATE
        u32.SetTimer(hwnd, 1, int(seconds * 1000), None)
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
    finally:
        u32.UnregisterClassW(name, wc.hInstance)
        done.set()
