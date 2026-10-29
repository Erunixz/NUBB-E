#!/usr/bin/env python3
"""A stand-in llama-server for tests: no GPU, no model, instant replies.

    python tests/fake_llm.py --port 8080

Speaks the same API the app uses:
  GET  /health                -> 503 while "loading" (--load-seconds), then {"status": "ok"}
  POST /v1/chat/completions   -> streams reasoning_content chunks, then content chunks
Memory-extraction calls (the app's EXTRACT_PROMPT) get "- fact" lines back.
Ignores any other llama-server flags, so the app can launch it like the real one.
Every request body is kept in FakeLLM.requests so tests can check what was sent.
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY = "Hi Erfan! I'm NUBB-E, ready to build. *happy beep*"
FACT = "Erfan's laptop has an RTX 4060 GPU"


class FakeLLM:
    def __init__(self, port=0, load_seconds=0.0):
        self.requests: list[dict] = []
        self.ready_at = time.time() + load_seconds
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != "/health":
                    self.send_error(404)
                elif time.time() < fake.ready_at:
                    self.send_json({"error": {"message": "Loading model"}}, 503)
                else:
                    self.send_json({"status": "ok"})

            def do_POST(self):
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                fake.requests.append(body)
                system = body["messages"][0]["content"] if body["messages"] else ""
                if "maintain a small memory" in system:
                    said = body["messages"][-1]["content"].split("Erfan said:")[-1].split("NUBB-E replied:")[0]
                    # repeats the fact even when it's "already known", so the app's dedupe gets tested
                    text = f"- {FACT}.\n- {FACT}" if "RTX 4060" in said else "NONE"
                    thinking = False
                else:
                    text = REPLY
                    thinking = (body.get("chat_template_kwargs") or {}).get("enable_thinking", True)
                if not body.get("stream"):
                    self.send_json({"choices": [{"message": {"role": "assistant", "content": text}}]})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if thinking:
                    for word in ("Let me", " think."):
                        self.chunk({"reasoning_content": word})
                for i in range(0, len(text), 8):
                    self.chunk({"content": text[i:i + 8]})
                self.wfile.write(b"data: [DONE]\n\n")

            def chunk(self, delta):
                self.wfile.write(f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n".encode())
                self.wfile.flush()

            def send_json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--load-seconds", type=float, default=0.0, help="pretend to load for this long")
    args, _ = p.parse_known_args()   # ignore real llama-server flags
    FakeLLM(args.port, args.load_seconds).server.serve_forever()
