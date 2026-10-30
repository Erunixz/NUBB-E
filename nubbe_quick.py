"""NUBB-E quick bar: a small see-through bar on top of everything, opened with a hotkey.

nubbe_app.py runs it (Windows + pywebview). Press the hotkey (Alt+Space by default,
quick_hotkey in config.toml) from any app, type, and the answer shows right in the
bar. Look reads the screen behind the bar. Insert pastes the answer into the app you
were in. Esc hides the bar. Quick chats are saved like any other chat.

Standard library + pywebview. The Windows bits (hotkey, focus, paste) use ctypes.
"""
import ctypes
import sys
import threading
import time
from ctypes import wintypes

TITLE = "NUBB-E Quick"
WIDTH, COLLAPSED, EXPANDED = 680, 74, 440
IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# Windows helpers (hotkey, focus, clipboard paste)
# ---------------------------------------------------------------------------
MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT, WM_HOTKEY = 0x4000, 0x0312
KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "`": 0xC0, "backquote": 0xC0,
        **{f"f{i}": 0x6F + i for i in range(1, 13)}}


def parse_hotkey(text: str) -> tuple[int, int]:
    """'alt+space' -> (modifier flags, virtual key code)."""
    mods, vk = 0, None
    for part in text.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in KEYS:
            vk = KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        else:
            raise ValueError(f"Unknown key '{part}' in quick_hotkey '{text}'")
    if vk is None:
        raise ValueError(f"quick_hotkey '{text}' needs a key, like alt+space")
    return mods, vk


def listen_hotkey(text: str, callback) -> None:
    """Calls callback() every time the hotkey is pressed, from any app. Runs its own thread."""
    mods, vk = parse_hotkey(text)
    ready, ok = threading.Event(), [False]

    def loop():
        u32 = ctypes.windll.user32
        ok[0] = bool(u32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk))
        ready.set()
        if not ok[0]:
            return
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                try:
                    callback()
                except Exception as e:
                    print(f"Quick bar error: {e}")

    threading.Thread(target=loop, daemon=True).start()
    ready.wait(2)
    if not ok[0]:
        raise OSError(f"Couldn't use {text} (another app may have it). Change quick_hotkey in config.toml.")


_ctrl_handler = None   # kept alive so Windows can keep calling it


def on_ctrl_c(callback) -> None:
    """Runs callback on Ctrl+C or closing the console. Needed because the window loop
    keeps Python's own Ctrl+C handling from running."""
    global _ctrl_handler
    if not IS_WINDOWS:
        return

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(event):
        callback()
        time.sleep(1)    # let the windows close and the brain stop
        return True
    _ctrl_handler = handler
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)


BAR_COLOR = "#0b1016"
RADIUS = 18


def see_through(hwnd: int, opacity: float = 0.94) -> bool:
    """Windows: makes the whole bar a bit see-through. Plain opacity only: a color key would
    also make the web content click-through, so the bar couldn't be clicked again."""
    if not (IS_WINDOWS and hwnd):
        return False
    u32 = ctypes.windll.user32
    style = u32.GetWindowLongW(hwnd, -20)                         # GWL_EXSTYLE
    u32.SetWindowLongW(hwnd, -20, style | 0x80000)                # WS_EX_LAYERED
    return bool(u32.SetLayeredWindowAttributes(hwnd, 0, int(255 * opacity), 0x2))   # LWA_ALPHA


def round_corners(hwnd: int, width: int, height: int) -> None:
    """Windows: cuts the window to a rounded rectangle (sizes in page pixels)."""
    if not (IS_WINDOWS and hwnd):
        return
    u32, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    scale = (u32.GetDpiForWindow(hwnd) or 96) / 96
    w, h, r = round(width * scale), round(height * scale), round(RADIUS * 2 * scale)
    u32.SetWindowRgn(hwnd, gdi.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r), True)   # Windows owns the region now


