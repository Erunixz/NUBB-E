"""NUBB-E's web tools: search the web and read pages, only when there's internet.

    import nubbe_web as web
    web.online()                          # quick, cached for a minute, sends nothing online
    web.SCHEMAS                           # OpenAI-style tools for llama-server (--jinja)
    web.call("web_search", {"query": "latest python version"}, log_file)

web_search uses DuckDuckGo's HTML results (no account, no API key). read_page
fetches one page and returns its readable text. Only the search words and the
pages it opens go online: chats, memories and files never do. Every search and
page is logged to ~/.nubbe/web.log (the app passes the path). Standard library only.
"""
import ctypes
import ipaddress
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from nubbe_config import CFG

ENABLED = CFG["web"]["web_enabled"]   # false: never offered, whatever the app's Web switch says
SEARCH_URLS = ["https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"]
TIMEOUT = 8                    # seconds per request: never hang waiting for the network
MAX_RESULTS = 6
MAX_PAGE_BYTES = 2_000_000
MAX_PAGE_CHARS = 12_000        # about 3k tokens per page
ONLINE_CACHE_SECONDS = 60
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

TOOLS = ("web_search", "read_page")


class WebError(Exception):
    """A friendly message for the model, like 'that page didn't load'."""


def _tool(name, description, props, required):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": props, "required": required}}}


SCHEMAS = [
    _tool("web_search", "Search the web (DuckDuckGo). Returns the top results: title, link and a short snippet.",
          {"query": {"type": "string", "description": "What to search for, like 'latest Python version'."}},
          ["query"]),
    _tool("read_page", "Read the text of one web page, like a search result or a link Erfan gave you.",
          {"url": {"type": "string", "description": "The full http(s) link."}}, ["url"]),
]

ONLINE_RULES = (
    "\n\n## Going online\n"
    "You run locally, and right now there's internet, so you can use web_search and read_page. "
    "Only go online when the answer depends on something you can't know locally: news and current events, "
    "prices, weather, recent releases, versions or docs, a link Erfan gives you, or a fact you aren't sure about. "
    "Normal chat, coding, explaining, math, his files and Look stay local: don't search for those. "
    "If he says \"search\" or \"look it up\", search. If he says not to go online, don't. "
    "When the snippets aren't enough, read_page the best result. "
)
CITE = ("When your answer used the web, end with the sources as markdown links, like: "
        "Sources: [python.org](https://www.python.org/downloads/). Never make up a link.")
CITE_VOICE = "When your answer used the web, say which site it came from (no links: you're talking out loud)."
# without internet, the model tends to state stale "latest" facts from its training as if they were current
NO_GUESS = (
    "Keep that answer short: say plainly you couldn't check, then tell him where to look (a site or a command). "
    "Don't name a latest version, price, score, winner or other current fact from memory, because your "
    "training is older than today and it would likely be wrong. Questions that don't need current info "
    "you answer normally."
)
OFFLINE_RULES = (
    "\n\n## Going online\n"
    "There's no internet right now, so you can't search the web. If the answer depends on current info "
    "(news, prices, weather, latest versions, recent events), say you couldn't check online. " + NO_GUESS
)
SWITCHED_OFF_RULES = (
    "\n\n## Going online\n"
    "Erfan has the Web switch off, so you can't search the web. If the answer depends on current info, "
    "say you can't check online while Web is off. " + NO_GUESS
)
# what a web tool returns when the internet turns out to be gone mid-reply
NO_INTERNET = ("No internet right now, so I couldn't check online (the connection is down, so don't search "
               "again). Tell Erfan you couldn't check online. " + NO_GUESS)


def rules(switched_on: bool, net: bool, voice: bool = False) -> str:
    # the model doesn't know today's date, so "latest" and search words would be stuck in its training year
    today = f"\nToday is {time.strftime('%A, %B %d, %Y').replace(' 0', ' ')}."
    if not ENABLED or not switched_on:
        return SWITCHED_OFF_RULES + today
    if not net:
        return OFFLINE_RULES + today
    return ONLINE_RULES + (CITE_VOICE if voice else CITE) + today + (
        " Search with words that fit today's date, and prefer the newest results.")


