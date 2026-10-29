"""Tests for nubbe_app.py against the fake brain (tests/fake_llm.py).

    .venv\\Scripts\\python -m pytest tests -q

Every test uses a temp home folder, so the real ~/.nubbe is never touched.
No GPU or model needed.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))
# point HOME at a temp folder before the app is imported, as a second safety net
_HOME = tempfile.mkdtemp(prefix="nubbe_test_home_")
os.environ["HOME"] = os.environ["USERPROFILE"] = _HOME

import nubbe_app as app  # noqa: E402
from fake_llm import FACT, REPLY, FakeLLM  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(base, path):
    return json.load(urllib.request.urlopen(base + path, timeout=10))


def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


def chat(base, message, **extra) -> list[dict]:
    """Sends a chat message and returns every streamed event."""
    req = urllib.request.Request(base + "/api/chat", data=json.dumps({"message": message, **extra}).encode(),
                                 headers={"Content-Type": "application/json"})
    events = []
    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    return events


def reply_of(events) -> str:
    return "".join(e["text"] for e in events if e["type"] == "delta")


def chat_requests(fake):
    """Requests the fake brain got for replies (not memory extraction)."""
    return [r for r in fake.requests if "maintain a small memory" not in r["messages"][0]["content"]]


def extract_requests(fake):
    return [r for r in fake.requests if "maintain a small memory" in r["messages"][0]["content"]]


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / ".nubbe"
    monkeypatch.setattr(app, "DATA_DIR", d)
    monkeypatch.setattr(app, "DB_PATH", d / "nubbe.db")
    app.init_db()
    return d


@pytest.fixture
def serve(data_dir):
    """Starts the app's web server; returns its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def fake(monkeypatch):
    """A fake brain started outside the app (so the app must never stop it)."""
    f = FakeLLM().start()
    monkeypatch.setattr(app, "LLM_URL", f.url)
    monkeypatch.setattr(app, "BRAIN", app.Brain(can_start=False))
    yield f
    f.stop()


@pytest.fixture
def base(serve, fake):
    yield serve
    # let background memory learning finish before the temp folder goes away
    wait_for(lambda: not app._memory_lock.locked(), 5)


