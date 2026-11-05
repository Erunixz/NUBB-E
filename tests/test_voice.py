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
    voice.GENERATING.clear(), voice.SPEAKING.clear(), voice.HUSH.clear(), voice.IN_CALL.clear()
    voice.PLAYER.clear()
    voice.RECENT_SPEECH.clear()
    voice.REPLY_SO_FAR[0] = ""
    monkeypatch.setattr(voice, "new_detector", lambda: None)   # tests that want one give their own


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
def run_call(levels, playing, speechy=None):
    """Feeds 0.1 s mic blocks at these loudness levels; playing[i]: another app plays sound then.
    speechy[i]: what the voice detector says about block i (none given: loudness only).
    Returns the length (seconds) of each turn the call would answer."""
    call = voice.Call(None, {}, queue.Queue(), threshold=0.001)
    turns, state = [], {"i": -1}

    class Detector:
        def prob(self, block):
            return speechy[state["i"]]
    call.vad = Detector() if speechy else None

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


# ---------------------------------------------------------------------------
# Phase 6.7a: instant cues (made once in NUBB-E's voice, then played at once)
# ---------------------------------------------------------------------------
class CountingTTS:
    def __init__(self):
        self.made = []

    def synth(self, text):
        self.made.append(text)
        return np.full(2400, 0.1, dtype=np.float32), 24000


def test_cues_are_made_once_and_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(voice.Cues, "DIR", tmp_path)
    tts = CountingTTS()
    first = voice.Cues()
    first.prepare(tts, "am_puck", 1.0)
    assert len(tts.made) == len(voice.Cues.LINES)
    assert len(list(tmp_path.rglob("*.wav"))) == len(voice.Cues.LINES)
    again = voice.Cues()                          # next start: loaded from disk, nothing made
    again.prepare(tts, "am_puck", 1.0)
    assert len(tts.made) == len(voice.Cues.LINES)
    audio, rate = again.get("On it.")
    assert rate == 24000 and audio.size == 2400
    voice.Cues().prepare(tts, "af_heart", 1.0)    # another voice gets its own set
    assert len(tts.made) == 2 * len(voice.Cues.LINES)


def test_speaker_plays_a_cue_without_making_it(tmp_path, monkeypatch):
    monkeypatch.setattr(voice.Cues, "DIR", tmp_path)
    cues = voice.Cues()
    cues.prepare(CountingTTS(), "am_puck", 1.0)
    monkeypatch.setattr(voice, "CUES", cues)
    monkeypatch.setattr(voice, "_face_follows_player", lambda q: None)
    tts, q = CountingTTS(), queue.Queue()
    played = []
    monkeypatch.setattr(voice.PLAYER, "add", lambda audio, rate: played.append(rate))
    worker = threading.Thread(target=voice.speaker_worker, args=(q, tts), daemon=True)
    worker.start()
    q.put("On it.")
    q.put("Something brand new.")
    q.put(None)
    worker.join(5)
    assert tts.made == ["Something brand new."] and len(played) == 2


def test_waking_up_is_said_once(monkeypatch):
    """When the brain was asleep, the app sends "waking" first: NUBB-E says so out loud, once."""
    import io
    stream = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in (
        {"type": "start", "chat_id": 1}, {"type": "waking"}, {"type": "waking"},
        {"type": "delta", "text": "Hi there."}, {"type": "done"})) + b"data: [DONE]\n\n"
    monkeypatch.setattr(voice.urllib.request, "urlopen", lambda req, *a, **k: io.BytesIO(stream))
    q = queue.Queue()
    st = {"use_app": True, "chat_id": None, "history": []}
    voice.stream_reply("hello", st, q)
    assert [q.get_nowait() for _ in range(q.qsize())] == ["Waking up, one sec.", "Hi there."]
    assert st["chat_id"] == 1


