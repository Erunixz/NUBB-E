"""Tests for NUBB-E's hands (nubbe_hands.py): planning, the safety rules, filling, stopping.

    .venv\\Scripts\\python -m pytest tests -q

The real mouse and keyboard are never used: a fake pair of hands records what would happen.
"""
import threading

import pytest

from test_app import app, chat_requests, data_dir, fake, get, post, reply_of, serve, base, wait_for  # noqa: F401
from test_tools import chat_answering

hands = app.hands

PLAN = {
    "fields": [
        {"label": "Full name", "kind": "text", "box": [265, 170, 735, 215], "current": "", "value": "Erfan Zamani"},
        {"label": "Email address", "kind": "text", "box": [265, 278, 735, 322], "current": "", "value": None},
        {"label": "Password", "kind": "password", "box": [265, 493, 735, 537], "current": "", "value": "hunter2"},
        {"label": "Why do you want to join?", "kind": "textarea", "box": [265, 600, 735, 725], "current": "",
         "value": "I love robots.\nAnd building things."},
        {"label": "Country", "kind": "select", "box": [265, 740, 735, 770], "current": "", "value": "Iran"},
    ],
    "buttons": [{"label": "Submit", "box": [265, 775, 375, 830]}],
}


class FakeHands:
    """Records clicks and typing. stop_at: raise Stopped on that click (1 = first)."""

    def __init__(self, stop_at=None, windows=None):
        self.actions, self.clicks, self.stop_at = [], 0, stop_at
        self.windows = windows or (lambda x, y: (7, "Robotics Club Signup"))

    def screen(self):
        return 0, 0, 1920, 1080

    point = hands.Hands.point

    def window_at(self, x, y):
        return self.windows(x, y)

    def check_stop(self):
        pass

    def click(self, x, y):
        self.clicks += 1
        if self.clicks == self.stop_at:
            raise hands.Stopped("you pressed Esc")
        self.actions.append(("click", x, y))

    def select_all(self):
        self.actions.append(("select_all",))

    def type(self, text):
        self.actions.append(("type", text))


@pytest.fixture
def typed_back(monkeypatch):
    """The close-up check reads back exactly what was typed (the last 'type' action)."""
    state = {"hands": None, "override": None}
    monkeypatch.setattr(hands, "check", lambda ask, f: state["override"] or
                        next(a[1] for a in reversed(state["hands"].actions) if a[0] == "type"))
    return state


# ---------------------------------------------------------------------------
# planning and the safety rules
# ---------------------------------------------------------------------------
def test_parse_json_from_a_chatty_answer():
    assert hands.parse_json('Sure!\n```json\n{"fields": [], "buttons": []}\n```') == {"fields": [], "buttons": []}
    with pytest.raises(ValueError):
        hands.parse_json("I can't see a form.")


def test_review_applies_the_safety_rules():
    todo, skipped, missing = hands.review(PLAN)
    assert [f["label"] for f in todo] == ["Full name", "Why do you want to join?"]
    assert skipped == [("Password", "private"), ("Country", "I can't do dropdowns or checkboxes yet")]
    assert missing == ["Email address"]


@pytest.mark.parametrize("field", [
    {"label": "Card number", "kind": "text", "value": "4000 0000 0000 0002"},
    {"label": "CVV", "kind": "text", "value": "123"},
    {"label": "Passport number", "kind": "text", "value": "X1234567"},
    {"label": "Notes", "kind": "text", "value": "pay with 4111 1111 1111 1111"},   # a card number by any name
    {"label": "Security code", "kind": "text", "value": "999"},
    {"label": "Bank account number", "kind": "text", "value": "12345678"},
])
def test_private_fields_are_never_filled(field):
    todo, skipped, _ = hands.review({"fields": [{**field, "box": [100, 100, 500, 150]}]})
    assert todo == [] and skipped == [(field["label"], "private")]


