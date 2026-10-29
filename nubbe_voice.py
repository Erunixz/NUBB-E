#!/usr/bin/env python3
"""nubbe_voice: talk to NUBB-E out loud, it talks back in one-liners.

Flow: mic -> faster-whisper (CPU) -> NUBB-E brain (GPU) -> Kokoro TTS (CPU) -> speaker
Speaks each sentence as soon as it's generated, so replies start fast.
With nubbe_app.py open, voice chats are saved, NUBB-E uses its memory of you,
and it learns new things automatically, same as text chats. The app's face
shows listening / thinking / talking. Without the app it still works, it
just won't save or remember.

Voice: Kokoro-82M (Apache 2.0), natural sounding and faster than real time,
run on the CPU so the GPU stays free for the brain. Piper still works as a
lighter fallback.

Setup (Python 3.10 to 3.12, Kokoro doesn't support 3.13 yet):
    pip install faster-whisper sounddevice numpy kokoro soundfile
    Install espeak-ng (Kokoro uses it for unusual words):
        Windows: the .msi from the espeak-ng releases page
        Linux:   sudo apt install espeak-ng      macOS: brew install espeak-ng
    Run once with internet so Whisper and Kokoro download their models.
    After that, set HF_HUB_OFFLINE=1 and it runs fully offline.
    Start nubbe_app.py (or at least llama-server on port 8080).

Run:
    python nubbe_voice.py                          # Kokoro, voice from config.toml
    python nubbe_voice.py --voice af_heart         # another Kokoro voice
    python nubbe_voice.py --voice voices/en_US-amy-medium.onnx   # Piper instead
    python nubbe_voice.py --mic-test               # measure your mic, suggest mic_threshold
Press Enter, talk, then stop talking. It answers out loud. Ctrl+C to quit.
The app's mic button runs this script with --serve (commands on stdin).
"""
import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Kokoro runs on the CPU by default so it never fights the brain for the 8 GB GPU.
# Pass --tts-gpu to let it use the GPU instead.
if "--tts-gpu" not in sys.argv:
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
# Offline first: load the speech models from the local cache without touching the network.
# Only if they aren't downloaded yet (first run) does it go online once (see load_models).
ASKED_OFFLINE = os.environ.get("HF_HUB_OFFLINE") == "1"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

try:
    from nubbe_face import set_face
except ImportError:
    def set_face(*args, **kwargs):
        pass

try:
    from nubbe_config import CFG, path
    APP_PORT, LLM_PORT, VOICE = CFG["app"]["port"], CFG["brain"]["port"], CFG["voice"]
except ImportError:
    APP_PORT, LLM_PORT = 8765, 8080
    VOICE = {"whisper_model": "base.en", "kokoro_voice": "af_heart", "kokoro_speed": 1.05,
             "piper_voice": "", "mic_threshold": 0.015}
    path = Path

APP_URL = f"http://127.0.0.1:{APP_PORT}/api/chat"            # NUBB-E app (saves chats + memory)
LLM_URL = f"http://localhost:{LLM_PORT}/v1/chat/completions"  # fallback: brain directly
GENERATING = threading.Event()   # LLM is still writing the reply
SPEAKING = threading.Event()     # a sentence is playing right now
MIC_RATE = 16000          # whisper expects 16 kHz mono
SILENCE_SECONDS = 0.6     # stop recording after this much quiet
MAX_RECORD_SECONDS = 20

VOICE_RULES = (
    "\n\n## Voice call\n"
    "You're on a voice call with Erfan, talking out loud like a friend on the phone. "
    "Keep it short and natural: usually one to three short sentences. React like a person "
    "(\"oh nice\", \"hmm\", \"wait, really?\") and sometimes ask something back. "
    "Only go longer if he asks for detail. Don't end every reply with a question. "
    "You can't hear your own voice, so if he says you pronounce a word wrong, fix it: add "
    "[say: word = spelling] at the very end, spelled so it sounds right when read aloud "
    "(for example [say: Erfan = Er-fawn]), and just say thanks. "
    "No markdown, no lists, no code blocks, no emoji: everything you write is read aloud, "
    "so say code and symbols in plain words. "
    "Now and then you may add a little robot sound, like *happy beep*, but rarely."
)


