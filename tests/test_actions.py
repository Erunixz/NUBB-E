"""Tests for everyday actions: understanding commands, the list, notes, timers, reminders, routines,
and the app running them without the brain. Nothing here opens an app, changes the volume or plays
music: the desk is replaced by fakes.

    .venv\\Scripts\\python -m pytest tests -q
"""
import json
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta

import pytest

from test_app import app, base, chat, chat_requests, data_dir, fake, get, post, reply_of, serve, wait_for  # noqa: F401
import nubbe_actions as actions
import nubbe_desk as desk

APPS = [("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"), ("Spotify", "SpotifyAB.Spotify!Spotify"),
        ("Visual Studio Code", "Microsoft.VisualStudioCode"), ("Google Chrome", "Chrome"),
        ("File Explorer", "Microsoft.Windows.Explorer"), ("Notepad", "Microsoft.WindowsNotepad!App")]


@pytest.fixture
def did(monkeypatch):
    """Fake desk: records what would have happened instead of doing it."""
    done = []
    monkeypatch.setattr(desk, "apps", lambda refresh=False: list(APPS))
    monkeypatch.setattr(desk, "open_app", lambda name: done.append(("open_app", name)) or f"Opened {desk.find_app(name)[0]}.")
    monkeypatch.setattr(desk, "open_url", lambda url: done.append(("open_url", url)) or "Opened.")
    monkeypatch.setattr(desk, "open_folder", lambda name: done.append(("open_folder", name)) or "Opened your folder.")
    monkeypatch.setattr(desk, "set_volume", lambda level=None, change=None, mute=None:
                        done.append(("volume", level, change, mute)) or "Volume is at 30%.")
    monkeypatch.setattr(desk, "get_volume", lambda: (42, False))
    monkeypatch.setattr(desk, "media", lambda action: done.append(("media", action)) or desk.MEDIA_SAYS[action])
    monkeypatch.setattr(desk, "now_playing", lambda: {"title": "Billie Jean", "artist": "Michael Jackson",
                                                      "album": "", "playing": True, "app": "Spotify"})
    monkeypatch.setattr(desk, "find_file", lambda name: done.append(("find_file", name)) or f"Found {name}.")
    monkeypatch.setattr(desk, "system_stats", lambda: "CPU 5%.")
    monkeypatch.setattr(desk, "notify", lambda title, text: done.append(("notify", text)))
    return done


@pytest.fixture
def acts(tmp_path):
    db = tmp_path / "a.db"
    with sqlite3.connect(db) as con:
        con.executescript(actions.SCHEMA)

    def connect():
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        return con
    return actions.Actions(connect, weather=lambda: "Sunny, +21°C")


# ---------------------------------------------------------------------------
# times
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, secs", [
    ("10 minutes", 600), ("10 min", 600), ("an hour and a half", 5400), ("half an hour", 1800),
    ("1 hour 30 minutes", 5400), ("2 hours and 15 minutes", 8100), ("90 seconds", 90), ("ten minutes", 600),
    ("a minute", 60), ("2 and a half hours", None), ("1h 30m", 5400), ("2 hours and a half", 9000),
    ("5", None), ("the pasta", None), ("10 minutes of fun", None), ("", None),
])
def test_parse_duration(text, secs):
    assert actions.parse_duration(text) == secs


NOW = datetime(2026, 11, 5, 14, 30)    # a Thursday, 2:30 pm


@pytest.mark.parametrize("text, when", [
    ("at 6", datetime(2026, 11, 5, 18, 0)),                 # past 6 am already: 6 pm
    ("at 3", datetime(2026, 11, 5, 15, 0)),
    ("at 2", datetime(2026, 11, 6, 2, 0)),                  # 2 pm has passed, so the next 2 o'clock
    ("at 6pm", datetime(2026, 11, 5, 18, 0)), ("6:45 pm", datetime(2026, 11, 5, 18, 45)),
    ("at 9am", datetime(2026, 11, 6, 9, 0)), ("at 18:00", datetime(2026, 11, 5, 18, 0)),
    ("in 20 minutes", NOW + timedelta(minutes=20)), ("in an hour and a half", NOW + timedelta(minutes=90)),
    ("tomorrow at 9", datetime(2026, 11, 6, 9, 0)), ("tomorrow at 5", datetime(2026, 11, 6, 17, 0)),
    ("tomorrow at 9am", datetime(2026, 11, 6, 9, 0)), ("tomorrow morning", datetime(2026, 11, 6, 9, 0)),
    ("tonight at 8", datetime(2026, 11, 5, 20, 0)), ("on friday at 5pm", datetime(2026, 11, 6, 17, 0)),
    ("monday at noon", datetime(2026, 11, 9, 12, 0)), ("at midnight", datetime(2026, 11, 6, 0, 0)),
    ("thursday at 10am", datetime(2026, 11, 12, 10, 0)),    # today is Thursday: next week's
    ("at the code", None), ("someday", None), ("at 25", None), ("in a while", None),
])
def test_parse_when(text, when):
    assert actions.parse_when(text, NOW) == when


