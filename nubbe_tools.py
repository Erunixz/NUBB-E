"""NUBB-E's file tools: they only work inside workspace_dir (config.toml): one folder, or "*".

    import nubbe_tools as tools
    tools.SCHEMAS                       # OpenAI-style tool list for llama-server (--jinja)
    tools.needs_yes("write_file")       # True: the app must show a Yes/No card first
    tools.card("write_file", args)      # what that card says
    tools.call("read_file", {"path": "notes.md"}, log_file)   # -> text for the model

workspace_dir = "*" means the whole laptop: every drive can be read, but
keys, saved passwords and browser data are always refused.

Read-only tools: list_dir, read_file, search_files.
Tools that change things: write_file, run_command. The app never runs these
without Erfan pressing Yes. Every call is logged to ~/.nubbe/tools.log
(the app passes the path). Standard library only.
"""
import functools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from nubbe_config import CFG

WORKSPACE_DIR = CFG["tools"]["workspace_dir"]   # "" turns the tools off
MAX_READ_CHARS = 40_000        # about 10k tokens, so a big file can't fill the whole context
MAX_WRITE_CHARS = 200_000
MAX_SEARCH_FILE = 1_000_000    # skip files bigger than this when searching
MAX_OUTPUT_CHARS = 8_000       # command output sent back to the model
COMMAND_TIMEOUT = 60
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}

READ_ONLY = {"list_dir", "read_file", "search_files"}
ASK_FIRST = {"write_file", "run_command"}


class ToolError(Exception):
    """A friendly message for the model, like 'that file doesn't exist'."""


def _tool(name, description, props, required):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": props, "required": required}}}


_PATH = {"type": "string", "description": "A full path like 'C:/Users/me/notes.md' or '~/Documents/notes.md', "
         "or a path relative to the starting folder, like 'notes/ideas.md'. '.' is the starting folder itself."}
SCHEMAS = [
    _tool("list_dir", "List the files and folders in a folder on Erfan's computer.", {"path": _PATH}, []),
    _tool("read_file", "Read a text file from Erfan's computer.", {"path": _PATH}, ["path"]),
    _tool("search_files", "Search the text files in a folder for keywords (case doesn't matter). "
          "Returns matching files with line numbers. Use it to find notes about a topic.",
          {"query": {"type": "string", "description": "One or a few keywords, like 'robot arm'."},
           "path": {**_PATH, "description": "Folder to search in (default: the starting folder)."}}, ["query"]),
    _tool("write_file", "Create or overwrite a text file. Erfan sees the exact change "
          "and must press Yes first.",
          {"path": _PATH, "content": {"type": "string", "description": "The full new text of the file."}},
          ["path", "content"]),
    _tool("run_command", "Run a shell command in the starting folder"
          f" ({'Windows cmd' if sys.platform == 'win32' else 'sh'}). Erfan sees the exact command and must "
          f"press Yes first. Stops after {COMMAND_TIMEOUT} seconds.",
          {"command": {"type": "string", "description": "The exact command line."}}, ["command"]),
]

FOLDER_RULES = (
    "\n\n## Your file tools\n"
    "You can use tools on Erfan's workspace folder ({root}): list_dir, read_file and search_files to look, "
    "write_file and run_command to change things. Only that folder works. "
)
LAPTOP_RULES = (
    "\n\n## Your file tools\n"
    "You can look at any file on Erfan's laptop with list_dir, read_file and search_files, no need to ask. "
    "Use full paths like 'C:/Users/...' or '~/Documents/...'; relative paths start in his home folder ({root}). "
    "Searching is much faster in one folder (like ~/Documents or ~/Desktop) than the whole home folder. "
    "write_file and run_command change things. "
)
RULES_END = (
    "When he asks about his notes or files, search or read them first, then answer from what's really there "
    "and say which file it came from. Never guess what a file says. If a tool refuses a path, just tell him "
    "why, without describing what that file probably contains. "
    "For write_file and run_command, the app shows Erfan a Yes/No card with the exact action, so just call "
    "the tool instead of asking in text. If he says No, don't try again unless he asks."
)

