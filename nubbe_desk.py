"""NUBB-E's desk: everyday things on this PC (Windows).

    import nubbe_desk as desk
    desk.open_app("calculator")      # -> "Opened Calculator."
    desk.set_volume(30)              # -> "Volume is at 30%."
    desk.media("next")               # -> "Skipped to the next song."
    desk.now_playing()               # -> {"title": ..., "artist": ..., "playing": True, "app": ...} or None
    desk.system_stats()              # -> "CPU 12%, memory 14.2 of 31.7 GB, ..."
    desk.find_files("resume")        # -> [Path, ...] (shown in Explorer, never run)

All of it is harmless and runs without a Yes card: opening an app or a site, volume, music, looking up
stats or file names. Nothing here deletes, sends or runs a found file.

Apps come from the Start menu (Get-StartApps), so Store apps like Calculator work too. Volume uses
Windows Core Audio and media uses Windows media controls (pywinrt), with media keys as the backup.
Standard library plus the optional winrt packages.
"""
import asyncio
import ctypes
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


class DeskError(Exception):
    """A friendly message, like "I can't find an app called Blender"."""


# ---------------------------------------------------------------------------
# apps
# ---------------------------------------------------------------------------
# Start menu entries that would change things or aren't apps: never opened by name
SKIP_APPS = re.compile(r"\b(uninstall\w*|reset|repair|remove|setup|installer|readme|release notes|license|"
                       r"documentation|help|website|manual)\b", re.IGNORECASE)
ALIASES = {"calc": "calculator", "vs code": "visual studio code", "vscode": "visual studio code",
           "code": "visual studio code", "chrome": "google chrome", "edge": "microsoft edge",
           "explorer": "file explorer", "files": "file explorer", "my files": "file explorer",
           "cmd": "command prompt", "store": "microsoft store", "teams": "microsoft teams",
           "task manager": "task manager", "the settings": "settings", "windows settings": "settings"}
_apps = {"list": [], "time": 0.0}
_apps_lock = threading.Lock()


def apps(refresh: bool = False) -> list[tuple[str, str]]:
    """(name, AppID) for everything in the Start menu. Cached for 10 minutes (it takes about a second)."""
    if not IS_WINDOWS:
        return []
    with _apps_lock:
        if refresh or not _apps["list"] or time.time() - _apps["time"] > 600:
            try:
                out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                      "Get-StartApps | ConvertTo-Json -Compress"], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=20, creationflags=NO_WINDOW).stdout
                data = json.loads(out or "[]")
                data = [data] if isinstance(data, dict) else data
                _apps["list"] = [(a["Name"], a["AppID"]) for a in data
                                 if a.get("Name") and a.get("AppID") and not SKIP_APPS.search(a["Name"])]
                _apps["time"] = time.time()
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        return list(_apps["list"])


def warm_up() -> None:
    """Reads the Start menu in the background, so the first "open ..." is instant."""
    threading.Thread(target=apps, daemon=True).start()


def plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w+#.' -]", " ", str(text).lower()).split())


def find_app(name: str) -> tuple[str, str] | None:
    """The Start menu app that best matches a spoken name, or None."""
    want = re.sub(r"^(the|my)\s+|\s+(app|application|program)$", "", plain(name))
    want = ALIASES.get(want, want)
    if not want:
        return None
    found = apps()
    for test in (lambda n: n == want,                                       # Calculator
                 lambda n: n.startswith(want + " ") or n.endswith(" " + want),   # Spotify Premium, Google Chrome
                 lambda n: re.search(rf"\b{re.escape(want)}\b", n)):        # Adobe Photoshop 2025
        hits = [a for a in found if test(a[0].lower())]
        if hits:
            return min(hits, key=lambda a: len(a[0]))
    close = difflib.get_close_matches(want, [a[0].lower() for a in found], n=1, cutoff=0.85)
    return next((a for a in found if a[0].lower() == close[0]), None) if close else None


def open_app(name: str) -> str:
    app = find_app(name)
    if not app:
        raise DeskError(f"I can't find an app called {name.strip()} in your Start menu.")
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app[1]}"], creationflags=NO_WINDOW)
    return f"Opened {app[0]}."


FOLDERS = {"downloads": "Downloads", "documents": "Documents", "desktop": "Desktop", "pictures": "Pictures",
           "photos": "Pictures", "videos": "Videos", "music": "Music", "home": "", "screenshots": "Pictures/Screenshots"}


