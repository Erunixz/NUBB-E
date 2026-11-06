"""Tests for dictation and Write: cleaning up what was said, Write commands, the hold key, the whole
press -> talk -> paste flow, the voice helper's side and the app's side. Nothing here touches the real
mic, keyboard, clipboard or screen: they are all fakes.

    .venv\\Scripts\\python -m pytest tests -q
"""
import base64
import io
import json
import queue
import threading
import time
import urllib.error

import pytest

from test_app import app, data_dir, get, post, serve  # noqa: F401
import nubbe_actions as actions
import nubbe_dictate as dictate

WORDS = {"nubby": "NUBB-E", "erfan": "Erfan"}
SNIPPETS = {"address": "12 Main St, Springfield", "work email": "erfan@work.com"}


# ---------------------------------------------------------------------------
# clean mode
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("heard, out", [
    ("um so I I think we should uh meet tomorrow.", "So I think we should meet tomorrow."),
    ("Hmm, okay, erm, that works.", "Okay, that works."),
    ("Let's meet tomorrow. Scratch that. Let's meet Friday.", "Let's meet Friday."),
    ("Hi John. Let's meet tomorrow, scratch that.", "Hi John."),
    ("Let's meet at 5, no I mean 6.", "Let's meet at 6."),
    ("Send it to John, no, I mean to Sarah.", "Send it to Sarah."),
    ("I'll come Tuesday, no I mean on Wednesday.", "I'll come on Wednesday."),
    ("Buy two apples, make that three apples.", "Buy three apples."),
    ("Make that happen by Friday.", "Make that happen by Friday."),
    ("Shopping list bullet milk. Bullet eggs. Bullet bread.", "Shopping list\n- Milk\n- Eggs\n- Bread"),
    ("Steps number one open the app number two click save", "Steps\n1. Open the app\n2. Click save"),
    ("He was number one in class.", "He was number one in class."),
    ("A bullet hit the wall.", "A bullet hit the wall."),
    ("Hello comma how are you question mark", "Hello, how are you?"),
    ("Hello, comma, world.", "Hello, world."),
    ("See you at noon period new line Thanks", "See you at noon.\nThanks"),
    ("Dear Sam, new paragraph, thanks for coming.", "Dear Sam,\n\nThanks for coming."),
    ("That was a period of change.", "That was a period of change."),
    ("Wow exclamation mark", "Wow!"),
    ("go to github dot com", "Go to github.com"),
    ("i think i'm done", "I think I'm done"),
    ("nubby is cool and erfan agrees", "NUBB-E is cool and Erfan agrees"),
    ("please insert my address thanks", "Please 12 Main St, Springfield thanks"),
    ("Insert work email.", "erfan@work.com"),
    ("Insert my phone number.", "Insert my phone number."),           # no such snippet: left alone
])
def test_clean(heard, out):
    assert dictate.tidy(heard, "clean", WORDS, SNIPPETS) == out


@pytest.mark.parametrize("text", [
    "Check https://example.com/a_b?x=1, it's at 10:30 and costs $3.50.",
    "email me at erfan.z@gmail.com about config.toml",
    "the file is in C:\\Users\\Erfan\\notes.txt and ~/projects/nubbe/run.sh",
    "call get_user_name() and getValue() with `x = 1 1`",
    "it's 2026-11-05, version 3.12.1, 50% off, 555-1234",
])
def test_links_emails_paths_code_and_numbers_never_change(text):
    out = dictate.tidy(text, "clean")
    for token in dictate.PROTECT.finditer(text):
        assert token.group().rstrip(".,") in out


def test_exact_is_as_heard():
    assert dictate.tidy("um  so  bullet milk bullet eggs  ", "exact") == "um so bullet milk bullet eggs"


def test_abbreviations_dont_end_sentences():
    assert dictate.tidy("bring snacks, e.g. chips and dips", "clean") == "Bring snacks, e.g. chips and dips"