EVERYWHERE = "*"   # workspace_dir = "*": the whole laptop (every drive); reading is free, changes need Yes
# Never opened, even in "*" mode: keys, saved passwords, browser data, Windows credentials, NUBB-E's own data.
# Reading one would put a secret into a saved chat.
SECRET_DIRS = [".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".nubbe",
               "AppData/Roaming/Microsoft/Credentials", "AppData/Local/Microsoft/Credentials",
               "AppData/Roaming/Microsoft/Protect", "AppData/Roaming/Microsoft/Crypto",
               "AppData/Local/Google/Chrome/User Data", "AppData/Local/Microsoft/Edge/User Data",
               "AppData/Local/BraveSoftware", "AppData/Roaming/Mozilla/Firefox/Profiles",
               "AppData/Roaming/Opera Software", "AppData/Roaming/Bitwarden", "AppData/Roaming/1Password",
               "AppData/Local/1Password"]
SECRET_NAMES = {".env", ".netrc", ".git-credentials", ".pgpass", ".npmrc", ".pypirc", "id_rsa", "id_ed25519",
                "id_ecdsa", "login data", "cookies", "web data", "key4.db", "logins.json", "sam", "security"}
SECRET_SUFFIXES = (".kdbx", ".pem", ".key", ".pfx", ".p12", ".ppk", ".keychain")
# big system folders a search skips on the way down (you can still search inside them directly)
SEARCH_SKIP = {"windows", "program files", "program files (x86)", "programdata", "appdata", "$recycle.bin",
               "system volume information", "recovery", "$windows.~ws", "$winreagent", "msocache"}
BINARY_EXT = {".exe", ".dll", ".sys", ".bin", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".mp3",
              ".wav", ".mp4", ".mkv", ".mov", ".avi", ".zip", ".7z", ".rar", ".gz", ".tar", ".iso", ".gguf",
              ".safetensors", ".onnx", ".pt", ".pdf", ".docx", ".xlsx", ".pptx", ".db", ".sqlite", ".pyc"}
SEARCH_SECONDS = 15
MAX_SCAN = 50_000


def rules() -> str:
    return (LAPTOP_RULES if WORKSPACE_DIR == EVERYWHERE else FOLDER_RULES).format(root=root()) + RULES_END


def needs_yes(name: str) -> bool:
    return name not in READ_ONLY


def roots() -> list[Path]:
    """Every folder the tools may use: one workspace folder, or every drive in "*" mode."""
    if not WORKSPACE_DIR:
        return []
    if WORKSPACE_DIR == EVERYWHERE:
        drives = os.listdrives() if hasattr(os, "listdrives") else ["/"]
        return [Path(d) for d in drives if Path(d).is_dir()]
    p = Path(os.path.expanduser(WORKSPACE_DIR))
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / p
    p = p.resolve()
    return [p] if p.is_dir() else []


def root() -> Path | None:
    """Where relative paths start (the home folder in "*" mode), or None when the tools are off."""
    if not roots():
        return None
    return Path.home().resolve() if WORKSPACE_DIR == EVERYWHERE else roots()[0]


def _under(p, folders) -> bool:
    """Plain string checks: pathlib's is_relative_to is far too slow for a search over thousands of files."""
    s = os.path.normcase(str(p))
    return any(s == f or s.startswith(f.rstrip(os.sep) + os.sep) for f in folders)


@functools.lru_cache(maxsize=4)
def _secret_dirs(home: str) -> tuple:
    return tuple(os.path.normcase(os.path.join(home, d.replace("/", os.sep))) for d in SECRET_DIRS)


def secret(p: Path) -> bool:
    if p.name.lower() in SECRET_NAMES or p.suffix.lower() in SECRET_SUFFIXES:
        return True
    return _under(p, _secret_dirs(os.path.expanduser("~")))


def allowed(p: Path) -> bool:
    return _under(p, [os.path.normcase(str(r)) for r in roots()]) and not secret(p)


