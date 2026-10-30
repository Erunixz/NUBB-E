"""Tests for the file tools (nubbe_tools.py) and the app's tool calling, with the fake brain.

    .venv\\Scripts\\python -m pytest tests -q

Every test uses a temp workspace folder and a temp home folder.
"""
import json
import os
import threading
import urllib.request

import pytest

from test_app import app, chat_requests, data_dir, fake, get, post, reply_of, serve, base, wait_for  # noqa: F401

tools = app.tools


# ---------------------------------------------------------------------------
# helpers and fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def ws(tmp_path, data_dir, monkeypatch):
    """A small workspace: two notes, a subfolder, a binary file, and a secret file just outside it."""
    w = tmp_path / "workspace"
    (w / "notes").mkdir(parents=True)
    (w / "notes" / "robots.md").write_text("# Robot ideas\nThe robot arm needs a stronger servo.\nBuy wheels.\n",
                                           encoding="utf-8")
    (w / "todo.txt").write_text("Finish phase 6\n", encoding="utf-8")
    (w / "photo.jpg").write_bytes(b"\xff\xd8\x00\x00binary")
    (tmp_path / "secret.txt").write_text("TOP SECRET 42", encoding="utf-8")
    monkeypatch.setattr(tools, "WORKSPACE_DIR", str(w))
    return w


def log_lines(data_dir):
    f = data_dir / "tools.log"
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def chat_answering(base, message, answer=None, **extra):
    """Sends a chat and, like Erfan, presses Yes (True) or No (False) on each card. None: never answers."""
    req = urllib.request.Request(base + "/api/chat", data=json.dumps({"message": message, **extra}).encode(),
                                 headers={"Content-Type": "application/json"})
    events = []
    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            events.append(ev)
            if ev["type"] == "confirm" and answer is not None:
                post(base, "/api/confirm", {"id": ev["id"], "ok": answer})
    return events


def tool_results(fake):
    return [m["content"] for m in chat_requests(fake)[-1]["messages"] if m.get("role") == "tool"]


# ---------------------------------------------------------------------------
# the sandbox
# ---------------------------------------------------------------------------
def test_paths_stay_inside_the_workspace(ws, tmp_path):
    assert tools.inside("notes/robots.md") == (ws / "notes" / "robots.md").resolve()
    assert tools.inside(".") == ws.resolve() and tools.inside("") == ws.resolve()
    assert tools.inside("notes/../todo.txt") == (ws / "todo.txt").resolve()
    for bad in ("../secret.txt", "notes/../../secret.txt", str(tmp_path / "secret.txt"), "C:/Windows/win.ini", "/etc"):
        with pytest.raises(tools.ToolError, match="outside the workspace"):
            tools.inside(bad)


def test_symlinks_out_of_the_workspace_are_refused(ws, tmp_path):
    try:
        os.symlink(tmp_path / "secret.txt", ws / "sneaky.txt")
        os.symlink(tmp_path, ws / "sneaky_dir", target_is_directory=True)
    except OSError:
        pytest.skip("this account can't make symlinks")
    with pytest.raises(tools.ToolError, match="outside"):
        tools.read_file("sneaky.txt")
    with pytest.raises(tools.ToolError, match="outside"):
        tools.list_dir("sneaky_dir")
    assert "sneaky" not in tools.list_dir(".")
    assert "SECRET" not in tools.search_files("secret")


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows thing")
def test_junctions_out_of_the_workspace_are_refused(ws, tmp_path):
    import subprocess   # junctions need no admin rights, so they're the easy way out on Windows
    subprocess.run(["cmd", "/c", "mklink", "/J", str(ws / "jump"), str(tmp_path)], check=True, capture_output=True)
    with pytest.raises(tools.ToolError, match="outside"):
        tools.read_file("jump/secret.txt")
    assert "jump" not in tools.list_dir(".")
    assert "SECRET" not in tools.search_files("secret")


def test_tools_off_without_a_workspace(data_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "WORKSPACE_DIR", "")
    assert tools.root() is None
    monkeypatch.setattr(tools, "WORKSPACE_DIR", str(tmp_path / "missing"))
    assert tools.root() is None
    with pytest.raises(tools.ToolError, match="No workspace"):
        tools.inside("x")