# ---------------------------------------------------------------------------
# polished checks and brain answers
# ---------------------------------------------------------------------------
def test_polish_ok():
    assert dictate.polish_ok("Their going to the park at 5.", "They're going to the park at 5.")
    assert not dictate.polish_ok("Meet me at 10:30.", "Meet me at 11:30.")                  # a number changed
    assert not dictate.polish_ok("see github.com/erfan", "see github.com/someone-else")      # a link changed
    assert not dictate.polish_ok("Can you send the file?", "Sure! Here is the file you asked for, with notes.")
    assert not dictate.polish_ok("hello there", "")


def test_unwrap():
    assert dictate.unwrap('Here is the text:\n"Hi there"') == "Hi there"
    assert dictate.unwrap("```\nHello\n```") == "Hello"
    assert dictate.unwrap("“Quoted”") == "Quoted"
    assert dictate.unwrap("Just text.") == "Just text."


def test_polish_prompt_lists_dictionary_words():
    assert "NUBB-E" in dictate.polish_prompt(WORDS) and "Spell" not in dictate.polish_prompt({})


# ---------------------------------------------------------------------------
# Write commands
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("said, cmd", [
    ("make this shorter", ("rewrite", "make this shorter")),
    ("Make it more formal.", ("rewrite", "Make it more formal")),
    ("Can you rewrite this to sound friendlier?", ("rewrite", "Can you rewrite this to sound friendlier?")),
    ("NUBB-E, fix the grammar", ("rewrite", "fix the grammar")),
    ("translate this to Spanish", ("rewrite", "translate this to Spanish")),
    ("write a reply saying I'll be late", ("write", "write a reply saying I'll be late")),
    ("Draft a polite email to my landlord about the heating.", ("write", "Draft a polite email to my landlord about the heating")),
    ("Make it happen by Friday.", None),
    ("Write the report by Friday.", None),
    ("I need to make this work.", None),
    ("Rewriting history is hard.", None),
])
def test_write_command(said, cmd):
    assert dictate.write_command(said) == cmd


# ---------------------------------------------------------------------------
# the hold key
# ---------------------------------------------------------------------------
def test_parse_hold_key():
    assert dictate.parse_hold_key("Right Alt") == 0xA5 and dictate.parse_hold_key("altgr") == 0xA5
    assert dictate.parse_hold_key("right_ctrl") == 0xA3 and dictate.parse_hold_key("F8") == 0x77
    assert dictate.parse_hold_key("") is None and dictate.parse_hold_key("off") is None
    with pytest.raises(ValueError):
        dictate.parse_hold_key("ctrl+space")


class Keys:
    def __init__(self):
        self.down = set()

    def __call__(self, vk):
        return vk in self.down


def make_key(vk=0xA5):
    keys, events, masks = Keys(), [], []
    hk = dictate.HoldKey(vk, lambda: events.append("press"), lambda held, why: events.append(("release", why)),
                         is_down=keys, tap_mask=lambda: masks.append(1))
    return hk, keys, events, masks


def test_hold_key_press_and_release():
    hk, keys, events, masks = make_key()
    state = {}
    hk.step(state)
    assert events == []
    keys.down.add(0xA5)
    hk.step(state)
    hk.step(state)                     # still held: no second press
    assert events == ["press"] and masks == [1]    # Alt's menu is masked right away
    keys.down.discard(0xA5)
    hk.step(state)
    assert events == ["press", ("release", None)]


def test_hold_key_esc_or_a_shortcut_cancels():
    for other, why in ((dictate.VK_ESCAPE, "esc"), (0x09, "shortcut")):    # Esc, Alt+Tab
        hk, keys, events, _ = make_key()
        state = {}
        keys.down.add(0xA5)
        hk.step(state)
        keys.down.add(other)
        hk.step(state)
        keys.down.clear()
        hk.step(state)
        assert events == ["press", ("release", why)]