def inside(rel) -> Path:
    """Resolves a path the model gave (following symlinks and '..') and refuses anything outside the
    workspace, or private files like keys and saved passwords."""
    base = root()
    if base is None:
        raise ToolError("No workspace folder is set up (workspace_dir in config.toml).")
    rel = os.path.expanduser(str(rel or ".").strip() or ".")
    target = (base / rel).resolve()     # an absolute path replaces base, then gets checked below
    if not any(target.is_relative_to(r) for r in roots()):
        raise ToolError(f"'{rel}' is outside the workspace folder, so I can't touch it.")
    if secret(target):
        raise ToolError(f"'{rel}' is private (keys, passwords or browser data), so I don't open it.")
    return target


def show(p: Path) -> str:
    """A path as Erfan would write it: relative to the workspace, or ~/... in "*" mode."""
    base = root()
    if not p.is_relative_to(base):
        return p.as_posix()
    rel = p.relative_to(base).as_posix()
    if WORKSPACE_DIR == EVERYWHERE:
        return "~" if rel == "." else "~/" + rel
    return rel if rel != "." else "(workspace)"


# OneDrive-style "online-only" files: opening one makes Windows download it (slow, and it goes online)
CLOUD_ONLY = 0x1000 | 0x40000 | 0x400000   # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS


def cloud_only(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & CLOUD_ONLY)


def _text(p: Path, limit: int) -> str | None:
    """File text, or None if it looks binary."""
    with open(p, "rb") as f:
        data = f.read(limit * 4 + 1)
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------
def list_dir(path=".") -> str:
    d = inside(path)
    if not d.is_dir():
        raise ToolError(f"'{path}' isn't a folder.")
    items = []
    for e in sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        try:
            if not allowed(e.resolve()):   # skip links that point outside, and private files
                continue
            items.append(e.name + "/" if e.is_dir() else f"{e.name}  ({e.stat().st_size:,} bytes)")
        except OSError:
            continue
    if not items:
        return f"{show(d)} is empty."
    more = f"\n... and {len(items) - 200} more" if len(items) > 200 else ""
    return f"{show(d)}:\n" + "\n".join(items[:200]) + more


def read_file(path) -> str:
    p = inside(path)
    if not p.is_file():
        raise ToolError(f"'{path}' doesn't exist." if not p.exists() else f"'{path}' is a folder, not a file.")
    if cloud_only(p.stat()):
        raise ToolError(f"'{path}' is an online-only cloud file, so it isn't on this laptop yet. "
                        "Open it once (or pick 'Always keep on this device') and I can read it.")
    text = _text(p, MAX_READ_CHARS)
    if text is None:
        raise ToolError(f"'{path}' isn't a text file, so I can't read it.")
    if len(text) > MAX_READ_CHARS:
        return text[:MAX_READ_CHARS] + f"\n\n[cut off here: the file is longer than {MAX_READ_CHARS:,} characters]"
    return text


def search_files(query, path=".") -> str:
    words = [w for w in str(query).lower().split() if w]
    if not words:
        raise ToolError("Give me a word to search for.")
    start = inside(path)
    if not start.is_dir():
        raise ToolError(f"'{path}' isn't a folder.")
    found, scanned, stopped = [], 0, False
    deadline = time.time() + SEARCH_SECONDS
    for folder, dirs, files in os.walk(start):
        here = Path(folder)
        if not allowed(here.resolve()):   # a link or junction out of the workspace
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                         and d.lower() not in SEARCH_SKIP and not secret(here / d))
        for name in sorted(files):
            scanned += 1
            if scanned > MAX_SCAN or time.time() > deadline:
                stopped = True
                break
            p = here / name
            if secret(p):
                continue
            text = None
            if p.suffix.lower() not in BINARY_EXT:
                try:
                    if p.is_symlink() and not allowed(p.resolve()):
                        continue
                    st = p.stat()
                    if st.st_size <= MAX_SEARCH_FILE and not cloud_only(st):   # cloud files: name match only
                        text = _text(p, MAX_SEARCH_FILE)
                except OSError:
                    continue
            hits = [w for w in words if w in name.lower()]
            lines = []
            if text:
                low = text.lower()
                hits += [w for w in words if w in low and w not in hits]
                for n, line in enumerate(text.splitlines(), 1):
                    if any(w in line.lower() for w in words):
                        lines.append(f"  {n}: {line.strip()[:200]}")
                        if len(lines) == 3:
                            break
            if hits:
                found.append((len(set(hits)), show(p), lines))
        if stopped:
            break
    note = (f"\n\n[stopped early after {scanned - 1:,} files: search a smaller folder to look further]"
            if stopped else "")
    if not found:
        return f"No files in {show(start)} mention {' or '.join(repr(w) for w in words)}." + note
    found.sort(key=lambda f: -f[0])
    out = [f"{name}\n" + "\n".join(lines) if lines else f"{name}  (name matches)" for _, name, lines in found[:20]]
    more = f"\n\n... and {len(found) - 20} more files" if len(found) > 20 else ""
    return "\n\n".join(out) + more + note


