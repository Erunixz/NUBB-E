#!/usr/bin/env python3
"""nubbe: text chat with NUBB-E in your terminal, fully offline.

The face (nubbe_face.py) shows what it's doing: thinking while it reasons,
talking while the reply streams in. Works fine without the face too.

Usage:
    python nubbe_chat.py            # smart mode, reasons before hard answers
    python nubbe_chat.py --fast     # skips reasoning, quicker replies
In the chat:  /reset clears the conversation, /exit quits.
"""
import argparse
import json
import time
import urllib.error
import urllib.request

try:
    from nubbe_face import set_face
except ImportError:
    def set_face(*args, **kwargs):
        pass

try:
    from nubbe_config import CFG
    LLM_PORT = CFG["brain"]["port"]
except ImportError:
    LLM_PORT = 8080

URL = f"http://localhost:{LLM_PORT}/v1/chat/completions"
DIM, RESET = "\033[2m", "\033[0m"


def stream_reply(history: list, think: bool) -> str:
    payload = {"model": "local", "stream": True, "messages": history}
    if not think:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})

    set_face("thinking")
    reply, shown_thinking, last_push = "", False, 0.0
    print("NUBB-E: ", end="", flush=True)
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            delta = json.loads(data)["choices"][0]["delta"]
            if delta.get("reasoning_content"):          # model is reasoning: face keeps thinking
                if not shown_thinking:
                    print(f"{DIM}(thinking...){RESET} ", end="", flush=True)
                    shown_thinking = True
                continue
            piece = delta.get("content") or ""
            if not piece:
                continue
            reply += piece
            print(piece, end="", flush=True)
            now = time.time()
            if now - last_push > 0.15:                  # don't flood the face with updates
                set_face("talking", reply[-300:])
                last_push = now
    print("\n")
    set_face("idle", reply[-300:])
    return reply


def main() -> None:
    p = argparse.ArgumentParser(description="Chat with NUBB-E.")
    p.add_argument("--prompt", default="nubbe_system_prompt.txt", help="personality file")
    p.add_argument("--fast", action="store_true", help="skip reasoning for quicker replies")
    args = p.parse_args()

    with open(args.prompt, encoding="utf-8") as f:
        system = {"role": "system", "content": f.read()}
    history = [system]

    set_face("happy", "Hi Erfan!")
    print("NUBB-E is here. /reset to start over, /exit to quit.\n")
    try:
        while True:
            text = input("You: ").strip()
            if not text:
                continue
            if text == "/exit":
                break
            if text == "/reset":
                history = [system]
                set_face("happy", "Fresh start!")
                print("(conversation cleared)\n")
                continue
            set_face("thinking", text, who="you")
            history.append({"role": "user", "content": text})
            try:
                reply = stream_reply(history, think=not args.fast)
            except urllib.error.URLError as e:
                history.pop()
                set_face("sleep", "Can't reach my brain. Is llama-server running?")
                print(f"\n(can't reach the model at {URL}. Is llama-server running? {e})\n")
                continue
            history.append({"role": "assistant", "content": reply})
            history[1:] = history[1:][-40:]             # keep recent turns so it stays fast
    except (KeyboardInterrupt, EOFError):
        pass
    set_face("sleep", "bye! *happy beep*")
    print("\nbye! *happy beep*")


if __name__ == "__main__":
    main()
