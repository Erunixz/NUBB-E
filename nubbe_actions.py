"""NUBB-E's everyday actions: quick commands, to-do list, notes, timers, reminders and routines.

    import nubbe_actions as actions
    acts = actions.Actions(connect)              # connect() -> sqlite3 connection to ~/.nubbe/nubbe.db
    acts.plan("open calculator and set the volume to 30")
        # -> [("open_app", {"name": "calculator"}), ("set_volume", {"level": 30})]
    acts.call("add_todo", {"item": "milk"})      # -> "Added milk to your list."
    actions.SCHEMAS                               # the same actions as tools for the brain

Rules first: plan() understands simple commands on its own, so they run at once without the brain (even
while it's asleep). Anything it doesn't fully understand returns None and goes to the brain as usual,
which can still use every action as a tool.

Safety: harmless actions (open an app or site, volume, music, timers, notes, the list) just run.
Clearing the list, deleting a note or a routine needs Erfan's Yes (needs_yes). Standard library only.
"""
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta

import nubbe_desk as desk

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (id INTEGER PRIMARY KEY, text TEXT NOT NULL, done INTEGER DEFAULT 0,
    created REAL, done_at REAL);
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, text TEXT NOT NULL, due REAL NOT NULL,
    kind TEXT NOT NULL, created REAL, fired INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS routines (name TEXT PRIMARY KEY, steps TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS dict_words (heard TEXT PRIMARY KEY, written TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS snippets (name TEXT PRIMARY KEY, text TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""
ASK_FIRST = {"clear_todos", "delete_note", "delete_routine", "delete_word", "delete_snippet"}
# dictation settings: understood from Erfan's own words only, never offered to the brain as tools
RULE_ONLY = {"dictation_mode", "add_word", "delete_word", "add_snippet", "delete_snippet", "list_dictionary"}
DICTATION_MODES = ("exact", "clean", "polished")


class ActionError(Exception):
    """A friendly message, like "There's nothing called milk on your list"."""


# ---------------------------------------------------------------------------
# understanding times: "10 minutes", "an hour and a half", "at 6", "tomorrow at 9am", "in 20 min"
# ---------------------------------------------------------------------------
NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
           "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
           "twenty five": 25, "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60,
           "ninety": 90, "a couple of": 2, "a couple": 2, "a few": 3}
_NUM = r"\d+(?:\.\d+)?|" + "|".join(sorted((re.escape(k) for k in NUMBERS), key=len, reverse=True))
UNITS = {"h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "m": 60, "min": 60, "mins": 60,
         "minute": 60, "minutes": 60, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}
_UNIT = r"hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s"
_PART = re.compile(rf"({_NUM})\s*({_UNIT})(\s+and\s+a\s+half)?\b")


def _num(text: str) -> float:
    return float(text) if re.fullmatch(r"\d+(?:\.\d+)?", text) else float(NUMBERS[text])


def parse_duration(text: str) -> int | None:
    """Seconds, or None unless the whole text is a length of time."""
    t = " ".join(str(text).lower().replace("-", " ").split())
    t = re.sub(r"\bhalf an hour\b", "30 minutes", t)
    t = re.sub(r"\ban hour and a half\b", "90 minutes", t)
    t = re.sub(r"\bhalf a minute\b", "30 seconds", t)
    total, pos = 0.0, 0
    for m in _PART.finditer(t):
        if t[pos:m.start()].strip(" ,") not in ("", "and"):
            return None
        unit = UNITS[m.group(2)]
        total += _num(m.group(1)) * unit + (unit / 2 if m.group(3) else 0)
        pos = m.end()
    if pos == 0 or t[pos:].strip(" ,.") or total <= 0 or total > 7 * 86400:
        return None
    return int(round(total))


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_CLOCK = re.compile(r"(?:at\s+)?(?:(noon|midday|midnight)|(\d{1,2})(?:[:.](\d{2}))?\s*(?:o'?\s?clock)?\s*"
                    r"(a\.?\s?m\.?|p\.?\s?m\.?)?)")


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """A time like "at 6", "6:30pm", "in 20 minutes", "tomorrow at 9", "on friday at 5 pm",
    "tonight at 8", "tomorrow morning". None unless the whole text is a time."""
    now = now or datetime.now()
    t = " ".join(str(text).lower().strip(" .,!?").split())
    if m := re.fullmatch(r"in\s+(.+)", t):
        secs = parse_duration(m.group(1))
        return now + timedelta(seconds=secs) if secs else None
    day, part = None, None
    words = {"today": 0, "tonight": 0, "tomorrow": 1, "the day after tomorrow": 2}
    for w, add in sorted(words.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{w}\b", t):
            day, t = now.date() + timedelta(days=add), re.sub(rf"\b{w}\b", " ", t, count=1)
            part = "evening" if w == "tonight" else part
            break
    for i, w in enumerate(WEEKDAYS):
        if m := re.search(rf"\b(?:on\s+|this\s+|next\s+)?{w}\b", t):
            ahead = (i - now.weekday()) % 7 or 7
            day, t = now.date() + timedelta(days=ahead), t[:m.start()] + " " + t[m.end():]
            break
    if m := re.search(r"\b(?:in the\s+|this\s+)?(morning|afternoon|evening|night)\b", t):
        part, t = m.group(1), t[:m.start()] + " " + t[m.end():]
    t = " ".join(t.replace(" on ", " ").split()).strip(" ,")
    if not t:
        if day is None and part is None:
            return None
        hour, minute, ampm = {"morning": 9, "afternoon": 15, "evening": 19, "night": 21}.get(part, 9), 0, None
    else:
        m = _CLOCK.fullmatch(t)
        if not m:
            return None
        if m.group(1):
            hour, minute, ampm = (12 if m.group(1) in ("noon", "midday") else 0), 0, "set"
        else:
            hour, minute = int(m.group(2)), int(m.group(3) or 0)
            ampm = (m.group(4) or "").replace(".", "").replace(" ", "") or None
            if hour > 23 or minute > 59 or (ampm and not 1 <= hour <= 12):
                return None
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif ampm is None and hour <= 12:
        if part in ("afternoon", "evening", "night") and hour < 12:
            hour += 12
        elif part is None and day is not None and 1 <= hour <= 6:
            hour += 12        # "tomorrow at 5" means 5 pm, not 5 am
    base = day or now.date()
    when = datetime.combine(base, datetime.min.time()).replace(hour=hour, minute=minute)
    if ampm is None and part is None and day is None and hour < 12:
        # "at 6": the next 6 o'clock, so 6 pm if it's already past 6 am
        return next(w for w in (when, when + timedelta(hours=12), when + timedelta(days=1)) if w > now)
    if day is None and when <= now:
        when += timedelta(days=1)         # earlier today has passed: tomorrow
    return when


def say_duration(secs: float) -> str:
    secs = int(round(secs))
    h, m, s = secs // 3600, secs % 3600 // 60, secs % 60
    parts = [f"{h} hour{'s' * (h != 1)}"] * bool(h) + [f"{m} minute{'s' * (m != 1)}"] * bool(m)
    if s and not h and m < 10:
        parts.append(f"{s} second{'s' * (s != 1)}")
    return " and ".join(parts) or "a moment"


def say_time(when: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    clock = when.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    days = (when.date() - now.date()).days
    if days == 0:
        return f"at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    if days < 7:
        return f"on {when.strftime('%A')} at {clock}"
    return f"on {when.strftime('%B')} {when.day} at {clock}"


# ---------------------------------------------------------------------------
# rules first: simple commands understood without the brain
# ---------------------------------------------------------------------------
I = re.IGNORECASE
LEAD = re.compile(r"^\W*(?:(?:hey|hi|ok|okay|yo)\W+)?(?:nubb-?e\W+)?"
                  r"(?:(?:please|can you|could you|would you|will you|can u|i want you to|i'd like you to|"
                  r"go ahead and|just|quickly)\s+)*", I)
TAIL = re.compile(r"(?:[\s,]+(?:please|for me|thanks|thank you|right now|now|nubb-?e))*[\s.!?]*$", I)
LIST = r"(?:my |the )?(?:to-?do |todo |to do |shopping |grocery )?list"
SITE_NAMES = "|".join(sorted((re.escape(s) for s in desk.SEARCH), key=len, reverse=True))
VERB_START = r"(?:open|launch|start|run|play|pause|resume|set|turn|mute|unmute|search|google|add|remind|take|note|" \
             r"next|skip|go|cross|check|mark|show|find|volume|make|create)\b"
SPLIT = re.compile(rf"\s*(?:,?\s*and then\s+|,?\s*then\s+|;\s*|,\s*and\s+(?={VERB_START})|,\s*(?={VERB_START})"
                   rf"|\s+and\s+(?={VERB_START}))", I)


def clean(text: str) -> str:
    t = " ".join(str(text).split())
    t = LEAD.sub("", t)
    return TAIL.sub("", t).strip()


class Actions:
    """Everyday actions on top of the app's database. web_search(query) -> results text (or None when
    offline), weather() -> "Light rain, +12°C" or None; both are optional."""

    def __init__(self, connect, web_search=None, weather=None, dictation_mode="clean"):
        self.connect = connect
        self.default_mode = dictation_mode
        self.web_search = web_search
        self.weather = weather

    # ---- storage ----
    def rows(self, sql, args=()) -> list[dict]:
        con = self.connect()
        try:
            with con:
                return [dict(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()

    def run(self, sql, args=()) -> int:
        con = self.connect()
        try:
            with con:
                cur = con.execute(sql, args)
                return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount
        finally:
            con.close()

    def routine(self, name: str) -> list[str] | None:
        r = self.rows("SELECT steps FROM routines WHERE name = ?", (_key(name),))
        return json.loads(r[0]["steps"]) if r else None

    # ---- understanding a command ----
    def plan(self, text: str) -> list[tuple[str, dict]] | None:
        """The actions for a simple command (several for "open X and Y"), or None to ask the brain."""
        t = clean(text)
        if not t or len(t) > 300 or "\n" in t:
            return None
        whole = LEAD.sub("", " ".join(str(text).split())).rstrip(" .!?")   # keeps a last word like "NUBB-E"
        if m := (re.fullmatch(r"(?:always )?spell (.+?) (?:as|like) (.+)", whole, I) or
                 re.fullmatch(r"when (?:i say|you hear) (.+?),? (?:write|type|spell it(?: as)?) (.+)", whole, I)):
            return [("add_word", {"heard": m[1], "written": m[2]})]
        one = self._one(t)
        if one:
            return [one]
        parts = [p for p in SPLIT.split(t) if p.strip()]
        if len(parts) < 2:
            return None
        steps = [self._one(clean(p)) for p in parts]
        return steps if all(steps) else None

    def _one(self, t: str) -> tuple[str, dict] | None:
        f = lambda pattern: re.fullmatch(pattern, t, I)       # noqa: E731
        if f(r"good morning(?: buddy)?|(?:my |the )?(?:morning|daily) briefing|brief me|what'?s my day(?: look like)?|"
             r"what does my day look like|what'?s (?:on )?(?:for )?today"):
            return "briefing", {}
        # dictation: mode, personal dictionary, snippets
        if m := (f(r"(?:set |switch |change |put )?(?:the |my )?dictation(?: mode)? (?:to |on |in )?(exact|clean|polished)(?: mode)?") or
                 f(r"(?:use |switch to )?(exact|clean|polished) (?:dictation|mode)(?: for dictation)?")):
            return "dictation_mode", {"mode": m[1].lower()}
        if f(r"what(?:'s| is) (?:the |my )?dictation mode|which dictation mode(?: is on| am i using)?"):
            return "dictation_mode", {}
        if m := (f(r"(?:always )?spell (.+?) (?:as|like) (.+)") or
                 f(r"when (?:i say|you hear) (.+?),? (?:write|type|spell it(?: as)?) (.+)")):
            return "add_word", {"heard": m[1], "written": m[2]}
        if m := f(r"(?:add|put|save) (.+?) (?:to|in) (?:my |the )?(?:dictionary|vocabulary|word list)"):
            return "add_word", {"written": m[1]}
        if m := f(r"(?:remove|delete|forget) (.+?) from (?:my |the )?(?:dictionary|vocabulary|word list)"):
            return "delete_word", {"word": m[1]}
        if m := f(r"(?:add|save|make|create) (?:a |the |new )?snippet (?:called |named |for )?(.+?)\s*(?::| is | as | that says | saying |-)\s*(.+)"):
            return "add_snippet", {"name": m[1], "text": m[2]}
        if m := f(r"(?:remove|delete|forget) (?:the |my )?snippet (?:called |named |for )?(.+)"):
            return "delete_snippet", {"name": m[1]}
        if f(r"(?:show|list|read|what'?s in|what is in)(?: me)? (?:my |the )?(?:dictionary|snippets|dictation words|"
             r"dictation settings)|what snippets do i have"):
            return "list_dictionary", {}
        # routines
        if m := f(r"(?:make|create|save|add|set up) (?:a |me a |new )?routine (?:called|named) (.+?)\s*(?::|-|,| that | which | to )\s*(.+)"):
            return "make_routine", {"name": m[1], "steps": [s for s in SPLIT.split(m[2]) if s.strip()]}
        if f(r"(?:what|which) routines do i have|(?:list|show)(?: me)? (?:my |the )?routines"):
            return "list_routines", {}
        if m := f(r"(?:run|start|do) (?:the |my )?(.+?)(?: routine)?"):
            if self.routine(m[1]):
                return "run_routine", {"name": m[1]}
        if self.routine(t):
            return "run_routine", {"name": t}
        # timers and reminders
        if m := f(r"(?:set|start|make)?\s?(?:a |an |me a )?timer (?:for |of )?(.+?)(?: (?:for|called) (?:the )?(.+))?"):
            if d := parse_duration(m[1]):
                return "set_timer", {"seconds": d, "label": m[2] or ""}
        if m := f(r"(?:set |start |make )?(?:a |an )?(.+?) timer(?: for (?:the )?(.+))?"):
            if d := parse_duration(m[1]):
                return "set_timer", {"seconds": d, "label": m[2] or ""}
        if m := f(r"remind me (?:to |about |that )?(.+)"):
            if r := self._reminder(m[1]):
                return r
        if f(r"how (?:much time|long)(?: is)?(?: left| remaining)?(?: (?:on|for) (?:the |my )?timers?)?|"
             r"(?:what are|show|list|read)(?: me)? (?:my |the )?(?:reminders|timers)|any reminders(?: today)?|"
             r"what (?:reminders|timers) do i have|(?:check )?(?:the |my )?timers?"):
            return "list_reminders", {}
        if m := f(r"(?:cancel|stop|delete|remove|turn off|clear) (?:the |my |all (?:the |my )?|that )?"
                  r"(timers?|reminders?|alarms?)(?: (?:about|for|to|called) (.+))?"):
            return "cancel_reminder", {"which": m[2] or ("timer" if m[1].startswith(("timer", "alarm")) else "reminder"),
                                       "all": t.lower().find(" all ") >= 0 or m[1].endswith("s")}
        # the list
        if m := f(rf"(?:add|put|write) (.+?) (?:to|on|onto) {LIST}") or f(r"add (.+?) to (?:my |the )?(?:to-?dos?|to do)"):
            return "add_todo", {"item": m[1]}
        if f(rf"(?:what'?s|what is) on {LIST}|(?:show|read|list|check)(?: me)? (?:my |the )?(?:to-?do|to do|todo|shopping)?\s?"
             r"(?:list|to-?dos)|what do i (?:have|need) to do(?: today)?"):
            return "list_todos", {}
        if m := (f(rf"(?:cross|check|tick|strike) (.+?) off(?: {LIST})?") or
                 f(rf"(?:cross|check|tick|strike) off (.+?)(?: (?:from|on|off) {LIST})?") or
                 f(r"mark (.+?) (?:as )?(?:done|complete|completed|finished)") or
                 f(rf"(?:remove|take) (.+?) (?:from|off) {LIST}") or f(r"i (?:did|finished|bought|got) (.+?) on my list")):
            return "done_todo", {"item": m[1]}
        if f(rf"(?:clear|empty|delete|wipe|reset) {LIST}(?: completely)?"):
            return "clear_todos", {}
        # notes
        if m := f(r"(?:take a note|make a note|note down|write down|jot down|add a note|note)(?: that)?\s*[:,-]?\s+(.+)"):
            return "add_note", {"text": m[1]}
        if m := (f(r"(?:search|find|look in|check|look through) (?:in )?my notes (?:for|about) (.+)") or
                 f(r"what did i (?:note|write down|jot down) about (.+)")):
            return "search_notes", {"query": m[1]}
        if f(r"(?:what are|show|read|list)(?: me)? my (?:last |latest |recent )?notes"):
            return "search_notes", {"query": ""}
        # music, video and sound
        if f(r"what'?s (?:this song|playing|this track|the song)(?: now| right now)?|what song is (?:this|playing)(?: now)?|"
             r"what am i listening to|who sings this|what(?:'s| is) the (?:name of this )?song"):
            return "now_playing", {}
        if f(r"(?:pause|stop|hold)(?: the| my| this)? (?:music|song|video|track|playback|podcast)|pause(?: it)?"):
            return "media", {"action": "pause"}
        if f(r"(?:play|resume|unpause)(?: the| my)?(?: music| song| video| playback| it)?|keep playing|"
             r"continue (?:the |my )?(?:music|song|video|playback)|play (?:some )?music"):
            return "media", {"action": "play"}
        if f(r"(?:next|skip)(?: (?:this|the))?(?: song| track| one| video)?|(?:play|go to) (?:the )?next(?: song| track| one)?"):
            return "media", {"action": "next"}
        if f(r"(?:previous|go back(?: to the)?(?: previous| last)?)(?: song| track| one)|play (?:the )?(?:previous|last) "
             r"(?:song|track|one)|previous|back one"):
            return "media", {"action": "previous"}
        if m := f(r"play (.+?) on (spotify|youtube)"):      # just "play X" goes to the brain ("play a game")
            return "play_music", {"query": m[1], "app": m[2].lower()}
        if m := (f(r"(?:set |change |turn |put )?(?:the )?(?:volume|sound) (?:to |at )?(\d{1,3})(?: ?%| percent)?") or
                 f(r"(?:set|turn|put) (?:it|the volume|the sound) to (\d{1,3})(?: ?%| percent)?")):
            if int(m[1]) <= 100:
                return "set_volume", {"level": int(m[1])}
        if m := (f(r"(?:turn|crank|bring|put) (?:the )?(?:volume|sound|it|music) (up|down)(?: (?:a bit|a little|a lot|by (\d{1,3})(?: ?%| percent)?))?") or
                 f(r"(?:volume|sound) (up|down)(?: by (\d{1,3})(?: ?%| percent)?)?")):
            step = int(m[2]) if m[2] else (20 if "a lot" in t.lower() else 10)
            return "set_volume", {"change": step if m[1].lower() == "up" else -step}
        if m := f(r"(?:make it |a bit |a little |go |be )?(louder|quieter|softer)(?: (?:a bit|a little))?"):
            return "set_volume", {"change": 10 if m[1].lower() == "louder" else -10}
        if m := f(r"(mute|unmute)(?: the| my)?(?: sound| volume| audio| music| it| everything| computer| laptop| pc)?"):
            return "set_volume", {"mute": m[1].lower() == "mute"}
        if f(r"what'?s the volume(?: at)?|how loud is it|what(?:'s| is) the volume (?:level|set to)"):
            return "set_volume", {}
        # searching a site in the browser
        if m := (f(rf"(?:search|look up|find|look for)(?: for)? (.+?) on ({SITE_NAMES})") or
                 f(rf"(?:search|look up)(?: on)? ({SITE_NAMES}) for (.+)")):
            q, site = (m[1], m[2]) if m.re.pattern.startswith("(?:search|look up|find|look for)") else (m[2], m[1])
            return "open_website", {"site": site, "search": q}
        if m := f(r"(google|youtube) (?!it$)(.+)"):
            return "open_website", {"site": m[1], "search": m[2]}
        # opening apps, folders and sites
        if m := f(r"(?:open|launch|start|run|bring up|fire up|load|go to|show me|take me to|pull up) (.+)"):
            return self._open(m[1], sites_only=bool(re.match(r"(go to|take me to)\b", t, I)))
        # PC stats
        if f(r"(?:how'?s|how is) (?:my |the |this )?(?:pc|computer|laptop|system)(?: doing)?|system (?:stats|status|info)|"
             r"(?:cpu|ram|gpu|battery|disk) (?:usage|level|left|status|temperature|temp)|(?:cpu|gpu|battery) ?(?:%|percent)?|"
             r"how much (?:ram|battery|disk space|storage)(?: is)?(?: am i using| left| is left| do i have(?: left)?| is used)?"):
            return "system_stats", {}
        # files by name
        if m := (f(r"(?:find|locate|search for) my (?!notes?\b)(.+?)(?: file| document| folder)?") or
                 f(r"(?:where'?s|where is) my (?!notes?\b)(.+?) (?:file|document|folder|pdf|doc|spreadsheet|slides)") or
                 f(r"(?:find|locate) (?:the )?(?:file|document|folder) (?:called |named )?(.+)") or
                 f(r"(?:find|locate|where'?s|where is) (\S+\.[a-z0-9]{2,5})")):
            return "find_file", {"name": m[1]}
        return None

    def _reminder(self, rest: str) -> tuple[str, dict] | None:
        """"call mom at 6" / "at 6 to call mom" / "in 20 minutes to stretch" -> set_reminder."""
        keys = list(re.finditer(r"\b(?:in|at|on|tomorrow|tonight|today|this|next|by)\b", rest, I))
        for k in keys:   # "to call mom at 6": the time is the end part
            what, when = rest[:k.start()].strip(" ,"), rest[k.start():].strip()
            if what and parse_when(when):
                return "set_reminder", {"text": re.sub(r"^(?:to|about|that)\s+", "", what, flags=I), "when": when}
        if m := re.fullmatch(r"(.+?)\s+(?:to|about|that)\s+(.+)", rest, I):   # "at 6 to call mom": time first
            if parse_when(m[1]):
                return "set_reminder", {"text": m[2], "when": m[1]}
        return None

    def _open(self, target: str, sites_only: bool = False) -> tuple[str, dict] | None:
        target = target.strip()
        if m := re.fullmatch(r"(.+?),? and (?:search|look up|find|look for)(?: for)? (.+)", target, I):
            if desk.search_url(m[1], m[2]):
                return "open_website", {"site": m[1], "search": m[2]}
            return None
        if not sites_only:
            if re.fullmatch(r"(?:my |the )?(?:downloads|documents|desktop|pictures|photos|videos|music|screenshots|home)"
                            r"(?: folder)?", target, I):
                return "open_folder", {"name": target}
            if desk.find_app(target):
                return "open_app", {"name": target}
        if desk.site_url(target):
            return "open_website", {"site": target}
        return None

    # ---- running actions (from rules or from the brain) ----
    def call(self, name: str, args: dict, approved: bool | None = None) -> str:
        fn = getattr(self, "do_" + name, None)
        if fn is None or name not in TOOLS | RULE_ONLY:
            return f"There's no action called {name}."
        if name in ASK_FIRST and not approved:
            return "Erfan said No, so nothing was changed." if approved is False else "Not allowed without Erfan's Yes."
        try:
            return fn(**args)
        except (ActionError, desk.DeskError) as e:
            return str(e)
        except TypeError:
            return f"Wrong details for {name}."
        except (OSError, sqlite3.Error) as e:
            return f"That didn't work: {getattr(e, 'strerror', None) or e}."

    def card(self, name: str, args: dict) -> dict:
        if name == "clear_todos":
            items = [r["text"] for r in self.rows("SELECT text FROM todos WHERE done = 0 ORDER BY id")]
            return {"title": "Clear your whole list", "target": f"{len(items)} item{'s' * (len(items) != 1)}",
                    "detail": "\n".join(items) or "(the list is empty)", "say": "Clear your whole list? Say yes or no."}
        if name == "delete_note":
            note = self._find_note(args.get("which", ""))
            return {"title": "Delete a note", "target": "", "detail": note["text"] if note else str(args),
                    "say": "Delete that note? Say yes or no."}
        if name == "delete_word":
            return {"title": "Remove a word from your dictionary", "target": str(args.get("word", "")), "detail": "",
                    "say": "Remove that word from your dictionary?"}
        if name == "delete_snippet":
            hit = self._snippet(args.get("name", ""))
            return {"title": "Delete a snippet", "target": hit["name"] if hit else str(args.get("name", "")),
                    "detail": hit["text"] if hit else "", "say": "Delete that snippet?"}
        if name == "delete_routine":
            return {"title": "Delete a routine", "target": str(args.get("name", "")),
                    "detail": "\n".join(self.routine(args.get("name", "")) or []), "say": "Delete that routine?"}
        return {"title": name, "target": "", "detail": json.dumps(args)}

    # the desk
    def do_open_app(self, name):
        return desk.open_app(name)

    def do_open_folder(self, name):
        return desk.open_folder(name)

    def do_open_website(self, site, search=""):
        return desk.open_website(site, search)

    def do_set_volume(self, level=None, change=None, mute=None):
        if level is None and change is None and mute is None:
            now, muted = desk.get_volume()
            return f"The volume is at {now}%" + (", but it's muted." if muted else ".")
        return desk.set_volume(level, change, mute)

    def do_media(self, action):
        return desk.media(action)

    def do_now_playing(self):
        return desk.describe_playing()

    def do_play_music(self, query, app="spotify"):
        return desk.play_music(query, app, self.web_search)

    def do_system_stats(self):
        return desk.system_stats()

    def do_find_file(self, name):
        return desk.find_file(name)

    # the list
    def do_add_todo(self, item):
        items = [i.strip() for i in re.split(r",\s*(?:and\s+)?|\s+and\s+", str(item)) if i.strip()]
        items = items if 1 < len(items) <= 10 and all(len(i.split()) <= 4 for i in items) else [str(item).strip()]
        for i in items:
            self.run("INSERT INTO todos(text, created) VALUES (?, ?)", (i[:200], time.time()))
        return f"Added {_join(items)} to your list."

    def do_list_todos(self):
        items = self.rows("SELECT text FROM todos WHERE done = 0 ORDER BY id")
        if not items:
            return "Your list is empty."
        return f"You have {len(items)} on your list: " + _join([r["text"] for r in items]) + "."

    def do_done_todo(self, item):
        open_items = self.rows("SELECT id, text FROM todos WHERE done = 0 ORDER BY id")
        hit = _best(item, open_items)
        if not hit:
            raise ActionError(f"There's nothing called {item} on your list.")
        self.run("UPDATE todos SET done = 1, done_at = ? WHERE id = ?", (time.time(), hit["id"]))
        left = len(open_items) - 1
        return f"Crossed off {hit['text']}." + (f" {left} left." if left else " Your list is all done!")

    def do_clear_todos(self):
        n = self.run("DELETE FROM todos")
        return f"Cleared your list ({n} item{'s' * (n != 1)})."

    # notes
    def do_add_note(self, text):
        text = str(text).strip()
        if not text:
            raise ActionError("What should the note say?")
        self.run("INSERT INTO notes(text, created) VALUES (?, ?)", (text[:4000], time.time()))
        return "Noted."

    def do_search_notes(self, query=""):
        words = [w for w in re.findall(r"\w+", str(query).lower()) if len(w) > 2 or not query]
        notes = self.rows("SELECT id, text, created FROM notes ORDER BY id DESC")
        if words:
            notes = [n for n in notes if any(w in n["text"].lower() for w in words)]
        if not notes:
            return f"No notes about {query}." if query else "You don't have any notes yet."
        when = lambda n: datetime.fromtimestamp(n["created"]).strftime("%b %d")   # noqa: E731
        head = f"Notes about {query}" if query else "Your latest notes"
        return head + ":\n" + "\n".join(f"- {n['text']} ({when(n)})" for n in notes[:8])

    def _find_note(self, which):
        notes = self.rows("SELECT id, text FROM notes ORDER BY id DESC")
        if str(which).strip().lower() in ("last", "latest", "the last one", "that", "that one", ""):
            return notes[0] if notes else None
        return _best(which, notes)

    def do_delete_note(self, which):
        note = self._find_note(which)
        if not note:
            raise ActionError("I couldn't find that note.")
        self.run("DELETE FROM notes WHERE id = ?", (note["id"],))
        return "Deleted the note."

    # timers and reminders
    def do_set_timer(self, seconds=None, minutes=None, duration="", label=""):
        secs = seconds or (minutes and float(minutes) * 60) or parse_duration(duration or "")
        if not secs or float(secs) <= 0:
            raise ActionError(f"I couldn't tell how long ({duration or seconds or minutes}). Try '10 minutes'.")
        label = str(label or "").strip()
        self.run("INSERT INTO reminders(text, due, kind, created) VALUES (?, ?, 'timer', ?)",
                 (label, time.time() + float(secs), time.time()))
        return f"Timer set for {say_duration(float(secs))}" + (f" ({label})." if label else ".")

    def do_set_reminder(self, text, when):
        due = parse_when(when)
        if not due:
            raise ActionError(f"I couldn't understand the time '{when}'. Try 'at 6pm', 'in 20 minutes' or 'tomorrow at 9'.")
        text = str(text).strip().rstrip(".")
        if not text:
            raise ActionError("What should I remind you about?")
        self.run("INSERT INTO reminders(text, due, kind, created) VALUES (?, ?, 'reminder', ?)",
                 (text[:300], due.timestamp(), time.time()))
        return f"Okay, I'll remind you to {_you(text)} {say_time(due)}."

    def do_list_reminders(self):
        items = self.rows("SELECT text, due, kind FROM reminders WHERE fired = 0 ORDER BY due")
        if not items:
            return "No timers or reminders set."
        now = time.time()
        lines = [(f"Timer{' (' + r['text'] + ')' if r['text'] else ''}: {say_duration(r['due'] - now)} left"
                  if r["kind"] == "timer" else
                  f"{_you(r['text'])[0].upper() + _you(r['text'])[1:]} {say_time(datetime.fromtimestamp(r['due']))}")
                 for r in items]
        return lines[0] + "." if len(lines) == 1 else "\n".join(f"- {line}" for line in lines)

    def do_cancel_reminder(self, which="", all=False):
        items = self.rows("SELECT id, text, kind FROM reminders WHERE fired = 0 ORDER BY due")
        kind = "timer" if str(which).lower().rstrip("s") in ("timer", "alarm") else \
            "reminder" if str(which).lower().rstrip("s") == "reminder" else None
        pick = [r for r in items if r["kind"] == kind] if kind else []
        if not kind:
            hit = _best(which, items)
            pick = [hit] if hit else []
        if not pick:
            raise ActionError(f"There's no {kind or 'timer or reminder'} like that to cancel.")
        if not all and len(pick) > 1:
            pick = pick[:1]   # the one due soonest
        for r in pick:
            self.run("DELETE FROM reminders WHERE id = ?", (r["id"],))
        what = kind or pick[0]["kind"]
        return f"Cancelled {len(pick)} {what}s." if len(pick) > 1 else f"Cancelled the {what}."

    def due(self, now: float | None = None) -> list[dict]:
        """Timers and reminders that are due now (each is only returned once)."""
        now = now or time.time()
        items = self.rows("SELECT id, text, due, kind FROM reminders WHERE fired = 0 AND due <= ? ORDER BY due", (now,))
        for r in items:
            self.run("UPDATE reminders SET fired = 1 WHERE id = ?", (r["id"],))
            r["late"] = now - r["due"] > 120   # NUBB-E wasn't running when it was due
        return items

    # routines
    def do_make_routine(self, name, steps):
        name = _key(name)
        steps = [clean(s) for s in (steps if isinstance(steps, list) else SPLIT.split(str(steps))) if clean(s)]
        if not name or not steps:
            raise ActionError("A routine needs a name and at least one step.")
        bad = [s for s in steps if not self._one(s) or self._one(s)[0] in ("run_routine", "make_routine")]
        if bad:
            raise ActionError(f"I don't know how to do this step on my own: {bad[0]}. Routines can open apps and "
                              "sites, play music, set the volume, timers, reminders and the list.")
        self.run("INSERT OR REPLACE INTO routines(name, steps, created) VALUES (?, ?, ?)",
                 (name, json.dumps(steps), time.time()))
        return f"Saved the routine \"{name}\": {_join(steps)}. Just say \"{name}\" to run it."

    def do_run_routine(self, name):
        steps = self.routine(name)
        if not steps:
            raise ActionError(f"There's no routine called {name}.")
        out = []
        for s in steps:
            step = self._one(s)
            out.append(self.call(*step) if step and step[0] not in ASK_FIRST | {"run_routine", "make_routine"}
                       else f"Skipped: {s}.")
        return " ".join(out)

    def do_list_routines(self):
        items = self.rows("SELECT name, steps FROM routines ORDER BY name")
        if not items:
            return "You don't have any routines yet. Try: make a routine called start my day: open Spotify and open Gmail."
        return "\n".join(f"- {r['name']}: {_join(json.loads(r['steps']))}" for r in items)

    def do_delete_routine(self, name):
        if not self.run("DELETE FROM routines WHERE name = ?", (_key(name),)):
            raise ActionError(f"There's no routine called {name}.")
        return f"Deleted the routine {name}."

    # dictation
    def dictation(self) -> dict:
        """What dictation needs: the mode, {heard: written} and {snippet name: text}."""
        r = self.rows("SELECT value FROM settings WHERE key = 'dictation_mode'")
        mode = r[0]["value"] if r and r[0]["value"] in DICTATION_MODES else self.default_mode
        return {"mode": mode,
                "words": {w["heard"]: w["written"] for w in self.rows("SELECT heard, written FROM dict_words")},
                "snippets": {n["name"]: n["text"] for n in self.rows("SELECT name, text FROM snippets")}}

    def do_dictation_mode(self, mode=None):
        if mode is None:
            return f"Dictation is in {self.dictation()['mode']} mode."
        mode = str(mode).lower().strip()
        if mode not in DICTATION_MODES:
            raise ActionError("Dictation modes are exact, clean and polished.")
        self.run("INSERT OR REPLACE INTO settings(key, value) VALUES ('dictation_mode', ?)", (mode,))
        return {"exact": "Dictation is exact now: your words as heard.",
                "clean": "Dictation is clean now: no ums, tidy punctuation, spoken lists.",
                "polished": "Dictation is polished now: the brain also fixes small mishearings."}[mode]

    def do_add_word(self, written, heard=None):
        written = str(written).strip().strip("\"'.")
        heard = " ".join(str(heard or written).lower().strip().strip("\"'.").split())
        if not written or not heard or len(written) > 60 or len(heard) > 60:
            raise ActionError("Tell me the word, like: spell nubby as NUBB-E.")
        self.run("INSERT OR REPLACE INTO dict_words(heard, written, created) VALUES (?, ?, ?)", (heard, written, time.time()))
        return (f"Okay, I'll write {written} when I hear {heard}." if heard != written.lower()
                else f"Added {written} to your dictionary.")

    def do_delete_word(self, word):
        w = " ".join(str(word).lower().strip().strip("\"'.").split())
        if not self.run("DELETE FROM dict_words WHERE heard = ? OR lower(written) = ?", (w, w)):
            raise ActionError(f"{word} isn't in your dictionary.")
        return f"Removed {word} from your dictionary."

    def _snippet(self, name):
        r = self.rows("SELECT name, text FROM snippets WHERE name = ?", (_snippet_key(name),))
        return r[0] if r else None

    def do_add_snippet(self, name, text):
        key, text = _snippet_key(name), str(text).strip()
        if not key or not text:
            raise ActionError("A snippet needs a name and text, like: add a snippet called my address: 12 Main St.")
        self.run("INSERT OR REPLACE INTO snippets(name, text, created) VALUES (?, ?, ?)", (key, text[:2000], time.time()))
        return f"Saved. Say \"insert my {key}\" while dictating and I'll type it."

    def do_delete_snippet(self, name):
        if not self.run("DELETE FROM snippets WHERE name = ?", (_snippet_key(name),)):
            raise ActionError(f"There's no snippet called {name}.")
        return f"Deleted the snippet {_snippet_key(name)}."

    def do_list_dictionary(self):
        d = self.dictation()
        lines = [f"Dictation mode: {d['mode']}."]
        if d["words"]:
            lines.append("Words: " + ", ".join(w if h == w.lower() else f"{h} -> {w}" for h, w in d["words"].items()) + ".")
        if d["snippets"]:
            lines.append("Snippets: " + ", ".join(f'"insert my {n}"' for n in d["snippets"]) + ".")
        if not d["words"] and not d["snippets"]:
            lines.append("No dictionary words or snippets yet. Try: spell nubby as NUBB-E, or add a snippet called "
                         "my address: ...")
        return "\n".join(lines)

    # good morning
    def do_briefing(self):
        now = datetime.now()
        hello = "Good morning!" if now.hour < 12 else "Good afternoon!" if now.hour < 18 else "Good evening!"
        parts = [f"{hello} It's {now.strftime('%A, %B')} {now.day}, {now.strftime('%I:%M %p').lstrip('0')}."]
        if self.weather:
            try:
                w = self.weather()
                if w:
                    parts.append(f"Weather: {w}.")
            except Exception:
                pass
        end = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()).timestamp()
        today = self.rows("SELECT text, due, kind FROM reminders WHERE fired = 0 AND kind = 'reminder' AND due < ? "
                          "ORDER BY due", (end,))
        if today:
            parts.append("Today: " + _join([f"{_you(r['text'])} {say_time(datetime.fromtimestamp(r['due']))}"
                                            for r in today]) + ".")
        todos = [r["text"] for r in self.rows("SELECT text FROM todos WHERE done = 0 ORDER BY id")]
        if todos:
            more = f", and {len(todos) - 5} more" if len(todos) > 5 else ""
            parts.append(f"On your list: {_join(todos[:5])}{more}.")
        if not today and not todos:
            parts.append("Nothing on your list and no reminders. A free day!")
        return " ".join(parts)


def _key(name: str) -> str:
    return " ".join(re.sub(r"[^\w' -]", " ", str(name).lower()).split())


def _snippet_key(name: str) -> str:
    """"my home address" -> "home address" (said as "insert my home address")."""
    return re.sub(r"^(?:my|the|our)\s+", "", _key(name))


def _join(items: list[str]) -> str:
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _you(text: str) -> str:
    """"call my mom" stays; "I need to call mom" -> "call mom"."""
    return re.sub(r"^(?:i need to|i have to|i should|i must|to)\s+", "", str(text).strip(), flags=I)


def _best(what: str, items: list[dict]) -> dict | None:
    """The item whose text best matches what Erfan said ("milk" finds "oat milk")."""
    want = _key(what)
    if not want:
        return None
    for test in (lambda t: t == want, lambda t: want in t, lambda t: all(w in t for w in want.split()),
                 lambda t: any(w in t for w in want.split() if len(w) > 3)):
        hits = [i for i in items if test(_key(i["text"]))]
        if hits:
            return hits[0]
    return None


# ---------------------------------------------------------------------------
# the same actions as tools for the brain
# ---------------------------------------------------------------------------
def _tool(name, description, props=None, required=()):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": props or {}, "required": list(required)}}}


_S = {"type": "string"}
SCHEMAS = [
    _tool("open_app", "Open an app on Erfan's PC by name (from the Start menu), like 'Spotify' or 'Calculator'.",
          {"name": {**_S, "description": "The app's name."}}, ["name"]),
    _tool("open_website", "Open a website in Erfan's browser, optionally searching it. Known sites: "
          + ", ".join(sorted(desk.SEARCH)) + ". Also any address like github.com.",
          {"site": {**_S, "description": "Site name or address."},
           "search": {**_S, "description": "What to search for on that site (optional)."}}, ["site"]),
    _tool("open_folder", "Open one of Erfan's folders in File Explorer.",
          {"name": {**_S, "enum": sorted(desk.FOLDERS)}}, ["name"]),
    _tool("set_volume", "Set or check the PC's volume. Give level (0-100), or change (+/-), or mute. "
          "Nothing given: says the current volume.",
          {"level": {"type": "integer"}, "change": {"type": "integer"}, "mute": {"type": "boolean"}}),
    _tool("media", "Control whatever is playing (Spotify, a video...).",
          {"action": {**_S, "enum": ["play", "pause", "play_pause", "next", "previous", "stop"]}}, ["action"]),
    _tool("now_playing", "What song or video is playing right now."),
    _tool("play_music", "Play a song, album or artist on Spotify (or show YouTube results).",
          {"query": _S, "app": {**_S, "enum": ["spotify", "youtube"]}}, ["query"]),
    _tool("system_stats", "Erfan's PC right now: CPU, memory, battery, disk, GPU, uptime."),
    _tool("find_file", "Find a file on Erfan's PC by its name (fast) and show it in File Explorer. "
          "Use search_files instead to search inside files.", {"name": {**_S, "description": "Words in the file name."}},
          ["name"]),
    _tool("add_todo", "Add something to Erfan's to-do / shopping list.", {"item": _S}, ["item"]),
    _tool("list_todos", "Read Erfan's to-do list."),
    _tool("done_todo", "Cross something off the list.", {"item": _S}, ["item"]),
    _tool("clear_todos", "Delete the whole list. Erfan must press Yes."),
    _tool("add_note", "Save a quick note for Erfan.", {"text": _S}, ["text"]),
    _tool("search_notes", "Search Erfan's quick notes (empty query: the latest ones).", {"query": _S}),
    _tool("delete_note", "Delete one note. Erfan must press Yes.",
          {"which": {**_S, "description": "Words from the note, or 'last'."}}, ["which"]),
    _tool("set_timer", "Start a timer. NUBB-E says when it's done.",
          {"duration": {**_S, "description": "Like '10 minutes' or '1 hour 30 minutes'."},
           "label": {**_S, "description": "What it's for (optional)."}}, ["duration"]),
    _tool("set_reminder", "Remind Erfan about something at a time. NUBB-E says it out loud and shows a notification.",
          {"text": {**_S, "description": "What to remind him, like 'call mom'."},
           "when": {**_S, "description": "Like 'at 6pm', 'in 20 minutes', 'tomorrow at 9am', 'on friday at 5pm'."}},
          ["text", "when"]),
    _tool("list_reminders", "The timers and reminders that are set."),
    _tool("cancel_reminder", "Cancel a timer or reminder.",
          {"which": {**_S, "description": "'timer', 'reminder', or words from the reminder."},
           "all": {"type": "boolean"}}, ["which"]),
    _tool("make_routine", "Save a routine: a name and simple steps that run together, like "
          "'start my day' = ['open Spotify', 'open Gmail'].",
          {"name": _S, "steps": {"type": "array", "items": _S}}, ["name", "steps"]),
    _tool("run_routine", "Run a saved routine.", {"name": _S}, ["name"]),
    _tool("list_routines", "List the saved routines."),
    _tool("delete_routine", "Delete a routine. Erfan must press Yes.", {"name": _S}, ["name"]),
    _tool("briefing", "Erfan's daily briefing: date, weather, today's reminders and the list."),
]
TOOLS = {s["function"]["name"] for s in SCHEMAS}

RULES = (
    "\n\n## Your everyday actions\n"
    "You can do things on Erfan's PC with tools: open apps, folders and websites (and search them), set the "
    "volume, control music and play songs, check the PC's stats, find files by name, keep his to-do list and "
    "quick notes, set timers and reminders (you say them out loud when they're due), run routines, and give a "
    "daily briefing. Just do it when he asks, no need to ask first. Clearing the list or deleting a note or "
    "routine shows him a Yes/No card. Quick notes and the list are separate from your memory of him."
)


def status(name: str, args: dict) -> str:
    """A short line for the chat and the face while an action runs."""
    return {"open_app": f"opening {args.get('name', '')}", "open_website": f"opening {args.get('site', '')}",
            "open_folder": f"opening {args.get('name', '')}", "set_volume": "volume", "media": "music",
            "now_playing": "checking what's playing", "play_music": f"finding {args.get('query', '')}",
            "system_stats": "checking the PC", "find_file": f"looking for {args.get('name', '')}",
            "briefing": "getting your day ready"}.get(name, name.replace("_", " "))