def foreground() -> int:
    return ctypes.windll.user32.GetForegroundWindow() if IS_WINDOWS else 0


def find_window(title: str) -> int:
    return ctypes.windll.user32.FindWindowW(None, title) if IS_WINDOWS else 0


def focus(hwnd: int) -> None:
    """Brings hwnd to the front with keyboard focus. Windows only lets the app that has the
    keyboard do that, so we briefly share input with it (AttachThreadInput)."""
    if not (IS_WINDOWS and hwnd):
        return
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)       # SW_RESTORE
    fg = u32.GetForegroundWindow()
    if fg == hwnd:
        return
    theirs = u32.GetWindowThreadProcessId(fg, None) if fg else 0
    mine = k32.GetCurrentThreadId()
    attached = bool(theirs and theirs != mine and u32.AttachThreadInput(mine, theirs, True))
    try:
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            u32.AttachThreadInput(mine, theirs, False)
    focus_page(hwnd)


def focus_page(hwnd: int) -> None:
    """The web page lives in a child window (Edge WebView2). Windows focuses the outer window
    but not always the page, so keys would go nowhere until a click. Hand focus to the page."""
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(child, _):
        name = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(child, name, 64)
        if name.value == "Chrome_RenderWidgetHostHWND":
            found.append(child)
            return False
        return True
    u32.EnumChildWindows(hwnd, each, 0)
    if not found:
        return
    theirs, mine = u32.GetWindowThreadProcessId(found[0], None), k32.GetCurrentThreadId()
    if theirs and theirs != mine and u32.AttachThreadInput(mine, theirs, True):
        u32.SetFocus(found[0])
        u32.AttachThreadInput(mine, theirs, False)


def _clipboard_text():
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.GlobalLock.restype = ctypes.c_void_p
    u32.GetClipboardData.restype = ctypes.c_void_p
    if not u32.OpenClipboard(None):
        return None
    try:
        h = u32.GetClipboardData(13)   # CF_UNICODETEXT
        if not h:
            return None
        p = k32.GlobalLock(ctypes.c_void_p(h))
        try:
            return ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(ctypes.c_void_p(h))
    finally:
        u32.CloseClipboard()


def _set_clipboard_text(text: str) -> bool:
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    h = k32.GlobalAlloc(0x0002, size)   # GMEM_MOVEABLE
    p = k32.GlobalLock(ctypes.c_void_p(h))
    ctypes.memmove(p, data, size)
    k32.GlobalUnlock(ctypes.c_void_p(h))
    for _ in range(10):                  # another app may be holding the clipboard for a moment
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        u32.EmptyClipboard()
        return bool(u32.SetClipboardData(13, ctypes.c_void_p(h)))
    finally:
        u32.CloseClipboard()


def paste_into(hwnd: int, text: str) -> bool:
    """Puts text in hwnd at its cursor (clipboard + Ctrl+V), then restores the old clipboard text."""
    old = _clipboard_text()
    if not _set_clipboard_text(text):
        return False
    focus(hwnd)
    time.sleep(0.25)                     # let the window take focus first
    kb = ctypes.windll.user32.keybd_event
    for key in (0x12, 0x10, 0x5B):       # make sure Alt / Shift / Win aren't still held from the hotkey
        kb(key, 0, 0x2, 0)
    kb(0x11, 0, 0, 0)                    # Ctrl down
    kb(0x56, 0, 0, 0)                    # V down
    kb(0x56, 0, 0x2, 0)                  # V up
    kb(0x11, 0, 0x2, 0)                  # Ctrl up
    time.sleep(0.4)                      # the app reads the clipboard during this time
    if old is not None:
        _set_clipboard_text(old)
    return True


