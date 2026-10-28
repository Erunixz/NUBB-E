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

Everything is stored on your PC in ~/.nubbe/nubbe.db. Nothing goes online.
Standard library only. Optional: `pip install pywebview` for a real app
window instead of a browser window.

Flags:
  --no-brain    don't start llama-server (you run it yourself)
  --browser     open in your normal browser instead of an app window
"""
import argparse
import json
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Edit these to match your setup (paths to llama-server and your model files)
# ---------------------------------------------------------------------------
LLAMA_SERVER = "llama-server"   # or a full path, e.g. r"C:\ai\llama.cpp\build\bin\Release\llama-server.exe"
LLAMA_ARGS = [
    "-m", "models/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf",
    "--mmproj", "models/mmproj.gguf",
    "-ngl", "99", "--n-cpu-moe", "28",
    "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0",
    "-c", "32768", "--jinja", "--port", "8080",
]
LLM_URL = "http://127.0.0.1:8080"
PORT = 8765                      # same port as nubbe_face.py, so the voice script drives this face too
HISTORY_TURNS = 40               # how many recent messages are sent to the model

HERE = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".nubbe"
DB_PATH = DATA_DIR / "nubbe.db"
PROMPT_PATH = HERE / "nubbe_system_prompt.txt"
from nubbe_face import FACE_JS, FACE_STATES   # shared face engine (keep nubbe_face.py next to this file)

VOICE_RULES = (
    "\n\n## Voice mode\n"
    "You are speaking out loud right now. Reply in ONE short sentence, like a quick spoken reply. "
    "Only go longer if Erfan explicitly asks for detail. "
    "No markdown, no lists, no code blocks, no emoji: everything you write is read aloud."
)

EXTRACT_PROMPT = (
    "You maintain a small memory about the user, Erfan. From the exchange below, list NEW lasting "
    "facts the user stated about himself: preferences, projects, goals, setup, people in his life, "
    "how he likes things done. Only facts the USER clearly said, never guesses or things the assistant said. "
    "Skip temporary things (today's mood, a one-off question). Skip anything already known. "
    "Never save passwords, ID numbers, card or bank numbers. "
    "Write each fact as a short sentence, one per line, starting with '- '. "
    "If there is nothing new worth remembering, reply with exactly NONE."
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
DEFAULT_SETTINGS = {"auto_memory": "1", "think": "1"}


def connect() -> sqlite3.Connection:
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
    with closing(connect()) as con, con:
        con.executescript(SCHEMA)
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


def start_llm():
    if llm_alive():
        print("Brain already running.")
        return None
    exe = shutil.which(LLAMA_SERVER) or (LLAMA_SERVER if Path(LLAMA_SERVER).exists() else None)
    if not exe:
        print(f"Couldn't find llama-server ('{LLAMA_SERVER}'). Set LLAMA_SERVER at the top of "
              "nubbe_app.py, or start it yourself and use --no-brain.")
        return None
    log = open(DATA_DIR / "llama-server.log", "w")
    print("Starting the brain (first load can take a minute)...")
    return subprocess.Popen([exe, *LLAMA_ARGS], stdout=log, stderr=subprocess.STDOUT)


def system_prompt() -> str:
    if PROMPT_PATH.exists():
        base = PROMPT_PATH.read_text(encoding="utf-8")
    else:
        base = "You are NUBB-E, Erfan's friendly, optimistic and very nerdy offline AI buddy."
    mems = rows("SELECT text FROM memories ORDER BY id")
    if mems:
        base += ("\n\n## What you remember about Erfan\n"
                 + "\n".join(f"- {m['text']}" for m in mems)
                 + "\nUse these naturally when they're relevant. Don't list them back unprompted.")
    return base


def llm_stream(messages: list, think: bool, max_tokens=None, temperature=None):
    """Yields ('reasoning', '') while the model thinks and ('content', text) as it answers."""
    payload = {"model": "local", "stream": True, "messages": messages}
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
            out = "".join(v for k, v in llm_stream(msgs, think=False, max_tokens=150, temperature=0.2)
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
        path = self.path.split("?")[0]
        if path == "/face.js":
            body = FACE_JS.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/events":
            self.events()
        elif path == "/api/health":
            self.send_json({"brain": llm_alive()})
        elif path == "/api/chats":
            self.send_json(rows("SELECT id, title, updated FROM chats ORDER BY updated DESC"))
        elif m := re.fullmatch(r"/api/chats/(\d+)", path):
            self.send_json(rows("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id", (int(m[1]),)))
        elif path == "/api/memory":
            self.send_json(rows("SELECT id, text, source FROM memories ORDER BY id DESC"))
        elif path == "/api/settings":
            self.send_json(get_settings())
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self.read_json()
        except json.JSONDecodeError:
            self.send_error(400)
            return
        if path == "/api/chat":
            self.chat(body)
        elif path == "/state":  # set_face() from other scripts
            if body.get("state") not in FACE_STATES:
                self.send_error(400)
                return
            broadcast(body)
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

    def chat(self, body: dict):
        text = str(body.get("message", "")).strip()
        if not text:
            self.send_error(400)
            return
        voice = bool(body.get("voice"))      # from nubbe_voice.py: short spoken replies
        now = time.time()
        chat_id = body.get("chat_id")
        if not chat_id or not rows("SELECT id FROM chats WHERE id = ?", (chat_id,)):
            title = ("Voice: " if voice else "") + title_from(text)
            chat_id = run("INSERT INTO chats(title, created, updated) VALUES (?, ?, ?)", (title, now, now))
        run("INSERT INTO messages(chat_id, role, content, created) VALUES (?, 'user', ?, ?)", (chat_id, text, now))
        history = rows("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))[-HISTORY_TURNS:]
        messages = [{"role": "system", "content": system_prompt() + (VOICE_RULES if voice else "")}] + history
        settings = get_settings()
        think = False if voice else settings["think"]

        self.start_stream()
        reply, told_reasoning = "", False
        try:
            self.emit({"type": "start", "chat_id": chat_id})
            for kind, val in llm_stream(messages, think=think, max_tokens=80 if voice else None):
                if kind == "reasoning":
                    if not told_reasoning:
                        self.emit({"type": "reasoning"})
                        told_reasoning = True
                else:
                    reply += val
                    self.emit({"type": "delta", "text": val})
            self.emit({"type": "done"})
        except urllib.error.URLError:
            self.safe_emit({"type": "error", "text": "Can't reach my brain. Is llama-server still loading?"})
        except OSError:
            pass  # window closed mid-reply; still save what we have
        finally:
            if reply:
                run("INSERT INTO messages(chat_id, role, content, created) VALUES (?, 'assistant', ?, ?)",
                    (chat_id, reply, time.time()))
                run("UPDATE chats SET updated = ? WHERE id = ?", (time.time(), chat_id))
                if voice:
                    broadcast({"event": "chat", "chat_id": chat_id})   # window shows the voice chat live
                if settings["auto_memory"]:
                    threading.Thread(target=extract_memories, args=(text, reply), daemon=True).start()

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


def open_window(url: str, force_browser: bool) -> bool:
    """Returns True if the window blocks until it's closed (pywebview)."""
    if not force_browser:
        try:
            import webview  # optional: pip install pywebview
            webview.create_window("NUBB-E", url, width=1200, height=820, background_color="#0b0f14")
            webview.start()
            return True
        except ImportError:
            pass
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
    args = p.parse_args()

    init_db()
    brain = None if args.no_brain else start_llm()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"NUBB-E is at {url}  (chats and memory saved in {DATA_DIR})")

    try:
        if not open_window(url, args.browser):
            print("Press Ctrl+C here to shut NUBB-E down.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        if brain:
            brain.terminate()
        print("bye! *happy beep*")


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
  chatId = id; msgs.innerHTML = '';
  const history = await api('/api/chats/' + id);
  history.forEach(m => bubble(m.role, m.content));
  if (!history.length) showEmpty();
  loadChats(); input.focus();
}
function newChat() { if (busy) return; chatId = null; showEmpty(); loadChats(); setFace('happy', 1200); input.focus(); }