def open_folder(name: str) -> str:
    key = re.sub(r"^(my|the)\s+|\s+folder$", "", plain(name))
    if key not in FOLDERS:
        raise DeskError(f"I don't know a folder called {name}.")
    p = Path.home() / FOLDERS[key]
    if not p.is_dir():
        raise DeskError(f"Your {key} folder isn't at {p}.")
    os.startfile(str(p))
    return f"Opened your {key or 'home'} folder."


# ---------------------------------------------------------------------------
# websites (they open in the normal browser: NUBB-E itself doesn't go online for this)
# ---------------------------------------------------------------------------
SITES = {"youtube": "https://www.youtube.com", "gmail": "https://mail.google.com", "google": "https://www.google.com",
         "github": "https://github.com", "reddit": "https://www.reddit.com", "wikipedia": "https://en.wikipedia.org",
         "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.com", "maps": "https://maps.google.com",
         "google maps": "https://maps.google.com", "chatgpt": "https://chatgpt.com", "x": "https://x.com",
         "twitter": "https://x.com", "linkedin": "https://www.linkedin.com", "whatsapp": "https://web.whatsapp.com",
         "google calendar": "https://calendar.google.com", "calendar": "https://calendar.google.com",
         "google drive": "https://drive.google.com", "drive": "https://drive.google.com",
         "google docs": "https://docs.google.com", "hugging face": "https://huggingface.co",
         "huggingface": "https://huggingface.co", "stack overflow": "https://stackoverflow.com",
         "stackoverflow": "https://stackoverflow.com", "instagram": "https://www.instagram.com",
         "facebook": "https://www.facebook.com", "twitch": "https://www.twitch.tv", "news": "https://news.google.com",
         "google news": "https://news.google.com", "spotify": "https://open.spotify.com",
         "arxiv": "https://arxiv.org", "claude": "https://claude.ai", "outlook": "https://outlook.live.com"}
SEARCH = {"youtube": "https://www.youtube.com/results?search_query={q}", "google": "https://www.google.com/search?q={q}",
          "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}", "github": "https://github.com/search?q={q}",
          "amazon": "https://www.amazon.com/s?k={q}", "reddit": "https://www.reddit.com/search/?q={q}",
          "maps": "https://www.google.com/maps/search/{q}", "google maps": "https://www.google.com/maps/search/{q}",
          "hugging face": "https://huggingface.co/models?search={q}", "huggingface": "https://huggingface.co/models?search={q}",
          "stack overflow": "https://stackoverflow.com/search?q={q}", "duckduckgo": "https://duckduckgo.com/?q={q}",
          "bing": "https://www.bing.com/search?q={q}", "netflix": "https://www.netflix.com/search?q={q}",
          "twitch": "https://www.twitch.tv/search?term={q}", "arxiv": "https://arxiv.org/a/search?query={q}",
          "spotify": "https://open.spotify.com/search/{q}", "gmail": "https://mail.google.com/mail/#search/{q}"}
DOMAIN = re.compile(r"^(https?://)?([\w-]+\.)+[a-z]{2,}(/\S*)?$", re.IGNORECASE)


def site_url(name: str) -> str | None:
    """A known site's address, or the address itself if it's a domain like github.com."""
    key = re.sub(r"^(the\s+)?|\s+(website|site|page|web ?site)$", "", plain(name)).removesuffix(".com")
    if key in SITES:
        return SITES[key]
    text = str(name).strip()
    if DOMAIN.match(text):
        return text if text.lower().startswith("http") else "https://" + text
    return None


def search_url(site: str, query: str) -> str | None:
    key = re.sub(r"^the\s+", "", plain(site))
    tpl = SEARCH.get(key)
    return tpl.format(q=urllib.parse.quote_plus(query.strip())) if tpl and query.strip() else None


def open_url(url: str) -> str:
    url = str(url).strip()
    if not url.lower().startswith(("https://", "http://", "spotify:")):
        raise DeskError("I only open web links (https://...).")
    os.startfile(url) if IS_WINDOWS else __import__("webbrowser").open(url)
    host = urllib.parse.urlparse(url).netloc.removeprefix("www.") or url.split(":")[0]
    return f"Opened {host}."


def open_website(site: str, search: str = "") -> str:
    if search:
        url = search_url(site, search)
        if not url:
            raise DeskError(f"I don't know how to search {site}.")
        open_url(url)
        return f"Searching {site.strip()} for {search.strip()}."
    url = site_url(site)
    if not url:
        raise DeskError(f"I don't know a website called {site}.")
    open_url(url)
    return f"Opened {site.strip()}." if plain(site) in SITES else f"Opened {url.split('://')[-1]}."


