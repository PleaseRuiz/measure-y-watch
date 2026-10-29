"""Fetch and parse San Mateo County's election results page.

The county page is server-rendered HTML; contest results sit in table cells
marked with data-race / data-contestant / data-votes attributes. Standard
library only, so the workflow needs no installs.
"""

import gzip
import re
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

SOURCE_URL = "https://www.livevoterturnout.com/ENR/sanmateocaenr/20/en/Y3Mvf_Index_20.html"
CONTEST_TITLE = "City of Belmont, Measure Y (Majority Approval Required)"
USER_AGENT = "measure-y-watch (+https://github.com/PleaseRuiz/measure-y-watch)"
PACIFIC = ZoneInfo("America/Los_Angeles")
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class ParseError(Exception):
    pass


def fetch(url=SOURCE_URL, timeout=60):
    """One GET of the county page. Returns the HTML as text."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        body = res.read()
        if res.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        charset = res.headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace")


def _squash(text):
    return re.sub(r"\s+", " ", text).strip()


def whole_number(text, what):
    cleaned = _squash(text).replace(",", "")
    if not re.fullmatch(r"\d+", cleaned):
        raise ParseError(f"{what} is not a whole number: {text!r}")
    return int(cleaned)


class _Collector(HTMLParser):
    """Collects the few pieces of text we need. Commented-out markup is ignored."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.capture = None      # key for text currently being captured
        self.depth = 0           # nesting depth inside the captured element
        self.text = {}           # key -> list of text chunks
        self.current_race = None # data-seq of the enclosing list-item
        self.race_titles = []    # (race_id, title)
        self.cells = []          # (race_id, contestant_id, kind, key)
        self.spans = []          # plain <span> texts, in order (for Ballots Counted)
        self.n = 0

    def _start_capture(self, key):
        self.capture, self.depth = key, 1
        self.text[key] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.capture is not None:
            self.text[self.capture].append(" ")  # keep nested elements' text apart
            if tag not in VOID_TAGS:
                self.depth += 1
            return
        classes = (a.get("class") or "").split()
        if tag == "div" and "list-item" in classes and a.get("data-seq"):
            self.current_race = a["data-seq"]
        if a.get("id") == "lblLastPublishedDateTime":
            self._start_capture("updated")
        elif tag == "span" and "race-title" in classes:
            self.n += 1
            key = f"title{self.n}"
            self.race_titles.append((self.current_race, key))
            self._start_capture(key)
        elif tag == "td" and a.get("data-race") and a.get("data-contestant"):
            for kind in ("candidate", "votes"):
                if a.get(f"data-{kind}") == "true":
                    self.n += 1
                    key = f"cell{self.n}"
                    self.cells.append((a["data-race"], a["data-contestant"], kind, key))
                    self._start_capture(key)
        elif tag == "span" and not a:
            self.n += 1
            key = f"span{self.n}"
            self.spans.append(key)
            self._start_capture(key)

    def handle_endtag(self, tag):
        if self.capture is not None and tag not in VOID_TAGS:
            self.depth -= 1
            if self.depth == 0:
                self.capture = None

    def handle_data(self, data):
        if self.capture is not None:
            self.text[self.capture].append(data)

    def get(self, key):
        return _squash("".join(self.text.get(key, [])))


def parse(html, contest_title=CONTEST_TITLE):
    """Return a dict with the contest's YES/NO totals and the page's header numbers.

    Raises ParseError if anything is missing or ambiguous.
    """
    c = _Collector()
    c.feed(html)
    c.close()

    # Website Updated, e.g. "10/26/2026 11:48:45 AM" (Pacific, no offset on the page)
    updated_raw = c.get("updated")
    if not updated_raw:
        raise ParseError("'Website Updated' time not found")
    try:
        updated = datetime.strptime(updated_raw, "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=PACIFIC)
    except ValueError:
        raise ParseError(f"'Website Updated' time not understood: {updated_raw!r}")

    # Countywide Ballots Counted: the span right after <span>Ballots Counted</span>
    spans = [c.get(k) for k in c.spans]
    try:
        i = spans.index("Ballots Counted")
        ballots = whole_number(spans[i + 1], "Ballots Counted")
    except (ValueError, IndexError):
        raise ParseError("'Ballots Counted' not found")

    # The contest, matched by its exact title
    race_ids = {race for race, key in c.race_titles if c.get(key) == contest_title}
    if len(race_ids) != 1 or None in race_ids:
        raise ParseError(f"expected one contest titled {contest_title!r}, found {len(race_ids)}")
    race = race_ids.pop()

    rows = {}
    for r, contestant, kind, key in c.cells:
        if r == race:
            rows.setdefault(contestant, {})[kind] = c.get(key)
    by_name = {}
    for row in rows.values():
        if "candidate" not in row or "votes" not in row:
            raise ParseError("contest row is missing its name or vote count")
        # The name cell repeats the label for screen readers ("YES YES")
        words = row["candidate"].upper().split()
        name = words[0] if words and len(set(words)) == 1 else " ".join(words)
        by_name[name] = whole_number(row["votes"], f"{name} votes")
    if set(by_name) != {"YES", "NO"}:
        raise ParseError(f"expected YES and NO rows, found {sorted(by_name)}")

    return {
        "contest": contest_title,
        "yes": by_name["YES"],
        "no": by_name["NO"],
        "county_updated": updated.isoformat(),
        "county_ballots_counted": ballots,
    }
