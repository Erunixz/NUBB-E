"""Tests for the smarter hands (Phase 6.7e): UI Automation names, clicking and filling by name, the
"where's the button" ring, and agent mode. Real UI Automation runs only against a window the test makes
itself, far off screen (never on Erfan's screen, never focused). Everything else uses fakes.

    .venv\\Scripts\\python -m pytest tests -q
"""
import sys
import threading
import time

import pytest

from test_app import app, base, chat_requests, data_dir, fake, get, post, reply_of, serve, wait_for  # noqa: F401
from test_hands import FakeHands
from test_tools import chat_answering
import nubbe_hands as hands
import nubbe_uia as uia


def C(name, kind="button", rect=(100, 100, 180, 130), **kw):
    return uia.Control(name, kind, rect, **kw)


SCREEN = [C("File", "menu item", (0, 0, 40, 20)), C("Export", "button"), C("Export settings...", "menu item", (0, 30, 90, 50)),
          C("Import", "button", (200, 100, 280, 130)), C("Search", "edit", (300, 10, 600, 40)),
          C("Delete account", "button", (400, 400, 500, 430)), C("Docs", "tab", (10, 60, 90, 80)),
          C("Save", "button", (500, 500, 560, 530), enabled=False)]


# ---------------------------------------------------------------------------
# matching words to controls
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("said, name", [
    ("the export button", "Export"), ("Export", "Export"), ("exprot", "Export"), ("export settings", "Export settings..."),
    ("the search box", "Search"), ("the docs tab", "Docs"), ("import button", "Import"),
    ("the share button", None), ("", None), ("print", None),
])
def test_find(said, name):
    hit = uia.find(SCREEN, said)
    assert (hit.name if hit else None) == name


def test_find_prefers_what_he_named_and_enabled_ones():
    two = [C("Save", "button", (0, 0, 50, 20), enabled=False), C("Save", "button", (0, 40, 50, 60))]
    assert uia.find(two, "save").rect == (0, 40, 50, 60)
    both = [C("Docs", "link", (0, 0, 50, 20)), C("Docs", "tab", (0, 40, 50, 60))]
    assert uia.find(both, "the docs tab").kind == "tab" and uia.find(both, "the docs link").kind == "link"


def test_fields_top_to_bottom():
    boxes = [C("Email", "edit", (10, 200, 90, 220)), C("Name", "edit", (10, 100, 90, 120)),
             C("Off", "edit", (10, 300, 90, 320), enabled=False), C("Go", "button")]
    assert [b.name for b in uia.fields(boxes)] == ["Name", "Email"]


def test_ring_rect_stays_on_screen():
    assert uia.ring_rect((100, 100, 200, 130)) == (86, 86, 214, 144)
    assert uia.ring_rect((0, 0, 50, 20), screen=(0, 0, 1920, 1080)) == (0, 0, 64, 34)


# ---------------------------------------------------------------------------
# real UI Automation, on a window this test makes off screen
# ---------------------------------------------------------------------------
@pytest.fixture
def own_window():
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    make = u32.CreateWindowExW
    make.restype = wintypes.HWND
    make.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                     ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    # no taskbar button, never activated, far away from every monitor
    parent = make(0x08000080, "STATIC", "NUBB-E test form", 0x80000000, -32000, -32000, 400, 300, None, None, None, None)
    for cls, text, x, y, w, style in [("STATIC", "Full name:", 10, 10, 100, 0), ("EDIT", "", 120, 10, 200, 0x800000),
                                      ("STATIC", "Password:", 10, 40, 100, 0), ("EDIT", "", 120, 40, 200, 0x20),
                                      ("BUTTON", "Export", 10, 80, 100, 0), ("BUTTON", "Delete account", 120, 80, 120, 0)]:
        make(0, cls, text, 0x50000000 | style, x, y, w, 22, parent, None, None, None)
    before = u32.GetForegroundWindow()
    u32.ShowWindow(parent, 4)            # SW_SHOWNOACTIVATE
    yield parent
    u32.DestroyWindow(parent)
    assert u32.GetForegroundWindow() == before      # focus never moved