# ---------------------------------------------------------------------------
# COM helpers (volume): call a method by its number in the object's table
# ---------------------------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]


def _guid(text):
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _com(obj, index, argtypes, *args) -> int:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])(obj, *args)


class _Speakers:
    """The default speakers' volume control (IAudioEndpointVolume), released when done."""

    def __enter__(self):
        if not IS_WINDOWS:
            raise DeskError("Volume control only works on Windows.")
        ctypes.windll.ole32.CoInitializeEx(None, 0)
        self.objs = []
        enum, dev, vol = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        if ctypes.windll.ole32.CoCreateInstance(ctypes.byref(_guid("BCDE0395-E52F-467C-8E3D-C4579291692E")), None, 0x17,
                                                ctypes.byref(_guid("A95664D2-9614-4F35-A746-DE8DB63617E6")),
                                                ctypes.byref(enum)):
            raise DeskError("I can't reach the sound settings.")
        self.objs.append(enum)
        if _com(enum, 4, [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], 0, 1, ctypes.byref(dev)):
            raise DeskError("There are no speakers to set the volume on.")
        self.objs.append(dev)
        if _com(dev, 3, [ctypes.POINTER(_GUID), ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                ctypes.byref(_guid("5CDF2C82-841E-4546-9722-0CF74078229A")), 0x17, None, ctypes.byref(vol)):
            raise DeskError("I can't reach the volume control.")
        self.objs.append(vol)
        return vol

    def __exit__(self, *exc):
        for obj in reversed(self.objs):
            _com(obj, 2, [])      # Release


def get_volume() -> tuple[int, bool]:
    with _Speakers() as vol:
        level, mute = ctypes.c_float(), ctypes.c_int()
        _com(vol, 9, [ctypes.POINTER(ctypes.c_float)], ctypes.byref(level))
        _com(vol, 15, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(mute))
        return round(level.value * 100), bool(mute.value)


def set_volume(level: int | None = None, change: int | None = None, mute: bool | None = None) -> str:
    """level: 0 to 100. change: up or down by this much. mute: True / False."""
    with _Speakers() as vol:
        if level is not None or change is not None:
            now = ctypes.c_float()
            _com(vol, 9, [ctypes.POINTER(ctypes.c_float)], ctypes.byref(now))
            new = level if level is not None else round(now.value * 100) + int(change)
            new = max(0, min(100, int(new)))
            _com(vol, 7, [ctypes.c_float, ctypes.c_void_p], ctypes.c_float(new / 100), None)
            if new > 0 and mute is None:
                mute = False      # setting a volume also unmutes, like the keyboard keys do
        if mute is not None:
            _com(vol, 14, [ctypes.c_int, ctypes.c_void_p], int(bool(mute)), None)
    level, muted = get_volume()
    return "Muted." if muted else f"Volume is at {level}%."


# ---------------------------------------------------------------------------
# music and video: Windows media controls (works with Spotify, browsers, Media Player...)
# ---------------------------------------------------------------------------
MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
MEDIA_SAYS = {"play": "Playing.", "pause": "Paused.", "play_pause": "Okay.", "next": "Skipped to the next one.",
              "previous": "Went back one.", "stop": "Stopped."}


def _session_call(fn):
    """Runs fn(session) on the current media session with winrt. None if winrt or a session isn't there."""
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    except ImportError:
        return None

    async def go():
        manager = await Manager.request_async()
        session = manager.get_current_session()
        return await fn(session) if session else None
    box = {}

    def run():   # its own thread, so it never clashes with an event loop or COM setup of the caller
        try:
            box["v"] = asyncio.run(go())
        except Exception:
            box["v"] = None
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    return box.get("v")


def now_playing() -> dict | None:
    async def read(session):
        props = await session.try_get_media_properties_async()
        status = session.get_playback_info().playback_status
        return {"title": props.title or "", "artist": props.artist or "", "album": props.album_title or "",
                "playing": int(status) == 4, "app": session.source_app_user_model_id or ""}
    info = _session_call(read)
    return info if info and (info["title"] or info["artist"]) else None


def describe_playing() -> str:
    info = now_playing()
    if not info:
        return "Nothing is playing right now."
    what = info["title"] + (f" by {info['artist']}" if info["artist"] else "")
    return ("Playing " if info["playing"] else "Paused: ") + what + "."


def _press(vk: int) -> None:
    u32 = ctypes.windll.user32
    u32.keybd_event(vk, 0, 1, 0)          # KEYEVENTF_EXTENDEDKEY
    u32.keybd_event(vk, 0, 1 | 2, 0)      # + KEYEVENTF_KEYUP


def media(action: str) -> str:
    """play / pause / play_pause / next / previous / stop."""
    action = {"toggle": "play_pause", "resume": "play", "skip": "next", "back": "previous",
              "prev": "previous"}.get(action, action)
    if action not in MEDIA_SAYS:
        raise DeskError(f"I don't know the media action '{action}'.")
    names = {"play": "try_play_async", "pause": "try_pause_async", "play_pause": "try_toggle_play_pause_async",
             "next": "try_skip_next_async", "previous": "try_skip_previous_async", "stop": "try_stop_async"}

    async def act(session):
        return await getattr(session, names[action])()
    if not _session_call(act):            # no winrt or no player that listens: press the media key
        if not IS_WINDOWS:
            raise DeskError("Media controls only work on Windows.")
        _press(MEDIA_KEYS["play_pause" if action in ("play", "pause") else action])
    return MEDIA_SAYS[action]


SPOTIFY_TRACK = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?track/([A-Za-z0-9]{22})")


def play_music(query: str, app: str = "spotify", search=None) -> str:
    """Plays a song or artist. search(query) -> text with links (the web search) finds the exact track
    on Spotify when online. Offline, or on YouTube, it opens the search instead."""
    query = query.strip()
    app = plain(app or "spotify")
    if app == "youtube":
        open_url(SEARCH["youtube"].format(q=urllib.parse.quote_plus(query)))
        return f"Opened YouTube results for {query}. Pick the one you want."
    spotify_app = find_app("spotify")
    track = None
    if search and spotify_app:
        try:
            m = SPOTIFY_TRACK.search(search(f"{query} site:open.spotify.com/track") or "")
            track = m.group(1) if m else None
        except Exception:
            track = None
    if track:
        before = now_playing()
        os.startfile(f"spotify:track:{track}")
        for _ in range(16):                # Spotify takes a moment to start: wait, then make sure it plays
            time.sleep(0.5)
            info = now_playing()
            if info and info != before and info["playing"]:
                return f"Playing {info['title']}" + (f" by {info['artist']}." if info["artist"] else ".")
        media("play")
        return f"Playing {query} on Spotify."
    if spotify_app:
        os.startfile(f"spotify:search:{query}")
        return f"I opened {query} in Spotify. Press play on the one you want."
    open_url(SEARCH["spotify"].format(q=urllib.parse.quote(query)))
    return f"Spotify isn't installed, so I opened {query} on the Spotify website."


# ---------------------------------------------------------------------------
# system stats (read only)
# ---------------------------------------------------------------------------
def _cpu_percent(seconds: float = 0.3) -> float | None:
    if not IS_WINDOWS:
        return None
    ft = ctypes.c_ulonglong

    def times():
        idle, kernel, user = ft(), ft(), ft()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        return idle.value, kernel.value + user.value
    i1, t1 = times()
    time.sleep(seconds)
    i2, t2 = times()
    return 100.0 * (1 - (i2 - i1) / (t2 - t1)) if t2 > t1 else None


def system_stats() -> str:
    parts = []
    try:   # the GPU check is slow to start, so it runs while the CPU is measured
        gpu = subprocess.Popen(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                                "--format=csv,noheader,nounits"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, creationflags=NO_WINDOW)
    except OSError:
        gpu = None
    cpu = _cpu_percent()
    if cpu is not None:
        parts.append(f"CPU {cpu:.0f}%")
    if IS_WINDOWS:
        class Mem(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("pt", ctypes.c_ulonglong), ("pa", ctypes.c_ulonglong),
                        ("vt", ctypes.c_ulonglong), ("va", ctypes.c_ulonglong), ("ve", ctypes.c_ulonglong)]
        m = Mem()
        m.len = ctypes.sizeof(m)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            parts.append(f"memory {(m.total - m.avail) / 2**30:.1f} of {m.total / 2**30:.1f} GB used")

        class Power(ctypes.Structure):
            _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte),
                        ("saver", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]
        p = Power()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p)) and p.pct != 255:
            parts.append(f"battery {p.pct}%" + (", plugged in" if p.ac == 1 else ", on battery"))
    try:
        d = shutil.disk_usage(Path.home().anchor or "/")
        parts.append(f"{d.free / 2**30:.0f} GB free on {Path.home().anchor.rstrip(chr(92)) or '/'}")
    except OSError:
        pass
    try:
        out = gpu.communicate(timeout=3)[0].strip().splitlines() if gpu else []
        if out:
            util, used, total, temp = [x.strip() for x in out[0].split(",")]
            parts.append(f"GPU {util}% busy, {float(used) / 1024:.1f} of {float(total) / 1024:.1f} GB video memory, {temp}°C")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        gpu.kill()
    if IS_WINDOWS:
        up = ctypes.windll.kernel32.GetTickCount64() / 1000 if hasattr(ctypes.windll.kernel32, "GetTickCount64") else 0
        if up:
            parts.append(f"on for {int(up // 3600)} h {int(up % 3600 // 60)} min")
    return (", ".join(parts) + ".") if parts else "I can't read the system stats here."


