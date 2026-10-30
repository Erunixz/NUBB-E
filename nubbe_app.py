#!/usr/bin/env python3
"""NUBB-E app: one launcher for everything, fully offline.

    python nubbe_app.py

What it does:
  - starts the brain (llama-server) if it isn't running yet
  - opens a NUBB-E window: animated face on top, chat below
  - saves every chat (sidebar, like any chat app)
  - remembers things about you (Memory panel). It picks up lasting facts
    you tell it, and you can add, check or delete them anytime
  - /remember <fact> in the chat saves a memory directly
  - nubbe_voice.py talks through the app too, so voice chats are saved
    and NUBB-E learns from them automatically
  - puts the brain to sleep after idle_unload_minutes with no chat (frees
    RAM and VRAM), and wakes it up again on your next message

Everything is stored on your PC in ~/.nubbe/nubbe.db. Nothing goes online.
Standard library only. Optional: `pip install pywebview` for a real app
window instead of a browser window.

Flags:
  --no-brain    don't start llama-server (you run it yourself)
  --browser     open in your normal browser instead of an app window
  --no-window   don't open a window, just serve the page
  --background  start with no app window, just the quick bar

Quick bar (Windows, pip install pywebview): press Alt+Space (quick_hotkey in
config.toml) from any app for a small see-through bar on top. Type and press
Enter, or Ctrl+Enter to Look at the screen behind it. Insert pastes the answer
where your cursor was. Esc hides it. Closing the app window keeps NUBB-E
running for the quick bar; quit from the bar's x button.

Look button: NUBB-E takes a screenshot (its own window hides for a moment)
and answers your question about it. Needs `pip install mss pillow` and the
mmproj vision file set in config.toml. Only "[screenshot]" is saved in the
chat, never the image itself.
"""
import argparse
import atexit
import json
import os
import queue
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Settings come from config.toml (paths to llama-server, models, ports)
# ---------------------------------------------------------------------------
from nubbe_config import CFG, llama_args, path   # keep nubbe_config.py next to this file

LLAMA_SERVER = str(path(CFG["brain"]["llama_server"]))
LLAMA_ARGS = llama_args(CFG["brain"])
LLM_URL = f"http://127.0.0.1:{CFG['brain']['port']}"
PORT = CFG["app"]["port"]        # same port as nubbe_face.py, so the voice script drives this face too
HISTORY_TURNS = CFG["app"]["history_turns"]   # how many recent messages are sent to the model

HERE = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".nubbe"
DB_PATH = DATA_DIR / "nubbe.db"
PROMPT_PATH = HERE / "nubbe_system_prompt.txt"
from nubbe_face import FACE_JS, FACE_STATES   # shared face engine (keep nubbe_face.py next to this file)
from nubbe_quick import QUICK_PAGE            # quick bar page (keep nubbe_quick.py next to this file)
import nubbe_tools as tools                   # file tools, only inside workspace_dir (keep next to this file)

VOICE_RULES = (
    "\n\n## Voice call\n"
    "You're on a voice call with Erfan, talking out loud like a friend on the phone. "
    "Keep it short and natural: usually one to three short sentences. React like a person "
    "(\"oh nice\", \"hmm\", \"wait, really?\") and sometimes ask something back. "
    "Only go longer if he asks for detail. Don't end every reply with a question. "
    "You can't hear your own voice, so if he says you pronounce a word wrong, fix it: add "
    "[say: word = spelling] at the very end, spelled so it sounds right when read aloud "
    "(for example [say: Erfan = Er-fawn]), and just say thanks. "
    "No markdown, no lists, no code blocks, no emoji: everything you write is read aloud, "
    "so say code and symbols in plain words. "
    "Now and then you may add a little robot sound, like *happy beep*, but rarely."
)

EXTRACT_PROMPT = (
    "You maintain a small memory about the user, Erfan. From the exchange below, list NEW lasting "
    "facts the user stated about himself: preferences, projects, goals, setup, people in his life, "
    "how he likes things done, and feedback about how NUBB-E should talk or behave (for example: wants "
    "shorter answers). Only facts the USER clearly said, never guesses or things the assistant said. "
    "Skip temporary things (today's mood, a one-off question). Skip anything already known. "
    "Never save passwords, ID numbers, card or bank numbers. "
    "Write each fact as a short sentence, one per line, starting with '- '. "
    "If there is nothing new worth remembering, reply with exactly NONE."
)

SUMMARY_PROMPT = (
    "Summarize this conversation between Erfan and NUBB-E in one or two short sentences: the topics, "
    "any decisions, and anything to follow up on. Plain text, no lists, no preamble."
)

# ---------------------------------------------------------------------------
# storage (SQLite, local file)
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, title TEXT NOT NULL, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL, content TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, text TEXT NOT NULL, source TEXT, created REAL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""
DEFAULT_SETTINGS = {"auto_memory": "1", "think": "0"}   # Fast by default: Smart thinks first, which is slow
CHAT_SLOT, MEMORY_SLOT, LOOK_SLOT, VOICE_SLOT = 0, 1, 2, 3   # fixed brain slots, so each keeps its prompt cached
LOOK_MEMORY_SECONDS = 600   # a screenshot stays in the conversation (in RAM only) for follow-up questions
LAST_LOOK: dict[int, dict] = {}   # chat id -> {"image", "msg_id", "time"}; never written to disk
CURRENT = {"chat_id": None}       # the chat open in the app, shared with the quick bar
VISION = bool(CFG["brain"].get("mmproj"))
MAX_TOOL_ROUNDS = 6         # tool calls per reply before NUBB-E has to answer
CONFIRM_SECONDS = 300       # a Yes/No card with no answer counts as No
PENDING: dict[str, dict] = {}   # confirmation card id -> {"event", "ok"}


