"""Tests for the voice call logic in nubbe_voice.py, with fake mic audio. No mic, speakers or models.

    .venv\\Scripts\\python -m pytest tests -q
"""
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

import test_app  # noqa: F401  (sets a temp home before anything else loads)
import nubbe_voice as voice


@pytest.fixture(autouse=True)
def quiet_face(monkeypatch):
    """The face lives in the real app: never talk to it from tests."""
    monkeypatch.setattr(voice, "set_face", lambda *a, **k: None)
    voice.GENERATING.clear(), voice.SPEAKING.clear(), voice.HUSH.clear()
    voice.PLAYER.clear()


# ---------------------------------------------------------------------------
# what counts as a look, a yes or a no
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, look", [
    ("Now what do you see right now?", True), ("What about now?", True), ("Can you look at this?", True),
    ("No, no, I opened an art page. What do you see?", True), ("check my screen", True),
    ("what do you think about Rust", False), ("Nice.", False),
])
def test_wants_look(text, look):
    assert voice.wants_look(text) is look


@pytest.mark.parametrize("text, yes, no, still", [
    ("Yes.", True, False, False), ("Yeah, go ahead.", True, False, False), ("Okay, do it!", True, False, False),
    ("No thanks.", False, True, False), ("Nope", False, True, False), ("Never mind", False, True, False),
    ("What?", False, False, True), ("Hello?", False, False, True), ("You there?", False, False, True),
    ("Yes, and also tell me the weather", False, False, False), ("You see what?", False, False, False),
])
def test_spoken_answers(text, yes, no, still):
    w = voice.plain_words(text)
    assert (bool(voice.SAID_YES.fullmatch(w)), bool(voice.SAID_NO.fullmatch(w)),
            bool(voice.STILL_THERE.fullmatch(w))) == (yes, no, still)


# ---------------------------------------------------------------------------
# the listen loop: background sound (a video) vs Erfan talking
# ---------------------------------------------------------------------------
def run_call(levels, playing):
    """Feeds 0.1 s mic blocks at these loudness levels; playing[i]: another app plays sound then.
    Returns the length (seconds) of each turn the call would answer."""
    call = voice.Call(None, {}, queue.Queue(), threshold=0.001)
    turns, state = [], {"i": -1}

    class Blocks:
        def get(self, timeout=None):
            state["i"] += 1
            if state["i"] >= len(levels):
                call.hangup.set()
                raise queue.Empty
            return np.full(1600, levels[state["i"]], dtype=np.float32)

    class Meter:
        def peak(self):
            return 0.1 if 0 <= state["i"] < len(playing) and playing[state["i"]] else 0.0
    call.blocks = Blocks()
    call._answer = lambda audio, barged, final=False: turns.append(round(len(audio) / voice.MIC_RATE, 1))
    real = voice.OutputMeter
    voice.OutputMeter = Meter
    try:
        call._listen_loop()
    finally:
        voice.OutputMeter = real
    return turns


def video(seconds, rng=np.random.default_rng(1)):
    """A video's dialogue coming into the mic: it rises and falls, with little pauses."""
    return list(rng.choice([0.004, 0.012, 0.02, 0.03], int(seconds * 10)))


def test_quiet_room_turn():
    turns = run_call([0.0005] * 5 + [0.05] * 10 + [0.0005] * 10, [False] * 25)
    assert len(turns) == 1 and 1.0 <= turns[0] <= 2.0   # + a little lead-in and the pause at the end


def test_a_video_playing_is_not_erfan_talking():
    levels = video(8)
    assert run_call(levels, [True] * len(levels)) == []


def test_erfan_talking_over_a_video_still_counts():
    levels = video(3) + [0.2] * 10 + video(3)
    turns = run_call(levels, [True] * len(levels))
    assert len(turns) == 1 and turns[0] < 2.5          # ends when he stops, not at the 20 s limit


def test_back_to_normal_after_the_video_stops():
    levels = video(2) + [0.0005] * 60 + [0.01] * 6 + [0.0005] * 10   # a soft "yes" after it's quiet again
    playing = [True] * 20 + [False] * 76
    assert len(run_call(levels, playing)) == 1


# ---------------------------------------------------------------------------
# answering: a spoken yes goes to the card, "what?" doesn't throw away the answer
# ---------------------------------------------------------------------------
@pytest.fixture
def call(monkeypatch):
    c = voice.Call(None, {"chat_id": None, "use_app": True}, queue.Queue(), threshold=0.001)
    started = []
    monkeypatch.setattr(voice, "stream_reply", lambda *a, **k: started.append(a[0]))
    c.started = started
    return c


def said(call, monkeypatch, text, barged=True):
    monkeypatch.setattr(voice, "transcribe", lambda stt, audio: text)
    call._answer(np.zeros(1600, dtype=np.float32), barged)
    if call.reply:
        call.reply.join(2)


def spoken(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_spoken_yes_answers_the_card(call, monkeypatch):
    answered = []
    monkeypatch.setattr(voice, "answer_card", lambda ok: answered.append(ok) or True)
    voice.GENERATING.set()                 # the request with the card is still waiting
    said(call, monkeypatch, "Yeah, go ahead.")
    assert answered == [True] and spoken(call.speak_q) == ["On it."] and call.started == []
    assert not voice.HUSH.is_set()         # the waiting reply wasn't cut off


def test_yes_without_a_card_is_just_talk(call, monkeypatch):
    monkeypatch.setattr(voice, "answer_card", lambda ok: False)
    said(call, monkeypatch, "Yes.", barged=False)
    assert call.started == ["Yes."]


def test_what_while_working_keeps_the_answer(call, monkeypatch):
    voice.GENERATING.set()
    voice.REPLY_SO_FAR[0] = ""
    said(call, monkeypatch, "What?")
    assert spoken(call.speak_q) == ["Still on it, one sec."] and call.started == [] and not voice.HUSH.is_set()


def test_a_real_question_still_interrupts(call, monkeypatch):
    voice.GENERATING.set()
    said(call, monkeypatch, "Actually, what's the time in Tokyo?")
    assert call.started == ["Actually, what's the time in Tokyo?"]


def test_a_look_says_so_right_away(call, monkeypatch):
    said(call, monkeypatch, "What do you see now?", barged=False)
    assert spoken(call.speak_q) == ["Let me look."] and call.started == ["What do you see now?"]


def test_answer_card_needs_the_token(monkeypatch):
    got = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.reply({"voice_pending": True})

        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.reply({"ok": True})

        def reply(self, obj):
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(voice, "APP_URL", f"http://127.0.0.1:{server.server_address[1]}/api/chat")
    try:
        monkeypatch.setattr(voice, "VOICE_TOKEN", "")
        assert voice.answer_card(True) is False and got == []
        monkeypatch.setattr(voice, "VOICE_TOKEN", "s3cret")
        assert voice.answer_card(True) is True and got == [{"token": "s3cret", "ok": True}]
    finally:
        server.shutdown()
        server.server_close()