def test_review_refuses_buttons_bad_boxes_and_done_fields():
    plan = {"fields": [
        {"label": "Name", "kind": "text", "box": [300, 790, 340, 810], "value": "Erfan"},       # on Submit
        {"label": "City", "kind": "text", "box": [0, 0, 2000, 10], "value": "x"},              # not 0 to 1000
        {"label": "Nick", "kind": "text", "box": [100, 100, 500, 150], "value": "E", "current": " e "},
        "junk",
    ], "buttons": [{"label": "Submit", "box": [265, 775, 375, 830]}, {"box": "bad"}]}
    todo, skipped, missing = hands.review(plan)
    assert todo == [] and missing == []
    assert skipped == [("Name", "too close to a button"), ("City", "couldn't tell where it is"),
                       ("Nick", "already filled in")]


def test_card_shows_the_whole_plan():
    c = hands.card(*hands.review(PLAN))
    assert c["title"] == "Fill in this form"
    assert c["target"] == "2 fields, one at a time. Esc or moving the mouse stops me."
    assert c["detail"].splitlines()[:2] == ["Full name: Erfan Zamani", "Why do you want to join?: I love robots."]
    assert "I won't fill: Password (private)" in c["detail"]
    assert "I don't know: Email address" in c["detail"]
    assert c["detail"].endswith("I won't press Submit or Send. That part is yours.")
    assert "hunter2" not in c["detail"]


def test_one_line_fields_never_get_enter():
    assert hands.text_for({"kind": "text", "value": "a\nb\tc"}) == "a b c"
    assert hands.text_for({"kind": "textarea", "value": "a\r\nb"}) == "a\nb"   # typed as Shift+Enter


@pytest.mark.parametrize("seen, expected, ok", [
    ("Erfan Zamani", "Erfan Zamani", True),
    ("erfan  zamani", "Erfan Zamani", True),
    ("Erfan Zamanl", "Erfan Zamani", True),                        # a misread letter
    ("I love robots. And building", "I love robots.\nAnd building things.", True),   # only part shows
    ("...building things.", "I love robots. And building things.", True),           # scrolled to the end
    ("EMPTY", "Erfan Zamani", False),
    ("", "Erfan", False),
    ("Robotics Club Signup", "Erfan Zamani", False),
])
def test_matches(seen, expected, ok):
    assert hands.matches(seen, expected) is ok


def test_report_in_plain_words():
    todo, skipped, missing = hands.review(PLAN)
    assert hands.report(["Full name"], "you pressed Esc", todo, skipped, missing) == (
        "I filled in Full name, then stopped because you pressed Esc. Still to do: Why do you want to join?. "
        "I didn't fill Password (private) and Country (I can't do dropdowns or checkboxes yet). "
        "I still need your Email address: tell me and I'll add it. Submit is all yours.")
    assert hands.report(["Full name", "Why do you want to join?"], None, todo, [], []) == (
        "Done! I filled in Full name and Why do you want to join?. Submit is all yours.")
    assert hands.report([], None, [], [], ["Email", "Phone"]) == "I still need your Email and Phone: tell me and I'll add them."
    assert hands.report([], None, [], [], []) == "I couldn't find any fields to fill on the screen."


@pytest.mark.parametrize("text, yes", [
    ("Fill this out for me", True), ("can you fill in the form?", True), ("my email is a@b.c, fill that in", True),
    ("fill out this application", True), ("How do I fill out a tax form?", False), ("fill the car with gas", False),
])
def test_wants_fill(text, yes):
    assert hands.wants_fill(text) is yes


def test_target_window_must_be_one_safe_window():
    todo, _, _ = hands.review(PLAN)
    assert hands.target_window(todo, FakeHands()) == (7, "Robotics Club Signup")
    for title in ("nubbe_app.py - NUBB-E - Visual Studio Code", "Windows PowerShell", "Command Prompt", "NUBB-E"):
        with pytest.raises(hands.TargetError, match="terminal or code editor"):
            hands.target_window(todo, FakeHands(windows=lambda x, y, t=title: (3, t)))
    with pytest.raises(hands.TargetError, match="one window"):
        hands.target_window(todo, FakeHands(windows=lambda x, y: (1 if y < 500 else 2, "A")))
    with pytest.raises(hands.TargetError, match="one window"):
        hands.target_window(todo, FakeHands(windows=lambda x, y: (0, "")))


