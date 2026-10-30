"""NUBB-E's hands: fills in forms and docs on screen, only after Erfan presses Yes.

    import nubbe_hands as hands
    plan = hands.plan_form(ask, request, facts)      # Look at the screen, decide which field gets what
    todo, skipped, missing = hands.review(plan)      # safety rules: no passwords, payment or ID fields
    hands.card(todo, skipped, missing)               # what the Yes/No card shows
    hands.fill(todo, ask, progress)                  # click and type, one field at a time, checking each

How it works: a screenshot goes to the vision model, which finds each field (as a box on a
0 to 1000 scale) and picks its value using only facts Erfan gave (memory, his request, the
chat). After Yes, NUBB-E clicks into each field, types the text (Windows SendInput, no
clipboard), and looks again to check it landed. Pressing Esc or moving the mouse stops it
at once. It never fills passwords, payment or ID fields, never clicks buttons, never presses
Enter in a one-line field, and never presses Submit or Send. That's always Erfan's job.

Windows only (ctypes). `ask(messages, max_tokens)` is the app's vision call.
"""
import ctypes
import difflib
import json
import re
import sys
import time
from contextlib import contextmanager

IS_WINDOWS = sys.platform == "win32"
TOOLS = ("fill_form",)
SUPPORTED = {"text", "textarea", "other"}       # dropdowns, checkboxes and radio buttons: not yet
PRIVATE = re.compile(
    r"pass(word|code|phrase)|\bpin\b|card|cvv|cvc|security code|expir|iban|swift|routing|"
    r"account (number|no)|sort code|\bbank\b|\bssn\b|social security|passport|national id|"
    r"\bid (number|no)\b|identity|licen[cs]e (number|no)|tax (id|number)|\btin\b|one.time|\botp\b|"
    r"verification code|2fa|captcha", re.IGNORECASE)
# "fill this out", "fill in the form", "can you fill it in"... but not "how do I fill out a tax form?"
FILL_WORDS = re.compile(r"\bfill(ing)? (it|this|that|these|them|everything|the (form|fields?|blanks?)|"
                        r"(this|the) (form|doc|document|page)|(it |this |that )?(out|in))\b", re.IGNORECASE)
HOW_TO = re.compile(r"\bhow (do|can|should|would|to)\b", re.IGNORECASE)
# windows NUBB-E never types into: in a terminal (VS Code has one built in) typed text can run as a command
NO_TYPING = re.compile(r"powershell|command prompt|cmd\.exe|windows terminal|\bterminal\b|git bash|\bwsl\b|"
                       r"ubuntu|visual studio code|^NUBB-E", re.IGNORECASE)
TYPE_DELAY = 0.012        # seconds between characters, so apps keep up (and Esc is checked often)
MOUSE_SLACK = 25          # pixels the mouse may drift before it counts as Erfan taking over

SCHEMA = {"type": "function", "function": {
    "name": "fill_form",
    "description": "Fill in the form or document that's open on Erfan's screen. This tool takes its own "
                   "screenshot, so you don't need one: it finds the fields, plans which text goes where, shows "
                   "Erfan the plan, and types it after he presses Yes. Use it whenever he asks you to fill "
                   "something out, or to type text into a field on screen.",
    "parameters": {"type": "object", "properties": {
        "request": {"type": "string", "description": "What Erfan wants filled in, in his words."},
        "details": {"type": "string", "description": "Facts from this chat or his files to use (optional). "
                                                     "Never make anything up."},
    }, "required": ["request"]}}}

RULES = (
    "\n\n## Your hands\n"
    "When Erfan asks you to fill in a form or doc on his screen (\"fill this out\", \"fill in this form\"), "
    "call fill_form right away. You can't see his screen yourself, but fill_form can: never ask him for a "
    "screenshot or which form he means. It shows him the plan on a Yes/No card and types only after Yes, so "
    "just call it. Pass any facts from this "
    "chat or his files in 'details'. Never invent facts about him: if something is missing, the tool tells "
    "you, and you ask him. You never fill passwords, payment or ID fields, and you never press Submit or "
    "Send: tell him that part is his."
)

PLAN_PROMPT = """You're helping Erfan fill in what's on his screen. Find every field in this screenshot and decide what goes in each.

Erfan's request: {request}

Facts you may use (and nothing else):
{facts}

Reply with JSON only, like:
{{"fields": [{{"label": "Full name", "kind": "text", "box": [x1, y1, x2, y2], "current": "", "value": "Erfan Zamani"}}],
  "buttons": [{{"label": "Submit", "box": [x1, y1, x2, y2]}}]}}

Rules:
- box is the input box itself (not its label), as coordinates from 0 to 1000 relative to the image width and height.
- kind is one of: text (one line), textarea (several lines), password, select (dropdown), checkbox, radio, other (a spot in a document).
- current is the text already in the field ("" if empty).
- value comes only from the facts above. If they don't say, use null. Never guess or invent.
- For an open question (like "Why do you want to join?"), write an answer only if Erfan's request asks you to write one, using only the facts above. Otherwise null.
- Never put anything in password, payment, bank or ID fields: value null.
- List every button (Submit, Send, Next...) in buttons."""