# ---------------------------------------------------------------------------
# Phase 6.7b: voice detection, Parakeet ears, NUBB-E's name, "Hey NUBB-E"
# ---------------------------------------------------------------------------
def test_voice_detector_ignores_noise():
    """The real Silero model (ships inside faster-whisper): fans, clicks and beeps aren't speech."""
    rng = np.random.default_rng(0)
    clicks = np.zeros(16000, np.float32)
    clicks[::1600], clicks[1::1600] = 0.5, -0.4
    sounds = {"silence": np.zeros(16000, np.float32), "fan": rng.normal(0, 0.01, 16000).astype(np.float32),
              "clicks": clicks, "beep": (0.05 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(np.float32)}
    for name, audio in sounds.items():
        d = voice.SpeechDetector()
        probs = [d.prob(audio[i:i + 1600]) for i in range(0, 16000, 1600)]   # 100 ms blocks, like a call
        assert max(probs) < voice.VAD_ON, name


def test_voice_detector_takes_any_block_size():
    d = voice.SpeechDetector()
    for size in (100, 1600, 700, 3000):          # not multiples of its 512-sample window
        assert 0.0 <= d.prob(np.zeros(size, np.float32)) <= 1.0
    assert len(d.buf) < voice.SpeechDetector.WINDOW


def test_loud_noise_is_not_a_turn():
    levels = [0.05] * 30                          # a loud fan right next to the mic
    assert run_call(levels, [False] * 30, speechy=[0.05] * 30) == []


def test_speech_turn_with_voice_detection():
    levels = [0.0005] * 5 + [0.05] * 10 + [0.0005] * 10
    speechy = [0.0] * 5 + [0.95] * 10 + [0.02] * 10
    turns = run_call(levels, [False] * 25, speechy)
    assert len(turns) == 1 and 1.0 <= turns[0] <= 2.0


def test_sentence_ends_even_with_a_fan_on():
    """Loudness alone would wait for the 20 s limit with a fan running; the detector hears him stop."""
    levels = [0.02] * 5 + [0.06] * 10 + [0.02] * 30
    speechy = [0.02] * 5 + [0.95] * 10 + [0.03] * 30
    turns = run_call(levels, [False] * 45, speechy)
    assert len(turns) == 1 and turns[0] < 2.5


@pytest.mark.parametrize("heard, fixed", [
    ("Hey newbie, what's the weather like today?", "Hey NUBB-E, what's the weather like today?"),
    ("Hey noobie", "Hey NUBB-E"), ("Hey Nuby, what's up?", "Hey NUBB-E, what's up?"),
    ("Hey NUBB!", "Hey NUBB-E!"), ("Hey NUBB-E.", "Hey NUBB-E."), ("Thanks nubby.", "Thanks NUBB-E."),
    ("I'm a newbie at Rust.", "I'm a newbie at Rust."),          # the real word stays
    ("Newbie, open Spotify.", "NUBB-E, open Spotify."),
])
def test_names_are_fixed(heard, fixed):
    assert voice.fix_names(heard) == fixed


@pytest.mark.parametrize("text, rest", [
    ("Hey NUBB-E, what's the time?", "what's the time?"), ("Hey NUBB-E.", ""), ("NUBB-E, open Spotify", "open Spotify"),
    ("Okay NUBB-E what's up", "what's up"), ("What's up?", None), ("I told NUBB-E about it", None),
    ("Hey, can you help?", None),
])
def test_wake_phrase(text, rest):
    assert voice.wake_rest(text) == rest


def test_parakeet_ears_fix_the_name():
    ears = voice.Parakeet.__new__(voice.Parakeet)
    ears.model = type("M", (), {"recognize": lambda self, audio, sample_rate: "Hey newbie, open Calculator."})()
    assert voice.transcribe(ears, np.full(1600, 0.1, np.float32)) == "Hey NUBB-E, open Calculator."


def test_ears_fall_back_to_whisper(monkeypatch):
    class Broken:
        def __init__(self):
            raise ImportError("no onnx_asr")
    monkeypatch.setattr(voice, "Parakeet", Broken)
    monkeypatch.setattr(voice, "WhisperModel", lambda *a, **k: "whisper")
    args = type("A", (), {"ears": "parakeet", "whisper": "base.en"})()
    assert voice.load_ears(args) == "whisper"
    args.ears = "whisper"
    assert voice.load_ears(args) == "whisper"


@pytest.fixture
def wake_call(monkeypatch):
    c = voice.Call(None, {"chat_id": 7, "use_app": True}, queue.Queue(), threshold=0.001, wake=True)
    started = []
    monkeypatch.setattr(voice, "stream_reply", lambda *a, **k: started.append(a[0]))
    monkeypatch.setattr(voice, "tell_app_to_wake", lambda: started.append("(app wakes the brain)"))
    c.started = started
    return c


def test_wake_mode_ignores_other_talk(wake_call, monkeypatch, capsys):
    said(wake_call, monkeypatch, "So I told him the password is pancakes.", barged=False)
    assert wake_call.started == [] and spoken(wake_call.speak_q) == []
    assert "pancakes" not in capsys.readouterr().out          # never printed (or saved anywhere)
    assert not wake_call.awake()


def test_wake_mode_answers_after_the_name(wake_call, monkeypatch):
    said(wake_call, monkeypatch, "Hey NUBB-E, what's the time?", barged=False)
    assert wake_call.started[-1] == "what's the time?" and "(app wakes the brain)" in wake_call.started
    assert wake_call.awake() and wake_call.st["chat_id"] is None     # a new session is a new chat
    said(wake_call, monkeypatch, "And in Tokyo?", barged=False)      # a follow-up needs no name
    assert wake_call.started[-1] == "And in Tokyo?"


def test_wake_mode_just_the_name_asks_back(wake_call, monkeypatch):
    said(wake_call, monkeypatch, "Hey NUBB-E.", barged=False)
    assert spoken(wake_call.speak_q) == ["Yes?"] and wake_call.awake()
    said(wake_call, monkeypatch, "Open my notes.", barged=False)
    assert wake_call.started[-1] == "Open my notes."


def test_wake_session_ends_after_the_follow_up_time(wake_call, monkeypatch):
    said(wake_call, monkeypatch, "Hey NUBB-E, hi!", barged=False)
    wake_call.awake_until = voice.time.time() - 0.1      # follow-up time is over
    wake_call._wake_session_tick()
    assert not wake_call.awake() and not voice.IN_CALL.is_set()
    said(wake_call, monkeypatch, "What about now?", barged=False)
    assert wake_call.started[-1] == "hi!"                # not answered: needs the name again


def test_wake_session_has_a_hard_limit(wake_call, monkeypatch):
    """A TV talking non-stop can't keep the session open forever."""
    said(wake_call, monkeypatch, "Hey NUBB-E, tell me a story.", barged=False)
    voice.SPEAKING.set()                                 # NUBB-E keeps talking...
    wake_call.session_end = voice.time.time() - 0.1      # ...past the hard limit
    wake_call._wake_session_tick()
    assert not wake_call.awake()


def test_own_short_cue_is_echo():
    voice.RECENT_SPEECH.append("Yes?")
    assert voice.Call._is_own_echo("Yes.")
    assert not voice.Call._is_own_echo("No.")


def test_serve_pauses_wake_listening_for_a_call(monkeypatch):
    import io
    made = []

    class FakeCall:
        def __init__(self, stt, st, q, threshold, wake=False):
            self.wake, self.hangup, self.done = wake, threading.Event(), threading.Event()
            made.append(self)

        def run(self):
            self.hangup.wait(5)
            self.done.set()

        def hang_up(self):
            self.hangup.set()
    monkeypatch.setattr(voice, "Call", FakeCall)
    monkeypatch.setattr(voice.sys, "stdin", io.StringIO("wake_on\ncall\nhangup\nwake_off\nquit\n"))
    voice.serve(None, {}, queue.Queue(), 0.001)
    assert [c.wake for c in made] == [True, False, True]       # wake, then the call, then wake again
    assert all(c.hangup.is_set() for c in made)                 # nothing left holding the mic