# ---------------------------------------------------------------------------
# filling
# ---------------------------------------------------------------------------
def test_fill_clicks_types_and_checks_each_field(typed_back):
    todo, _, _ = hands.review(PLAN)
    todo[1]["current"] = "old text"
    fake_hands = typed_back["hands"] = FakeHands()
    lines, logged = [], []
    done, stopped = hands.fill(todo, None, lines.append, lambda f, o: logged.append((f["label"], o)), fake_hands,
                               window=(7, "Robotics Club Signup"))
    assert done == ["Full name", "Why do you want to join?"] and stopped is None
    assert fake_hands.actions == [
        ("click", 960, 208), ("type", "Erfan Zamani"),          # box center (500, 192.5) on a 1920x1080 screen
        ("click", 960, 716), ("select_all",), ("type", "I love robots.\nAnd building things."),
    ]
    assert lines == ["filling: Full name", "filling: Why do you want to join?"]
    assert logged == [("Full name", "ok"), ("Why do you want to join?", "ok")]


def test_esc_stops_at_once(typed_back):
    todo, _, _ = hands.review(PLAN)
    fake_hands = typed_back["hands"] = FakeHands(stop_at=2)
    logged = []
    done, stopped = hands.fill(todo, None, log=lambda f, o: logged.append(o), hands=fake_hands)
    assert done == ["Full name"] and stopped == "you pressed Esc"
    assert [a[0] for a in fake_hands.actions] == ["click", "type"]      # nothing typed after Esc
    assert logged == ["ok", "stopped: you pressed Esc"]


def test_another_window_in_the_way_stops_it(typed_back):
    todo, _, _ = hands.review(PLAN)
    fake_hands = typed_back["hands"] = FakeHands(windows=lambda x, y: (7, "Form") if y < 500 else (9, "Chat app"))
    done, stopped = hands.fill(todo, None, hands=fake_hands, window=(7, "Form"))
    assert done == ["Full name"] and stopped == "'Why do you want to join?' isn't in \"Form\" anymore (another window is there)"
    assert [a[0] for a in fake_hands.actions] == ["click", "type"]


def test_a_wrong_looking_field_stops_everything(typed_back):
    todo, _, _ = hands.review(PLAN)
    fake_hands = typed_back["hands"] = FakeHands()
    typed_back["override"] = "Robotics Club Signup"      # it typed somewhere else
    done, stopped = hands.fill(todo, None, hands=fake_hands)
    assert done == [] and "doesn't look right" in stopped
    assert len([a for a in fake_hands.actions if a[0] == "click"]) == 1


# ---------------------------------------------------------------------------
# in the chat
# ---------------------------------------------------------------------------
@pytest.fixture
def on(monkeypatch, base):
    """Hands on, with a fake screen: plan_form returns PLAN, fill records instead of typing."""
    monkeypatch.setattr(app, "HANDS", True)
    seen = {"plans": [], "fills": []}

    def plan_form(ask, request, facts):
        seen["plans"].append((request, facts))
        return seen.get("plan", PLAN)

    def fill(todo, ask, progress, log, window=None):
        seen["window"] = window
        seen["fills"].append([f["label"] for f in todo])
        for f in todo:
            progress(f"filling: {f['label']}")
            log(f, "ok")
        return [f["label"] for f in todo], None
    monkeypatch.setattr(hands, "plan_form", plan_form)
    monkeypatch.setattr(hands, "fill", fill)
    monkeypatch.setattr(hands, "target_window", lambda todo: (7, "Robotics Club Signup"))
    return seen