def test_say_time_and_duration():
    assert actions.say_time(datetime(2026, 11, 5, 18, 0), NOW) == "at 6 PM"
    assert actions.say_time(datetime(2026, 11, 6, 9, 30), NOW) == "tomorrow at 9:30 AM"
    assert actions.say_time(datetime(2026, 11, 9, 12, 0), NOW) == "on Monday at 12 PM"
    assert actions.say_duration(600) == "10 minutes" and actions.say_duration(5400) == "1 hour and 30 minutes"
    assert actions.say_duration(90) == "1 minute and 30 seconds"


# ---------------------------------------------------------------------------
# understanding commands (rules first) and leaving the rest to the brain
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, plan", [
    ("Open Calculator.", [("open_app", {"name": "Calculator"})]),
    ("Hey NUBB-E, can you open calculator please?", [("open_app", {"name": "calculator"})]),
    ("launch vs code", [("open_app", {"name": "vs code"})]),
    ("open youtube and search for care bears", [("open_website", {"site": "youtube", "search": "care bears"})]),
    ("Open Spotify and set the volume to 20", [("open_app", {"name": "Spotify"}), ("set_volume", {"level": 20})]),
    ("open my downloads folder", [("open_folder", {"name": "my downloads folder"})]),
    ("go to github.com", [("open_website", {"site": "github.com"})]),
    ("volume 30", [("set_volume", {"level": 30})]), ("turn it down a lot", [("set_volume", {"change": -20})]),
    ("louder", [("set_volume", {"change": 10})]), ("unmute", [("set_volume", {"mute": False})]),
    ("pause the music", [("media", {"action": "pause"})]), ("next song", [("media", {"action": "next"})]),
    ("what's playing?", [("now_playing", {})]),
    ("play Billie Jean on Spotify", [("play_music", {"query": "Billie Jean", "app": "spotify"})]),
    ("Set a timer for 10 minutes.", [("set_timer", {"seconds": 600, "label": ""})]),
    ("10 minute timer for the pasta", [("set_timer", {"seconds": 600, "label": "pasta"})]),
    ("Remind me at 6 to call mom.", [("set_reminder", {"text": "call mom", "when": "at 6"})]),
    ("remind me to look at the code in 10 minutes",
     [("set_reminder", {"text": "look at the code", "when": "in 10 minutes"})]),
    ("remind me tomorrow at 9am to submit the report",
     [("set_reminder", {"text": "submit the report", "when": "tomorrow at 9am"})]),
    ("cancel the timer", [("cancel_reminder", {"which": "timer", "all": False})]),
    ("add milk and eggs to my shopping list", [("add_todo", {"item": "milk and eggs"})]),
    ("cross milk off my list", [("done_todo", {"item": "milk"})]), ("clear my list", [("clear_todos", {})]),
    ("take a note: the wifi password is on the router", [("add_note", {"text": "the wifi password is on the router"})]),
    ("good morning", [("briefing", {})]), ("how's my laptop doing", [("system_stats", {})]),
    ("find my resume", [("find_file", {"name": "resume"})]),
    ("search for transformers on hugging face", [("open_website", {"site": "hugging face", "search": "transformers"})]),
    ("remind me to call mom at 6 and open spotify",
     [("set_reminder", {"text": "call mom", "when": "at 6"}), ("open_app", {"name": "spotify"})]),
])
def test_rules_understand_commands(acts, did, text, plan):
    assert acts.plan(text) == plan


@pytest.mark.parametrize("text", [
    "what's this", "continue", "play a game with me", "where's my phone", "open source licenses explained",
    "explain recursion", "remind me about the thing", "start a new project idea", "stop",
    "search for the latest python version", "What do you think about Rust?", "I'm a newbie at Rust",
    "open the pod bay doors", "Hello", "My laptop has an RTX 4060.", "add some tests to this function",
    "turn this into a list", "next time be shorter", "remember that I like tea",
])
def test_everything_else_goes_to_the_brain(acts, did, text):
    assert acts.plan(text) is None