# ---------------------------------------------------------------------------
# finding files by name (fast; a found file is shown in Explorer, never opened or run)
# ---------------------------------------------------------------------------
FIND_SECONDS = 4
FIND_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "appdata", "site-packages", ".cache",
             "$recycle.bin", "anaconda3", "miniconda3", ".conda"}
FILLER = {"my", "the", "a", "an", "file", "document", "doc", "called", "named", "folder"}


def _starts() -> list[Path]:
    home = Path.home()
    first = [home / n for n in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")]
    first += sorted(p for p in home.glob("OneDrive*") if p.is_dir())
    return [p for p in first if p.is_dir()] + [home]


def find_files(name: str, limit: int = 8, seconds: float = FIND_SECONDS) -> list[Path]:
    """Files (and folders) whose name has every word of `name`, best first: Desktop, Documents and
    Downloads before the rest of the home folder. Private files (keys, passwords) are never listed."""
    import nubbe_tools as tools
    words = [w for w in re.findall(r"[\w.]+", str(name).lower()) if w not in FILLER]
    if not words:
        raise DeskError("Tell me the file's name.")
    found, seen = [], set()
    deadline = time.time() + seconds
    for start in _starts():
        for folder, dirs, files in os.walk(start):
            if time.time() > deadline:
                break
            here = Path(folder)
            if os.path.normcase(str(here)) in seen:
                dirs[:] = []
                continue
            seen.add(os.path.normcase(str(here)))
            dirs[:] = [d for d in dirs if d.lower() not in FIND_SKIP and not d.startswith(".")
                       and d.lower() not in tools.SEARCH_SKIP]
            for n in files + dirs:
                low = re.sub(r"[_\-.]+", " ", n.lower())
                if all(w in low or w in n.lower() for w in words):
                    p = here / n
                    if tools.secret(p):
                        continue
                    stem = re.sub(r"[_\-.]+", " ", Path(n).stem.lower())
                    score = (0 if stem == " ".join(words) else 1 if stem.startswith(words[0]) else 2)
                    try:
                        mtime = p.stat().st_mtime
                    except OSError:
                        mtime = 0
                    found.append((score, -mtime, p))
    found.sort(key=lambda f: (f[0], f[1]))
    return [p for _, _, p in found[:limit]]


def describe_found(name: str, paths: list[Path]) -> str:
    if not paths:
        return f"I couldn't find a file called {name} in your folders."
    home = Path.home()
    show = [("~/" + p.relative_to(home).as_posix()) if p.is_relative_to(home) else str(p) for p in paths]
    if len(paths) == 1:
        return f"Found it: {show[0]}. I showed it in File Explorer."
    return f"Found {len(paths)}, newest best match first:\n" + "\n".join(f"- {s}" for s in show) + \
        "\nI showed the first one in File Explorer."


def show_in_explorer(path) -> None:
    """Shows a file selected in File Explorer. It's never opened or run."""
    if IS_WINDOWS:
        subprocess.Popen(["explorer.exe", "/select,", str(path)], creationflags=NO_WINDOW)


def find_file(name: str) -> str:
    paths = find_files(name)
    if paths:
        show_in_explorer(paths[0])
    return describe_found(name, paths)


# ---------------------------------------------------------------------------
# a Windows notification (reminders), shown even when no NUBB-E window is open
# ---------------------------------------------------------------------------
TOAST_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode($env:NUBBE_TOAST_TITLE)) | Out-Null
$n.Item(1).AppendChild($t.CreateTextNode($env:NUBBE_TOAST_TEXT)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notify(title: str, text: str) -> None:
    """A Windows notification. The text goes in through environment variables, never into the script."""
    if not IS_WINDOWS:
        return
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", TOAST_SCRIPT], creationflags=NO_WINDOW,
                         env=dict(os.environ, NUBBE_TOAST_TITLE=title[:100], NUBBE_TOAST_TEXT=text[:300]),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except (ImportError, RuntimeError):
        pass
