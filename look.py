#!/usr/bin/env python3
"""look: screenshot your screen and ask your local model about it.

Usage:
    look                         -> "What's on my screen?"
    look "why is this erroring"  -> asks your question about the screenshot
    look -m 2 "explain this"     -> use monitor 2
    look --save "what is this"   -> also saves the screenshot to last_look.jpg

Goes through the NUBB-E app when it's open (the look is saved in your chats,
and the app hides its own window while it captures). If the app is closed,
it asks llama-server directly instead.

Needs: pip install mss pillow
Needs llama-server running with a vision model + its mmproj file.
nubbe_app.py uses screenshot_b64() and hidden_window() from here too.
"""
import argparse
import base64
import io
import json
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

try:
    from nubbe_config import CFG
    LLM_PORT, APP_PORT = CFG["brain"]["port"], CFG["app"]["port"]
except ImportError:
    LLM_PORT, APP_PORT = 8080, 8765

DEFAULT_URL = f"http://localhost:{LLM_PORT}/v1/chat/completions"
APP_URL = f"http://127.0.0.1:{APP_PORT}/api/look"
DEFAULT_QUESTION = "What's on my screen? Be brief."


def screenshot(monitor_idx: int = 1, max_width: int = 1280):
    """Grabs one monitor (1 = primary, 0 = all) as a PIL image, at most max_width wide."""
    import mss                     # imported here so the app still runs without them
    from PIL import Image
    with getattr(mss, "MSS", mss.mss)() as sct:   # mss 10+ renamed mss.mss to mss.MSS
        monitors = sct.monitors    # [0] = all screens combined, [1] = primary, [2] = second...
        if monitor_idx >= len(monitors):
            raise ValueError(f"Monitor {monitor_idx} not found (you have {len(monitors) - 1}).")
        shot = sct.grab(monitors[monitor_idx])

    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > max_width:      # smaller image = faster answer, less context used
        new_h = round(img.height * max_width / img.width)
        img = img.resize((max_width, new_h), Image.LANCZOS)
    return img


def to_b64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def screenshot_b64(monitor_idx: int = 1, max_width: int = 1280) -> str:
    return to_b64(screenshot(monitor_idx, max_width))


@contextmanager
def hidden_window(titles=("NUBB-E", "NUBB-E Quick")):
    """Windows: hides NUBB-E's own windows (app and quick bar) while the screenshot is taken,
    so it sees what's behind them, then shows them again and gives focus back."""
    hidden, u32, active = [], None, 0
    if sys.platform == "win32":
        try:
            import ctypes
            u32 = ctypes.windll.user32
            active = u32.GetForegroundWindow()
            for title in titles:
                hwnd = u32.FindWindowW(None, title)
                if hwnd and u32.IsWindowVisible(hwnd) and not u32.IsIconic(hwnd):
                    u32.ShowWindow(hwnd, 0)    # SW_HIDE
                    hidden.append(hwnd)
            if hidden:
                time.sleep(0.25)               # let the screen repaint without them
        except Exception:
            hidden = []
    try:
        yield
    finally:
        for hwnd in hidden:
            u32.ShowWindow(hwnd, 8)            # SW_SHOWNA (show without stealing focus)
        if active in hidden:
            u32.SetForegroundWindow(active)


def stream(req, app: bool) -> None:
    with urllib.request.urlopen(req) as resp:
        for raw in resp:  # stream tokens as they arrive
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            obj = json.loads(data)
            if app:
                if obj.get("type") == "error":
                    sys.exit(obj["text"])
                delta = obj.get("text") if obj.get("type") == "delta" else None
            else:
                delta = obj["choices"][0]["delta"].get("content")
            if delta:
                print(delta, end="", flush=True)
    print()


def ask_app(question: str, monitor: int, width: int) -> bool:
    """Asks through the NUBB-E app. Returns False if the app isn't open."""
    body = {"message": question, "monitor": monitor, "width": width}
    req = urllib.request.Request(APP_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        stream(req, app=True)
        return True
    except urllib.error.URLError:
        return False


def ask(image_b64: str, question: str, url: str) -> None:
    payload = {
        "model": "local",
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }
        ],
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        stream(req, app=False)
    except urllib.error.URLError as e:
        sys.exit(f"Can't reach the model at {url}. Is llama-server running?\n({e})")


def main() -> None:
    p = argparse.ArgumentParser(description="Screenshot + ask your local model.")
    p.add_argument("question", nargs="*", help="what to ask about the screen")
    p.add_argument("-m", "--monitor", type=int, default=1, help="1 = primary, 2 = second, 0 = all")
    p.add_argument("-w", "--width", type=int, default=1280, help="max screenshot width in px")
    p.add_argument("--url", default=DEFAULT_URL, help="llama-server chat endpoint (used if the app is closed)")
    p.add_argument("--save", action="store_true", help="save screenshot as last_look.jpg")
    args = p.parse_args()

    question = " ".join(args.question) or DEFAULT_QUESTION
    if args.save:
        screenshot(args.monitor, args.width).save("last_look.jpg", quality=85)
    if ask_app(question, args.monitor, args.width):
        return
    try:
        image = screenshot_b64(args.monitor, args.width)
    except ValueError as e:
        sys.exit(str(e))
    ask(image, question, args.url)


if __name__ == "__main__":
    main()