# ---------------------------------------------------------------------------
# doing them
# ---------------------------------------------------------------------------
def test_todo_list(acts, did):
    assert acts.call("add_todo", {"item": "milk and eggs"}) == "Added milk and eggs to your list."
    assert acts.call("add_todo", {"item": "call the dentist"}) == "Added call the dentist to your list."
    assert acts.call("list_todos", {}) == "You have 3 on your list: milk, eggs and call the dentist."
    assert acts.call("done_todo", {"item": "Milk"}) == "Crossed off milk. 2 left."
    assert acts.call("done_todo", {"item": "pizza"}) == "There's nothing called pizza on your list."
    assert acts.call("clear_todos", {}) == "Not allowed without Erfan's Yes."
    assert acts.call("clear_todos", {}, approved=False) == "Erfan said No, so nothing was changed."
    assert acts.rows("SELECT COUNT(*) AS n FROM todos")[0]["n"] == 3
    assert acts.call("clear_todos", {}, approved=True) == "Cleared your list (3 items)."
    assert acts.call("list_todos", {}) == "Your list is empty."


def test_notes(acts):
    assert acts.call("add_note", {"text": "The wifi password is on the router"}) == "Noted."
    acts.call("add_note", {"text": "Ideas: a robot arm"})
    assert "wifi password" in acts.call("search_notes", {"query": "wifi"})
    assert "robot arm" not in acts.call("search_notes", {"query": "wifi"})
    assert acts.call("search_notes", {"query": ""}).startswith("Your latest notes:\n- Ideas: a robot arm")
    assert acts.call("delete_note", {"which": "wifi"}) == "Not allowed without Erfan's Yes."
    assert "wifi" in acts.card("delete_note", {"which": "wifi"})["detail"]
    assert acts.call("delete_note", {"which": "wifi"}, approved=True) == "Deleted the note."
    assert acts.call("search_notes", {"query": "wifi"}) == "No notes about wifi."


def test_timer_goes_off_once(acts, monkeypatch):
    assert acts.call("set_timer", {"seconds": 600, "label": "pasta"}) == "Timer set for 10 minutes (pasta)."
    assert acts.call("set_timer", {"duration": "an hour and a half"}) == "Timer set for 1 hour and 30 minutes."
    assert acts.call("set_timer", {"duration": "a while"}).startswith("I couldn't tell how long")
    assert acts.due() == []
    later = time.time() + 601
    due = acts.due(later)
    assert [(r["kind"], r["text"], r["late"]) for r in due] == [("timer", "pasta", False)]
    assert acts.due(later) == []                     # never twice
    assert acts.call("list_reminders", {}).startswith("Timer: 1 hour and ")


def test_reminders(acts):
    assert acts.call("set_reminder", {"text": "I need to call mom", "when": "in 20 minutes"}).startswith(
        "Okay, I'll remind you to call mom at")
    assert acts.call("set_reminder", {"text": "x", "when": "whenever"}).startswith("I couldn't understand the time")
    acts.call("set_reminder", {"text": "pay rent", "when": "tomorrow at 9am"})
    assert "Call mom at" in acts.call("list_reminders", {}) and "Pay rent tomorrow at 9 AM" in acts.call("list_reminders", {})
    assert acts.call("cancel_reminder", {"which": "rent"}) == "Cancelled the reminder."
    assert acts.call("cancel_reminder", {"which": "timer"}) == "There's no timer like that to cancel."
    missed = acts.due(time.time() + 3600)            # NUBB-E was off when it was due
    assert missed[0]["text"] == "I need to call mom" and missed[0]["late"]


def test_routines(acts, did):
    out = acts.call("make_routine", {"name": "Start my day", "steps": ["open Spotify", "volume 20", "open gmail"]})
    assert out.startswith('Saved the routine "start my day"')
    assert acts.plan("start my day") == [("run_routine", {"name": "start my day"})]
    assert acts.plan("run the start my day routine") == [("run_routine", {"name": "start my day"})]
    assert acts.call("run_routine", {"name": "start my day"}) == "Opened Spotify. Volume is at 30%. Opened gmail."
    assert ("open_app", "Spotify") in did and ("volume", 20, None, None) in did
    assert acts.call("make_routine", {"name": "bad", "steps": ["write my essay"]}).startswith(
        "I don't know how to do this step on my own: write my essay")
    assert acts.call("make_routine", {"name": "sneaky", "steps": ["clear my list"]}) .startswith("Saved")
    acts.call("add_todo", {"item": "milk"})
    assert acts.call("run_routine", {"name": "sneaky"}) == "Skipped: clear my list."   # deleting never runs unasked
    assert acts.rows("SELECT COUNT(*) AS n FROM todos")[0]["n"] == 1