# ---------------------------------------------------------------------------
# read-only tools
# ---------------------------------------------------------------------------
def test_list_dir(ws):
    out = tools.list_dir(".")
    assert out.splitlines()[1] == "notes/"            # folders first
    assert f"todo.txt  ({(ws / 'todo.txt').stat().st_size} bytes)" in out
    with pytest.raises(tools.ToolError, match="isn't a folder"):
        tools.list_dir("todo.txt")


def test_read_file(ws, monkeypatch):
    assert tools.read_file("notes/robots.md").startswith("# Robot ideas")
    with pytest.raises(tools.ToolError, match="doesn't exist"):
        tools.read_file("nope.md")
    with pytest.raises(tools.ToolError, match="isn't a text file"):
        tools.read_file("photo.jpg")
    monkeypatch.setattr(tools, "MAX_READ_CHARS", 10)
    assert tools.read_file("notes/robots.md").startswith("# Robot id\n\n[cut off here")


def test_search_files(ws):
    out = tools.search_files("SERVO")
    assert out.startswith("notes/robots.md\n  2: The robot arm needs a stronger servo.")
    assert "todo.txt" not in out
    assert "notes/robots.md" in tools.search_files("robots")       # file name matches too
    assert tools.search_files("banana").startswith("No files")
    assert tools.search_files("robot", "notes").startswith("notes/robots.md")


# ---------------------------------------------------------------------------
# tools in the chat
# ---------------------------------------------------------------------------
def test_notes_question_uses_search_and_answers_from_the_file(base, fake, ws, data_dir):
    fake.plan = [("search_files", {"query": "servo"})]
    events = chat_answering(base, "What's in my notes about the servo?")
    assert {"type": "tool", "name": "search_files", "text": "searching for 'servo'"} in events
    assert "confirm" not in [e["type"] for e in events]          # reading needs no Yes
    assert "stronger servo" in reply_of(events)
    first, second = chat_requests(fake)[-2:]
    assert [t["function"]["name"] for t in first["tools"]] == ["list_dir", "read_file", "search_files",
                                                                "write_file", "run_command"]
    assert "Your file tools" in first["messages"][0]["content"]
    call = second["messages"][-2]["tool_calls"][0]
    assert call["function"]["name"] == "search_files" and json.loads(call["function"]["arguments"]) == {"query": "servo"}
    assert second["messages"][-1]["tool_call_id"] == call["id"]
    saved = get(base, f"/api/chats/{events[0]['chat_id']}")
    assert saved[-1]["content"] == reply_of(events)              # only the answer is saved, not the tool steps
    assert log_lines(data_dir)[-1] | {"time": ""} == {"time": "", "tool": "search_files",
                                                      "args": {"query": "servo"}, "outcome": "ok"}


def test_write_file_only_after_yes(base, fake, ws, data_dir):
    fake.plan = [("write_file", {"path": "notes/new.md", "content": "Hello from NUBB-E"})]
    events = chat_answering(base, "Make a note", answer=True)
    card = next(e for e in events if e["type"] == "confirm")
    assert card["title"] == "Write a file" and card["target"] == "notes/new.md (new file)"
    assert card["detail"] == "Hello from NUBB-E"                 # the exact text, before anything is written
    assert {"type": "confirmed", "id": card["id"], "ok": True} in events
    assert (ws / "notes" / "new.md").read_text(encoding="utf-8") == "Hello from NUBB-E"
    assert tool_results(fake) == ["Created notes/new.md (17 characters)."]
    entry = log_lines(data_dir)[-1]
    assert entry["args"] == {"path": "notes/new.md", "content": "<17 characters>"}   # contents aren't logged
    assert entry["outcome"] == "ok"