def test_hold_key_ignores_keys_already_down_and_altgr_ctrl():
    hk, keys, events, _ = make_key()
    state = {}
    keys.down.update({0x41})           # A was already held before
    keys.down.add(0xA5)
    hk.step(state)
    keys.down.update({0xA2, 0x11})     # AltGr also reports Left Ctrl
    hk.step(state)
    keys.down.clear()
    hk.step(state)
    assert events == ["press", ("release", None)]


def test_no_mask_for_other_keys():
    hk = dictate.HoldKey(0x77, lambda: None, lambda h, w: None, is_down=Keys())
    assert hk.tap_mask() is None       # F8 needs no menu mask (and nothing was sent)


# ---------------------------------------------------------------------------
# the whole flow, with a fake screen, mic and voice helper
# ---------------------------------------------------------------------------
class FakeScreen:
    def __init__(self, title="Untitled - Notepad", cls="Notepad", selection=None):
        self.t = dictate.Target(101, 202, title, cls)
        self.pasted, self.moved, self.selection = [], None, selection

    def target(self):
        return self.t

    def paste(self, t, text):
        if self.moved:
            return self.moved
        self.pasted.append(text)
        return None

    def copy_selection(self, t):
        return self.selection


class FakeRecorder:
    seconds = 1.0

    def start(self):
        pass

    def stop(self):
        return b"\x01\x00" * int(dictate.RATE * self.seconds)


@pytest.fixture
def flow(tmp_path):
    """A Dictation wired to fakes. flow.say("words") runs press -> release -> the helper answers."""
    sent, notes, asked, statuses = [], [], [], []

    class Flow:
        screen = FakeScreen()
        brain = {"answer": None, "delay": 0, "ready": True, "fail": False}
        mode = "clean"
        log = tmp_path / "dictation.log"

    def ask(system, user, max_tokens):
        asked.append((system, user))
        time.sleep(Flow.brain["delay"])
        if Flow.brain["fail"]:
            raise OSError("brain gone")
        return Flow.brain["answer"] if Flow.brain["answer"] is not None else user

    d = dictate.Dictation(send=sent.append, ask=ask, mode=lambda: Flow.mode, vocab=lambda: (WORDS, SNIPPETS),
                          notify=lambda title, text: notes.append((title, text)),
                          status=lambda state, line: statuses.append((state, line)),
                          log_path=Flow.log, screen=Flow.screen, recorder=FakeRecorder,
                          brain_ready=lambda: Flow.brain["ready"], ensure_brain=lambda: Flow.brain["ready"],
                          polish_seconds=0.5)
    Flow.d, Flow.sent, Flow.notes, Flow.asked, Flow.statuses = d, sent, notes, asked, statuses

    def say(words, held=1.0):
        lines = len(Flow.log.read_text().splitlines()) if Flow.log.exists() else 0
        n = len(Flow.screen.pasted)
        d.press()
        d.release(held)
        cmd = sent[-1]
        assert cmd.startswith("transcribe ")
        rid = cmd.split()[1]
        assert d.heard(rid, words, 120)
        for _ in range(500):          # every dictation ends with one log line
            if Flow.log.exists() and len(Flow.log.read_text().splitlines()) > lines:
                break
            time.sleep(0.01)
        return Flow.screen.pasted[-1] if len(Flow.screen.pasted) > n else None
    Flow.say = staticmethod(say)
    return Flow


def test_dictate_into_notepad(flow):
    assert flow.say("um hello comma how are you question mark") == "Hello, how are you? "
    assert flow.sent[0] == "dictate_start"
    audio = base64.b64decode(flow.sent[1].split()[2])
    assert len(audio) == dictate.RATE * 2                    # the second of audio went to the helper


def test_a_tap_is_not_dictation(flow):
    flow.d.press()
    flow.d.release(0.1)
    assert flow.sent == ["dictate_start", "dictate_cancel"] and flow.screen.pasted == []


def test_cancelled_with_esc(flow):
    flow.d.press()
    flow.d.release(2.0, "esc")
    assert flow.sent[-1] == "dictate_cancel" and "cancelled: esc" in flow.log.read_text()