def test_fill_this_out_after_yes(on, base, fake, data_dir):
    post(base, "/api/memory", {"text": "Erfan's favorite language is Python"})
    fake.plan = [("fill_form", {"request": "fill this out", "details": "He wants to join the robotics club"})]
    events = chat_answering(base, "Please complete the signup on my screen", answer=True)
    assert "fill_form" in [t["function"]["name"] for t in chat_requests(fake)[0]["tools"]]
    assert "Your hands" in chat_requests(fake)[0]["messages"][0]["content"]
    request, facts = on["plans"][0]
    assert request == "fill this out"
    assert facts == ["Erfan's favorite language is Python", "Erfan just said: Please complete the signup on my screen",
                     "From this chat: He wants to join the robotics club"]
    card = next(e for e in events if e["type"] == "confirm")
    assert card["title"] == "Fill in this form" and "Full name: Erfan Zamani" in card["detail"]
    assert card["target"].startswith('in "Robotics Club Signup": 2 fields') and on["window"] == (7, "Robotics Club Signup")
    assert on["fills"] == [["Full name", "Why do you want to join?"]]
    assert [e["text"] for e in events if e["type"] == "tool"] == [
        "looking at the form", "filling: Full name", "filling: Why do you want to join?"]
    # the app says what happened itself, word for word: no extra brain round to retell it
    assert len(chat_requests(fake)) == 1
    assert reply_of(events) == ("Done! I filled in Full name and Why do you want to join?. I didn't fill Password "
                                "(private) and Country (I can't do dropdowns or checkboxes yet). I still need your "
                                "Email address: tell me and I'll add it. Submit is all yours.")
    log = (data_dir / "tools.log").read_text(encoding="utf-8")
    assert '"tool": "fill_field"' in log and "Erfan Zamani" not in log      # values aren't logged


def test_no_means_nothing_is_typed(on, base, fake):
    fake.plan = [("fill_form", {"request": "fill this out"})]
    events = chat_answering(base, "Fill this out", answer=False)
    assert on["fills"] == [] and reply_of(events) == "Okay, I didn't type anything."


def test_nothing_to_fill_asks_instead_of_showing_a_card(on, base, fake):
    on["plan"] = {"fields": [{"label": "Email", "kind": "text", "box": [1, 1, 9, 9], "value": None}]}
    fake.plan = [("fill_form", {"request": "fill this"})]
    events = chat_answering(base, "Fill this", answer=True)
    assert "confirm" not in [e["type"] for e in events] and on["fills"] == []
    assert reply_of(events) == "I still need your Email: tell me and I'll add it."


def test_fill_this_out_goes_straight_to_the_hands(on, base, fake):
    events = chat_answering(base, "Fill this out for me", answer=True)    # the brain never chose a tool
    assert chat_requests(fake) == [] and on["fills"] == [["Full name", "Why do you want to join?"]]
    assert reply_of(events).startswith("Done! I filled in Full name")
    assert get(base, f"/api/chats/{events[0]['chat_id']}")[-1]["content"] == reply_of(events)


def test_refuses_terminals_and_editors(on, base, fake, monkeypatch):
    def nope(todo):
        raise hands.TargetError('"Windows PowerShell" looks like a terminal or code editor, and typing there could run commands')
    monkeypatch.setattr(hands, "target_window", nope)
    events = chat_answering(base, "Fill this out", answer=True)
    assert "confirm" not in [e["type"] for e in events] and on["fills"] == []
    assert reply_of(events).startswith('I won\'t type there: "Windows PowerShell" looks like a terminal')


def test_voice_card_pops_up_the_quick_bar(on, base, fake, monkeypatch):
    popped, sent = [], []
    monkeypatch.setitem(app.APP_HOOKS, "show_quick", lambda: popped.append(1))
    real = app.broadcast
    monkeypatch.setattr(app, "broadcast", lambda m: (sent.append(m), real(m)))
    fake.plan = [("fill_form", {"request": "fill this out"})]

    def press_yes_in_the_bar():
        assert wait_for(lambda: app.PENDING)
        post(base, "/api/confirm", {"id": next(iter(app.PENDING)), "ok": True})
    threading.Thread(target=press_yes_in_the_bar).start()
    chat_answering(base, "Fill this out", voice=True, source="call")     # the voice helper never answers cards
    assert popped == [1] and on["fills"] == [["Full name", "Why do you want to join?"]]
    assert [m["event"] for m in sent if m.get("event") in ("confirm", "confirmed")] == ["confirm", "confirmed"]


def test_hands_off_without_vision(base, fake):
    chat_answering(base, "Fill this out")
    assert "fill_form" not in str(chat_requests(fake)[-1].get("tools"))