@pytest.mark.own_window
@pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation")
def test_reads_a_real_window(own_window):
    found = uia.read(own_window)
    assert {(c.name, c.kind) for c in found} >= {("Full name:", "edit"), ("Export", "button"),
                                                  ("Delete account", "button")}
    pw = next(c for c in found if c.kind == "edit" and c.password)
    assert pw.name == "Password:" and pw.value == ""
    box = uia.fields(found)[0]
    assert box.name == "Full name:" and not box.password and uia.still_at(box)
    mod, _ = uia._uia()
    box.element.GetCurrentPattern(mod.UIA_ValuePatternId).QueryInterface(mod.IUIAutomationValuePattern).SetValue("Erfan")
    assert uia.value_of(box) == "Erfan" and uia.value_of(pw) == ""
    assert uia.find(found, "the export button").name == "Export"


@pytest.mark.own_window
@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_ring_comes_and_goes_off_screen(own_window):
    t0 = time.time()
    assert uia.ring((-31990, -31990, -31900, -31970), seconds=0.3, wait=True)    # off screen: nothing to see
    assert time.time() - t0 < 2


# ---------------------------------------------------------------------------
# clicking and filling by name
# ---------------------------------------------------------------------------
def test_find_control_uses_the_front_window():
    hit = hands.find_control("the export button", read=lambda hwnd: SCREEN, front=lambda: (7, "Report - Excel"))
    assert hit[0].name == "Export" and hit[1] == (7, "Report - Excel")
    assert hands.find_control("export", read=lambda hwnd: [], front=lambda: (7, "x")) is None
    assert hands.find_control("export", read=lambda hwnd: SCREEN, front=lambda: (0, "")) is None
    assert hands.find_control("export", read=lambda hwnd: 1 / 0, front=lambda: (7, "x")) is None   # falls back


def test_review_control_safety():
    assert hands.review_control(SCREEN[1], "export", (7, "x"))["label"] == "Export"
    with pytest.raises(hands.TargetError, match="yours to press"):
        hands.review_control(SCREEN[5], "delete my account", (7, "x"))
    with pytest.raises(hands.TargetError, match="greyed out"):
        hands.review_control(SCREEN[7], "save", (7, "x"))
    assert hands.review_control(C("Send money - Gmail", "tab"), "gmail", (7, "x"))["kind"] == "tab"


def test_click_control_checks_its_still_there():
    spot = hands.review_control(SCREEN[1], "export", (7, "Excel"))
    fh = FakeHands(windows=lambda x, y: (7, "Excel"))
    assert hands.click_control(spot, (7, "Excel"), fh, still_at=lambda c: True) == "Done, I clicked Export."
    assert fh.actions == [("click", 140, 115)]
    fh = FakeHands(windows=lambda x, y: (7, "Excel"))
    assert "isn't where it was" in hands.click_control(spot, (7, "Excel"), fh, still_at=lambda c: False)
    fh = FakeHands(windows=lambda x, y: (9, "Other"))
    assert "isn't where it was" in hands.click_control(spot, (7, "Excel"), fh, still_at=lambda c: True)
    assert fh.actions == []


FORM = [C("Full name", "edit", (100, 100, 400, 120)), C("Email", "edit", (100, 140, 400, 160)),
        C("Password", "edit", (100, 180, 400, 200), password=True), C("Card number", "edit", (100, 220, 400, 240)),
        C("City", "edit", (100, 260, 400, 280), value="Tehran"), C("Submit", "button")]


def test_plan_form_by_name():
    asked = []

    def ask_text(system, user, max_tokens):
        asked.append(user)
        return '{"fields": [{"id": 1, "value": "Erfan Zamani"}, {"id": 3, "value": "hunter2"}, {"id": 5, "value": "Tehran"},' \
               ' {"id": 4, "value": "4111 1111 1111 1111"}, {"id": 99, "value": "x"}]}'
    plan = hands.plan_form_uia(ask_text, "fill this out", ["Erfan's name is Erfan Zamani"], (7, "Signup"), FORM)
    assert "1. Full name" in asked[0] and "3. Password (password)" in asked[0] and "(now: 'Tehran')" in asked[0]
    todo, skipped, missing = hands.review_uia(plan)
    assert [f["label"] for f in todo] == ["Full name"] and todo[0]["value"] == "Erfan Zamani"
    assert ("Password", "private") in skipped and ("Card number", "private") in skipped
    assert ("City", "already filled in") in skipped and missing == ["Email"]
    assert hands.plan_form_uia(ask_text, "x", [], (7, "x"), [C("Go")]) is None      # no fields: use the screenshot