def write_file(path, content) -> str:
    p = inside(path)
    if p.is_dir():
        raise ToolError(f"'{path}' is a folder.")
    content = str(content)
    if len(content) > MAX_WRITE_CHARS:
        raise ToolError("That's too much text for one file write.")
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content, encoding="utf-8")
    return f"{'Overwrote' if existed else 'Created'} {show(p)} ({len(content):,} characters)."


def run_command(command) -> str:
    command = str(command).strip()
    if not command:
        raise ToolError("The command is empty.")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        r = subprocess.run(command, shell=True, cwd=inside("."), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=COMMAND_TIMEOUT, creationflags=flags)
    except subprocess.TimeoutExpired:
        return f"The command was still running after {COMMAND_TIMEOUT} seconds, so I stopped it."
    out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "\n[output cut off]"
    return f"Exit code {r.returncode}.\n{out or '(no output)'}"


TOOLS = {"list_dir": list_dir, "read_file": read_file, "search_files": search_files,
         "write_file": write_file, "run_command": run_command}


# ---------------------------------------------------------------------------
# for the app: confirmation cards, status lines, logging, running a call
# ---------------------------------------------------------------------------
def card(name: str, args: dict) -> dict:
    """What the Yes/No card shows: a title, and the exact action."""
    if name == "write_file":
        try:
            p = inside(args.get("path"))
            where = show(p) + (" (replaces the current file)" if p.exists() else " (new file)")
        except ToolError as e:
            where = str(e)
        return {"title": "Write a file", "target": where, "detail": str(args.get("content", ""))}
    if name == "run_command":
        return {"title": "Run a command", "target": f"in {root() or 'the workspace'}",
                "detail": str(args.get("command", ""))}
    return {"title": name, "target": "", "detail": json.dumps(args)}


def status(name: str, args: dict) -> str:
    """A short line for the chat and the face while a tool runs."""
    return {"list_dir": f"looking in {args.get('path') or 'the workspace'}",
            "read_file": f"reading {args.get('path', '')}",
            "search_files": f"searching for '{args.get('query', '')}'",
            "write_file": f"writing {args.get('path', '')}",
            "run_command": f"running: {args.get('command', '')}"}.get(name, name)


def log(log_file: Path, name: str, args: dict, outcome: str) -> None:
    """One JSON line per tool call. File contents aren't copied into the log, only their size."""
    safe = {k: (f"<{len(str(v)):,} characters>" if k == "content" else v) for k, v in args.items()}
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": name, "args": safe,
                                "outcome": outcome}) + "\n")
    except OSError:
        pass


def call(name: str, args: dict, log_file: Path, approved: bool | None = None) -> str:
    """Runs one tool and returns the text for the model. Tools that change things need approved=True."""
    if name not in TOOLS:
        log(log_file, name, args, "unknown tool")
        return f"There's no tool called {name}."
    if needs_yes(name) and not approved:
        log(log_file, name, args, "declined" if approved is False else "not asked")
        return "Erfan said No, so nothing was done." if approved is False else "Not allowed without Erfan's Yes."
    try:
        result = TOOLS[name](**args)
    except ToolError as e:
        log(log_file, name, args, f"error: {e}")
        return str(e)
    except TypeError:
        log(log_file, name, args, "error: bad arguments")
        return f"Wrong arguments for {name}."
    except OSError as e:
        log(log_file, name, args, f"error: {e}")
        return f"That didn't work: {e.strerror or e}."
    log(log_file, name, args, "ok")
    return result