# ---------- listening ----------
def record_until_silence(threshold: float, stop: threading.Event | None = None) -> np.ndarray:
    """Record from the mic until the speaker goes quiet (or `stop` is set: the mic button)."""
    chunk = int(MIC_RATE * 0.1)  # 100 ms blocks
    frames, quiet_blocks, heard_voice = [], 0, False
    needed_quiet = int(SILENCE_SECONDS / 0.1)

    with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="float32") as stream:
        for _ in range(int(MAX_RECORD_SECONDS / 0.1)):
            if stop is not None and stop.is_set():
                break
            block, _ = stream.read(chunk)
            frames.append(block[:, 0].copy())
            loud = np.sqrt(np.mean(block ** 2)) > threshold
            if loud:
                heard_voice, quiet_blocks = True, 0
            elif heard_voice:
                quiet_blocks += 1
                if quiet_blocks >= needed_quiet:
                    break
    return np.concatenate(frames)


NOISE_GUESSES = {"you", "thank you", "thanks", "bye", "the", "uh", "um", "hmm", "so", "okay", "oh"}


def transcribe(model: WhisperModel, audio: np.ndarray) -> str:
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if 0 < peak < 0.5:
        audio = audio * (0.5 / peak)      # quiet mic: bring it up to a normal level, Whisper hears much better
    segments, _ = model.transcribe(audio, language="en", vad_filter=True,
                                   initial_prompt="NUBB-E, Erfan.")   # so it knows these names
    # skip bits Whisper itself isn't sure were speech (noise, a click, NUBB-E's own echo)
    text = " ".join(s.text.strip() for s in segments
                    if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0).strip()
    if text.lower().strip(" .!?,") in NOISE_GUESSES:
        return ""                         # Whisper's usual guesses on noise or a breath
    return text


# ---------- speaking ----------
SAY_AS = {"Erfan": "Er-fawn", "NUBB-E": "Noobee"}   # how to pronounce names ([voice.say] in config.toml)
LEARNED_SAY = Path.home() / ".nubbe" / "say.json"    # words Erfan taught it to say right, during calls
SAY_TAG = re.compile(r"\[say:\s*([^=\]]+?)\s*=\s*([^\]]+?)\s*\]", re.IGNORECASE)


