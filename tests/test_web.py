"""Tests for the web tools (nubbe_web.py) and going online from the chat, with the fake brain.

    .venv\\Scripts\\python -m pytest tests -q

No real internet: a tiny local server plays DuckDuckGo and a web page.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_app import app, chat, chat_requests, data_dir, fake, get, post, reply_of, serve, base  # noqa: F401

web = app.web
REAL_CHECK_URL, REAL_DNS = web.check_url, socket.getaddrinfo   # before any test swaps them out


# ---------------------------------------------------------------------------
# a fake internet
# ---------------------------------------------------------------------------
def ddg_page(base_url):
    return f"""<html><body>
<div class="result results_links result--ad"><a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=x">Buy snakes</a></div>
<div class="result results_links"><h2 class="result__title">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg={base_url}%2Fpython&amp;rut=abc">Download <b>Python</b> | Python.org</a></h2>
  <a class="result__snippet" href="x">The latest <b>Python</b> release is 3.14.0.</a></div>
<div class="result results_links"><h2 class="result__title">
  <a rel="nofollow" class="result__a" href="https://docs.python.org/3/whatsnew/">What's New</a></h2>
  <a class="result__snippet" href="x">Changes in each version.</a></div>
</body></html>"""


PAGE = """<html><head><title>Download Python | Python.org</title><script>var x = "noise";</script></head>
<body><nav>Menu Docs Community</nav><header>Top banner</header>
<main><h1>Download the latest version</h1><p>Python 3.14.0 is the newest release.</p>
<p>""" + "More release notes here. " * 30 + """</p></main><footer>Copyright</footer></body></html>"""


@pytest.fixture
def internet(monkeypatch):
    """A local server playing DuckDuckGo and python.org. Every request it gets is in .hits."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            hits.append(("POST", self.path, body))
            self.reply(ddg_page(url.replace(":", "%3A").replace("/", "%2F")))

        def do_GET(self):
            hits.append(("GET", self.path, ""))
            if self.path == "/python":
                self.reply(PAGE)
            elif self.path == "/file.zip":
                self.reply("PK", "application/zip")
            else:
                self.send_error(404)

        def reply(self, text, kind="text/html"):
            data = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(web, "SEARCH_URLS", [url + "/html/"])
    monkeypatch.setattr(web, "check_url", lambda u: u)   # the fake internet lives on localhost
    monkeypatch.setattr(web, "_net", {"online": True, "until": 1e18})
    server.url, server.hits = url, hits
    yield server
    server.shutdown()
    server.server_close()


def log_lines(data_dir):
    f = data_dir / "web.log"
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def tool_results(fake):
    return [m["content"] for m in chat_requests(fake)[-1]["messages"] if m.get("role") == "tool"]


def offered(request):
    return [t["function"]["name"] for t in request.get("tools", [])]


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_parse_duckduckgo_html_results():
    results = web.parse_results(ddg_page("https%3A%2F%2Fwww.python.org"))
    assert results == [
        {"title": "Download Python | Python.org", "url": "https://www.python.org/python",
         "snippet": "The latest Python release is 3.14.0."},
        {"title": "What's New", "url": "https://docs.python.org/3/whatsnew/", "snippet": "Changes in each version."},
    ]   # the ad is gone, the redirect link is unwrapped


def test_parse_duckduckgo_lite_results():
    page = """<table><tr><td><a rel="nofollow" href="https://www.python.org/" class='result-link'>Python.org</a></td></tr>
              <tr><td class='result-snippet'>Official <b>home</b> of Python.</td></tr></table>"""
    assert web.parse_results(page) == [{"title": "Python.org", "url": "https://www.python.org/",
                                        "snippet": "Official home of Python."}]


def test_page_text_is_readable():
    title, text = web.page_text(PAGE)
    assert title == "Download Python | Python.org"
    assert text.startswith("Download the latest version\n\nPython 3.14.0 is the newest release.")
    assert "noise" not in text and "Menu" not in text and "Copyright" not in text and "banner" not in text


def test_local_addresses_are_never_opened():
    for bad in ("http://127.0.0.1:8765/api/chats", "http://localhost/", "http://192.168.1.1/admin",
                "http://10.0.0.5/", "http://[::1]/", "file:///C:/Windows/win.ini", "ftp://example.com/", "python.org"):
        with pytest.raises(web.WebError):
            web.check_url(bad)
    handler = web._SafeRedirects()
    with pytest.raises(web.WebError):   # a public page can't redirect us to this laptop either
        handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1:8765/api/memory")