def test_made_from_one_sentence(acts, did):
    plan = acts.plan("make a routine called focus: mute and open notepad")
    assert plan == [("make_routine", {"name": "focus", "steps": ["mute", "open notepad"]})]
    acts.call(*plan[0])
    assert acts.call("list_routines", {}) == "- focus: mute and open notepad"


def test_briefing(acts, monkeypatch):
    acts.call("add_todo", {"item": "milk"})
    acts.call("set_reminder", {"text": "call mom", "when": "in 1 minute"})
    out = acts.call("briefing", {})
    assert out.startswith("Good ") and "Weather: Sunny, +21°C." in out
    assert "call mom at" in out and "On your list: milk." in out
    acts.weather = lambda: None                       # offline: no weather, no fuss
    acts.call("cancel_reminder", {"which": "call mom"})
    acts.call("done_todo", {"item": "milk"})
    out = acts.call("briefing", {})
    assert "Weather" not in out and "A free day!" in out


def test_volume_and_music(acts, did):
    assert acts.call("set_volume", {}) == "The volume is at 42%."
    assert acts.call("set_volume", {"level": 30}) == "Volume is at 30%."
    assert acts.call("now_playing", {}) == "Playing Billie Jean by Michael Jackson."
    assert acts.call("media", {"action": "next"}) == "Skipped to the next one."
    assert acts.call("nonsense", {}) == "There's no action called nonsense."
    assert acts.call("set_timer", {"bogus": 1}) == "Wrong details for set_timer."


# ---------------------------------------------------------------------------
# the desk itself (safe parts only: matching, addresses, finding files in a temp home)
# ---------------------------------------------------------------------------
def test_find_app(monkeypatch):
    monkeypatch.setattr(desk, "apps", lambda refresh=False: list(APPS))
    assert desk.find_app("calculator")[0] == "Calculator"
    assert desk.find_app("the calculator app")[0] == "Calculator"
    assert desk.find_app("vs code")[0] == "Visual Studio Code"
    assert desk.find_app("chrome")[0] == "Google Chrome"
    assert desk.find_app("calculater")[0] == "Calculator"            # a small typo is fine
    assert desk.find_app("blender") is None and desk.find_app("") is None