async function send(text) {
  if (text.startsWith('/remember ')) {
    const fact = text.slice(10).trim();
    if (fact) { await api('/api/memory', { text: fact }); bubble('note', 'Saved to memory: ' + fact); setFace('wink'); }
    return;
  }
  busy = true; sendBtn.disabled = true;
  bubble('user', text);
  const out = bubble('assistant', ''); out.innerHTML = '<span class="thinking">...</span>';
  setFace('thinking');
  const userMood = reactToUser(text);
  if (userMood) setFace(userMood);
  let reply = '', pending = false;
  const render = () => { pending = false; out.innerHTML = md(reply); msgs.scrollTop = msgs.scrollHeight; };
  try {
    const res = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
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
        else if (ev.type === 'reasoning') out.innerHTML = '<span class="thinking">thinking...</span>';
        else if (ev.type === 'delta') { reply += ev.text; setFace('talking'); if (!pending) { pending = true; requestAnimationFrame(render); } }
        else if (ev.type === 'error') { out.innerHTML = '<span class="thinking">' + esc(ev.text) + '</span>'; }
      }
    }
    render();
  } catch (e) {
    out.innerHTML = '<span class="thinking">Lost connection to NUBB-E.</span>';
  }
  setFace('idle');
  const replyMood = reactToReply(reply);
  if (replyMood) setFace(replyMood);
  busy = false; sendBtn.disabled = false; loadChats(); input.focus();
}

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

// brain status: face sleeps until the model is loaded
async function checkBrain() {
  let up = false;
  try { up = (await api('/api/health')).brain; } catch (e) {}
  $('brainDot').classList.toggle('on', up);
  $('brainText').textContent = up ? 'brain online (offline mode)' : 'brain loading...';
  if (up && !brainUp) { setFace('idle'); setFace('excited'); }
  if (!up && !busy) setFace('sleep');
  brainUp = up;
}

// face updates from other scripts (voice, look) via set_face()
const es = new EventSource('/events');
es.onmessage = e => {
  const d = JSON.parse(e.data);
  if (d.event === 'memory' && $('memPanel').classList.contains('open')) loadMemory();
  if (d.event === 'chat' && !busy) { if (chatId === d.chat_id) openChat(d.chat_id); else loadChats(); }
  if (d.state && !busy) setFace(d.state);
};

(async () => {
  settings = await api('/api/settings'); showMode();
  showEmpty(); loadChats(); checkBrain(); setInterval(checkBrain, 3000); input.focus();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