# ---------------------------------------------------------------------------
# online / offline
# ---------------------------------------------------------------------------
def test_online_check_is_local_and_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(web, "_connected", lambda: calls.append(1) or True)
    monkeypatch.setattr(web, "_net", {"online": True, "until": 0.0})
    assert web.online() and web.online() and web.online()
    assert len(calls) == 1                       # asked once, then cached for a minute
    web.mark_offline()
    assert web.online() is False and len(calls) == 1


def test_no_internet_marks_offline_fast(monkeypatch):
    monkeypatch.setattr(web, "_net", {"online": True, "until": 1e18})

    def no_dns(*a, **k):
        raise socket.gaierror(11001, "getaddrinfo failed")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    with pytest.raises(web.WebError, match="No internet"):
        web.web_search("latest python")
    assert web.online() is False                 # the next chats don't even offer the web tools


# ---------------------------------------------------------------------------
# in the chat
# ---------------------------------------------------------------------------
def test_search_then_read_with_sources(base, fake, internet, data_dir):
    page = internet.url + "/python"
    fake.plan = [("web_search", {"query": "latest python version"}), ("read_page", {"url": page})]
    events = chat(base, "What's the latest version of Python?")
    tools = [e["text"] for e in events if e["type"] == "tool"]
    assert tools == ["searching: latest python version", "reading: 127.0.0.1"]
    assert "confirm" not in [e["type"] for e in events]          # no Yes needed to look things up
    assert "3.14.0 is the newest release" in reply_of(events)
    first = chat_requests(fake)[-3]
    assert offered(first) == ["web_search", "read_page"]
    assert "Going online" in first["messages"][0]["content"] and "Sources:" in first["messages"][0]["content"]
    assert internet.hits[0] == ("POST", "/html/", "q=latest+python+version")   # only the search words went out
    assert [(e["tool"], e.get("query") or e.get("url"), e["outcome"]) for e in log_lines(data_dir)] == [
        ("web_search", "latest python version", "ok"), ("read_page", page, "ok")]


def test_search_results_go_to_the_brain(base, fake, internet):
    fake.plan = [("web_search", {"query": "python"})]
    chat(base, "Look it up: python")
    result = tool_results(fake)[0]
    assert result.startswith(f"1. Download Python | Python.org\n{internet.url}/python\nThe latest Python release")
    assert "Buy snakes" not in result


def test_normal_chat_makes_no_network_calls(base, fake, internet, monkeypatch):
    fetched = []
    monkeypatch.setattr(web, "fetch", lambda *a, **k: fetched.append(a))
    events = chat(base, "Explain recursion")
    assert reply_of(events) and fetched == [] and internet.hits == []


def test_offline_means_no_web_tools(base, fake, internet, monkeypatch):
    monkeypatch.setattr(web, "_net", {"online": False, "until": 1e18})
    fake.plan = [("web_search", {"query": "news"})]
    events = chat(base, "What's in the news today?")
    sent = chat_requests(fake)[-1]
    assert "web_search" not in offered(sent) and internet.hits == []
    assert "no internet right now" in sent["messages"][0]["content"]
    assert "couldn't check online" in sent["messages"][0]["content"]
    assert reply_of(events) and events[-1]["type"] == "done"


def test_web_switch(base, fake, internet):
    assert get(base, "/api/settings")["web"] is True and get(base, "/api/settings")["web_allowed"] is True
    post(base, "/api/settings", {"web": False})
    chat(base, "Latest Python?")
    sent = chat_requests(fake)[-1]
    assert offered(sent) == [] and "Web switch off" in sent["messages"][0]["content"]
    post(base, "/api/settings", {"web": True})
    chat(base, "Latest Python?")
    assert offered(chat_requests(fake)[-1]) == ["web_search", "read_page"]


def test_config_can_turn_web_off_completely(base, fake, internet, monkeypatch):
    monkeypatch.setattr(web, "ENABLED", False)
    assert get(base, "/api/settings")["web_allowed"] is False      # the app hides its Web button
    chat(base, "Latest Python?")
    assert offered(chat_requests(fake)[-1]) == []