def test_fill_by_name_checks_each_field():
    todo = [{"label": "Full name", "control": FORM[0], "current": "", "value": "Erfan Zamani"},
            {"label": "City", "control": FORM[4], "current": "Tehran", "value": "Shiraz"}]
    fh = FakeHands(windows=lambda x, y: (7, "Signup"))
    typed = lambda c: next(a[1] for a in reversed(fh.actions) if a[0] == "type")   # noqa: E731
    done, stopped = hands.fill_uia(todo, hands=fh, window=(7, "Signup"), value_of=typed, still_at=lambda c: True)
    assert done == ["Full name", "City"] and stopped is None
    assert fh.actions == [("click", 250, 110), ("type", "Erfan Zamani"), ("click", 250, 270), ("select_all",),
                          ("type", "Shiraz")]
    fh = FakeHands(windows=lambda x, y: (7, "Signup"))
    done, stopped = hands.fill_uia(todo, hands=fh, window=(7, "Signup"), value_of=lambda c: "something else",
                                   still_at=lambda c: True)
    assert done == [] and "doesn't look right" in stopped
    done, stopped = hands.fill_uia(todo, hands=FakeHands(windows=lambda x, y: (9, "Other")), window=(7, "Signup"),
                                   value_of=typed, still_at=lambda c: True)
    assert done == [] and "another window" in stopped
    done, stopped = hands.fill_uia(todo, hands=FakeHands(windows=lambda x, y: (7, "Signup")), window=(7, "Signup"),
                                   value_of=typed, still_at=lambda c: False)
    assert done == [] and "page changed" in stopped
    fh = FakeHands(stop_at=2, windows=lambda x, y: (7, "Signup"))
    fh_typed = lambda c: next(a[1] for a in reversed(fh.actions) if a[0] == "type")   # noqa: E731
    done, stopped = hands.fill_uia(todo, hands=fh, window=(7, "Signup"), value_of=fh_typed, still_at=lambda c: True)
    assert done == ["Full name"] and stopped == "you pressed Esc"


# ---------------------------------------------------------------------------
# words that start each one
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, yes", [
    ("where's the export button?", True), ("Where is the settings icon", True), ("show me where the search box is", True),
    ("point to the save button", True), ("where is Paris", False), ("how do I find the export button", False),
    ("where's my resume file", False), ("click the export button", False),
])
def test_wants_where(text, yes):
    assert hands.wants_where(text) is yes


@pytest.mark.parametrize("text, target", [
    ("where's the share button?", "the share button"), ("can you click on the Docs tab please", "the Docs tab"),
    ("show me where the search box is", "the search box"), ("Export", "Export"),
])
def test_clean_target(text, target):
    assert hands.clean_target(text) == target


@pytest.mark.parametrize("text, task", [
    ("take care of this: open YouTube and search for lo-fi", "open YouTube and search for lo-fi"),
    ("NUBB-E, handle this for me: start a 10 minute timer and open Spotify", "start a 10 minute timer and open Spotify"),
    ("agent mode: add milk to my list", "add milk to my list"),
    ("take care of yourself", None), ("can you take care of it", None), ("what is agent mode", None),
])
def test_agent_task(text, task):
    assert hands.agent_task(text) == task


# ---------------------------------------------------------------------------
# agent planning
# ---------------------------------------------------------------------------
ALLOWED = hands.agent_tools(app.actions.SCHEMAS)


def plan_from(json_text):
    return hands.plan_agent(lambda s, u, n: json_text, "do it", ALLOWED)


def test_plan_agent_checks_every_step():
    steps, why = plan_from('{"steps": [{"tool": "open_website", "args": {"site": "youtube", "search": "lo-fi"}, "say": "Open YouTube"},'
                           ' {"tool": "wait", "args": {"seconds": 99}}, {"tool": "click_on", "args": {"target": "first video"}, "say": "Play the first video"}]}')
    assert [s["tool"] for s in steps] == ["open_website", "wait", "click_on"] and why == ""
    assert steps[1]["args"] == {"seconds": 10.0}                         # waits are capped
    assert plan_from('{"steps": [{"tool": "run_command", "args": {"command": "format c:"}}]}')[0] == []
    assert plan_from('{"steps": [{"tool": "click_on", "args": {"target": "Buy"}, "say": "Press Buy now"}]}')[0] == []
    assert plan_from('{"steps": [], "why": "I cannot send emails"}') == ([], "I cannot send emails")
    many = ",".join(['{"tool": "set_volume", "args": {"level": 10}}'] * 9)
    assert plan_from('{"steps": [' + many + ']}')[0] == []
    assert "dictation_mode" not in ALLOWED and "write_file" not in ALLOWED and "run_command" not in ALLOWED