VERIFY_PROMPT = ("This is a close-up of a form field labeled \"{label}\". What text is typed inside the input box? "
                 "Reply with only that text, exactly as shown. If the box is empty, reply EMPTY.")


def wants_fill(text: str) -> bool:
    """Erfan asked NUBB-E to fill in what's on screen (the app then calls fill_form directly)."""
    return bool(FILL_WORDS.search(text)) and not HOW_TO.search(text)


class TargetError(Exception):
    """The fields aren't in one window NUBB-E may type into."""


# ---------------------------------------------------------------------------
# planning and the safety rules
# ---------------------------------------------------------------------------
def parse_json(text: str):
    """The model's JSON, even inside ```json fences or with a sentence around it."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON in the answer")
    return json.loads(text[start:end + 1])


def plan_form(ask, request: str, facts: list[str]) -> dict:
    """Looks at the screen (NUBB-E's own windows hide for it) and asks the vision model for a plan."""
    from look import hidden_window, screenshot_b64
    with dpi_aware(), hidden_window():
        shot = screenshot_b64(1, 1280)
    prompt = PLAN_PROMPT.format(request=request.strip() or "(none)",
                                facts="\n".join(f"- {f}" for f in facts) or "- (none)")
    answer = ask([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shot}"}},
        {"type": "text", "text": prompt}]}], 1200)
    return parse_json(answer)


def _box(b):
    try:
        x1, y1, x2, y2 = (float(v) for v in b)
    except (TypeError, ValueError):
        return None
    if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
        return None
    return [x1, y1, x2, y2]


def center(box) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def looks_like_card(value: str) -> bool:
    """A 13 to 19 digit number that passes the Luhn check: almost surely a payment card."""
    for run in re.findall(r"\d[\d -]{11,}\d", value):
        digits = [int(c) for c in run if c.isdigit()]
        if 13 <= len(digits) <= 19:
            total = sum(d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2)
                        for i, d in enumerate(reversed(digits)))
            if total % 10 == 0:
                return True
    return False


def review(plan: dict) -> tuple[list, list, list]:
    """Splits the model's plan into (to fill, skipped with a reason, missing info). The rules here
    don't trust the model: private fields and anything on a button are refused whatever it said."""
    buttons = [b for b in (_box(x.get("box")) for x in plan.get("buttons") or [] if isinstance(x, dict)) if b]
    todo, skipped, missing = [], [], []
    for f in plan.get("fields") or []:
        if not isinstance(f, dict):
            continue
        label = " ".join(str(f.get("label") or "a field").split())[:80]
        kind = str(f.get("kind") or "text").lower()
        box = _box(f.get("box"))
        value = f.get("value")
        value = None if value is None else str(value).strip()
        current = str(f.get("current") or "").strip()
        if kind == "password" or PRIVATE.search(label):
            skipped.append((label, "private"))
        elif kind not in SUPPORTED:
            skipped.append((label, "I can't do dropdowns or checkboxes yet"))
        elif box is None:
            skipped.append((label, "couldn't tell where it is"))
        elif not value:
            missing.append(label)
        elif looks_like_card(value):
            skipped.append((label, "private"))
        elif any(b[0] - 10 <= center(box)[0] <= b[2] + 10 and b[1] - 10 <= center(box)[1] <= b[3] + 10
                 for b in buttons):
            skipped.append((label, "too close to a button"))
        elif " ".join(current.split()).casefold() == " ".join(value.split()).casefold():
            skipped.append((label, "already filled in"))
        else:
            todo.append({"label": label, "kind": kind, "box": box, "value": value, "current": current})
    return todo, skipped, missing


def card(todo, skipped, missing, window: str = "") -> dict:
    lines = [f"{f['label']}: {f['value']}" for f in todo]
    if skipped:
        lines += ["", "I won't fill: " + ", ".join(f"{label} ({why})" for label, why in skipped)]
    if missing:
        lines += ["", "I don't know: " + ", ".join(missing)]
    lines += ["", "I won't press Submit or Send. That part is yours."]
    return {"title": "Fill in this form",
            "target": (f"in \"{window}\": " if window else "") + f"{len(todo)} field{'s' if len(todo) != 1 else ''}, "
                      "one at a time. Esc or moving the mouse stops me.",
            "detail": "\n".join(lines)}