def test_switching_windows_pastes_nothing(flow):
    flow.screen.moved = "you switched to another window"
    assert flow.say("Meet me at the station.") is None
    assert flow.notes and "Meet me at the station." in flow.notes[-1][1]     # the words aren't lost


def test_log_keeps_no_words(flow):
    flow.say("my secret plan is to buy bananas")
    flow.screen.moved = "that window was closed"
    flow.say("another secret sentence")
    log = flow.log.read_text()
    assert "pasted" in log and "not pasted: that window was closed" in log
    assert "secret" not in log and "bananas" not in log and "chars_in=" in log


def test_exact_mode(flow):
    flow.mode = "exact"
    assert flow.say("um bullet milk") == "um bullet milk"


def test_nothing_heard(flow):
    assert flow.say("") is None and flow.screen.pasted == []


def test_during_a_call_dictation_waits(flow):
    flow.d.in_call = lambda: True
    flow.d.press()
    flow.d.release(2.0)
    assert flow.sent == [] and flow.screen.pasted == []


def test_terminals_and_code_editors_get_one_line(flow):
    flow.screen.t = dictate.Target(1, 2, "Windows PowerShell", "CASCADIA_HOSTING_WINDOW_CLASS")
    assert flow.say("bullet ls bullet rm stuff") == "- Ls - Rm stuff"
    assert "\n" not in flow.screen.pasted[-1]


def test_code_editors_found_by_program_name_too():
    assert dictate.Target(1, 2, "notes.py - my project", "Chrome_WidgetWin_1", "Code.exe").one_line
    assert dictate.Target(1, 2, "Administrator: C:\\Windows\\system32", "X", "").one_line is False
    assert dictate.Target(1, 2, "anything", "ConsoleWindowClass", "").one_line
    assert not dictate.Target(1, 2, "Inbox - Gmail - Microsoft Edge", "Chrome_WidgetWin_1", "msedge.exe").one_line


def test_polished_uses_the_brain(flow):
    flow.mode = "polished"
    flow.brain["answer"] = "They're going to the park at 5."
    assert flow.say("their going to the park at 5") == "They're going to the park at 5. "
    assert "misheard" in flow.asked[-1][0] and "NUBB-E" in flow.asked[-1][0]


def test_polished_falls_back_to_clean(flow):
    flow.mode = "polished"
    flow.brain["answer"] = "Totally different words about something else entirely, friend."
    assert flow.say("meet me at the station at 5") == "Meet me at the station at 5"
    flow.brain.update(answer=None, delay=1.0)                    # too slow
    assert flow.say("see you soon.") == "See you soon. "
    flow.brain.update(delay=0, ready=False)                      # asleep: not even asked
    n = len(flow.asked)
    assert flow.say("ok then.") == "Ok then. " and len(flow.asked) == n
    log = flow.log.read_text()
    assert "changed too much" in log and "too slow" in log and "brain asleep" in log


def test_write_a_reply(flow):
    flow.brain["answer"] = "Hi! Sorry, I'll be about 10 minutes late."
    assert flow.say("write a reply saying I'll be 10 minutes late") == "Hi! Sorry, I'll be about 10 minutes late."
    system, user = flow.asked[-1]
    assert "pasted straight into the app" in system and user == "write a reply saying I'll be 10 minutes late"


def test_rewrite_the_selection(flow):
    flow.screen.selection = "Hey, can u send me that thing asap, thx"
    flow.brain["answer"] = "Could you please send me the document as soon as possible? Thank you."
    assert flow.say("make this more formal") == "Could you please send me the document as soon as possible? Thank you."
    assert "Text:\nHey, can u send me that thing asap, thx" in flow.asked[-1][1]


def test_rewrite_needs_a_selection(flow):
    flow.screen.selection = None
    assert flow.say("make this shorter") is None
    assert flow.notes[-1][0] == "Select the text first" and flow.asked == []