# ---------------------------------------------------------------------------
# is there internet? (fast, cached; the local check sends nothing over the network)
# ---------------------------------------------------------------------------
_net = {"online": True, "until": 0.0}


def _connected() -> bool:
    """Asks Windows whether any network connection is up. Purely local, no traffic.
    Elsewhere we assume yes, and a failed request marks us offline instead."""
    if sys.platform != "win32":
        return True
    try:
        flags = ctypes.c_ulong()
        return bool(ctypes.windll.wininet.InternetGetConnectedState(ctypes.byref(flags), 0))
    except (OSError, AttributeError):
        return True


def online() -> bool:
    if time.time() >= _net["until"]:
        _net.update(online=_connected(), until=time.time() + ONLINE_CACHE_SECONDS)
    return _net["online"]


def mark_offline() -> None:
    """A request couldn't reach the internet: don't offer the web tools for a minute."""
    _net.update(online=False, until=time.time() + ONLINE_CACHE_SECONDS)


# ---------------------------------------------------------------------------
# fetching (public addresses only, so a page can't point NUBB-E at this laptop or the home network)
# ---------------------------------------------------------------------------
def check_url(url: str) -> str:
    url = str(url).strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebError(f"'{url}' isn't a web link (it needs to start with http:// or https://).")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        if any(parts.hostname == urllib.parse.urlsplit(u).hostname for u in SEARCH_URLS):
            mark_offline()   # can't even find DuckDuckGo: we're offline
            raise WebError(NO_INTERNET) from None
        raise WebError(f"Couldn't find {parts.hostname}. No internet, or the link is wrong.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise WebError(f"{parts.hostname} is on this computer or the local network, so I won't open it.")
    return url


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)   # a redirect can't sneak to a local address either
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirects)


def fetch(url: str, data: bytes | None = None) -> tuple[str, str]:
    """Returns (text, content type). Raises WebError with a friendly message."""
    req = urllib.request.Request(check_url(url), data=data, headers={
        "User-Agent": UA, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5", "Accept-Language": "en-US,en;q=0.8"})
    try:
        with _opener.open(req, timeout=TIMEOUT) as r:
            kind = r.headers.get_content_type()
            raw = r.read(MAX_PAGE_BYTES + 1)
            charset = r.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        raise WebError(f"The site answered with error {e.code}.") from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.gaierror) or "getaddrinfo" in str(reason):
            mark_offline()
            raise WebError(NO_INTERNET) from None
        raise WebError(f"The page didn't load ({reason}).") from None
    try:
        return raw[:MAX_PAGE_BYTES].decode(charset, "replace"), kind
    except LookupError:
        return raw[:MAX_PAGE_BYTES].decode("utf-8", "replace"), kind


# ---------------------------------------------------------------------------
# search results and readable page text
# ---------------------------------------------------------------------------
class _Results(HTMLParser):
    """Reads DuckDuckGo's html and lite result pages."""

    def __init__(self):
        super().__init__()
        self.results, self._in, self._tag = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and ("result__a" in cls or "result-link" in cls):
            self.results.append({"title": "", "url": _real_link(a.get("href", "")), "snippet": ""})
            self._in = "title"
        elif self.results and ("result__snippet" in cls or "result-snippet" in cls):
            self._in, self._tag = "snippet", tag

    def handle_endtag(self, tag):
        if self._in == "title" and tag == "a" or self._in == "snippet" and tag == self._tag:
            self._in = None

    def handle_data(self, data):
        if self._in and self.results:
            self.results[-1][self._in] += data


def _real_link(href: str) -> str:
    """DuckDuckGo wraps result links in a redirect (//duckduckgo.com/l/?uddg=<real link>)."""
    if href.startswith("//"):
        href = "https:" + href
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
    return q["uddg"][0] if "uddg" in q else href


def parse_results(page: str) -> list[dict]:
    p = _Results()
    p.feed(page)
    out, seen = [], set()
    for r in p.results:
        url, title = r["url"], " ".join(r["title"].split())
        if not url.startswith("http") or "duckduckgo.com/y.js" in url or url in seen or not title:
            continue   # ads and duplicates
        seen.add(url)
        out.append({"title": title, "url": url, "snippet": " ".join(r["snippet"].split())})
    return out


SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe",
             "template", "button", "select"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6",
              "pre", "blockquote", "table", "ul", "ol", "dd", "dt"}