def learned_say() -> dict:
    try:
        return json.loads(LEARNED_SAY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def learn_say(word: str, sound: str) -> None:
    words = learned_say()
    words[word.strip()] = sound.strip()
    LEARNED_SAY.parent.mkdir(parents=True, exist_ok=True)
    LEARNED_SAY.write_text(json.dumps(words, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"(learned to say {word!r} as {sound!r})", flush=True)


def clean_for_speech(text: str) -> str:
    text = re.sub(r"[*_`#>|]", "", text)            # strip markdown symbols
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", text)  # strip emoji
    for word, sound in {**SAY_AS, **VOICE.get("say", {}), **learned_say()}.items():
        text = re.sub(rf"(?<![\w-]){re.escape(word)}(?![\w-])", sound, text, flags=re.IGNORECASE)
    return text.strip()


class KokoroTTS:
    """Kokoro-82M: natural voice, 24 kHz. Voices: af_heart, af_bella, am_michael, ..."""
    def __init__(self, voice: str, speed: float):
        from kokoro import KPipeline
        import torch
        torch.set_num_threads(min(12, os.cpu_count() or 4))   # 12 of 16 threads: fast speech, leaves room for the brain
        self.pipe = KPipeline(lang_code=voice[0])   # 'a' = American English (af_/am_ voices), 'b' = British
        self.voice, self.speed = voice, speed

    def synth(self, text: str):
        chunks = []
        for _, _, audio in self.pipe(text, voice=self.voice, speed=self.speed):
            if audio is None:
                continue
            a = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            chunks.append(a.astype(np.float32))
        return (np.concatenate(chunks) if chunks else np.zeros(0, np.float32)), 24000


class PiperTTS:
    """Piper: lighter, more robotic. Needs the .onnx voice and its .onnx.json next to it."""
    def __init__(self, voice: str):
        with open(voice + ".json", encoding="utf-8") as f:
            self.rate = json.load(f)["audio"]["sample_rate"]
        self.voice = voice

    def synth(self, text: str):
        # the piper command sits next to this Python when installed in the venv, even if it isn't on PATH
        exe = shutil.which("piper") or str(Path(sys.executable).parent / "piper")
        raw = subprocess.run([exe, "--model", self.voice, "--output-raw"],
                             input=text.encode(), capture_output=True).stdout
        return np.frombuffer(raw, dtype=np.int16), self.rate


def load_tts(voice: str, speed: float, piper_fallback: str | None):
    if voice.endswith(".onnx"):
        return PiperTTS(voice)
    try:
        tts = KokoroTTS(voice, speed)
        tts.synth("Hi.")                     # warm up so the first real reply is fast
        return tts
    except Exception as e:
        if piper_fallback:
            print(f"(Kokoro unavailable: {e}. Using Piper instead.)")
            return PiperTTS(piper_fallback)
        raise SystemExit(f"Kokoro couldn't start: {e}\n"
                         "Install it with: pip install kokoro soundfile  (and espeak-ng), "
                         "or pass --piper-voice voices/<name>.onnx as a fallback.")


HUSH = threading.Event()         # the mic button asked it to stop talking
RECORDING = threading.Event()    # the mic is open right now
IN_CALL = threading.Event()      # a hands-free call is on: go back to listening after each reply
LAST_SPOKE = [0.0]               # when NUBB-E last finished a sentence (its echo can linger a moment)
RECENT_SPEECH: list[str] = []    # what NUBB-E said lately, to spot its own voice coming back into the mic
REPLY_SO_FAR = [""]             # the reply being written right now (its echo can only contain these words)


class Player:
    """Plays sentences back to back with no gaps, and can pause, resume or clear instantly
    (so NUBB-E can stop the moment Erfan talks, and pick up again if it was just a noise)."""
    RATE = 24000

    def __init__(self):
        self.lock = threading.Lock()
        self.chunks: list[np.ndarray] = []
        self.pos = 0
        self.paused = False
        self.stream = None

    def _start(self) -> None:
        if self.stream is None:
            self.stream = sd.OutputStream(samplerate=self.RATE, channels=1, dtype="float32",
                                          blocksize=1200, callback=self._fill)
            self.stream.start()

    def add(self, audio: np.ndarray, rate: int) -> None:
        audio = np.asarray(audio, dtype=np.float32)
        if audio.dtype != np.float32 or np.abs(audio).max() > 1.5:      # Piper gives 16-bit numbers
            audio = audio / 32768.0
        if rate != self.RATE:
            audio = np.interp(np.linspace(0, len(audio) - 1, int(len(audio) * self.RATE / rate)),
                              np.arange(len(audio)), audio).astype(np.float32)
        with self.lock:
            self.chunks.append(audio)
        self._start()

    def _fill(self, out, frames, _time, _status) -> None:
        out[:] = 0
        played = 0
        with self.lock:
            if not self.paused:
                while played < frames and self.chunks:
                    chunk = self.chunks[0]
                    n = min(frames - played, len(chunk) - self.pos)
                    out[played:played + n, 0] = chunk[self.pos:self.pos + n]
                    played += n
                    self.pos += n
                    if self.pos >= len(chunk):
                        self.chunks.pop(0)
                        self.pos = 0
        if played:
            SPEAKING.set()
        elif SPEAKING.is_set():
            SPEAKING.clear()
            LAST_SPOKE[0] = time.time()

    def pause(self) -> None:
        with self.lock:
            self.paused = True

    def resume(self) -> None:
        with self.lock:
            self.paused = False

    def clear(self) -> None:
        with self.lock:
            self.chunks, self.pos, self.paused = [], 0, False

    def has_audio(self) -> bool:
        with self.lock:
            return bool(self.chunks)


PLAYER = Player()


def hush(q: "queue.Queue[str | None]") -> None:
    """Stops speaking right now and drops whatever was still queued."""
    HUSH.set()
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break
    PLAYER.clear()


def speaker_worker(q: "queue.Queue[str | None]", tts) -> None:
    """Turns sentences into audio. The next sentence is made while the current one plays,
    so there's no wait between sentences."""
    threading.Thread(target=_face_follows_player, args=(q,), daemon=True).start()
    while True:
        sentence = q.get()
        if sentence is None:
            break
        sentence = clean_for_speech(sentence)
        if not sentence or HUSH.is_set():
            continue
        audio, rate = tts.synth(sentence)
        if audio.size and not HUSH.is_set():
            RECENT_SPEECH.append(sentence)
            del RECENT_SPEECH[:-6]
            PLAYER.add(np.concatenate([audio, np.zeros(int(rate * 0.06), dtype=audio.dtype)]), rate)


def _face_follows_player(q) -> None:
    """Face shows talking while audio plays, then thinking / listening / idle."""
    was = None
    while True:
        time.sleep(0.1)
        now = ("talking" if SPEAKING.is_set() else
               "thinking" if GENERATING.is_set() or not q.empty() or PLAYER.has_audio() else
               "listening" if IN_CALL.is_set() else "idle")
        if now != was and not (now == "thinking" and RECORDING.is_set()):
            try:
                set_face(now, RECENT_SPEECH[-1] if now == "talking" and RECENT_SPEECH else None)
            except Exception:
                pass                       # the face is nice to have; never let it stop the voice
            was = now


# ---------- thinking ----------
def _deltas_from(req, st: dict):
    """Yields reply text from either the NUBB-E app or llama-server directly."""
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            obj = json.loads(data)
            if "choices" in obj:                       # llama-server format
                piece = obj["choices"][0]["delta"].get("content") or ""
            elif obj.get("type") == "start":           # app format
                st["chat_id"] = obj["chat_id"]
                continue
            elif obj.get("type") == "delta":
                piece = obj["text"]
            elif obj.get("type") == "error":
                raise ConnectionError(obj["text"])
            else:
                continue
            if piece:
                yield piece


def _deltas(heard: str, st: dict, look: bool = False):
    if st["use_app"]:
        # through the app: chat gets saved, memories are used and learned.
        # /api/look also takes a screenshot (NUBB-E's own windows hide for it)
        url = APP_URL.replace("/api/chat", "/api/look") if look else APP_URL
        req = urllib.request.Request(url, data=json.dumps(
            {"chat_id": st["chat_id"], "message": heard, "voice": True, "source": st.get("source")}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            yield from _deltas_from(req, st)
            return
        except urllib.error.URLError:
            st["use_app"] = False
            print("\n(NUBB-E app isn't open, talking to the brain directly. "
                  "This chat won't be saved or remembered.)\nNUBB-E: ", end="")
    st["history"].append({"role": "user", "content": heard})
    payload = {
        "model": "local",
        "stream": True,
        "max_tokens": 120,                                  # keeps it short, like talking
        "chat_template_kwargs": {"enable_thinking": False},  # skip slow reasoning in voice mode
        "messages": st["history"],
    }
    req = urllib.request.Request(LLM_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    yield from _deltas_from(req, st)


def stream_reply(heard: str, st: dict, speak_q: "queue.Queue[str | None]", look: bool = False) -> str:
    full, buf = "", ""
    GENERATING.set()
    try:
        for delta in _deltas(heard, st, look):
            if HUSH.is_set():
                break            # stopped by the mic button: don't queue more speech
            print(delta, end="", flush=True)
            full += delta
            REPLY_SO_FAR[0] = full
            buf += delta
            for tag in SAY_TAG.finditer(buf):        # [say: word = spelling]: a word Erfan just taught it
                learn_say(tag.group(1), tag.group(2))
            buf = SAY_TAG.sub("", buf)
            if re.search(r"\[(s|sa|say|say:[^\]]*)?$", buf, re.IGNORECASE):
                continue                         # a tag may still be coming: don't speak half of it
            # speak each finished sentence right away. Whole sentences only: Kokoro needs the
            # full sentence to get the melody right (pieces sound robotic and rushed)
            while m := re.search(r"[.!?](\s|$)", buf):
                speak_q.put(buf[:m.end()])
                buf = buf[m.end():]
    finally:
        GENERATING.clear()
    if buf.strip() and not HUSH.is_set():
        speak_q.put(buf)
    elif speak_q.empty() and not SPEAKING.is_set():
        set_face("listening" if IN_CALL.is_set() else "idle")   # nothing left to say
    print()
    if not st["use_app"]:
        st["history"].append({"role": "assistant", "content": full})
        st["history"][1:] = st["history"][1:][-20:]  # keep recent turns so it stays fast
    return full


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--voice", default=VOICE["kokoro_voice"],
                   help="Kokoro voice name (af_heart, af_bella, am_michael, ...) or a Piper .onnx file")
    p.add_argument("--speed", type=float, default=VOICE["kokoro_speed"], help="Kokoro speaking speed")
    p.add_argument("--piper-voice", default=str(path(VOICE["piper_voice"])) if VOICE["piper_voice"] else None,
                   help="Piper .onnx voice to fall back to if Kokoro can't load")
    p.add_argument("--tts-gpu", action="store_true", help="let Kokoro use the GPU (default: CPU)")
    p.add_argument("--prompt", default="nubbe_system_prompt.txt", help="personality file")
    p.add_argument("--whisper", default=VOICE["whisper_model"], help="tiny.en / base.en / small.en")
    p.add_argument("--threshold", type=float, default=VOICE["mic_threshold"],
                   help="mic loudness to count as talking")
    p.add_argument("--serve", action="store_true",
                   help="controlled by the app's mic button (commands on stdin) instead of Enter")
    p.add_argument("--mic-test", action="store_true", help="measure your mic and suggest mic_threshold")
    p.add_argument("--push-to-talk", action="store_true",
                   help="press Enter for each question instead of a hands-free call")
    args = p.parse_args()
    if args.mic_test:
        mic_test()
        return

    if not Path(args.prompt).is_absolute() and not Path(args.prompt).exists():
        args.prompt = str(Path(__file__).resolve().parent / args.prompt)   # works from any folder
    with open(args.prompt, encoding="utf-8") as f:
        system = f.read() + VOICE_RULES

    print("Loading speech models on CPU...", flush=True)
    stt, tts = load_models(args)

    speak_q: "queue.Queue[str | None]" = queue.Queue()
    threading.Thread(target=speaker_worker, args=(speak_q, tts), daemon=True).start()

    st = {"use_app": True, "chat_id": None, "history": [{"role": "system", "content": system}]}
    if args.serve:
        serve(stt, st, speak_q, args.threshold)
        return
    set_face("happy", "Hi Erfan!")
    try:
        if args.push_to_talk:
            print("NUBB-E is listening. Press Enter, then talk. Ctrl+C to quit.")
            while True:
                input()
                one_turn(stt, st, speak_q, args.threshold)
        else:
            print("On a call with NUBB-E: just talk. Ctrl+C to hang up.")
            Call(stt, st, speak_q, args.threshold).run()
    except KeyboardInterrupt:
        speak_q.put(None)
        set_face("sleep", "bye!")
        print("\nbye!")


def mic_test() -> None:
    """Measures the room and Erfan's voice, suggests mic_threshold, and saves it if he says y."""
    def levels(seconds):
        with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="float32") as stream:
            return np.array([float(np.sqrt(np.mean(stream.read(int(MIC_RATE * 0.1))[0] ** 2)))
                             for _ in range(int(seconds / 0.1))])
    input("Mic test. Stay quiet for 3 seconds after you press Enter...")
    quiet = levels(3)
    input("Now press Enter and talk normally for 4 seconds (say anything)...")
    talk = levels(4)
    room, voice = float(np.percentile(quiet, 95)), float(np.percentile(talk, 60))
    print(f"Room noise: {room:.4f}   Your voice: {voice:.4f}")
    if voice < room * 1.5:
        print("Your voice is barely louder than the room. Move closer to the mic or check the input device.")
        return
    # halfway between room and voice (on a log scale), but never below a third of the voice:
    # Windows noise suppression can make a quiet room read as 0, which would make any click count as talking
    suggested = round(max((room * voice) ** 0.5, voice / 3), 4)
    if voice < 0.01:
        print("Your mic is quite quiet. Raising its input volume in Windows sound settings will help NUBB-E hear you.")
    print(f"Suggested mic_threshold: {suggested}  (now: {VOICE['mic_threshold']})")
    if input("Save it to config.toml? (y/n) ").strip().lower() != "y":
        return
    cfg = Path(__file__).resolve().parent / "config.toml"
    text = cfg.read_text(encoding="utf-8")
    new = re.sub(r"(?m)^mic_threshold = [0-9.]+", f"mic_threshold = {suggested}", text)
    if new == text:
        print("Couldn't find mic_threshold in config.toml. Add it under [voice] yourself.")
        return
    cfg.write_text(new, encoding="utf-8")
    print("Saved. The app's mic button uses it next time voice starts.")


def load_models(args):
    """Whisper (ears) + Kokoro (voice), both on the CPU so the GPU stays free for the brain."""
    def load():
        stt = WhisperModel(args.whisper, device="cpu", compute_type="int8")
        tts = load_tts(args.voice, args.speed, args.piper_voice)
        audio, rate = tts.synth("Hello there.")           # warm up the ears too, so the first question is fast
        if audio.size:
            idx = np.linspace(0, len(audio) - 1, int(len(audio) * MIC_RATE / rate)).astype(int)
            transcribe(stt, audio[idx].astype(np.float32) / max(1.0, float(np.abs(audio).max())))
        return stt, tts
    try:
        return load()
    except (Exception, SystemExit) as e:
        if os.environ.get("HF_HUB_OFFLINE") != "1" or ASKED_OFFLINE:
            raise
        print(f"(speech models not downloaded yet: {e})\nDownloading them once (about 800 MB)...", flush=True)
        os.environ["HF_HUB_OFFLINE"] = "0"
        import huggingface_hub.constants
        huggingface_hub.constants.HF_HUB_OFFLINE = False
        return load()


def one_turn(stt, st: dict, speak_q, threshold: float, stop: threading.Event | None = None) -> None:
    """Listen, understand, answer out loud. One spoken exchange."""
    HUSH.clear()
    print("(listening...)", flush=True)
    set_face("listening")
    RECORDING.set()
    try:
        audio = record_until_silence(threshold, stop)
    finally:
        RECORDING.clear()
    heard = transcribe(stt, audio)
    if not heard:
        print("(didn't catch that)", flush=True)
        set_face("idle", "Didn't catch that.")
        return
    print(f"You: {heard}\nNUBB-E: ", end="", flush=True)
    set_face("thinking", heard, who="you")
    try:
        stream_reply(heard, st, speak_q)
    except (urllib.error.URLError, ConnectionError) as e:
        set_face("sleep", "Can't reach my brain.")
        print(f"\n(can't reach the brain: {e})", flush=True)


UNFINISHED_ENDINGS = {"about", "and", "but", "so", "or", "because", "like", "um", "uh", "the", "a", "an",
                      "to", "of", "with", "for", "if", "that", "my", "your", "is", "was", "think", "mean"}


def sounds_unfinished(text: str) -> bool:
    """True for "what do you think about..." : Erfan paused mid-sentence and has more to say."""
    t = text.strip().lower()
    if t.endswith(("...", "\u2026", ",")):
        return True
    words = re.findall(r"[a-z']+", t)
    return bool(words) and words[-1] in UNFINISHED_ENDINGS and not t.endswith(("?", "!"))


LOOK_WORDS = re.compile(r"\b(can you (look|see)|could you (look|see)|take a look|look (at|here)|"
                        r"see my screen|on my screen|what am i (looking at|doing))\b", re.IGNORECASE)


def wants_look(text: str) -> bool:
    """True when Erfan asks NUBB-E to look at his screen during a call."""
    return bool(LOOK_WORDS.search(text))


def barge_in_allowed() -> bool:
    """Interrupting NUBB-E by talking. On by default: when the mic hears something while it
    talks, it pauses, and resumes if it was only a noise or its own echo."""
    return str(VOICE.get("barge_in", True)).lower() not in ("false", "0", "no", "off")


class Call:
    """Hands-free voice call, like a phone call: the mic stays open, NUBB-E answers when
    Erfan pauses, then listens again. Talking while NUBB-E speaks interrupts it."""
    BLOCK = 0.1            # seconds of audio per step
    START_BLOCKS = 2       # this much loud audio starts a turn (0.2 s)
    BARGE_BLOCKS = 3       # interrupting needs a little more (0.3 s), so a cough doesn't cut it off
    PREROLL_BLOCKS = 3     # keep 0.3 s from just before the speech started, so the first word isn't clipped

    def __init__(self, stt, st: dict, speak_q, threshold: float):
        self.stt, self.st, self.speak_q, self.threshold = stt, st, speak_q, threshold
        self.blocks: "queue.Queue[np.ndarray]" = queue.Queue()
        self.hangup = threading.Event()
        self.done = threading.Event()
        self.reply: threading.Thread | None = None
        self.echo = threshold      # how loud NUBB-E's own voice comes back into the mic (learned as it talks)
        self.barge_in = barge_in_allowed()
        self.pending = None        # "what do you think about..." : audio waiting for the rest of the sentence
        self.pending_until = 0.0
        self.pending_barged = False

    def run(self) -> None:
        """Blocks until hang_up() is called."""
        IN_CALL.set()
        self.st["chat_id"], self.st["source"] = None, "call"     # one saved chat per call
        set_face("listening", "On a call")
        print("(call started: just talk)", flush=True)
        try:
            with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="float32",
                                blocksize=int(MIC_RATE * self.BLOCK),
                                callback=lambda data, *_: self.blocks.put(data[:, 0].copy())):
                self._listen_loop()
        except sd.PortAudioError as e:
            print(f"(can't open the mic: {e})", flush=True)
            set_face("sleep", "Can't open the mic.")
        finally:
            self.done.set()
            IN_CALL.clear()
            hush(self.speak_q)
            self.st["source"] = None
            set_face("idle", "Call ended")
            print("(call ended)", flush=True)

    def hang_up(self) -> None:
        self.hangup.set()

    def _listen_loop(self) -> None:
        pre: list[np.ndarray] = []
        speech: list[np.ndarray] = []
        loud = quiet = 0
        barged = False
        while not self.hangup.is_set():
            if self.pending is not None and not speech and time.time() > self.pending_until:
                audio, self.pending = self.pending, None     # no rest came: answer what we have
                self._answer(audio, self.pending_barged, final=True)
            try:
                block = self.blocks.get(timeout=0.2)
            except queue.Empty:
                continue
            level = float(np.sqrt(np.mean(block ** 2)))
            talking = SPEAKING.is_set() or time.time() - LAST_SPOKE[0] < 0.3   # + its echo tail
            if talking and not self.barge_in:
                pre, loud = [], 0        # interrupting turned off: wait until it's done talking
                continue
            gate = max(self.threshold * 2.5, self.echo * 3) if talking else self.threshold
            if talking and not speech and loud == 0 and level <= gate:
                # learn how loud its own echo is, slowly, and only while nobody seems to be talking
                # (otherwise Erfan's quieter syllables would raise the bar he has to get over)
                self.echo = 0.9 * self.echo + 0.1 * level
            if not speech:
                pre = (pre + [block])[-self.PREROLL_BLOCKS:]
                loud = loud + 1 if level > gate else max(0, loud - 1)   # a dip between syllables is fine
                if loud >= (self.BARGE_BLOCKS if talking else self.START_BLOCKS):
                    speech, quiet = list(pre), 0
                    barged = talking or GENERATING.is_set() or PLAYER.has_audio()
                    if barged:
                        PLAYER.pause()           # stop talking right away; resume if it was just a noise
                    set_face("listening")
                continue
            speech.append(block)
            quiet = 0 if level > self.threshold else quiet + 1
            if quiet * self.BLOCK >= SILENCE_SECONDS or len(speech) * self.BLOCK >= MAX_RECORD_SECONDS:
                audio = np.concatenate(speech)
                if self.pending is not None:                  # the rest of an unfinished sentence
                    audio, barged = np.concatenate([self.pending, audio]), barged or self.pending_barged
                    self.pending = None
                self._answer(audio, barged)
                speech, pre, loud = [], [], 0

    def _answer(self, audio: np.ndarray, barged: bool, final: bool = False) -> None:
        heard = transcribe(self.stt, audio)
        if not heard or self._is_own_echo(heard):
            PLAYER.resume()                        # just a noise or its own echo: carry on talking
            print("(ignored a noise)" if not heard else f"(ignored its own echo: {heard})", flush=True)
            return
        if not final and sounds_unfinished(heard):
            self.pending, self.pending_until, self.pending_barged = audio, time.time() + 1.5, barged
            print(f"(waiting for the rest: {heard})", flush=True)
            return
        if barged:
            hush(self.speak_q)                     # it was really Erfan: drop the rest of the old reply
        if self.reply and self.reply.is_alive():   # let the interrupted reply wind down
            HUSH.set()
            self.reply.join(3)
        HUSH.clear()
        print(f"You: {heard}\nNUBB-E: ", end="", flush=True)
        set_face("thinking", heard, who="you")

        look = wants_look(heard)

        def reply():
            try:
                stream_reply(heard, self.st, self.speak_q, look=look)
            except (urllib.error.URLError, ConnectionError) as e:
                set_face("sleep", "Can't reach my brain.")
                print(f"\n(can't reach the brain: {e})", flush=True)
        self.reply = threading.Thread(target=reply, daemon=True)
        self.reply.start()

    @staticmethod
    def _is_own_echo(heard: str) -> bool:
        """True if the mic just picked up NUBB-E's own voice from the speakers."""
        import difflib
        said = " ".join(RECENT_SPEECH[-3:] + [REPLY_SO_FAR[0]]).lower()
        h = re.sub(r"[^a-z0-9 ]", "", heard.lower())
        return bool(said) and len(h) > 3 and difflib.SequenceMatcher(
            None, h, re.sub(r"[^a-z0-9 ]", "", said)).find_longest_match().size >= max(12, len(h) * 0.6)


def serve(stt, st: dict, speak_q, threshold: float) -> None:
    """Controlled by the app: reads one command per line on stdin.
    call   = start a hands-free call.   hangup = end it.
    listen = push-to-talk turn, or stop recording early if already listening.
    stop   = stop listening / stop talking (ends a call too).   quit = exit."""
    stop = threading.Event()
    busy = threading.Event()
    call: list[Call] = []

    def turn():
        try:
            one_turn(stt, st, speak_q, threshold, stop)
        finally:
            busy.clear()

    def end_call():
        if call:
            call.pop().hang_up()

    print("READY", flush=True)
    set_face("idle")
    for line in sys.stdin:
        cmd = line.strip()
        if cmd == "quit":
            break
        if call and call[0].done.is_set():
            call.clear()                  # the last call ended by itself (e.g. mic problem)
        if cmd == "call":
            if not call and not busy.is_set():
                call.append(Call(stt, st, speak_q, threshold))
                threading.Thread(target=call[0].run, daemon=True).start()
            continue
        if cmd in ("hangup", "stop") and call:
            end_call()
            continue
        if call:                          # during a call, the push-to-talk commands do nothing
            continue
        if RECORDING.is_set():            # any press while recording: done talking, answer now
            stop.set()
            continue
        if cmd == "stop" or busy.is_set() or SPEAKING.is_set():
            hush(speak_q)                 # press while it thinks or talks: be quiet
            set_face("idle")
            continue
        if cmd == "listen":
            hush(speak_q)
            stop.clear()
            busy.set()
            threading.Thread(target=turn, daemon=True).start()
    end_call()
    hush(speak_q)
    speak_q.put(None)


if __name__ == "__main__":
    main()