# ---------------------------------------------------------------------------
# the bar window
# ---------------------------------------------------------------------------
class QuickBar:
    """The bar's window plus the Python side of its buttons (called from its page as pywebview.api.*)."""

    def __init__(self, webview, url: str, on_open_app, on_quit):
        self._webview = webview
        self._open_app, self._quit = on_open_app, on_quit
        self._prev = 0          # the window Erfan was in before opening the bar (Insert goes there)
        self._visible = False
        self._styled = False
        screen = webview.screens[0] if webview.screens else None
        x = (screen.width - WIDTH) // 2 if screen else None
        y = int(screen.height * 0.12) if screen else None
        self._window = webview.create_window(
            TITLE, url + "/quick", js_api=self, width=WIDTH, height=COLLAPSED, x=x, y=y,
            frameless=True, easy_drag=False, on_top=True, hidden=True,
            resizable=False, background_color=BAR_COLOR, focus=True)

    def toggle(self) -> None:
        bar = find_window(TITLE)
        if self._visible and foreground() == bar:
            self.hide()
            return
        if self._visible:              # open but behind your focus: bring it back instead of hiding
            self.activate()
            return
        hwnd = foreground()
        if hwnd and hwnd != find_window(TITLE):
            self._prev = hwnd
        if not self._styled:
            self._styled = see_through(find_window(TITLE))
            round_corners(find_window(TITLE), WIDTH, COLLAPSED)
        self._window.show()
        self._visible = True
        bar = find_window(TITLE)
        u32 = ctypes.windll.user32
        for _ in range(30):                # show() happens a moment later on the window's own thread
            if u32.IsWindowVisible(bar):
                break
            time.sleep(0.01)
        for _ in range(3):                 # Windows sometimes ignores the first try
            focus(bar)
            if foreground() == bar:
                break
            time.sleep(0.05)
        self._window.run_js("window.quickOpened && quickOpened()")   # run_js doesn't wait, so the hotkey stays snappy

    # --- called from the bar's page ---
    def activate(self) -> None:
        """Gives the bar keyboard focus again (after Erfan clicked into another app and back)."""
        focus(find_window(TITLE))
        self._window.run_js("window.quickOpened && quickOpened(false)")

    def hide(self) -> None:
        self._window.hide()
        self._visible = False

    def set_height(self, height) -> None:
        height = max(COLLAPSED, min(EXPANDED, int(height)))
        self._window.resize(WIDTH, height)
        round_corners(find_window(TITLE), WIDTH, height)

    def insert(self, text: str) -> bool:
        """Pastes text into the window Erfan was in before opening the bar."""
        if not self._prev or not text:
            return False
        self.hide()
        return paste_into(self._prev, text)

    def open_app(self) -> None:
        self.hide()
        self._open_app()

    def quit(self) -> None:
        self._quit()


