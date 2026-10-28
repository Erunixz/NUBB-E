#!/usr/bin/env python3
"""look: screenshot your screen and ask your local model about it.

Usage:
    look                         -> "What's on my screen?"
    look "why is this erroring"  -> asks your question about the screenshot
    look -m 2 "explain this"     -> use monitor 2
    look --save "what is this"   -> also saves the screenshot to last_look.jpg

Needs: pip install mss pillow
Needs llama-server running with a vision model + its mmproj file.
"""
import argparse
import base64
import io
import json
import sys
import urllib.error
import urllib.request

import mss
from PIL import Image

DEFAULT_URL = "http://localhost:8080/v1/chat/completions"


def grab(monitor_idx: int, max_width: int, save: bool) -> str:
    with mss.mss() as sct:
        monitors = sct.monitors  # [0] = all screens combined, [1] = primary, [2] = second...
        if monitor_idx >= len(monitors):
            sys.exit(f"Monitor {monitor_idx} not found (you have {len(monitors) - 1}).")
        shot = sct.grab(monitors[monitor_idx])

    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > max_width:  # smaller image = faster answer, less context used
        new_h = round(img.height * max_width / img.width)
        img = img.resize((max_width, new_h), Image.LANCZOS)

    if save:
        img.save("last_look.jpg", quality=85)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def ask(image_b64: str, question: str, url: str) -> None:
    payload = {
        "model": "local",
        "stream": True,
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
        with urllib.request.urlopen(req) as resp:
            for raw in resp:  # stream tokens as they arrive
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                delta = json.loads(data)["choices"][0]["delta"].get("content")
                if delta:
                    print(delta, end="", flush=True)
        print()
    except urllib.error.URLError as e:
        sys.exit(f"Can't reach the model at {url}. Is llama-server running?\n({e})")


def main() -> None:
    p = argparse.ArgumentParser(description="Screenshot + ask your local model.")
    p.add_argument("question", nargs="*", help="what to ask about the screen")
    p.add_argument("-m", "--monitor", type=int, default=1, help="1 = primary, 2 = second, 0 = all")
    p.add_argument("-w", "--width", type=int, default=1280, help="max screenshot width in px")
    p.add_argument("--url", default=DEFAULT_URL, help="llama-server chat endpoint")
    p.add_argument("--save", action="store_true", help="save screenshot as last_look.jpg")
    args = p.parse_args()

    question = " ".join(args.question) or "What's on my screen? Be brief."
    ask(grab(args.monitor, args.width, args.save), question, args.url)


if __name__ == "__main__":
    main()