def _list(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def report(done, stopped, todo, skipped, missing) -> str:
    """What happened, in plain words. The app says this itself: the model used to retell it wrong."""
    left = [f["label"] for f in todo if f["label"] not in done]
    parts = []
    if done and not stopped:
        parts.append(f"Done! I filled in {_list(done)}.")
    elif done:
        parts.append(f"I filled in {_list(done)}, then stopped because {stopped}.")
    elif stopped:
        parts.append(f"I stopped before typing anything because {stopped}.")
    if stopped and left:
        parts.append(f"Still to do: {_list(left)}.")
    if skipped:
        parts.append(f"I didn't fill {_list(f'{label} ({why})' for label, why in skipped)}.")
    if missing:
        parts.append(f"I still need your {_list(missing)}: tell me and I'll add {'it' if len(missing) == 1 else 'them'}.")
    if done:
        parts.append("Submit is all yours.")
    return " ".join(parts) or "I couldn't find any fields to fill on the screen."


# ---------------------------------------------------------------------------
# checking a field: a close-up screenshot and a quick question to the vision model
# ---------------------------------------------------------------------------
def matches(seen: str, expected: str) -> bool:
    """Loose check: the model reads text from pixels, a long field shows only part of it, and wraps."""
    s, e = (" ".join(t.casefold().split()).strip(".… ") for t in (seen, expected))   # "..." marks cut-off text
    if not s or s == "empty":
        return False
    n = min(len(e), 40)
    return (s in e and len(s) >= 5) or e in s or \
        difflib.SequenceMatcher(None, s[:n], e[:n]).ratio() >= 0.75 or \
        difflib.SequenceMatcher(None, s[-n:], e[-n:]).ratio() >= 0.75


def check(ask, field: dict) -> str:
    """Reads back what's in the field now."""
    from look import screenshot, to_b64
    img = screenshot(1, 100_000)                  # full size, so small text stays readable
    x1, y1, x2, y2 = (v / 1000 * (img.width if i % 2 == 0 else img.height) for i, v in enumerate(field["box"]))
    pad = max(20, (y2 - y1) * 0.6)
    crop = img.crop((max(0, x1 - 20), max(0, y1 - pad * 1.5), min(img.width, x2 + 20), min(img.height, y2 + pad)))
    if crop.width > 900:
        crop = crop.resize((900, round(crop.height * 900 / crop.width)))
    answer = ask([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{to_b64(crop)}"}},
        {"type": "text", "text": VERIFY_PROMPT.format(label=field["label"])}]}], 200)
    return answer.strip().strip('"')


# ---------------------------------------------------------------------------
# Windows input: SendInput clicks and keys, with Esc and the mouse as the stop buttons
# ---------------------------------------------------------------------------
if IS_WINDOWS:
    from ctypes import wintypes

    class _MOUSE(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _KEY(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _HW(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSE), ("ki", _KEY), ("hi", _HW)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RETURN, VK_ESCAPE, VK_A = 0x10, 0x11, 0x12, 0x5B, 0x0D, 0x1B, 0x41
KEYUP, UNICODE = 0x2, 0x4
LEFTDOWN, LEFTUP = 0x2, 0x4


def _send(*events) -> None:
    arr = (_INPUT * len(events))(*events)
    ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))


def _key(vk=0, scan=0, flags=0):
    i = _INPUT(type=1)
    i.u.ki = _KEY(vk, scan, flags, 0, 0)
    return i


def _mouse(flags):
    i = _INPUT(type=0)
    i.u.mi = _MOUSE(0, 0, 0, flags, 0, 0)
    return i


@contextmanager
def dpi_aware():
    """Real screen pixels for this thread, so screenshots, the mouse and clicks all agree."""
    old = None
    if IS_WINDOWS:
        f = ctypes.windll.user32.SetThreadDpiAwarenessContext
        f.restype, f.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
        old = f(ctypes.c_void_p(-4))     # per-monitor aware v2
    try:
        yield
    finally:
        if old:
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))


class Stopped(Exception):
    """Erfan pressed Esc or moved the mouse."""