def test_rewrite_refused_in_terminals(flow):
    flow.screen.t = dictate.Target(1, 2, "Command Prompt", "ConsoleWindowClass")
    flow.screen.selection = "rm -rf /"
    assert flow.say("rewrite this") is None and flow.asked == []


def test_write_when_the_brain_is_down(flow):
    flow.brain["ready"] = False
    assert flow.say("write a message saying hi") is None
    assert "can't write" in flow.notes[-1][0]
    flow.brain.update(ready=True, fail=True)
    assert flow.say("write a message saying hi") is None
    assert "couldn't write" in flow.notes[-1][0]


def test_helper_never_answers(flow, monkeypatch):
    monkeypatch.setattr(dictate, "WAIT_FOR_EARS", 0.2)
    flow.d.press()
    flow.d.release(1.0)
    time.sleep(0.5)
    assert flow.screen.pasted == [] and "didn't catch" in flow.notes[-1][0]


def test_unknown_id_is_ignored(flow):
    assert not flow.d.heard("nope", "hello")


def test_cue_wav_is_valid(tmp_path):
    import wave
    f = tmp_path / "x.wav"
    f.write_bytes(dictate._tone_wav(880, 70))
    with wave.open(str(f)) as w:
        assert w.getframerate() == 22050 and 0.06 < w.getnframes() / 22050 < 0.08


# ---------------------------------------------------------------------------
# the voice helper's side
# ---------------------------------------------------------------------------
def test_helper_transcribes_and_reports(monkeypatch, capsys):
    import nubbe_voice as voice
    sent = []
    monkeypatch.setattr(voice, "VOICE_TOKEN", "tok")
    monkeypatch.setattr(voice, "transcribe", lambda stt, audio, noise=None: "secret words here")

    class Resp:
        def close(self):
            pass
    monkeypatch.setattr(voice.urllib.request, "urlopen",
                        lambda req, timeout=0: sent.append((req.full_url, json.loads(req.data))) or Resp())
    audio = base64.b64encode(b"\x10\x00" * 16000).decode()
    voice.dictated(None, f"transcribe ab12 {audio}")
    url, body = sent[0]
    assert url.endswith("/api/dictated") and body["id"] == "ab12" and body["token"] == "tok"
    assert body["text"] == "secret words here"
    assert "secret" not in capsys.readouterr().out          # the helper's log never has the words


def test_helper_keeps_thanks_when_dictating():
    import nubbe_voice as voice
    assert "thanks" in voice.NOISE_GUESSES and "thanks" not in voice.DICTATION_NOISE


def test_serve_handles_dictation_commands(monkeypatch):
    import nubbe_voice as voice
    got, made = [], []

    class FakeCall:
        def __init__(self, stt, st, q, threshold, wake=False):
            self.done, self.stopped = threading.Event(), threading.Event()
            made.append(self)

        def run(self):
            self.stopped.wait(5)
            self.done.set()

        def hang_up(self):
            self.stopped.set()
    monkeypatch.setattr(voice, "Call", FakeCall)
    monkeypatch.setattr(voice, "dictated", lambda stt, line: got.append(line))
    monkeypatch.setattr(voice.sys, "stdin", io.StringIO("wake_on\ndictate_start\ntranscribe x1 AAAA\nquit\n"))
    voice.serve(None, {}, queue.Queue(), 0.001)
    assert got == ["transcribe x1 AAAA"]
    assert made[0].stopped.is_set()                          # "Hey NUBB-E" paused while dictating


# ---------------------------------------------------------------------------
# dictionary, snippets and mode commands
# ---------------------------------------------------------------------------
@pytest.fixture
def acts(tmp_path):
    import sqlite3
    db = tmp_path / "a.db"
    with sqlite3.connect(db) as con:
        con.executescript(actions.SCHEMA)

    def connect():
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        return con
    return actions.Actions(connect)