class _Text(HTMLParser):
    """Readable text of a page: skips scripts, menus and footers; prefers <main> or <article>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.all, self.main = "", [], []
        self._skip = self._main = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in ("main", "article"):
            self._main += 1
        elif tag == "title":
            self._in_title = True
        if tag in BLOCK_TAGS:
            self._add("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._add("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in ("main", "article"):
            self._main = max(0, self._main - 1)
        elif tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self._add("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._add(data)

    def _add(self, s):
        self.all.append(s)
        if self._main:
            self.main.append(s)


def _tidy(parts) -> str:
    lines = (" ".join(line.split()) for line in "".join(parts).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def page_text(html: str) -> tuple[str, str]:
    """(title, readable text) of an HTML page."""
    p = _Text()
    p.feed(html)
    main = _tidy(p.main)
    return " ".join(p.title.split()), main if len(main) > 500 else _tidy(p.all)


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------
def web_search(query) -> str:
    query = " ".join(str(query).split())
    if not query:
        raise WebError("Give me something to search for.")
    results, err = [], None
    for url in SEARCH_URLS:   # the lite page is the backup if the main one changes or blocks us
        try:
            page, _ = fetch(url, urllib.parse.urlencode({"q": query}).encode())
        except WebError as e:
            err = e
            if "No internet" in str(e):
                raise
            continue
        results = parse_results(page)[:MAX_RESULTS]
        if results:
            break
    if not results:
        if err:
            raise err
        return f"No results for '{query}'."
    return "\n\n".join(f"{i}. {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(results, 1))


def read_page(url) -> str:
    text, kind = fetch(url)
    if kind in ("text/html", "application/xhtml+xml"):
        title, body = page_text(text)
    elif kind.startswith("text/") or kind in ("application/json", "application/xml"):
        title, body = "", text.strip()
    else:
        raise WebError(f"That link is a {kind} file, not a page I can read.")
    if not body:
        raise WebError("The page loaded but had no readable text (it may need JavaScript).")
    if len(body) > MAX_PAGE_CHARS:
        body = body[:MAX_PAGE_CHARS] + "\n\n[cut off here: the page is longer]"
    return (f"{title}\n{url}\n\n" if title else f"{url}\n\n") + body


def status(name: str, args: dict) -> str:
    """A short line for the chat and the face."""
    if name == "web_search":
        return f"searching: {args.get('query', '')}"
    host = urllib.parse.urlsplit(str(args.get("url", ""))).hostname or args.get("url", "")
    return f"reading: {host}"


def log(log_file, name: str, args: dict, outcome: str) -> None:
    """One JSON line per search or page. Only what went online: the search words or the link."""
    what = {"query": args.get("query")} if name == "web_search" else {"url": args.get("url")}
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": name, **what,
                                "outcome": outcome}) + "\n")
    except OSError:
        pass


def call(name: str, args: dict, log_file) -> str:
    """Runs one web tool and returns the text for the model."""
    fn = {"web_search": web_search, "read_page": read_page}[name]
    try:
        result = fn(**args)
    except WebError as e:
        log(log_file, name, args, "error: " + str(e).split(". ")[0])   # the advice for the model stays out of the log
        return str(e)
    except TypeError:
        log(log_file, name, args, "error: bad arguments")
        return f"Wrong arguments for {name}."
    except (OSError, ValueError) as e:   # anything else the network throws: a friendly answer, never a dead reply
        log(log_file, name, args, f"error: {e}")
        return f"That didn't work ({e}). " + NO_GUESS
    log(log_file, name, args, "ok")
    return result