def test_agent_card():
    card = hands.agent_card("open youtube", [{"tool": "open_website", "args": {}, "say": "Open YouTube"}])
    assert card["title"] == "Take care of this" and "1. Open YouTube" in card["detail"] and "Esc stops me" in card["detail"]


# ---------------------------------------------------------------------------
# in the app
# ---------------------------------------------------------------------------
@pytest.fixture
def screen(monkeypatch, base):
    """Hands on, and a front window with SCREEN's controls (fakes: nothing real is read, clicked or ringed)."""
    monkeypatch.setattr(app, "HANDS", True)
    seen = {"rings": [], "clicks": []}
    monkeypatch.setattr(hands, "front_controls", lambda read=None, front=None: ((7, "Report - Excel"), SCREEN))
    monkeypatch.setattr(app.uia, "ring", lambda rect, seconds=4, wait=False: seen["rings"].append(rect))
    monkeypatch.setattr(hands, "click_control", lambda spot, window: seen["clicks"].append(spot["label"])
                        or f"Done, I clicked {spot['label']}.")
    monkeypatch.setattr(hands, "plan_click", lambda ask, target: (_ for _ in ()).throw(AssertionError("no screenshot needed")))
    return seen


def test_where_is_rings_without_clicking(screen, base, fake, data_dir):
    events = chat_answering(base, "where's the export button?")
    assert screen["rings"] == [(100, 100, 180, 130)] and screen["clicks"] == []
    assert reply_of(events) == "There it is: I put an orange ring around Export (button) for a few seconds."
    assert "confirm" not in [e["type"] for e in events] and chat_requests(fake) == []
    assert '"outcome": "found (by name)"' in (data_dir / "tools.log").read_text(encoding="utf-8")


def test_where_is_not_found(screen, base, fake, monkeypatch):
    monkeypatch.setattr(hands, "plan_click", lambda ask, target: {"found": False})   # the screenshot can't see it either
    events = chat_answering(base, "where's the share button?")
    assert screen["rings"] == [] and reply_of(events) == "I can't see the share button on the screen."


def test_click_by_name_asks_first(screen, base, fake):
    events = chat_answering(base, "click the export button", answer=True)
    card = next(e for e in events if e["type"] == "confirm")
    assert card["detail"] == "Export (button)" and card["target"] == 'in "Report - Excel"'
    assert screen["clicks"] == ["Export"] and reply_of(events) == "Done, I clicked Export."


def test_never_presses_delete_by_name(screen, base, fake):
    events = chat_answering(base, "click the delete account button", answer=True)
    assert screen["clicks"] == [] and "yours to press" in reply_of(events)


def test_fill_by_name_in_the_app(base, fake, monkeypatch, data_dir):
    monkeypatch.setattr(app, "HANDS", True)
    monkeypatch.setattr(hands, "front_controls", lambda read=None, front=None: ((7, "Signup"), FORM))
    monkeypatch.setattr(app, "ask_text", lambda s, u, n: '{"fields": [{"id": 1, "value": "Erfan Zamani"}]}')
    filled = []

    def fill_uia(todo, progress, log, window=None):
        for f in todo:
            progress(f"filling: {f['label']}")
            log(f, "ok")
            filled.append((f["label"], f["value"]))
        return [f["label"] for f in todo], None
    monkeypatch.setattr(hands, "fill_uia", fill_uia)
    monkeypatch.setattr(hands, "plan_form", lambda *a: (_ for _ in ()).throw(AssertionError("no screenshot needed")))
    events = chat_answering(base, "fill this out for me", answer=True)
    card = next(e for e in events if e["type"] == "confirm")
    assert "Full name: Erfan Zamani" in card["detail"] and card["target"].startswith('in "Signup"')
    assert filled == [("Full name", "Erfan Zamani")]
    assert reply_of(events).startswith("Done! I filled in Full name.")
    assert "Erfan Zamani" not in (data_dir / "tools.log").read_text(encoding="utf-8")