def test_voice_can_search_but_not_touch_files(base, fake, internet, tmp_path, monkeypatch):
    monkeypatch.setattr(app.tools, "WORKSPACE_DIR", str(tmp_path))
    chat(base, "What's the weather?", voice=True, source="call")
    sent = chat_requests(fake)[-1]
    assert offered(sent) == ["web_search", "read_page"]
    assert "say which site it came from" in sent["messages"][0]["content"]
    chat(base, "Hi")
    assert offered(chat_requests(fake)[-1])[:1] == ["list_dir"] and "web_search" in offered(chat_requests(fake)[-1])


def test_web_errors_are_friendly(base, fake, internet):
    fake.plan = [("read_page", {"url": internet.url + "/file.zip"}), ("read_page", {"url": internet.url + "/missing"})]
    chat(base, "Read these")
    results = [m["content"] for m in chat_requests(fake)[-1]["messages"] if m.get("role") == "tool"]
    assert results == ["That link is a application/zip file, not a page I can read.",
                       "The site answered with error 404."]


def test_open_link_endpoint(base, monkeypatch):
    opened = []
    monkeypatch.setattr(app.webbrowser, "open", opened.append)
    assert post(base, "/api/open", {"url": "https://www.python.org/"}) == {"ok": True}
    assert post(base, "/api/open", {"url": "javascript:alert(1)"}) == {"ok": False}
    assert post(base, "/api/open", {"url": "file:///C:/Windows"}) == {"ok": False}
    assert opened == ["https://www.python.org/"]


def test_offline_answers_dont_guess_current_facts(base, fake, internet, monkeypatch):
    monkeypatch.setattr(web, "_net", {"online": False, "until": 1e18})
    chat(base, "Latest Python?")
    assert "Don't name a latest version" in chat_requests(fake)[-1]["messages"][0]["content"]

    def no_dns(host, *a, **k):   # online a moment ago, gone now: the tool's answer carries the same advice
        if host == "127.0.0.1":
            return REAL_DNS(host, *a, **k)
        raise socket.gaierror(11001, "getaddrinfo failed")
    monkeypatch.setattr(web, "_net", {"online": True, "until": 1e18})
    monkeypatch.setattr(web, "check_url", REAL_CHECK_URL)
    monkeypatch.setattr(web, "SEARCH_URLS", ["https://html.duckduckgo.com/html/"])
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    fake.plan = [("web_search", {"query": "latest python"})]
    chat(base, "Latest Python?")
    assert tool_results(fake)[0] == web.NO_INTERNET


def test_unexpected_network_errors_still_get_an_answer(base, fake, internet, monkeypatch):
    def broken(*a, **k):
        raise ConnectionResetError("connection reset")
    monkeypatch.setattr(web, "fetch", broken)
    fake.plan = [("read_page", {"url": "https://example.com/"})]
    events = chat(base, "Read this")
    assert tool_results(fake)[0].startswith("That didn't work (connection reset).")
    assert events[-1]["type"] == "done" and reply_of(events)


def test_voice_is_reminded_to_stay_short_after_a_search(base, fake, internet):
    fake.plan = [("web_search", {"query": "f1 winner"})]
    chat(base, "Who won the last F1 race?", voice=True, source="call")
    assert tool_results(fake)[0].endswith(app.VOICE_AFTER_TOOL)
    fake.plan = [("web_search", {"query": "f1 winner"})]
    chat(base, "Who won the last F1 race?")
    assert not tool_results(fake)[0].endswith(app.VOICE_AFTER_TOOL)   # text chats can use lists and links


def test_no_retrying_after_the_internet_drops(base, fake, internet, monkeypatch):
    monkeypatch.setattr(web, "check_url", REAL_CHECK_URL)
    monkeypatch.setattr(web, "SEARCH_URLS", ["https://html.duckduckgo.com/html/"])

    def no_dns(host, *a, **k):
        if host == "127.0.0.1":
            return REAL_DNS(host, *a, **k)
        raise socket.gaierror(11001, "getaddrinfo failed")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    fake.plan = [("web_search", {"query": "node"}), ("web_search", {"query": "node again"})]
    events = chat(base, "Latest Node.js?")
    assert [e["text"] for e in events if e["type"] == "tool"] == ["searching: node"]   # tried once
    assert offered(chat_requests(fake)[-1]) == []