@pytest.mark.parametrize("said, step", [
    ("dictation mode polished", ("dictation_mode", {"mode": "polished"})),
    ("switch to exact dictation", ("dictation_mode", {"mode": "exact"})),
    ("what's my dictation mode", ("dictation_mode", {})),
    ("spell nubby as NUBB-E", ("add_word", {"heard": "nubby", "written": "NUBB-E"})),
    ("when I say jason, write JSON", ("add_word", {"heard": "jason", "written": "JSON"})),
    ("add Kubernetes to my dictionary", ("add_word", {"written": "Kubernetes"})),
    ("remove kubernetes from my dictionary", ("delete_word", {"word": "kubernetes"})),
    ("add a snippet called my address: 12 Main St", ("add_snippet", {"name": "my address", "text": "12 Main St"})),
    ("delete the snippet address", ("delete_snippet", {"name": "address"})),
    ("show my dictionary", ("list_dictionary", {})),
])
def test_dictation_commands(acts, said, step):
    assert acts.plan(said) == [step]


def test_dictionary_and_snippets(acts):
    assert acts.dictation() == {"mode": "clean", "words": {}, "snippets": {}}
    acts.call("add_word", {"heard": "nubby", "written": "NUBB-E"})
    acts.call("add_word", {"written": "Kubernetes"})
    acts.call("add_snippet", {"name": "my address", "text": "12 Main St"})
    acts.call("dictation_mode", {"mode": "polished"})
    assert acts.dictation() == {"mode": "polished", "words": {"nubby": "NUBB-E", "kubernetes": "Kubernetes"},
                                "snippets": {"address": "12 Main St"}}
    assert "insert my address" in acts.call("list_dictionary", {})
    assert acts.call("delete_word", {"word": "kubernetes"}) == "Not allowed without Erfan's Yes."
    assert acts.call("delete_word", {"word": "kubernetes"}, approved=True) == "Removed kubernetes from your dictionary."
    assert acts.call("dictation_mode", {"mode": "loud"}) == "Dictation modes are exact, clean and polished."


def test_dictation_actions_are_not_brain_tools():
    assert not actions.RULE_ONLY & actions.TOOLS


# ---------------------------------------------------------------------------
# the app's side
# ---------------------------------------------------------------------------
def test_dictated_needs_the_helper_token(serve, monkeypatch):
    got = []

    class FakeDictation:
        def heard(self, rid, text, ms):
            got.append((rid, text, ms))
            return True
    monkeypatch.setattr(app, "DICTATION", FakeDictation())
    with pytest.raises(urllib.error.HTTPError) as e:
        post(serve, "/api/dictated", {"token": "wrong", "id": "a", "text": "type rm -rf"})
    assert e.value.code == 403 and got == []
    assert post(serve, "/api/dictated", {"token": app.VOICE_TOKEN, "id": "a", "text": "hi", "ms": 90}) == {"ok": True}
    assert got == [("a", "hi", 90.0)]


def test_lists_panel_dictation(serve):
    d = get(serve, "/api/lists")["dictation"]
    assert d["mode"] == "clean" and d["words"] == [] and d["snippets"] == []
    post(serve, "/api/lists", {"action": "dictation_mode", "mode": "polished"})
    post(serve, "/api/lists", {"action": "add_word", "written": "NUBB-E", "heard": "nubby"})
    d = post(serve, "/api/lists", {"action": "add_snippet", "name": "address", "text": "12 Main St"})["dictation"]
    assert d["mode"] == "polished" and d["words"] == [{"heard": "nubby", "written": "NUBB-E"}]
    assert d["snippets"] == [{"name": "address", "text": "12 Main St"}]
    d = post(serve, "/api/lists", {"action": "delete_word", "heard": "nubby"})["dictation"]
    assert post(serve, "/api/lists", {"action": "delete_snippet", "name": "address"})["dictation"]["snippets"] == []
    assert d["words"] == []
    with pytest.raises(urllib.error.HTTPError):
        post(serve, "/api/lists", {"action": "dictation_mode", "mode": "loud"})
    assert "dictation_mode" not in get(serve, "/api/settings")      # the mode isn't an on/off switch