def test_write_file_no_means_no(base, fake, ws, data_dir):
    fake.plan = [("write_file", {"path": "todo.txt", "content": "wiped"})]
    events = chat_answering(base, "Clear my todo", answer=False)
    assert next(e for e in events if e["type"] == "confirm")["target"] == "todo.txt (replaces the current file)"
    assert (ws / "todo.txt").read_text(encoding="utf-8") == "Finish phase 6\n"
    assert tool_results(fake) == ["Erfan said No, so nothing was done."]
    assert log_lines(data_dir)[-1]["outcome"] == "declined"


def test_no_answer_counts_as_no(base, fake, ws, monkeypatch):
    monkeypatch.setattr(app, "CONFIRM_SECONDS", 0.3)
    fake.plan = [("run_command", {"command": "echo hi > made.txt"})]
    events = chat_answering(base, "Run it", answer=None)
    assert {"type": "confirmed", "id": next(e["id"] for e in events if e["type"] == "confirm"), "ok": False} in events
    assert not (ws / "made.txt").exists()
    assert app.PENDING == {}


def test_run_command_after_yes_runs_in_the_workspace(base, fake, ws, data_dir):
    fake.plan = [("run_command", {"command": "echo hello from the workspace"})]
    events = chat_answering(base, "Say hello", answer=True)
    card = next(e for e in events if e["type"] == "confirm")
    assert card["title"] == "Run a command" and card["detail"] == "echo hello from the workspace"
    assert card["target"] == f"in {ws.resolve()}"
    result = tool_results(fake)[0]
    assert result.startswith("Exit code 0.") and "hello from the workspace" in result
    assert log_lines(data_dir)[-1]["args"] == {"command": "echo hello from the workspace"}


def test_only_a_real_yes_counts(base, fake, ws):
    assert post(base, "/api/confirm", {"id": "made-up", "ok": True}) == {"ok": False}
    fake.plan = [("write_file", {"path": "a.txt", "content": "x"})]
    req = urllib.request.Request(base + "/api/chat", data=json.dumps({"message": "write"}).encode(),
                                 headers={"Content-Type": "application/json"})

    def answer_with_a_string():   # "yes" as text (not true) is still No
        assert wait_for(lambda: app.PENDING)
        post(base, "/api/confirm", {"id": next(iter(app.PENDING)), "ok": "yes"})
    threading.Thread(target=answer_with_a_string).start()
    urllib.request.urlopen(req, timeout=30).read()
    assert not (ws / "a.txt").exists()


def test_path_outside_is_refused_in_chat(base, fake, ws, data_dir):
    fake.plan = [("read_file", {"path": "../secret.txt"})]
    reply = reply_of(chat_answering(base, "Read the secret"))
    assert "outside the workspace" in reply and "outside the workspace folder" in tool_results(fake)[0]
    assert "TOP SECRET" not in json.dumps(chat_requests(fake))        # the secret never reached the brain
    assert log_lines(data_dir)[-1]["outcome"].startswith("error: '../secret.txt' is outside")


def test_bad_tool_calls_dont_crash(base, fake, ws, data_dir):
    fake.plan = [("delete_everything", {}), ("read_file", {"wrong": 1})]
    events = chat_answering(base, "Do weird things")
    assert events[-1]["type"] == "done"
    assert [e["outcome"] for e in log_lines(data_dir)] == ["unknown tool", "error: bad arguments"]


def test_tool_rounds_are_limited(base, fake, ws):
    fake.plan = [("list_dir", {"path": "."})] * 20
    events = chat_answering(base, "Loop forever")
    assert [e["type"] for e in events].count("tool") == app.MAX_TOOL_ROUNDS
    assert "tools" not in chat_requests(fake)[-1]             # the last round has to answer
    assert events[-1]["type"] == "done" and reply_of(events)


def test_no_tools_in_voice_or_without_a_workspace(base, fake, ws, monkeypatch):
    fake.plan = [("list_dir", {})]
    chat_answering(base, "Hi on a call", voice=True, source="call")
    assert "tools" not in chat_requests(fake)[-1]
    assert "Your file tools" not in chat_requests(fake)[-1]["messages"][0]["content"]
    monkeypatch.setattr(tools, "WORKSPACE_DIR", "")
    chat_answering(base, "Hi")
    assert "tools" not in chat_requests(fake)[-1]