QUICK_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>NUBB-E Quick</title>
<style>
  :root { --glow: #6ff3ff; --text: #d9faff; --muted: #7fa4ad; --line: #1f3a44; }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: #0b1016; font: 15px/1.45 system-ui, "Segoe UI", sans-serif; color: var(--text); overflow: hidden; }
  .bar { background: #0b1016; border: 1px solid #2a5866; border-radius: 18px; overflow: hidden; }
  .row { display: flex; gap: 10px; align-items: center; padding: 10px 12px; }
  .face { width: 46px; height: 40px; flex: none; cursor: move; }
  .face canvas { pointer-events: none; }
  .face canvas { width: 100%; height: 100%; display: block; }
  textarea { flex: 1; resize: none; overflow: hidden; height: 40px; max-height: 120px; padding: 9px 12px; border-radius: 12px; border: 1px solid var(--line);
             background: #ffffff08; color: var(--text); font: inherit; outline: none; }
  textarea:focus { border-color: var(--glow); }
  button { cursor: pointer; border-radius: 10px; font: inherit; padding: 8px 12px; border: 1px solid var(--line);
           background: transparent; color: var(--muted); white-space: nowrap; }
  button:hover { color: var(--text); border-color: var(--glow); }
  button:disabled { opacity: .4; cursor: default; }
  .look { color: var(--glow); border-color: var(--glow); }
  .call.on { color: #ff6b8b; border-color: #ff6b8b; }
  .chatname { padding: 0 16px 6px; color: var(--muted); font-size: 12px; display: none; }
  .chatname.show { display: block; }
  .icon { padding: 6px 9px; }
  #out { display: none; padding: 0 16px 12px; max-height: 330px; overflow-y: auto; }
  #out.show { display: block; }
  #answer { white-space: normal; word-wrap: break-word; }
  #answer pre { background: #ffffff0d; padding: 8px 10px; border-radius: 8px; overflow-x: auto; }
  #answer code { background: #ffffff10; padding: 1px 4px; border-radius: 4px; }
  #answer a { color: var(--glow); }
  .status { color: var(--muted); font-style: italic; }
  .card { display: none; margin: 0 16px 12px; padding: 10px 12px; border: 1px solid var(--glow); border-radius: 12px; font-size: 14px; }
  .card.show { display: block; }
  .card pre { max-height: 140px; overflow: auto; background: #ffffff0d; padding: 8px 10px; border-radius: 8px;
              white-space: pre-wrap; word-break: break-word; margin: 8px 0; }
  .cardbtns { display: flex; gap: 8px; }
  .actions { display: none; gap: 8px; padding: 0 16px 12px; }
  .actions.show { display: flex; }
  .hint { margin-left: auto; color: var(--muted); font-size: 12px; align-self: center; }
</style>
</head>
<body>
<div class="bar" id="bar">
  <div class="row">
    <div class="face pywebview-drag-region" id="faceBox" title="Drag to move"><canvas id="c"></canvas></div>
    <textarea id="input" rows="1" placeholder="Ask NUBB-E...  (Enter: send, Ctrl+Enter: Look, Esc: hide)"></textarea>
    <button class="look" id="lookBtn" title="Read the screen behind this bar">Look</button>
    <button class="icon call" id="callBtn" title="Start a voice call">&#128222;</button>
    <button class="icon" id="appBtn" title="Open the NUBB-E app">&#9634;</button>
    <button class="icon" id="quitBtn" title="Quit NUBB-E">&times;</button>
  </div>
  <div class="chatname" id="chatName"></div>
  <div id="out"><div id="answer"></div></div>
  <div class="card" id="card">
    <div id="cardTitle"></div><pre id="cardDetail"></pre>
    <div class="cardbtns"><button id="cardNo">No</button><button class="look" id="cardYes">Yes, do it</button><span class="hint" id="cardAnswer"></span></div>
  </div>
  <div class="actions" id="actions">
    <button id="insertBtn" title="Paste this answer where your cursor was">Insert</button>
    <button id="copyBtn">Copy</button>
    <button id="newBtn" title="Start a fresh quick chat">New</button>
    <span class="hint" id="hint"></span>
  </div>
</div>
<script src="/face.js"></script>
<script>
const $ = id => document.getElementById(id);
const input = $('input'), out = $('out'), answer = $('answer'), actions = $('actions');
let FACE = null;
try { FACE = NubbeFace($('c'), { box: $('faceBox') }); FACE.set('idle'); } catch (e) {}
const face = s => { try { FACE && (FACE.isReaction(s) ? FACE.react(s) : FACE.set(s)); } catch (e) {} };
const api = () => (window.pywebview && window.pywebview.api) || null;
let chatId = null, busy = false, reply = '';

function esc(s) { return s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function md(src) {
  return src.split('```').map((part, i) => {
    if (i % 2) { const nl = part.indexOf('\n'); return '<pre><code>' + esc((nl >= 0 ? part.slice(nl + 1) : part).replace(/\n$/, '')) + '</code></pre>'; }
    return esc(part).replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
               .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<)]+[^\s<).,;:!?'"])/g,
                (m, text, url, bare) => '<a class="ext" href="' + (url || bare) + '">' + (text || bare) + '</a>').replace(/\n/g, '<br>');
  }).join('');
}
// links in answers (like web sources) open in the normal browser
document.addEventListener('click', e => {
  const a = e.target.closest('a.ext'); if (!a) return;
  e.preventDefault();
  fetch('/api/open', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: a.getAttribute('href').replace(/&amp;/g, '&') }) });
});
function plain(src) {   // what Insert pastes: no markdown symbols
  return src.replace(/```[^\n]*\n?/g, '').replace(/\*\*([^*\n]+)\*\*/g, '$1').replace(/__([^_\n]+)__/g, '$1')
            .replace(/`([^`\n]+)`/g, '$1').replace(/^#+\s*/gm, '')
            .replace(/\[([^\]\n]+)\]\((https?:[^)\s]+)\)/g, '$1 ($2)').trim();
}
function fit() { requestAnimationFrame(() => api() && api().set_height($('bar').offsetHeight)); }
function status(text) { out.classList.add('show'); answer.innerHTML = '<span class="status">' + esc(text) + '</span>'; fit(); }

async function ask(text, look) {
  if (busy) return;
  busy = true; $('lookBtn').disabled = true; reply = '';
  actions.classList.remove('show'); $('hint').textContent = '';
  status(look ? 'looking...' : '...');
  face('thinking'); if (look) face('lookaround');
  let pending = false;
  const render = () => { pending = false; answer.innerHTML = md(reply); out.scrollTop = out.scrollHeight; fit(); };
  try {
    const res = await fetch(look ? '/api/look' : '/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, message: text, source: 'quick' }) });
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 2);
        if (!line.startsWith('data:')) continue;
        const ev = JSON.parse(line.slice(5));
        if (ev.type === 'start') chatId = ev.chat_id;
        else if (ev.type === 'waking') { face('sleep'); status('waking up, one sec...'); }
        else if (ev.type === 'reasoning') { face('thinking'); status('thinking...'); }
        else if (ev.type === 'delta') { reply += ev.text; face('talking'); if (!pending) { pending = true; requestAnimationFrame(render); } }
        else if (ev.type === 'error') status(ev.text);
        else if (ev.type === 'tool') { face('thinking'); if (!reply) status(ev.text + '...'); }
        else if (ev.type === 'confirm') { confirmCard(ev); face('curious'); }
        else if (ev.type === 'confirmed') settleCard(ev.ok);
      }
    }
    if (reply) { render(); actions.classList.add('show'); fit(); }
  } catch (e) { status('Lost connection to NUBB-E.'); }
  face('idle');
  busy = false; $('lookBtn').disabled = false; input.focus();
}