def test_uninstallers_are_never_apps(monkeypatch):
    raw = [{"Name": "Uninstall Zoom", "AppID": "x"}, {"Name": "Reset Spyder Settings", "AppID": "y"},
           {"Name": "Zoom", "AppID": "z"}]
    monkeypatch.setattr(desk.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": json.dumps(raw)})())
    monkeypatch.setattr(desk, "IS_WINDOWS", True)
    assert desk.apps(refresh=True) == [("Zoom", "z")]
    desk._apps["time"] = 0


def test_site_addresses():
    assert desk.site_url("YouTube") == "https://www.youtube.com"
    assert desk.site_url("github.com") == "https://github.com"
    assert desk.site_url("the gmail website") == "https://mail.google.com"
    assert desk.site_url("my essay") is None
    assert desk.search_url("youtube", "care bears") == "https://www.youtube.com/results?search_query=care+bears"
    with pytest.raises(desk.DeskError):
        desk.open_url("file:///C:/Windows/System32/cmd.exe")      # only web links


def test_find_files_in_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for p in ("Documents/Resume 2026.pdf", "Documents/old/resume_draft.docx", "Downloads/cat.png",
              ".ssh/resume_key.pem", "AppData/Local/resume.tmp"):
        (home / p).parent.mkdir(parents=True, exist_ok=True)
        (home / p).write_text("x")
    monkeypatch.setattr(desk.Path, "home", classmethod(lambda cls: home))
    found = [p.name for p in desk.find_files("my resume")]
    assert found[0] in ("Resume 2026.pdf", "resume_draft.docx") and set(found) == {"Resume 2026.pdf", "resume_draft.docx"}
    assert desk.find_files("nothing like this") == []


# ---------------------------------------------------------------------------
# in the app: quick commands skip the brain, the brain can use them as tools
# ---------------------------------------------------------------------------
@pytest.fixture
def app_did(did, monkeypatch, data_dir):
    monkeypatch.setattr(app.VOICE, "send", lambda cmd: did.append(("voice", cmd)) or "ok")
    return did


def test_quick_command_needs_no_brain(serve, app_did):
    """The brain is down here (no fake): simple commands still work, and are saved in the chat."""
    events = chat(serve, "add milk to my list")
    assert reply_of(events) == "Added milk to your list." and events[-1]["type"] == "done"
    assert app.rows("SELECT text FROM todos") == [{"text": "milk"}]
    cid = events[0]["chat_id"]
    assert [m["content"] for m in get(serve, f"/api/chats/{cid}")] == ["add milk to my list", "Added milk to your list."]
    assert reply_of(chat(serve, "open calculator and volume 30")) == "Opened Calculator. Volume is at 30%."
    assert ("open_app", "calculator") in app_did and ("volume", 30, None, None) in app_did


def test_quick_commands_dont_touch_the_brain(base, fake, app_did):
    chat(base, "timer 5 minutes")
    assert chat_requests(fake) == []
    chat(base, "What should I cook tonight?")             # a normal question still goes to the brain
    assert len(chat_requests(fake)) == 1


def test_clearing_the_list_asks_first(serve, app_did):
    app.ACTIONS.call("add_todo", {"item": "milk"})

    def press(ok):
        assert wait_for(lambda: app.PENDING)
        post(serve, "/api/confirm", {"id": next(iter(app.PENDING)), "ok": ok})
    t = threading.Thread(target=press, args=(False,))
    t.start()
    events = chat(serve, "clear my list")
    t.join()
    assert any(e["type"] == "confirm" for e in events) and reply_of(events) == "Erfan said No, so nothing was changed."
    assert len(app.rows("SELECT id FROM todos")) == 1
    t = threading.Thread(target=press, args=(True,))
    t.start()
    assert reply_of(chat(serve, "clear my list")) == "Cleared your list (1 item)."
    t.join()


def test_brain_can_use_actions_as_tools(base, fake, app_did):
    fake.plan = [("set_reminder", {"text": "stretch", "when": "in 20 minutes"})]
    events = chat(base, "I keep forgetting to stretch, can you nudge me in a bit?")
    assert any(e["type"] == "tool" for e in events)
    assert app.rows("SELECT text, kind FROM reminders") == [{"text": "stretch", "kind": "reminder"}]
    offered = {t["function"]["name"] for t in chat_requests(fake)[0]["tools"]}
    assert {"set_timer", "open_app", "add_todo", "briefing"} <= offered
    assert "Your everyday actions" in chat_requests(fake)[0]["messages"][0]["content"]


def test_reminder_goes_off_out_loud(data_dir, app_did, monkeypatch):
    seen = []
    monkeypatch.setattr(app, "broadcast", lambda msg: seen.append(msg))
    app.ACTIONS.call("set_timer", {"seconds": 1, "label": "tea"})
    for r in app.ACTIONS.due(time.time() + 2):
        app.fire_reminder(r)
    assert ("voice", "say Your tea timer is done!") in app_did
    assert ("notify", "Your tea timer is done!") in app_did
    assert {"event": "reminder", "text": "Your tea timer is done!"} in seen


def test_lists_panel(serve, app_did):
    post(serve, "/api/lists", {"action": "add_todo", "text": "bread"})
    post(serve, "/api/lists", {"action": "add_note", "text": "Buy a new mouse"})
    app.ACTIONS.call("set_timer", {"seconds": 300})
    lists = get(serve, "/api/lists")
    assert [t["text"] for t in lists["todos"]] == ["bread"] and lists["notes"][0]["text"] == "Buy a new mouse"
    assert lists["reminders"][0]["kind"] == "timer" and 290 < lists["reminders"][0]["left"] <= 300
    tid, nid, rid = lists["todos"][0]["id"], lists["notes"][0]["id"], lists["reminders"][0]["id"]
    assert post(serve, "/api/lists", {"action": "done_todo", "id": tid})["todos"][0]["done"] == 1
    assert post(serve, "/api/lists", {"action": "delete_note", "id": nid})["notes"] == []
    assert post(serve, "/api/lists", {"action": "cancel_reminder", "id": rid})["reminders"] == []
    with pytest.raises(urllib.error.HTTPError):
        post(serve, "/api/lists", {"action": "drop_tables"})


def test_media_endpoint(serve, app_did):
    assert get(serve, "/api/media")["title"] == "Billie Jean"
    assert post(serve, "/api/media", {"action": "next"}) == {"text": "Skipped to the next one."}


def test_tools_not_offered_are_refused(base, fake, app_did, data_dir, monkeypatch):
    """The brain asks for web search with the Web switch off: refused, nothing goes online."""
    went_online = []
    monkeypatch.setattr(app.web, "call", lambda *a, **k: went_online.append(a) or "results")
    post(base, "/api/settings", {"web": False})
    fake.plan = [("web_search", {"query": "news"})]
    events = chat(base, "Any news today?")
    assert went_online == [] and events[-1]["type"] == "done"
    log = [json.loads(line) for line in (data_dir / "tools.log").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["tool"] == "web_search" and log[-1]["outcome"] == "not offered"