@pytest.fixture
def owned_brain(monkeypatch, data_dir):
    """A brain the app starts itself: tests/fake_llm.py run as a real subprocess."""
    port = free_port()
    monkeypatch.setattr(app, "LLM_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(app, "LLAMA_SERVER", sys.executable)
    monkeypatch.setattr(app, "LLAMA_ARGS", [str(TESTS / "fake_llm.py"), "--port", str(port),
                                            "--load-seconds", "0.5", "-ngl", "99"])
    brain = app.Brain(can_start=True, idle_minutes=0.5 / 60)
    monkeypatch.setattr(app, "BRAIN", brain)
    yield brain
    brain.stop()


# ---------------------------------------------------------------------------
# chats
# ---------------------------------------------------------------------------
def test_create_chat_and_stream_reply(base, fake):
    post(base, "/api/settings", {"think": True})
    events = chat(base, "Hey NUBB-E, how are you?")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert "reasoning" in kinds                       # smart mode streams thinking first
    assert kinds.index("reasoning") < kinds.index("delta")
    assert reply_of(events) == REPLY
    chats = get(base, "/api/chats")
    assert len(chats) == 1 and chats[0]["id"] == events[0]["chat_id"]
    assert chats[0]["title"] == "Hey NUBB-E, how are you?"


def test_chat_saved_and_reopened(base, fake):
    cid = chat(base, "First message")[0]["chat_id"]
    chat(base, "Second message", chat_id=cid)
    msgs = get(base, f"/api/chats/{cid}")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == "First message" and msgs[1]["content"] == REPLY
    # the follow-up was sent with the earlier turns as history
    sent = chat_requests(fake)[-1]["messages"]
    assert [m["content"] for m in sent[1:]] == ["First message", REPLY, "Second message"]
    assert len(get(base, "/api/chats")) == 1


def test_rename_and_delete_chat(base, fake):
    cid = chat(base, "Rename me")[0]["chat_id"]
    post(base, f"/api/chats/{cid}/rename", {"title": "Robot plans"})
    assert get(base, "/api/chats")[0]["title"] == "Robot plans"
    post(base, f"/api/chats/{cid}/delete", {})
    assert get(base, "/api/chats") == []
    assert get(base, f"/api/chats/{cid}") == []      # its messages went with it


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------
def test_remember_saves_memory(base, fake):
    # the window turns "/remember <fact>" into this call
    assert post(base, "/api/memory", {"text": "Erfan is building NUBB-E"})["ok"]
    assert post(base, "/api/memory", {"text": "   "})["ok"] is False
    mems = get(base, "/api/memory")
    assert [(m["text"], m["source"]) for m in mems] == [("Erfan is building NUBB-E", "you")]
    post(base, f"/api/memory/{mems[0]['id']}/delete", {})
    assert get(base, "/api/memory") == []


def test_auto_memory_learned_and_deduped(base, fake):
    cid = chat(base, "My laptop has an RTX 4060.")[0]["chat_id"]
    assert wait_for(lambda: get(base, "/api/memory"))
    chat(base, "Remember, it's an RTX 4060.", chat_id=cid)
    assert wait_for(lambda: len(extract_requests(fake)) == 2)
    assert wait_for(lambda: not app._memory_lock.locked())
    mems = get(base, "/api/memory")
    assert [(m["text"], m["source"]) for m in mems] == [(FACT, "auto")]
    # the second extraction call was told what's already known
    assert FACT in extract_requests(fake)[-1]["messages"][1]["content"]


def test_no_memory_from_small_talk(base, fake):
    chat(base, "Hi there")
    assert wait_for(lambda: len(extract_requests(fake)) == 1)
    assert wait_for(lambda: not app._memory_lock.locked())
    assert get(base, "/api/memory") == []


def test_chat_and_memory_use_separate_slots(base, fake):
    chat(base, "My laptop has an RTX 4060.")
    assert wait_for(lambda: len(extract_requests(fake)) == 1)
    assert chat_requests(fake)[-1]["id_slot"] == app.CHAT_SLOT
    assert extract_requests(fake)[-1]["id_slot"] == app.MEMORY_SLOT != app.CHAT_SLOT


def test_memory_included_in_system_prompt(base, fake):
    post(base, "/api/memory", {"text": "Erfan is building NUBB-E"})
    chat(base, "What am I working on?")
    system = chat_requests(fake)[-1]["messages"][0]["content"]
    assert "What you remember about Erfan" in system
    assert "- Erfan is building NUBB-E" in system


# ---------------------------------------------------------------------------
# voice + settings
# ---------------------------------------------------------------------------
def test_voice_adds_rules_and_limits_tokens(base, fake):
    events = chat(base, "Quick question", voice=True)
    req = chat_requests(fake)[-1]
    assert "## Voice mode" in req["messages"][0]["content"]
    assert req["max_tokens"] == 80
    assert req["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning" not in [e["type"] for e in events]
    assert get(base, "/api/chats")[0]["title"].startswith("Voice: ")
    # normal chats don't get voice rules
    chat(base, "Normal question")
    req = chat_requests(fake)[-1]
    assert "## Voice mode" not in req["messages"][0]["content"] and "max_tokens" not in req


def test_settings_toggles(base, fake):
    assert get(base, "/api/settings") == {"auto_memory": True, "think": False}   # Fast by default
    events = chat(base, "Fast mode please")
    assert chat_requests(fake)[-1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning" not in [e["type"] for e in events]
    assert wait_for(lambda: len(extract_requests(fake)) == 1)   # auto memory was still on

    post(base, "/api/settings", {"auto_memory": False, "think": True})
    chat(base, "My laptop has an RTX 4060.")
    time.sleep(0.5)
    assert len(extract_requests(fake)) == 1 and get(base, "/api/memory") == []
    assert "chat_template_kwargs" not in chat_requests(fake)[-1]
    post(base, "/api/settings", {"bogus": True})
    assert get(base, "/api/settings") == {"auto_memory": False, "think": True}


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------
def test_brain_down_gives_clean_error(serve, monkeypatch):
    monkeypatch.setattr(app, "LLM_URL", f"http://127.0.0.1:{free_port()}")
    monkeypatch.setattr(app, "BRAIN", app.Brain(can_start=False))
    events = chat(serve, "Anyone home?")
    assert events[-1]["type"] == "error" and "isn't running" in events[-1]["text"]
    assert get(serve, "/api/health") == {"brain": False, "state": "offline"}
    # the message is still saved, so nothing is lost
    assert get(serve, f"/api/chats/{events[0]['chat_id']}")[0]["content"] == "Anyone home?"


def test_brain_that_fails_to_start_gives_clean_error(serve, monkeypatch):
    monkeypatch.setattr(app, "LLM_URL", f"http://127.0.0.1:{free_port()}")
    monkeypatch.setattr(app, "LLAMA_SERVER", sys.executable)
    monkeypatch.setattr(app, "LLAMA_ARGS", ["-c", "import sys; sys.exit(1)"])
    monkeypatch.setattr(app, "BRAIN", app.Brain(can_start=True))
    events = chat(serve, "Wake up?")
    assert events[-1]["type"] == "error" and "didn't start" in events[-1]["text"]


def test_bad_requests_dont_crash(base, fake):
    req = urllib.request.Request(base + "/api/chat", data=b"{not json", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base, "/api/chat", {"message": "   "})
    assert e.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base, "/api/chat", ["not", "a", "dict"])
    assert e.value.code == 400
    assert reply_of(chat(base, "Still alive?")) == REPLY


def test_db_deleted_while_running(base, fake, data_dir):
    chat(base, "Before")
    assert wait_for(lambda: len(extract_requests(fake)) == 1)
    assert wait_for(lambda: not app._memory_lock.locked())    # Windows can't delete an open file
    for f in data_dir.glob("nubbe.db*"):
        f.unlink()
    assert get(base, "/api/chats") == []          # fresh, empty DB instead of a crash
    assert get(base, "/api/settings") == {"auto_memory": True, "think": False}
    assert reply_of(chat(base, "After")) == REPLY
    assert len(get(base, "/api/chats")) == 1


# ---------------------------------------------------------------------------
# auto-unload
# ---------------------------------------------------------------------------
def test_idle_unload_and_wake(serve, owned_brain):
    with owned_brain.lock:
        assert owned_brain.start()
    assert get(serve, "/api/health")["state"] in ("loading", "online")
    assert reply_of(chat(serve, "Hello")) == REPLY

    time.sleep(0.6)                                  # longer than idle_unload (0.5 s)
    assert owned_brain.sleep_if_idle()
    assert owned_brain.proc is None
    assert get(serve, "/api/health") == {"brain": False, "state": "asleep"}

    events = chat(serve, "You there?")               # next message wakes it up
    kinds = [e["type"] for e in events]
    assert kinds.index("waking") < kinds.index("delta") and kinds[-1] == "done"
    assert reply_of(events) == REPLY
    assert get(serve, "/api/health") == {"brain": True, "state": "online"}


def test_never_unloads_mid_reply(owned_brain):
    with owned_brain.lock:
        owned_brain.start()
    assert owned_brain.ensure()
    with owned_brain.use():
        time.sleep(0.6)
        assert not owned_brain.sleep_if_idle()       # a reply is in progress
    assert app.llm_alive()


def test_idle_watcher_puts_brain_to_sleep(owned_brain):
    with owned_brain.lock:
        owned_brain.start()
    assert owned_brain.ensure()
    threading.Thread(target=owned_brain.watch_idle, daemon=True).start()
    assert wait_for(lambda: owned_brain.status() == "asleep", 5)
    assert not app.llm_alive()


def test_never_unloads_a_brain_it_didnt_start(fake):
    brain = app.Brain(can_start=False, idle_minutes=0.001)
    time.sleep(0.2)
    assert not brain.sleep_if_idle()
    assert app.llm_alive()


def test_stop_always_stops_owned_brain(owned_brain):
    with owned_brain.lock:
        owned_brain.start()
    assert owned_brain.ensure()
    proc = owned_brain.proc
    owned_brain.stop()
    assert proc.poll() is not None and not app.llm_alive()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_brain_dies_if_app_is_killed(tmp_path):
    """Even a hard crash of the app (no cleanup code runs) takes llama-server down with it."""
    port = free_port()
    script = tmp_path / "parent.py"
    script.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        sys.path.insert(0, {str(ROOT)!r})
        import nubbe_app
        child = subprocess.Popen([sys.executable, {str(TESTS / 'fake_llm.py')!r}, "--port", "{port}"])
        job = nubbe_app.tie_to_app(child)
        print("ready" if job else "nojob", flush=True)
        time.sleep(60)
    """))
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    parent = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert parent.stdout.readline().strip() == "ready"
        url = f"http://127.0.0.1:{port}/health"

        def child_up():
            try:
                urllib.request.urlopen(url, timeout=1).close()
                return True
            except Exception:
                return False
        assert wait_for(child_up, 10)
        parent.kill()                                # hard kill: no atexit, no finally
        assert wait_for(lambda: not child_up(), 10)
    finally:
        parent.kill()