// a Yes/No card before NUBB-E changes a file or runs a command (No is the default)
let cardId = null;
function confirmCard(ev) {
  cardId = ev.id;
  $('cardTitle').textContent = ev.title + ': ' + ev.target; $('cardDetail').textContent = ev.detail;
  $('cardAnswer').textContent = ''; $('card').querySelectorAll('button').forEach(b => b.disabled = false);
  $('card').classList.add('show'); $('cardNo').focus(); fit();
}
function settleCard(ok) {
  if (!cardId) return;
  cardId = null; $('card').querySelectorAll('button').forEach(b => b.disabled = true);
  $('cardAnswer').textContent = ok ? 'You said Yes.' : 'Not done.';
  setTimeout(() => { if (!cardId) { $('card').classList.remove('show'); fit(); } }, 1500);
  input.focus();
}
function answerCard(ok) {
  if (!cardId) return;
  fetch('/api/confirm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: cardId, ok }) });
  settleCard(ok);
}
$('cardNo').onclick = () => answerCard(false);
$('cardYes').onclick = () => answerCard(true);

function send(look) {
  const text = input.value.trim() || (look ? "What's on my screen?" : '');
  if (!text) return;
  input.value = ''; input.style.height = '';
  ask(text, look);
}
input.addEventListener('keydown', e => {
  if (e.key === 'Escape') { e.preventDefault(); answerCard(false); api() && api().hide(); }   // hiding says No to an open card
  else if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); send(true); }
  else if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(false); }
});
document.addEventListener('keydown', e => { if (e.key === 'Escape' && e.target !== input) { answerCard(false); api() && api().hide(); } });
input.addEventListener('input', () => { input.style.height = ''; input.style.height = Math.min(input.scrollHeight, 120) + 'px'; fit(); });
$('lookBtn').onclick = () => send(true);
$('appBtn').onclick = () => api() && api().open_app();
$('quitBtn').onclick = () => { if (confirm('Quit NUBB-E? The brain will shut down too.')) api() && api().quit(); };
$('insertBtn').onclick = async () => {
  const ok = api() && await api().insert(plain(reply));
  if (!ok) $('hint').textContent = "Couldn't insert. Click where you want the text, then open me again.";
};
$('copyBtn').onclick = async () => { try { await navigator.clipboard.writeText(plain(reply)); $('hint').textContent = 'Copied!'; } catch (e) {} };
function clearView() {
  reply = ''; out.classList.remove('show'); actions.classList.remove('show'); answer.innerHTML = '';
  $('chatName').classList.remove('show');
}
$('newBtn').onclick = () => {
  chatId = null; clearView(); input.focus(); fit();
  fetch('/api/current', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"chat_id": null}' });
};