def take_screenshot(monitor: int = 1, width: int = 1280) -> str:
    """Screenshot as base64 JPEG, with the NUBB-E window hidden while it's taken (see look.py)."""
    from look import hidden_window, screenshot_b64
    with hidden_window():
        return screenshot_b64(monitor, width)


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():   # deleted while running: start fresh instead of crashing
        init_db()
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def rows(sql: str, args=()) -> list[dict]:
    with closing(connect()) as con, con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def run(sql: str, args=()) -> int:
    with closing(connect()) as con, con:
        return con.execute(sql, args).lastrowid


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as con, con:
        con.executescript(SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(chats)")}
        if "summary" not in cols:   # added later: a short running summary of each chat
            con.execute("ALTER TABLE chats ADD COLUMN summary TEXT")
            con.execute("ALTER TABLE chats ADD COLUMN summary_n INTEGER DEFAULT 0")
        con.execute("PRAGMA journal_mode = WAL")
        for k, v in DEFAULT_SETTINGS.items():
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def get_settings() -> dict:
    return {r["key"]: r["value"] == "1" for r in rows("SELECT key, value FROM settings")}


def title_from(text: str) -> str:
    text = " ".join(text.split())
    return text[:40] + ("..." if len(text) > 40 else "") or "New chat"


# ---------------------------------------------------------------------------
# the brain
# ---------------------------------------------------------------------------
def llm_alive() -> bool:
    try:
        urllib.request.urlopen(LLM_URL + "/health", timeout=1).close()
        return True
    except Exception:
        return False


def tie_to_app(proc):
    """Windows: put the brain in a job that closes with the app, so llama-server can never
    outlive NUBB-E (even if the app crashes or its console is closed). Returns the job handle."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(proc._handle))
        return job
    except Exception:
        return None


class Brain:
    """Owns llama-server: starts it, wakes it on demand, and puts it to sleep when idle.
    Only a brain this app started is ever stopped."""

    def __init__(self, can_start=True, idle_minutes=0.0):
        self.can_start = can_start
        self.idle_seconds = idle_minutes * 60
        self.proc = None
        self.job = None
        self.log = None
        self.asleep = False
        self.active = 0              # replies in progress (never unload during one)
        self.last_used = time.time()
        self.lock = threading.Lock()

    def status(self) -> str:
        if llm_alive():
            return "online"
        if self.proc and self.proc.poll() is None:
            return "loading"
        return "asleep" if self.asleep else "offline"

    def start(self) -> bool:
        """Starts llama-server if it isn't running or loading. Call with self.lock held."""
        if llm_alive() or (self.proc and self.proc.poll() is None):
            return True
        exe = LLAMA_SERVER if Path(LLAMA_SERVER).exists() else shutil.which(CFG["brain"]["llama_server"])
        if not exe:
            print(f"Couldn't find llama-server ('{LLAMA_SERVER}'). Set llama_server in "
                  "config.toml, or start it yourself and use --no-brain.")
            return False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if self.log:
            self.log.close()
        self.log = open(DATA_DIR / "llama-server.log", "w")
        print("Starting the brain (first load can take a minute)...")
        self.proc = subprocess.Popen([exe, *LLAMA_ARGS], stdout=self.log, stderr=subprocess.STDOUT)
        self.job = tie_to_app(self.proc)
        self.asleep = False
        self.last_used = time.time()
        threading.Thread(target=self.warm_up, args=(self.proc,), daemon=True).start()
        return True

    def warm_up(self, proc) -> None:
        """Once loaded, send a tiny hidden message with the system prompt, so the GPU is warmed
        up and the prompt is cached before Erfan's first real message."""
        while not llm_alive():
            if proc.poll() is not None:
                return
            time.sleep(0.5)
        warm_slot(system_prompt(with_tools=True), CHAT_SLOT)

    def ensure(self, on_wait=None, timeout=600) -> bool:
        """Makes sure the brain is up, waking it if needed. Calls on_wait() once if we have to wait."""
        with self.lock:
            if llm_alive():
                return True
            if not self.can_start or not self.start():
                return False
        if on_wait:
            on_wait()
        end = time.time() + timeout
        while time.time() < end:
            if llm_alive():
                return True
            if not self.proc or self.proc.poll() is not None:
                return False   # crashed while loading (see llama-server.log)
            time.sleep(0.5)
        return False

    @contextmanager
    def use(self):
        """Wrap every brain call in this, so it's never put to sleep mid-reply."""
        with self.lock:
            self.active += 1
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1
                self.last_used = time.time()

    def _kill(self) -> None:
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        if self.log:
            self.log.close()
            self.log = None

    def stop(self) -> None:
        with self.lock:
            self._kill()

    def sleep_if_idle(self) -> bool:
        with self.lock:
            idle = (self.proc and self.proc.poll() is None and not self.active
                    and time.time() - self.last_used > self.idle_seconds)
            if idle:
                self._kill()
                self.asleep = True
        if idle:
            print("No chats for a while: brain is asleep to free memory. It wakes on your next message.")
            broadcast({"state": "sleep", "status": "asleep"})
        return bool(idle)

    def watch_idle(self) -> None:
        if not self.idle_seconds:
            return
        while True:
            time.sleep(min(30.0, max(0.2, self.idle_seconds / 4)))
            self.sleep_if_idle()


BRAIN = Brain(idle_minutes=CFG["brain"]["idle_unload_minutes"])


def tool_list(slot: int):
    """File tools are offered in text chats (not voice, which stays fast) when the workspace folder exists."""
    return tools.SCHEMAS if slot == CHAT_SLOT and tools.root() else None


def warm_slot(system: str, slot: int) -> None:
    """Sends a tiny hidden message so this slot has the system prompt cached (and the GPU is warm)."""
    try:
        with BRAIN.use():
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": "hi"}]
            for _ in llm_stream(msgs, think=False, max_tokens=1, slot=slot, tools=tool_list(slot)):
                pass
    except Exception:
        pass


class Voice:
    """The call button: runs nubbe_voice.py --serve in the background (speech models load once,
    on the CPU) and sends it one command per line: call / hangup / listen / stop / quit."""

    def __init__(self):
        self.proc = None
        self.job = None
        self.log = None
        self.in_call = False
        self.lock = threading.Lock()

    def send(self, cmd: str) -> str:
        with self.lock:
            if cmd in ("call", "hangup"):
                self.in_call = cmd == "call"
            elif cmd == "stop":
                self.in_call = False
            if self.proc is None or self.proc.poll() is not None:
                self.in_call = self.in_call and cmd == "call"
                if cmd not in ("listen", "call"):
                    return "off"
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                if self.log:
                    self.log.close()
                self.log = open(DATA_DIR / "voice.log", "w", encoding="utf-8")
                self.proc = subprocess.Popen(
                    [sys.executable, str(HERE / "nubbe_voice.py"), "--serve"], cwd=HERE,
                    stdin=subprocess.PIPE, stdout=self.log, stderr=subprocess.STDOUT, text=True,
                    env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"))
                self.job = tie_to_app(self.proc)
                broadcast({"state": "thinking", "status": "connecting..."})
                status = "starting"
                if llm_alive():   # get the brain's voice slot ready while the speech models load
                    threading.Thread(target=warm_slot, args=(system_prompt() + VOICE_RULES, VOICE_SLOT),
                                     daemon=True).start()
            else:
                status = "ok"
            try:
                self.proc.stdin.write(cmd + "\n")
                self.proc.stdin.flush()
            except OSError:
                return "off"
            return status

    def stop(self) -> None:
        with self.lock:
            self.in_call = False
            p, self.proc = self.proc, None
            if p and p.poll() is None:
                try:
                    p.stdin.write("quit\n")
                    p.stdin.flush()
                    p.wait(3)
                except (OSError, subprocess.TimeoutExpired):
                    p.kill()
            if self.log:
                self.log.close()
                self.log = None


VOICE = Voice()


def system_prompt(chat_id=None, with_tools=False) -> str:
    if PROMPT_PATH.exists():
        base = PROMPT_PATH.read_text(encoding="utf-8")
    else:
        base = "You are NUBB-E, Erfan's friendly, optimistic and very nerdy offline AI buddy."
    mems = rows("SELECT text FROM memories ORDER BY id")
    if mems:
        base += ("\n\n## What you remember about Erfan\n"
                 + "\n".join(f"- {m['text']}" for m in mems)
                 + "\nUse these naturally when they're relevant. Don't list them back unprompted.")
    recent = rows("SELECT title, summary FROM chats WHERE summary IS NOT NULL AND summary != '' "
                  "AND id != ? AND updated > ? ORDER BY updated DESC LIMIT 5",
                  (chat_id or 0, time.time() - 14 * 86400))
    if recent:   # what you talked about in other chats lately, so NUBB-E can connect the dots
        base += ("\n\n## Recent conversations with Erfan (other chats)\n"
                 + "\n".join(f"- {r['title']}: {r['summary']}" for r in recent)
                 + "\nBring these up only when they're relevant, like a friend who remembers.")
    if with_tools and tools.root():
        base += tools.rules()
    return base


def llm_stream(messages: list, think: bool, max_tokens=None, temperature=None, slot=CHAT_SLOT, tools=None):
    """Yields ('reasoning', '') while the model thinks and ('content', text) as it answers.
    With tools, ends with ('tool_calls', [...]) if the model wants to use some."""
    payload = {"model": "local", "stream": True, "messages": messages, "id_slot": slot}
    if tools:
        payload["tools"] = tools
    calls: dict[int, dict] = {}
    if not think:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature
    req = urllib.request.Request(LLM_URL + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"]
            except (KeyError, IndexError, json.JSONDecodeError):
                continue
            if delta.get("reasoning_content"):
                yield "reasoning", ""
            if delta.get("content"):
                yield "content", delta["content"]
            for tc in delta.get("tool_calls") or []:   # streamed in pieces: name first, arguments bit by bit
                c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                c["id"] = tc.get("id") or c["id"]
                fn = tc.get("function") or {}
                c["name"] += fn.get("name") or ""
                c["arguments"] += fn.get("arguments") or ""
    if calls:
        yield "tool_calls", [{"id": c["id"] or f"call_{i}", "type": "function",
                              "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                             for i, c in sorted(calls.items())]


_memory_lock = threading.Lock()


def extract_memories(user_text: str, reply: str) -> None:
    """Runs after a reply: saves any lasting facts the user shared."""
    with _memory_lock:
        known = [m["text"] for m in rows("SELECT text FROM memories")]
        msgs = [
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": "Already known:\n" + ("\n".join(f"- {k}" for k in known) or "(nothing yet)")
             + f"\n\nErfan said:\n{user_text}\n\nNUBB-E replied:\n{reply[:1500]}"},
        ]
        try:
            with BRAIN.use():
                out = "".join(v for k, v in llm_stream(msgs, think=False, max_tokens=150, temperature=0.2,
                                                       slot=MEMORY_SLOT)
                              if k == "content")
        except Exception:
            return
        seen = {k.lower() for k in known}
        added = 0
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            fact = line[2:].strip().rstrip(".")
            if 3 < len(fact) < 200 and fact.lower() not in seen and added < 3:
                run("INSERT INTO memories(text, source, created) VALUES (?, 'auto', ?)", (fact, time.time()))
                seen.add(fact.lower())
                added += 1
        if added:
            broadcast({"event": "memory"})


def summarize_chat(chat_id: int) -> None:
    """Keeps a one or two sentence summary of each chat, so other chats can refer back to it."""
    with _memory_lock:
        chat = rows("SELECT summary_n FROM chats WHERE id = ?", (chat_id,))
        msgs = rows("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))
        if not chat or len(msgs) < 4 or len(msgs) - (chat[0]["summary_n"] or 0) < 4:
            return   # refresh every two exchanges or so
        talk = "\n".join(f"{'Erfan' if m['role'] == 'user' else 'NUBB-E'}: {m['content'][:400]}" for m in msgs[-30:])
        try:
            with BRAIN.use():
                out = "".join(v for k, v in llm_stream(
                    [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": talk}],
                    think=False, max_tokens=90, temperature=0.2, slot=MEMORY_SLOT) if k == "content")
        except Exception:
            return
        out = " ".join(out.split())[:400]
        if out:
            run("UPDATE chats SET summary = ?, summary_n = ? WHERE id = ?", (out, len(msgs), chat_id))


def learn_from(chat_id: int, text: str, reply: str, memory: bool) -> None:
    """After each reply, in the background: remember new facts, refresh this chat's summary."""
    if memory:
        extract_memories(text, reply)
    summarize_chat(chat_id)


# ---------------------------------------------------------------------------
# face events (also accepts set_face() calls from nubbe_voice.py / nubbe_chat.py)
# ---------------------------------------------------------------------------
_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()


def broadcast(msg: dict) -> None:
    with _clients_lock:
        for q in _clients:
            q.put(msg)


# ---------------------------------------------------------------------------
# web server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def emit(self, obj):
        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
        self.wfile.flush()

    # routes
    def do_GET(self):
        self.guarded(self.get)

    def do_POST(self):
        self.guarded(self.post)

    def guarded(self, route):
        """A broken request or DB hiccup gets a clean error, never a crashed handler."""
        try:
            route()
        except (sqlite3.Error, ValueError, TypeError) as e:
            print(f"Request {self.path} failed: {e}")
            try:
                self.send_json({"error": str(e)}, 500)
            except OSError:
                pass
        except OSError:
            pass   # window closed

    def get(self):
        path = self.path.split("?")[0]
        if path == "/face.js":
            body = FACE_JS.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/", "/quick"):
            body = (PAGE if path == "/" else QUICK_PAGE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/events":
            self.events()
        elif path == "/api/health":
            state = BRAIN.status()
            self.send_json({"brain": state == "online", "state": state})
        elif path == "/api/chats":
            self.send_json(rows("SELECT id, title, updated FROM chats ORDER BY updated DESC"))
        elif m := re.fullmatch(r"/api/chats/(\d+)", path):
            self.send_json(rows("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id", (int(m[1]),)))
        elif path == "/api/memory":
            self.send_json(rows("SELECT id, text, source FROM memories ORDER BY id DESC"))
        elif path == "/api/settings":
            self.send_json(get_settings())
        elif path == "/api/current":
            self.send_json(CURRENT)
        elif path == "/api/voice":
            self.send_json({"in_call": VOICE.in_call})
        else:
            self.send_error(404)

    def post(self):
        path = self.path.split("?")[0]
        try:
            body = self.read_json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400)
            return
        if not isinstance(body, dict):
            self.send_error(400)
            return
        if path == "/api/chat":
            self.chat(body)
        elif path == "/api/voice":   # the call button
            action = body.get("action")
            if action not in ("call", "hangup", "listen", "stop"):
                self.send_error(400)
                return
            self.send_json({"voice": VOICE.send(action), "in_call": VOICE.in_call})
            broadcast({"event": "call", "in_call": VOICE.in_call})   # app window and quick bar buttons
        elif path == "/api/current":      # the app window opened a chat (or a new one)
            cid = body.get("chat_id")
            CURRENT["chat_id"] = int(cid) if cid else None
            self.send_json(CURRENT)
        elif path == "/api/look":
            self.chat(body, look=True)
        elif path == "/api/confirm":     # Erfan pressed Yes or No on a tool card
            card = PENDING.get(str(body.get("id", "")))
            if card:
                card["ok"] = body.get("ok") is True
                card["event"].set()
            self.send_json({"ok": bool(card)})
        elif path == "/state":  # set_face() from other scripts
            if body.get("state") not in FACE_STATES:
                self.send_error(400)
                return
            broadcast(body)
            if body.get("text") in ("Call ended", "Can't open the mic.") and VOICE.in_call:
                VOICE.in_call = False
                broadcast({"event": "call", "in_call": False})
            self.send_response(204)
            self.end_headers()
        elif m := re.fullmatch(r"/api/chats/(\d+)/delete", path):
            run("DELETE FROM chats WHERE id = ?", (int(m[1]),))
            self.send_json({"ok": True})
        elif m := re.fullmatch(r"/api/chats/(\d+)/rename", path):
            run("UPDATE chats SET title = ? WHERE id = ?", (str(body.get("title", ""))[:80] or "Chat", int(m[1])))
            self.send_json({"ok": True})
        elif path == "/api/memory":
            text = str(body.get("text", "")).strip()
            if text:
                run("INSERT INTO memories(text, source, created) VALUES (?, 'you', ?)", (text[:300], time.time()))
            self.send_json({"ok": bool(text)})
        elif m := re.fullmatch(r"/api/memory/(\d+)/delete", path):
            run("DELETE FROM memories WHERE id = ?", (int(m[1]),))
            self.send_json({"ok": True})
        elif path == "/api/settings":
            for k in DEFAULT_SETTINGS:
                if k in body:
                    run("UPDATE settings SET value = ? WHERE key = ?", ("1" if body[k] else "0", k))
            self.send_json(get_settings())
        else:
            self.send_error(404)

    def events(self):
        self.start_stream()
        q: queue.Queue = queue.Queue()
        with _clients_lock:
            _clients.append(q)
        try:
            while True:
                try:
                    self.emit(q.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except OSError:
            pass
        finally:
            with _clients_lock:
                if q in _clients:
                    _clients.remove(q)

    def chat(self, body: dict, look: bool = False):
        text = str(body.get("message", "")).strip()
        if look:
            text = text or "What's on my screen?"
        if not text:
            self.send_error(400)
            return
        voice = bool(body.get("voice"))      # from nubbe_voice.py: short spoken replies
        image = None
        if look:   # capture first, so it's the screen as it was when Erfan asked
            try:
                if not VISION:
                    raise RuntimeError("My eyes are off. Set mmproj in config.toml to turn vision on.")
                image = take_screenshot(int(body.get("monitor", 1)), int(body.get("width", 1280)))
            except Exception as e:
                msg = str(e) if isinstance(e, (RuntimeError, ValueError)) else f"I couldn't take a screenshot ({e})."
                if isinstance(e, ImportError):
                    msg = "Look needs two helpers: pip install mss pillow"
                self.start_stream()
                self.safe_emit({"type": "error", "text": msg})
                return
        saved_text = "[screenshot] " + text if look else text   # images are never stored, only this marker
        now = time.time()
        chat_id = body.get("chat_id")
        if not chat_id or not rows("SELECT id FROM chats WHERE id = ?", (chat_id,)):
            title = ("Call: " if voice and body.get("source") == "call" else "Voice: " if voice
                     else "Quick: " if body.get("source") == "quick"
                     else "Look: " if look else "") + title_from(text)
            chat_id = run("INSERT INTO chats(title, created, updated) VALUES (?, ?, ?)", (title, now, now))
        msg_id = run("INSERT INTO messages(chat_id, role, content, created) VALUES (?, 'user', ?, ?)",
                     (chat_id, saved_text, now))
        if not voice:
            CURRENT["chat_id"] = chat_id          # the app window and the quick bar follow this chat
        if image:
            LAST_LOOK[chat_id] = {"image": image, "msg_id": msg_id, "time": now}
        for cid in [c for c, v in LAST_LOOK.items() if now - v["time"] > LOOK_MEMORY_SECONDS]:
            del LAST_LOOK[cid]
        history = rows("SELECT id, role, content FROM messages WHERE chat_id = ? ORDER BY id",
                       (chat_id,))[-HISTORY_TURNS:]
        shot = LAST_LOOK.get(chat_id)
        for h in history:
            if shot and h["id"] == shot["msg_id"]:   # the screenshot rides along for follow-up questions
                asked = h["content"].removeprefix("[screenshot] ")
                h["content"] = [
                    {"type": "text", "text": "(I'm sharing a screenshot of my screen so you can see what I mean. "
                                             "Use it as context to answer what I asked. Only describe the screen "
                                             f"if I ask what's on it.) {asked}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shot['image']}"}}]
            del h["id"]
        slot = VOICE_SLOT if voice else CHAT_SLOT
        offered = tool_list(slot)
        messages = [{"role": "system", "content": system_prompt(chat_id, with_tools=bool(offered))
                     + (VOICE_RULES if voice else "")}] + history
        settings = get_settings()
        think = False if voice else settings["think"]

        self.start_stream()
        reply, told_reasoning = "", False

        def waking():
            self.emit({"type": "waking"})
            broadcast({"state": "sleep", "status": "waking up"})

        try:
            with BRAIN.use():
                self.emit({"type": "start", "chat_id": chat_id})
                if not BRAIN.ensure(on_wait=waking):
                    self.emit({"type": "error", "text": (
                        "My brain didn't start. Check ~/.nubbe/llama-server.log to see why."
                        if BRAIN.can_start else
                        "My brain isn't running. Start llama-server, or start me without --no-brain.")})
                    return
                for rnd in range(MAX_TOOL_ROUNDS + 1):   # the last round has no tools, so it must answer
                    calls, said = [], ""
                    for kind, val in llm_stream(messages, think=think, max_tokens=120 if voice else None,
                                                slot=slot, tools=offered if rnd < MAX_TOOL_ROUNDS else None):
                        if kind == "reasoning":
                            if not told_reasoning:
                                self.emit({"type": "reasoning"})
                                told_reasoning = True
                        elif kind == "tool_calls":
                            calls = val
                        else:
                            if not said and reply and not reply.endswith("\n"):
                                val = "\n\n" + val          # text before and after a tool call
                            said += val
                            reply += val
                            self.emit({"type": "delta", "text": val})
                    if not calls:
                        break
                    messages.append({"role": "assistant", "content": said, "tool_calls": calls})
                    for c in calls:
                        messages.append({"role": "tool", "tool_call_id": c["id"], "content": self.use_tool(c)})
                    told_reasoning = False
                self.emit({"type": "done"})
        except urllib.error.HTTPError as e:
            self.safe_emit({"type": "error", "text": f"My brain had trouble with that one (error {e.code}). Try again?"})
        except urllib.error.URLError:
            self.safe_emit({"type": "error", "text": "Can't reach my brain. Is llama-server still loading?"})
        except OSError:
            # window closed mid-reply, or the brain dropped the connection; still save what we have
            self.safe_emit({"type": "error", "text": "Lost the connection to my brain. Try again?"})
        finally:
            if reply:
                try:
                    if voice:   # [say: word = spelling] is a note for the voice, not part of the chat
                        reply = re.sub(r"\s*\[say:[^\]]*\]", "", reply).strip() or reply
                    run("INSERT INTO messages(chat_id, role, content, created) VALUES (?, 'assistant', ?, ?)",
                        (chat_id, reply, time.time()))
                    run("UPDATE chats SET updated = ? WHERE id = ?", (time.time(), chat_id))
                except sqlite3.Error as e:
                    print(f"Couldn't save the reply: {e}")
                # the app window and the quick bar both show this chat live
                broadcast({"event": "chat", "chat_id": chat_id, "current": CURRENT["chat_id"] == chat_id})
                # screen contents aren't facts about Erfan, so looks don't feed the memory
                threading.Thread(target=learn_from, args=(chat_id, text, reply, settings["auto_memory"] and not look),
                                 daemon=True).start()

    def use_tool(self, c: dict) -> str:
        """Runs one tool call from the model. Anything that changes files or runs a command waits for
        Erfan's Yes on a card in the chat; no answer, a closed window or a timeout all count as No."""
        name = c["function"]["name"]
        try:
            args = json.loads(c["function"]["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            tools.log(DATA_DIR / "tools.log", name, {}, "error: arguments weren't valid JSON")
            return "Your tool arguments weren't valid JSON. Try again."
        line = tools.status(name, args)
        self.emit({"type": "tool", "name": name, "text": line})
        broadcast({"state": "thinking", "status": line[:60]})
        approved = None
        if tools.needs_yes(name) and name in tools.TOOLS:
            cid = secrets.token_hex(16)
            PENDING[cid] = {"event": threading.Event(), "ok": False}
            try:
                self.emit({"type": "confirm", "id": cid, **tools.card(name, args)})
                PENDING[cid]["event"].wait(CONFIRM_SECONDS)
                approved = PENDING[cid]["ok"]
            finally:
                del PENDING[cid]
            self.emit({"type": "confirmed", "id": cid, "ok": approved})
        return tools.call(name, args, DATA_DIR / "tools.log", approved)

    def safe_emit(self, obj):
        try:
            self.emit(obj)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------
APP_BROWSERS = [
    "msedge", "chrome", "google-chrome", "chromium", "chromium-browser", "brave",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def run_pywebview(webview, url: str, background: bool) -> None:
    """App window + quick bar (Windows). Blocks until NUBB-E is quit."""
    quick = CFG["app"]["quick_bar"] and sys.platform == "win32"
    state = {"main": None, "quitting": False}

    def quit_all():
        state["quitting"] = True
        for w in list(webview.windows):
            w.destroy()

    def open_main():
        w = state["main"]
        if w is not None:
            w.show()
            w.restore()
            return
        w = webview.create_window("NUBB-E", url, width=1200, height=820, background_color="#0b0f14")
        if quick and CFG["app"]["close_to_background"]:
            def on_closing():
                if state["quitting"]:
                    return True
                w.hide()         # closing the app window keeps NUBB-E running for the quick bar
                print(f"App window hidden. NUBB-E keeps running: {hotkey} opens the quick bar.")
                return False
            w.events.closing += on_closing
        state["main"] = w

    hotkey = CFG["app"]["quick_hotkey"]
    if background and not quick:
        print("Background mode needs the quick bar (Windows, quick_bar = true). Opening the app window.")
    if not background or not quick:
        open_main()
    if not quick:
        webview.start()
        return
    import nubbe_quick
    bar = nubbe_quick.QuickBar(webview, url, open_main, quit_all)
    nubbe_quick.on_ctrl_c(quit_all)

    def started():
        try:
            nubbe_quick.listen_hotkey(hotkey, bar.toggle)
            print(f"Quick bar ready: press {hotkey} from any app. Quit from its x button or Ctrl+C here.")
        except (OSError, ValueError) as e:
            print(e)
    webview.start(started)


def open_window(url: str, force_browser: bool, background: bool = False) -> bool:
    """Returns True if the window blocks until NUBB-E is quit (pywebview)."""
    if not force_browser:
        try:
            import webview  # pip install pywebview (needed for the quick bar)
        except ImportError:
            webview = None
        if webview:
            run_pywebview(webview, url, background)
            return True
        if background:
            print("Background mode needs pywebview: pip install pywebview")
        for name in APP_BROWSERS:
            exe = shutil.which(name) or (name if Path(name).exists() else None)
            if exe:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1200,820"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return False
    webbrowser.open(url)
    return False


def main() -> None:
    p = argparse.ArgumentParser(description="NUBB-E: your offline AI buddy.")
    p.add_argument("--no-brain", action="store_true", help="don't start llama-server")
    p.add_argument("--browser", action="store_true", help="open in a normal browser tab")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--no-window", action="store_true", help="don't open a window (just serve the page)")
    p.add_argument("--background", action="store_true",
                   help="start with no app window, just the quick bar hotkey")
    args = p.parse_args()

    init_db()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        sys.exit(f"Port {args.port} is busy. Is NUBB-E already open? (or change [app] port in config.toml)")
    server.daemon_threads = True

    BRAIN.can_start = not args.no_brain
    atexit.register(BRAIN.stop)   # always stop the brain we started, however we exit
    atexit.register(VOICE.stop)
    for sig in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), lambda *_: sys.exit(0))
    if llm_alive():
        print("Brain already running (started outside NUBB-E, so it won't be put to sleep).")
    elif BRAIN.can_start:
        with BRAIN.lock:
            BRAIN.start()
    threading.Thread(target=BRAIN.watch_idle, daemon=True).start()

    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"NUBB-E is at {url}  (chats and memory saved in {DATA_DIR})")

    try:
        if args.no_window or not open_window(url, args.browser, args.background):
            print("Press Ctrl+C here to shut NUBB-E down.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        VOICE.stop()
        BRAIN.stop()
        print("bye!")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NUBB-E</title>
<style>
  :root { --bg: #0b0f14; --panel: #10161d; --line: #1c2631; --glow: #6ff3ff; --text: #d9faff;
          --muted: #7d93a3; --you: #17303a; }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text);
               font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  body { display: grid; grid-template-columns: 250px 1fr; height: 100vh; overflow: hidden; }
  button { font: inherit; color: inherit; background: none; border: 0; cursor: pointer; }

  /* sidebar */
  aside { background: var(--panel); border-right: 1px solid var(--line); display: flex;
          flex-direction: column; min-height: 0; }
  .brand { padding: 16px 16px 8px; font-weight: 700; letter-spacing: .12em; color: var(--glow); }
  .side-btn { margin: 4px 12px; padding: 9px 12px; border-radius: 10px; text-align: left;
              border: 1px solid var(--line); }
  .side-btn:hover { border-color: var(--glow); }
  #chats { flex: 1; overflow-y: auto; padding: 8px; }
  .chat-item { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-radius: 8px;
               cursor: pointer; color: var(--muted); }
  .chat-item:hover, .chat-item.active { background: #16202a; color: var(--text); }
  .chat-item span { flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .chat-item button { opacity: 0; color: var(--muted); padding: 0 4px; }
  .chat-item:hover button { opacity: 1; }
  .side-foot { padding: 10px 14px; font-size: 12px; color: var(--muted); border-top: 1px solid var(--line); }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #e0a84a;
         margin-right: 6px; vertical-align: 1px; }
  .dot.on { background: #5fe39a; }

  /* main */
  main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  #faceBox { position: relative; height: 34vh; min-height: 180px; flex: none;
             border-bottom: 1px solid var(--line); }
  #faceBox canvas { position: absolute; inset: 0; }
  #faceStatus { position: absolute; top: 10px; left: 0; right: 0; text-align: center; color: var(--glow);
                opacity: .6; letter-spacing: .35em; text-transform: uppercase; font-size: 11px; }
  #msgs { flex: 1; overflow-y: auto; padding: 20px 24px; display: flex; flex-direction: column; gap: 12px; }
  .msg { max-width: 760px; padding: 10px 14px; border-radius: 14px; word-wrap: break-word; }
  .msg.user { align-self: flex-end; background: var(--you); }
  .msg.assistant { align-self: flex-start; background: var(--panel); border: 1px solid var(--line); }
  .msg.note { align-self: center; color: var(--muted); font-size: 13px; padding: 4px; }
  .msg .thinking { color: var(--muted); font-style: italic; }
  .tool-line { align-self: flex-start; color: var(--muted); font-size: 12px; padding: 0 6px; }
  .tool-line::before { content: "\2192  "; color: var(--glow); }
  .tool-card { align-self: flex-start; max-width: 80%; background: var(--panel); border: 1px solid var(--glow);
               border-radius: 12px; padding: 10px 14px; font-size: 14px; }
  .tool-card .target { color: var(--muted); }
  .tool-card pre { max-height: 220px; overflow: auto; background: var(--bg); padding: 8px 10px; border-radius: 8px;
                   white-space: pre-wrap; word-break: break-word; margin: 8px 0; }
  .tool-btns { display: flex; gap: 8px; align-items: center; }
  .tool-btns button { padding: 6px 14px; border-radius: 8px; border: 1px solid var(--line); color: var(--text); }
  .tool-btns .yes { border-color: var(--glow); color: var(--glow); }
  .tool-btns button:disabled { opacity: .4; cursor: default; }
  .tool-btns .answer { color: var(--muted); font-size: 13px; }
  .tool-card.done { border-color: var(--line); }
  .msg pre { background: #070a0e; padding: 10px; border-radius: 8px; overflow-x: auto; }
  .msg code { font-family: ui-monospace, Consolas, monospace; font-size: 13px; }
  .msg :not(pre) > code { background: #070a0e; padding: 1px 5px; border-radius: 4px; }
  .empty { margin: auto; color: var(--muted); text-align: center; }

  /* composer */
  form { display: flex; gap: 10px; padding: 14px 24px 18px; border-top: 1px solid var(--line); align-items: flex-end; }
  textarea { flex: 1; resize: none; max-height: 180px; padding: 11px 14px; border-radius: 12px;
             background: var(--panel); color: var(--text); border: 1px solid var(--line); font: inherit; }
  textarea:focus { outline: none; border-color: var(--glow); }
  .send { padding: 10px 18px; border-radius: 12px; background: var(--glow); color: #04222a; font-weight: 700; }
  .send:disabled { opacity: .4; cursor: default; }
  .mode { padding: 10px 12px; border-radius: 12px; border: 1px solid var(--line); color: var(--muted);
          white-space: nowrap; }
  .look { padding: 10px 14px; border-radius: 12px; border: 1px solid var(--glow); color: var(--glow);
          white-space: nowrap; }
  .look:disabled { opacity: .4; cursor: default; }
  .mic { padding: 10px 13px; white-space: nowrap; border-radius: 12px; border: 1px solid var(--line); color: var(--muted); }
  .mic.listening { border-color: #ff6b8b; color: #ff6b8b; box-shadow: 0 0 12px #ff6b8b55; }
  .mic.busy { border-color: var(--glow); color: var(--glow); }

  /* memory panel */
  #memPanel { position: fixed; inset: 0; background: #0009; display: none; align-items: center; justify-content: center; }
  #memPanel.open { display: flex; }
  .mem-card { width: min(560px, 92vw); max-height: 80vh; display: flex; flex-direction: column;
              background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 18px; gap: 10px; }
  .mem-card h2 { margin: 0; font-size: 17px; color: var(--glow); }
  .mem-card p { margin: 0; color: var(--muted); font-size: 13px; }
  #memList { overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
  .mem { display: flex; gap: 8px; align-items: center; padding: 8px 10px; border-radius: 8px; background: #16202a; }
  .mem span { flex: 1; }
  .mem small { color: var(--muted); font-size: 11px; }
  .mem button { color: var(--muted); }
  .mem button:hover { color: #ff8a8a; }
  .mem-add { display: flex; gap: 8px; }
  .mem-add input { flex: 1; padding: 8px 10px; border-radius: 8px; background: var(--bg); color: var(--text);
                   border: 1px solid var(--line); font: inherit; }
  .row { display: flex; justify-content: space-between; align-items: center; color: var(--muted); font-size: 13px; }
</style>
</head>
<body>
<aside>
  <div class="brand">NUBB-E</div>
  <button class="side-btn" id="newChat">+ New chat</button>
  <button class="side-btn" id="openMem">Memory</button>
  <div id="chats"></div>
  <div class="side-foot"><span class="dot" id="brainDot"></span><span id="brainText">brain loading...</span></div>
</aside>

<main>
  <div id="faceBox"><canvas id="c"></canvas><div id="faceStatus"></div></div>
  <div id="msgs"></div>
  <form id="composer">
    <button type="button" class="mode" id="modeBtn" title="Smart reasons before answering; Fast skips it">Smart</button>
    <textarea id="input" rows="1" placeholder="Talk to NUBB-E...  (/remember something to save it)"></textarea>
    <button type="button" class="mic" id="micBtn" title="Start a voice call: just talk, NUBB-E answers out loud. You can interrupt it.">&#128222; Call</button>
    <button type="button" class="look" id="lookBtn" title="NUBB-E takes a screenshot and answers your question about it">Look</button>
    <button class="send" id="sendBtn">Send</button>
  </form>
</main>

<div id="memPanel">
  <div class="mem-card">
    <h2>What NUBB-E remembers about you</h2>
    <p>Stored only on this PC. NUBB-E uses these in every chat. Delete anything you don't want it to know.</p>
    <div class="row"><label><input type="checkbox" id="autoMem"> Learn from chats automatically</label>
      <button id="closeMem">Close</button></div>
    <div class="mem-add"><input id="memInput" placeholder="Add something, e.g. I prefer Python over Java">
      <button class="send" id="memAdd">Add</button></div>
    <div id="memList"></div>
  </div>
</div>

<script src="/face.js"></script>
<script>
/* ===================== face (engine served from /face.js) ===================== */
const faceBox = document.getElementById('faceBox'), faceStatus = document.getElementById('faceStatus');
const FACE = NubbeFace(document.getElementById('c'), { box: faceBox });
const LABELS = { listening: 'listening', thinking: 'thinking', sleep: 'zzz' };
let face = 'sleep', happyTimer = null;
function setFace(s, ms) {
  if (FACE.isReaction(s)) { FACE.react(s); return; }      // reactions play on top, then fade back
  clearTimeout(happyTimer);
  face = s; faceStatus.textContent = LABELS[s] || ''; FACE.set(s);
  if (ms) happyTimer = setTimeout(() => setFace('idle'), ms);
}
// simple offline "mood reading" so the face reacts to what you say and what it replies
function reactToUser(text) {
  const t = text.toLowerCase();
  if (/\b(thanks|thank you|ty|love you|you'?re the best|awesome|amazing|let'?s go|yay|it works|it worked)\b/.test(t)) return 'excited';
  if (/\b(sad|tired|exhausted|stressed|bad day|ugh|i failed|depressed|lonely)\b/.test(t)) return 'sad';
  if (/\b(wow|whoa|no way|omg|wait what|wtf)\b/.test(t)) return 'surprised';
  if (/\b(error|bug|broken|crash(ed)?|won'?t|doesn'?t work|not working|fail(s|ed)?|stuck|confused)\b/.test(t)) return 'confused';
  if (t.trim().endsWith('?')) return 'curious';
  return null;
}
function reactToReply(text) {
  const t = text.toLowerCase();
  if (/(let'?s go|🎉|woo+|yay|awesome|nice!|great job|you got this|found it)/.test(t)) return 'excited';
  if (/\b(sorry|unfortunately|that sucks|rough)\b/.test(t)) return 'sad';
  if (/\b(i'?m not sure|i don'?t know|not certain)\b/.test(t)) return 'confused';
  if (/\*happy beep\*|😊|😄/.test(t)) return 'giggle';
  if (t.trim().endsWith('?')) return 'curious';
  return null;
}

/* ===================== app ===================== */
const $ = id => document.getElementById(id);
const msgs = $('msgs'), input = $('input'), sendBtn = $('sendBtn');
let chatId = null, busy = false, brainUp = false, settings = { think: true, auto_memory: true };

const api = (url, body) => fetch(url, body === undefined ? {} :
  { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json());

function esc(s) { return s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function md(src) {   // tiny offline markdown: code blocks, inline code, bold, line breaks
  return src.split('```').map((part, i) => {
    if (i % 2) { const nl = part.indexOf('\n'); return '<pre><code>' + esc((nl >= 0 ? part.slice(nl + 1) : part).replace(/\n$/, '')) + '</code></pre>'; }
    return esc(part).replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>').replace(/\n/g, '<br>');
  }).join('');
}
function bubble(role, text) {
  msgs.querySelector('.empty')?.remove();
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  if (role === 'assistant') d.innerHTML = md(text); else d.textContent = text;
  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
  return d;
}
function showEmpty() { msgs.innerHTML = '<div class="empty">Hi Erfan! What are we building today?</div>'; }

async function loadChats() {
  const list = await api('/api/chats');
  const box = $('chats'); box.innerHTML = '';
  for (const c of list) {
    const el = document.createElement('div');
    el.className = 'chat-item' + (c.id === chatId ? ' active' : '');
    el.innerHTML = `<span>${esc(c.title)}</span><button title="Rename">&#9998;</button><button title="Delete">&times;</button>`;
    el.onclick = () => openChat(c.id);
    const [ren, del] = el.querySelectorAll('button');
    ren.onclick = async e => { e.stopPropagation(); const n = prompt('Rename chat', c.title); if (n) { await api(`/api/chats/${c.id}/rename`, { title: n }); loadChats(); } };
    del.onclick = async e => { e.stopPropagation(); if (confirm('Delete this chat?')) { await api(`/api/chats/${c.id}/delete`, {}); if (chatId === c.id) newChat(); loadChats(); } };
    box.appendChild(el);
  }
}
async function openChat(id) {
  if (busy) return;
  if (chatId !== id) api('/api/current', { chat_id: id });   // the quick bar follows the open chat
  chatId = id; msgs.innerHTML = '';
  const history = await api('/api/chats/' + id);
  history.forEach(m => bubble(m.role, m.content));
  if (!history.length) showEmpty();
  loadChats(); input.focus();
}
function newChat() { if (busy) return; chatId = null; api('/api/current', { chat_id: null }); showEmpty(); loadChats(); setFace('happy', 1200); input.focus(); }

async function send(text, look = false) {
  if (!look && text.startsWith('/remember ')) {
    const fact = text.slice(10).trim();
    if (fact) { await api('/api/memory', { text: fact }); bubble('note', 'Saved to memory: ' + fact); setFace('wink'); }
    return;
  }
  busy = true; sendBtn.disabled = true; $('lookBtn').disabled = true;
  bubble('user', look ? '[screenshot] ' + text : text);
  const out = bubble('assistant', ''); out.innerHTML = '<span class="thinking">' + (look ? 'looking...' : '...') + '</span>';
  setFace('thinking');
  const userMood = look ? 'lookaround' : reactToUser(text);
  if (userMood) setFace(userMood);
  let reply = '', pending = false;
  const render = () => { pending = false; out.innerHTML = md(reply); msgs.scrollTop = msgs.scrollHeight; };
  try {
    const res = await fetch(look ? '/api/look' : '/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                           body: JSON.stringify({ chat_id: chatId, message: text }) });
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
        if (ev.type === 'start') { if (chatId !== ev.chat_id) { chatId = ev.chat_id; loadChats(); } }
        else if (ev.type === 'waking') { setFace('sleep'); faceStatus.textContent = 'waking up'; out.innerHTML = '<span class="thinking">waking up, one sec...</span>'; }
        else if (ev.type === 'reasoning') { setFace('thinking'); out.innerHTML = '<span class="thinking">thinking...</span>'; }
        else if (ev.type === 'delta') { reply += ev.text; setFace('talking'); if (!pending) { pending = true; requestAnimationFrame(render); } }
        else if (ev.type === 'error') { out.innerHTML = '<span class="thinking">' + esc(ev.text) + '</span>'; }
        else if (ev.type === 'tool') { toolLine(out, ev.text); setFace('thinking'); if (!reply) out.innerHTML = '<span class="thinking">...</span>'; }
        else if (ev.type === 'confirm') { confirmCard(out, ev); setFace('curious'); }
        else if (ev.type === 'confirmed') settleCard(ev.id, ev.ok);
      }
    }
    if (reply) render();   // keeps an error message visible when there's no reply
  } catch (e) {
    out.innerHTML = '<span class="thinking">Lost connection to NUBB-E.</span>';
  }
  setFace('idle');
  const replyMood = reactToReply(reply);
  if (replyMood) setFace(replyMood);
  busy = false; sendBtn.disabled = false; $('lookBtn').disabled = false; loadChats(); input.focus();
}

// file tools: a small line for each step, and a Yes/No card before anything changes (No is the default)
function toolLine(out, text) {
  const el = document.createElement('div'); el.className = 'tool-line'; el.textContent = text;
  msgs.insertBefore(el, out); msgs.scrollTop = msgs.scrollHeight;
}
function confirmCard(out, ev) {
  const el = document.createElement('div'); el.className = 'tool-card'; el.id = 'card-' + ev.id;
  el.innerHTML = '<b></b> <span class="target"></span><pre></pre>'
    + '<div class="tool-btns"><button class="no">No</button><button class="yes">Yes, do it</button><span class="answer"></span></div>';
  el.querySelector('b').textContent = ev.title + ':';
  el.querySelector('.target').textContent = ev.target;
  el.querySelector('pre').textContent = ev.detail;
  const answer = ok => { api('/api/confirm', { id: ev.id, ok }); settleCard(ev.id, ok); };
  el.querySelector('.no').onclick = () => answer(false);
  el.querySelector('.yes').onclick = () => answer(true);
  msgs.insertBefore(el, out); msgs.scrollTop = msgs.scrollHeight;
  el.querySelector('.no').focus();
}
function settleCard(id, ok) {
  const el = $('card-' + id); if (!el || el.classList.contains('done')) return;
  el.classList.add('done'); el.querySelectorAll('button').forEach(b => b.disabled = true);
  el.querySelector('.answer').textContent = ok ? 'You said Yes.' : 'Not done.';
  input.focus();
}

// call button: a hands-free voice call. The voice helper reports listening / thinking / talking
// through the face events; the button shows whether a call is on.
let inCall = false;
function showCall(on) {
  inCall = on;
  const b = $('micBtn');
  b.classList.toggle('listening', on);
  b.innerHTML = on ? '&#128222; End call' : '&#128222; Call';
  b.title = on ? 'Hang up' : 'Start a voice call: just talk, NUBB-E answers out loud. You can interrupt it.';
}
function showMic(state, status) {
  if (state === 'idle' && status === 'Call ended') showCall(false);
  if (status === "Can't open the mic.") { showCall(false); faceStatus.textContent = "can't open the mic"; }
}
$('micBtn').onclick = async () => {
  const r = await api('/api/voice', { action: inCall ? 'hangup' : 'call' });
  showCall(r.in_call);
  if (r.voice === 'starting') { setFace('thinking'); faceStatus.textContent = 'connecting...'; }
};

$('lookBtn').onclick = () => {   // the question box is optional: empty means "what's on my screen?"
  if (busy) return;
  const text = input.value.trim() || "What's on my screen?";
  input.value = ''; input.style.height = '';
  send(text, true);
};

$('composer').onsubmit = e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = ''; input.style.height = '';
  send(text);
};
input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('composer').requestSubmit(); } });
input.addEventListener('input', () => { input.style.height = ''; input.style.height = Math.min(input.scrollHeight, 180) + 'px'; });
input.addEventListener('input', () => {   // eyes watch what you type, following along the line
  const r = input.getBoundingClientRect(), line = input.value.split('\n').pop();
  FACE.lookAt(r.left + 16 + Math.min(r.width - 32, line.length * 7.5), r.top + r.height / 2, 1500);
});
$('newChat').onclick = newChat;

// smart / fast mode
function showMode() { $('modeBtn').textContent = settings.think ? 'Smart' : 'Fast'; }
$('modeBtn').onclick = async () => { settings = await api('/api/settings', { think: !settings.think }); showMode(); };

// memory panel
async function loadMemory() {
  const list = await api('/api/memory'), box = $('memList');
  box.innerHTML = list.length ? '' : '<p>Nothing yet. Tell NUBB-E about yourself, or add something above.</p>';
  for (const m of list) {
    const el = document.createElement('div'); el.className = 'mem';
    el.innerHTML = `<span>${esc(m.text)}</span><small>${m.source === 'auto' ? 'learned' : 'added'}</small><button title="Forget">&times;</button>`;
    el.querySelector('button').onclick = async () => { await api(`/api/memory/${m.id}/delete`, {}); loadMemory(); };
    box.appendChild(el);
  }
}
$('openMem').onclick = () => { $('memPanel').classList.add('open'); $('autoMem').checked = settings.auto_memory; loadMemory(); };
$('closeMem').onclick = () => $('memPanel').classList.remove('open');
$('memPanel').onclick = e => { if (e.target.id === 'memPanel') $('memPanel').classList.remove('open'); };
$('autoMem').onchange = async e => { settings = await api('/api/settings', { auto_memory: e.target.checked }); };
async function addMem() { const v = $('memInput').value.trim(); if (v) { await api('/api/memory', { text: v }); $('memInput').value = ''; loadMemory(); } }
$('memAdd').onclick = addMem;
$('memInput').addEventListener('keydown', e => { if (e.key === 'Enter') addMem(); });

// brain status: face sleeps until the model is loaded, and while it naps after idle time
const BRAIN_TEXT = { online: 'brain online (offline mode)', loading: 'brain loading...',
                     asleep: 'brain asleep, wakes when you write', offline: 'brain offline' };
async function checkBrain() {
  let up = false, state = 'offline';
  try { ({ brain: up, state } = await api('/api/health')); } catch (e) { state = 'offline'; }
  $('brainDot').classList.toggle('on', up);
  $('brainText').textContent = BRAIN_TEXT[state] || BRAIN_TEXT.offline;
  if (up && !brainUp && !busy) { setFace('idle'); setFace('excited'); }
  if (!up && !busy) { setFace('sleep'); if (state === 'loading') faceStatus.textContent = 'waking up'; }
  brainUp = up;
}

// face updates from other scripts (voice, look) via set_face()
const es = new EventSource('/events');
es.onmessage = e => {
  const d = JSON.parse(e.data);
  if (d.event === 'memory' && $('memPanel').classList.contains('open')) loadMemory();
  // a reply finished somewhere (here, the quick bar, or a call): show it if it's the chat we're on,
  // or switch to it when the quick bar continued the current chat
  if (d.event === 'chat' && !busy) { if (chatId === d.chat_id || d.current) openChat(d.chat_id); else loadChats(); }
  if (d.event === 'call') showCall(d.in_call);
  if (d.state && !busy) { setFace(d.state); if (d.status) faceStatus.textContent = d.status; }
  if (d.state) showMic(d.state, d.status);
};

(async () => {
  settings = await api('/api/settings'); showMode();
  showCall((await api('/api/voice')).in_call);
  showEmpty(); loadChats(); checkBrain(); setInterval(checkBrain, 3000); input.focus();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