def test_fill_by_name_refuses_terminals(base, fake, monkeypatch):
    monkeypatch.setattr(app, "HANDS", True)
    monkeypatch.setattr(hands, "front_controls", lambda read=None, front=None: ((7, "Windows PowerShell"), FORM))
    events = chat_answering(base, "fill this out", answer=True)
    assert "confirm" not in [e["type"] for e in events] and "terminal or code editor" in reply_of(events)


@pytest.fixture
def agent_plan(monkeypatch):
    plan = {"steps": [{"tool": "set_timer", "args": {"duration": "10 minutes"}, "say": "Start a 10 minute timer"},
                      {"tool": "wait", "args": {"seconds": 0.5}, "say": "Wait a moment"},
                      {"tool": "add_todo", "args": {"item": "stretch"}, "say": "Add stretch to your list"}], "why": ""}
    monkeypatch.setattr(hands, "plan_agent", lambda ask, task, allowed: (plan["steps"], plan["why"]))
    return plan


def test_agent_runs_the_plan_after_yes(base, fake, agent_plan):
    events = chat_answering(base, "take care of this: start a 10 minute timer and add stretch to my list", answer=True)
    card = next(e for e in events if e["type"] == "confirm")
    assert card["title"] == "Take care of this" and "1. Start a 10 minute timer" in card["detail"]
    assert [e["text"] for e in events if e["type"] == "tool"][1:] == [
        "step 1 of 3: Start a 10 minute timer", "step 2 of 3: Wait a moment", "step 3 of 3: Add stretch to your list"]
    assert reply_of(events) == "1. Timer set for 10 minutes.\n3. Added stretch to your list."
    assert app.rows("SELECT text FROM todos") == [{"text": "stretch"}] and len(app.rows("SELECT id FROM reminders")) == 1
    assert chat_requests(fake) == []        # straight to agent mode, the plan itself is from plan_agent


def test_agent_no_means_nothing_runs(base, fake, agent_plan):
    events = chat_answering(base, "take care of this: add stretch to my list", answer=False)
    assert reply_of(events) == "Okay, I didn't do anything." and app.rows("SELECT id FROM todos") == []


def test_agent_stops_when_a_step_fails(base, fake, agent_plan):
    agent_plan["steps"].insert(0, {"tool": "cancel_reminder", "args": {"which": "nothing like this"}, "say": "Cancel it"})
    events = chat_answering(base, "take care of this: tidy up my timers", answer=True)
    assert "I stopped there" in reply_of(events) and app.rows("SELECT id FROM todos") == []


def test_agent_with_no_plan_says_why(base, fake, agent_plan):
    agent_plan.update(steps=[], why="sending emails is yours to do")
    events = chat_answering(base, "take care of this: email my boss", answer=True)
    assert reply_of(events) == "I can't take care of that one: sending emails is yours to do."
    assert "confirm" not in [e["type"] for e in events]


def test_agent_stops_on_esc(base, fake, agent_plan, monkeypatch):
    presses = iter([False, False, True] + [True] * 50)   # (old press cleared), step 1 runs, then Esc
    monkeypatch.setattr(hands, "esc_pressed", lambda: next(presses))
    events = chat_answering(base, "take care of this: timer and list", answer=True)
    assert "because you pressed Esc" in reply_of(events) and app.rows("SELECT id FROM todos") == []


def test_the_brain_cant_start_agent_mode_itself(base, fake, data_dir):
    fake.plan = [("agent_task", {"task": "delete all my files"})]
    chat_answering(base, "hmm what should I do today")
    assert '"tool": "agent_task"' in (data_dir / "tools.log").read_text(encoding="utf-8")
    assert "not offered" in (data_dir / "tools.log").read_text(encoding="utf-8")


def test_where_is_offered_to_the_brain(screen, base, fake):
    fake.plan = [("where_is", {"target": "the export button"})]
    chat_answering(base, "I can never find how to export this report")
    assert "where_is" in [t["function"]["name"] for t in chat_requests(fake)[0]["tools"]]
    assert screen["rings"] == [(100, 100, 180, 130)]