class Hands:
    """Clicks and types, checking for Esc and mouse movement between every key."""

    def __init__(self):
        self.u32 = ctypes.windll.user32
        self.u32.GetAsyncKeyState(VK_ESCAPE)   # clears "pressed since last time"
        self.where = None

    def screen(self) -> tuple[int, int, int, int]:
        """The primary monitor (left, top, width, height) in real pixels."""
        return 0, 0, self.u32.GetSystemMetrics(0), self.u32.GetSystemMetrics(1)

    def point(self, box) -> tuple[int, int]:
        """A field's center on screen, from its 0 to 1000 box."""
        left, top, width, height = self.screen()
        cx, cy = center(box)
        return round(left + cx / 1000 * width), round(top + cy / 1000 * height)

    def window_at(self, x: int, y: int) -> tuple[int, str]:
        """The top window at a screen point, skipping NUBB-E's own (they hide while NUBB-E works)."""
        u32, found = self.u32, []
        dwm = ctypes.windll.dwmapi

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):            # top-level windows, front to back
            if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
                return True
            cloaked = wintypes.DWORD()
            dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)   # hidden by Windows (other desktops)
            r = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            title = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, title, 256)
            if cloaked.value or not (r.left <= x < r.right and r.top <= y < r.bottom) or not title.value \
                    or title.value in ("NUBB-E", "NUBB-E Quick"):
                return True
            found.append((hwnd, title.value))
            return False
        u32.EnumWindows(each, 0)
        return found[0] if found else (0, "")

    def check_stop(self) -> None:
        if self.u32.GetAsyncKeyState(VK_ESCAPE) & 0x8001:
            raise Stopped("you pressed Esc")
        if self.where:
            p = wintypes.POINT()
            self.u32.GetCursorPos(ctypes.byref(p))
            if abs(p.x - self.where[0]) > MOUSE_SLACK or abs(p.y - self.where[1]) > MOUSE_SLACK:
                raise Stopped("you moved the mouse")

    def click(self, x: int, y: int) -> None:
        self.check_stop()
        _send(*(_key(vk, 0, KEYUP) for vk in (VK_MENU, VK_SHIFT, VK_CONTROL, VK_LWIN)))   # nothing still held
        self.u32.SetCursorPos(x, y)
        self.where = (x, y)
        _send(_mouse(LEFTDOWN), _mouse(LEFTUP))
        time.sleep(0.2)

    def select_all(self) -> None:
        _send(_key(VK_CONTROL), _key(VK_A), _key(VK_A, 0, KEYUP), _key(VK_CONTROL, 0, KEYUP))
        time.sleep(0.05)

    def type(self, text: str) -> None:
        for ch in text:
            self.check_stop()
            if ch == "\n":   # Shift+Enter: a new line in text boxes, docs and chat boxes, never "send"
                _send(_key(VK_SHIFT), _key(VK_RETURN), _key(VK_RETURN, 0, KEYUP), _key(VK_SHIFT, 0, KEYUP))
            else:            # typed as Unicode (emoji become two UTF-16 units), so any keyboard layout works
                codes = ch.encode("utf-16-le")
                for k in range(0, len(codes), 2):
                    u = int.from_bytes(codes[k:k + 2], "little")
                    _send(_key(0, u, UNICODE), _key(0, u, UNICODE | KEYUP))
            time.sleep(TYPE_DELAY)


def target_window(todo: list, hands=None) -> tuple[int, str]:
    """The one window all the fields are in. Refuses terminals, code editors and NUBB-E itself."""
    with dpi_aware():
        hands = hands or Hands()
        wins = {hands.window_at(*hands.point(f["box"])) for f in todo}
    if len(wins) != 1 or not next(iter(wins))[0]:
        raise TargetError("the fields don't all seem to be in one window")
    hwnd, title = next(iter(wins))
    if NO_TYPING.search(title):
        raise TargetError(f"\"{title}\" looks like a terminal or code editor, and typing there could run commands")
    return hwnd, title


def text_for(field: dict) -> str:
    """One-line fields never get a newline: Enter there can submit the form."""
    value = field["value"].replace("\t", " ").replace("\r", "")
    return " ".join(value.split()) if field["kind"] == "text" else value


def fill(todo: list, ask, progress=lambda line: None, log=lambda field, outcome: None, hands=None,
         window=None) -> tuple:
    """Fills each field in turn. Returns (labels done, why it stopped or None).
    window (hwnd, title): every click must land in it, or NUBB-E stops."""
    from look import hidden_window
    done = []
    with dpi_aware(), hidden_window():
        hands = hands or Hands()
        try:
            for f in todo:
                progress(f"filling: {f['label']}")
                x, y = hands.point(f["box"])
                if window and hands.window_at(x, y)[0] != window[0]:
                    log(f, "stopped: another window is there now")
                    return done, f"'{f['label']}' isn't in \"{window[1]}\" anymore (another window is there)"
                hands.click(x, y)
                if f["current"] and f["kind"] in ("text", "textarea"):
                    hands.select_all()    # replace what's there; only inside a form box, never a whole doc
                text = text_for(f)
                hands.type(text)
                hands.check_stop()
                time.sleep(0.3)           # let the app draw it
                seen = check(ask, f)
                hands.check_stop()
                if not matches(seen, text):
                    log(f, f"check failed: shows '{seen[:60]}'")
                    return done, f"'{f['label']}' doesn't look right (I see '{seen[:60]}'), so I stopped to be safe"
                log(f, "ok")
                done.append(f["label"])
        except Stopped as e:
            log(f, f"stopped: {e}")
            return done, str(e)
    return done, None
