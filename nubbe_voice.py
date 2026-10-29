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
    "\n\n## Voice mode\n"
    "You are speaking out loud right now. Reply in ONE short sentence, like a quick spoken reply. "
    "Only go longer if Erfan explicitly asks for detail. "
    "No markdown, no lists, no code blocks, no emoji: everything you write is read aloud. "
    "Out loud you may add a little robot sound now and then, like *happy beep* or *nerd beep*, "
    "but sparingly, never in every reply."
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


def transcribe(model: WhisperModel, audio: np.ndarray) -> str:
    segments, _ = model.transcribe(audio, language="en", vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


# ---------- speaking ----------
def clean_for_speech(text: str) -> str:
    text = re.sub(r"[*_`#>|]", "", text)            # strip markdown symbols
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", text)  # strip emoji
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


def hush(q: "queue.Queue[str | None]") -> None:
    """Stops speaking right now and drops whatever was still queued."""
    HUSH.set()
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break
    sd.stop()


def speaker_worker(q: "queue.Queue[str | None]", tts) -> None:
    """Turns sentences into audio and plays them in order."""
    while True:
        sentence = q.get()
        if sentence is None:
            break
        sentence = clean_for_speech(sentence)
        if not sentence or HUSH.is_set():
            continue
        audio, rate = tts.synth(sentence)
        if audio.size and not HUSH.is_set():
            SPEAKING.set()
            set_face("talking", sentence)
            sd.play(audio, samplerate=rate)
            sd.wait()
            SPEAKING.clear()
        if q.empty():  # nothing else queued: still writing, or done
            set_face("thinking" if GENERATING.is_set() else "idle")


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


def _deltas(heard: str, st: dict):
    if st["use_app"]:
        # through the app: chat gets saved, memories are used and learned
        req = urllib.request.Request(APP_URL, data=json.dumps(
            {"chat_id": st["chat_id"], "message": heard, "voice": True}).encode(),
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
        "max_tokens": 80,                                   # keeps it a one-liner
        "chat_template_kwargs": {"enable_thinking": False},  # skip slow reasoning in voice mode
        "messages": st["history"],
    }
    req = urllib.request.Request(LLM_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    yield from _deltas_from(req, st)


def stream_reply(heard: str, st: dict, speak_q: "queue.Queue[str | None]") -> str:
    full, buf = "", ""
    GENERATING.set()
    try:
        for delta in _deltas(heard, st):
            if HUSH.is_set():
                break            # stopped by the mic button: don't queue more speech
            print(delta, end="", flush=True)
            full += delta
            buf += delta
            # send each finished sentence to the speaker right away
            # speak each finished sentence right away, and long ones in pieces at commas,
            # so the voice starts sooner (short pieces are quicker to turn into audio)
            while True:
                if m := re.search(r"[.!?](\s|$)", buf):
                    end = m.end()
                elif len(buf) > 40 and (m := re.search(r"[,;:]\s", buf[20:])):
                    end = 20 + m.end()
                elif len(buf) > 50 and (m := re.search(r"\s(?=(and|but|because|so|which|while|or)\s)", buf[25:])):
                    end = 25 + m.end()      # a natural spoken pause before the joining word
                else:
                    break
                speak_q.put(buf[:end])
                buf = buf[end:]
    finally:
        GENERATING.clear()
    if buf.strip() and not HUSH.is_set():
        speak_q.put(buf)
    elif speak_q.empty() and not SPEAKING.is_set():
        set_face("idle")   # nothing left to say
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
    print("NUBB-E is listening. Press Enter, then talk. Ctrl+C to quit.")
    try:
        while True:
            input()
            one_turn(stt, st, speak_q, args.threshold)
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
    suggested = round((room * voice) ** 0.5, 4)   # halfway between them (on a log scale)
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


def serve(stt, st: dict, speak_q, threshold: float) -> None:
    """Controlled by the app's mic button: reads one command per line on stdin.
    listen = start a turn, or stop recording early if already listening.
    stop   = stop listening / stop talking.   quit = exit."""
    stop = threading.Event()
    busy = threading.Event()

    def turn():
        try:
            one_turn(stt, st, speak_q, threshold, stop)
        finally:
            busy.clear()

    print("READY", flush=True)
    set_face("idle")
    for line in sys.stdin:
        cmd = line.strip()
        if cmd == "quit":
            break
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
    hush(speak_q)
    speak_q.put(None)


if __name__ == "__main__":
    main()