// stay in sync with the chat open in the app: show its latest answer and keep talking in it
async function follow(id) {
  if (busy) return;
  if (!id) { if (chatId) { chatId = null; clearView(); fit(); } return; }
  chatId = id;
  try {
    const [msgs, chats] = await Promise.all([fetch('/api/chats/' + id).then(r => r.json()), fetch('/api/chats').then(r => r.json())]);
    const last = [...msgs].reverse().find(m => m.role === 'assistant');
    const chat = chats.find(c => c.id === id);
    $('chatName').textContent = chat ? 'in: ' + chat.title : ''; $('chatName').classList.toggle('show', !!chat);
    if (last) { reply = last.content; answer.innerHTML = md(reply); out.classList.add('show'); actions.classList.add('show'); }
    else { out.classList.remove('show'); actions.classList.remove('show'); }
  } catch (e) {}
  fit();
}
window.quickOpened = async (fresh = true) => {
  input.focus();
  if (fresh) {
    input.select(); face('curious');
    try { const cur = await fetch('/api/current').then(r => r.json()); if (cur.chat_id !== chatId) follow(cur.chat_id); } catch (e) {}
    try { showCall((await fetch('/api/voice').then(r => r.json())).in_call); } catch (e) {}
  }
  fit();
};

// call button, shared with the app window
let inCall = false;
function showCall(on) { inCall = on; $('callBtn').classList.toggle('on', on); $('callBtn').title = on ? 'End the call' : 'Start a voice call'; }
$('callBtn').onclick = async () => {
  const r = await fetch('/api/voice', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ action: inCall ? 'hangup' : 'call' }) }).then(r => r.json());
  showCall(r.in_call);
  if (r.voice === 'starting') status('connecting the call...');
};

// live updates from the app: replies in the current chat, call on/off, face states from a call
const events = new EventSource('/events');
events.onmessage = e => {
  const d = JSON.parse(e.data);
  if (d.event === 'chat' && d.current && !busy) follow(d.chat_id);
  if (d.event === 'call') showCall(d.in_call);
  if (d.state && !busy) face(d.state);
};
// clicking the bar after using another app: take keyboard focus back (Windows won't do it on its own here)
document.addEventListener('mousedown', e => {
  if (!document.hasFocus() && api() && !e.target.closest('.pywebview-drag-region')) api().activate();
}, true);
window.addEventListener('pywebviewready', fit);
</script>
</body>
</html>
"""
