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
    python nubbe_voice.py                          # Kokoro, voice af_heart
    python nubbe_voice.py --voice am_michael       # another Kokoro voice
    python nubbe_voice.py --voice voices/en_US-amy-medium.onnx   # Piper instead
Press Enter, talk, then stop talking. It answers out loud. Ctrl+C to quit.
"""
import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request

# Kokoro runs on the CPU by default so it never fights the brain for the 8 GB GPU.
# Pass --tts-gpu to let it use the GPU instead.
if "--tts-gpu" not in sys.argv:
    os.environ["CUDA_VISIBLE_DEVICES"] = ""

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

try:
    from nubbe_face import set_face
except ImportError:
    def set_face(*args, **kwargs):
        pass

APP_URL = "http://127.0.0.1:8765/api/chat"            # NUBB-E app (saves chats + memory)
LLM_URL = "http://localhost:8080/v1/chat/completions"  # fallback: brain directly
GENERATING = threading.Event()   # LLM is still writing the reply
SPEAKING = threading.Event()     # a sentence is playing right now
MIC_RATE = 16000          # whisper expects 16 kHz mono
SILENCE_SECONDS = 0.8     # stop recording after this much quiet
MAX_RECORD_SECONDS = 20

VOICE_RULES = (
    "\n\n## Voice mode\n"
    "You are speaking out loud right now. Reply in ONE short sentence, like a quick spoken reply. "
    "Only go longer if Erfan explicitly asks for detail. "
    "No markdown, no lists, no code blocks, no emoji: everything you write is read aloud."
)


# ---------- listening ----------
def record_until_silence(threshold: float) -> np.ndarray:
    """Record from the mic until the speaker goes quiet."""
    chunk = int(MIC_RATE * 0.1)  # 100 ms blocks
    frames, quiet_blocks, heard_voice = [], 0, False
    needed_quiet = int(SILENCE_SECONDS / 0.1)

    with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="float32") as stream:
        for _ in range(int(MAX_RECORD_SECONDS / 0.1)):
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
        with open(voice + ".json") as f:
            self.rate = json.load(f)["audio"]["sample_rate"]
        self.voice = voice

    def synth(self, text: str):
        raw = subprocess.run(["piper", "--model", self.voice, "--output_raw"],
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


def speaker_worker(q: "queue.Queue[str | None]", tts) -> None:
    """Turns sentences into audio and plays them in order."""
    while True:
        sentence = q.get()
        if sentence is None:
            break
        sentence = clean_for_speech(sentence)
        if not sentence:
            continue
        audio, rate = tts.synth(sentence)
        if audio.size:
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
            print(delta, end="", flush=True)
            full += delta
            buf += delta
            # send each finished sentence to the speaker right away
            while (m := re.search(r"[.!?](\s|$)", buf)):
                speak_q.put(buf[: m.end()])
                buf = buf[m.end():]
    finally:
        GENERATING.clear()
    if buf.strip():
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
    p.add_argument("--voice", default="af_heart",
                   help="Kokoro voice name (af_heart, af_bella, am_michael, ...) or a Piper .onnx file")
    p.add_argument("--speed", type=float, default=1.05, help="Kokoro speaking speed")
    p.add_argument("--piper-voice", help="Piper .onnx voice to fall back to if Kokoro can't load")
    p.add_argument("--tts-gpu", action="store_true", help="let Kokoro use the GPU (default: CPU)")
    p.add_argument("--prompt", default="nubbe_system_prompt.txt", help="personality file")
    p.add_argument("--whisper", default="small.en", help="tiny.en / base.en / small.en")
    p.add_argument("--threshold", type=float, default=0.015, help="mic loudness to count as talking")
    args = p.parse_args()

    with open(args.prompt, encoding="utf-8") as f:
        system = f.read() + VOICE_RULES

    print("Loading speech models on CPU...")
    stt = WhisperModel(args.whisper, device="cpu", compute_type="int8")  # keeps the GPU free for Qwen
    tts = load_tts(args.voice, args.speed, args.piper_voice)

    speak_q: "queue.Queue[str | None]" = queue.Queue()
    threading.Thread(target=speaker_worker, args=(speak_q, tts), daemon=True).start()

    st = {"use_app": True, "chat_id": None, "history": [{"role": "system", "content": system}]}
    set_face("happy", "Hi Erfan!")
    print("NUBB-E is listening. Press Enter, then talk. Ctrl+C to quit.")
    try:
        while True:
            input()
            print("(listening...)")
            set_face("listening")
            heard = transcribe(stt, record_until_silence(args.threshold))
            if not heard:
                print("(didn't catch that)")
                set_face("idle", "Didn't catch that.")
                continue
            print(f"You: {heard}\nNUBB-E: ", end="")
            set_face("thinking", heard, who="you")
            try:
                stream_reply(heard, st, speak_q)
            except (urllib.error.URLError, ConnectionError) as e:
                set_face("sleep", "Can't reach my brain.")
                print(f"\n(can't reach the brain: {e})")
    except KeyboardInterrupt:
        speak_q.put(None)
        set_face("sleep", "bye! *happy beep*")
        print("\nbye! *happy beep*")


if __name__ == "__main__":
    main()
