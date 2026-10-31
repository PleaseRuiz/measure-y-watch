"""Fetch and parse San Mateo County's election results.

Two feeds, both from the county's results site:
- the results page (HTML, the main feed): server-rendered, with contest results in table
  cells marked with data-race / data-contestant / data-votes attributes;
- the summary XML file (the backup feed): one <Table> element per contestant.
Standard library only, so the workflow needs no installs.
"""

import codecs
import gzip
import http.client
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

SOURCE_URL = "https://www.livevoterturnout.com/ENR/sanmateocaenr/20/en/Y3Mvf_Index_20.html"
XML_URL = "https://www.livevoterturnout.com/ENR/sanmateocaenr/20/summary_20.xml"
CONTEST_TITLE = "City of Belmont, Measure Y (Majority Approval Required)"
USER_AGENT = "measure-y-watch (+https://github.com/PleaseRuiz/measure-y-watch)"
PACIFIC = ZoneInfo("America/Los_Angeles")
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
PAGE_TIME_FORMAT = "%m/%d/%Y %I:%M:%S %p"  # the results page's Website Updated, e.g. 10/26/2026 11:48:45 AM


class ParseError(Exception):
    pass


def _read_all(res, seconds):
    """The whole response body. The socket timeout covers each wait; this caps the total, so a reply that
    trickles in fails here instead of running into the job's time limit."""
    deadline = time.monotonic() + seconds
    chunks = []
    while True:
        chunk = res.read(1 << 16)
        if not chunk:
            missing = getattr(res, "length", None)  # read(n) doesn't check Content-Length; read() did
            if isinstance(missing, int) and missing > 0:
                raise http.client.IncompleteRead(b"".join(chunks), missing)
            return b"".join(chunks)
        chunks.append(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError(f"the response took more than {seconds} s")


def _get(url, timeout):
    """One GET. Returns (body bytes, charset or None, headers dict)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",  # only gzip: the server can answer brotli, which the standard library can't read
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        body = _read_all(res, 2 * timeout)
        encoding = (res.headers.get("Content-Encoding") or "").strip().lower()
        charset = res.headers.get_content_charset()
        headers = dict(res.headers.items())
    if encoding == "gzip":
        body = gzip.decompress(body)
    elif encoding not in ("", "identity"):
        raise ParseError(f"the county server sent {encoding!r}-compressed data, which this reader can't unpack")
    return body, charset, headers


def _decode(body, charset):
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:  # a charset name Python doesn't know
        return body.decode("utf-8", errors="replace")


def fetch(url=SOURCE_URL, timeout=60):
    """One GET of the county page. Returns the HTML as text."""
    body, charset, _ = _get(url, timeout)
    return _decode(body, charset)


def fetch_xml(url=XML_URL, timeout=60):
    """One GET of the county's summary XML. Returns (text, response headers)."""
    body, charset, headers = _get(url, timeout)
    if body.startswith(codecs.BOM_UTF8):
        charset = "utf-8"  # a byte order mark decides the encoding
    return _decode(body, charset), headers


def _squash(text):
    return re.sub(r"\s+", " ", text).strip()


def whole_number(text, what):
    cleaned = _squash(text).replace(",", "")
    if not re.fullmatch(r"\d+", cleaned):
        raise ParseError(f"{what} is not a whole number: {text!r}")
    return int(cleaned)


def county_time(dt):
    """A time the way the county page shows it, e.g. 11/3/2026 8:10:45 PM (Pacific)."""
    dt = dt.astimezone(PACIFIC)
    return f"{dt.month}/{dt.day}/{dt.year} {dt.hour % 12 or 12}:{dt.minute:02d}:{dt.second:02d} {'AM' if dt.hour < 12 else 'PM'}"


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


def _row_name(text):
    # As in parse(): the page's name cell repeats the label for screen readers ("YES YES")
    words = _squash(text).upper().split()
    return words[0] if words and len(set(words)) == 1 else " ".join(words)


# ---------- the backup feed: the county's summary XML ----------

def _header(headers, name):
    """A response header by name, any case. headers can be a dict, an HTTP message or None."""
    if not headers:
        return None
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return value
    return None


def _xml_root(text):
    text = text.lstrip("\ufeff")  # a UTF-8 byte order mark, if the text was decoded without removing it
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):
        # The county's file has no DTD. Refusing one means no entity is ever declared, let alone fetched.
        raise ParseError("XML feed has a DOCTYPE or ENTITY declaration; not reading it")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)  # already decoded: drop the declaration and its encoding
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ParseError(f"XML feed is not well-formed XML ({e})")
    for el in root.iter():  # ignore namespaces, should the county add one
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _xml_updated(root, headers):
    """(time, where it came from). The file's Last-Modified header matched the results page's Website
    Updated to the second when checked (Oct 26: 11:48:46 vs 11:48:45 AM); GeneratedDate was about 7
    minutes earlier, so it's only the fallback."""
    modified = _header(headers, "Last-Modified")
    if modified:
        try:
            dt = parsedate_to_datetime(modified)
        except (TypeError, ValueError, IndexError):
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)  # HTTP dates are GMT
            return dt.astimezone(PACIFIC).replace(microsecond=0), "Last-Modified"
    generated = _squash(root.findtext("GeneratedDate") or "")
    if not generated:
        raise ParseError("XML feed has no usable Last-Modified header and no GeneratedDate")
    try:
        dt = datetime.fromisoformat(generated)
    except ValueError:
        raise ParseError(f"XML GeneratedDate not understood: {generated!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PACIFIC)
    return dt.astimezone(PACIFIC).replace(microsecond=0), "GeneratedDate"


def parse_xml(text, headers=None, contest_title=CONTEST_TITLE):
    """Same result as parse(), from the county's summary XML and its response headers.

    Raises ParseError if anything is missing or ambiguous.
    """
    root = _xml_root(text)
    updated, _ = _xml_updated(root, headers)

    ballots_text = root.findtext("TotalBallotsCast")
    if ballots_text is None:
        raise ParseError("'TotalBallotsCast' not found in the XML feed")
    ballots = whole_number(ballots_text, "TotalBallotsCast")

    # The contest, matched by its exact title (RaceName); one row per contestant
    rows = [t for t in root.findall("Table") if _squash(t.findtext("RaceName") or "") == contest_title]
    race_ids = {_squash(t.findtext("RaceID") or "") for t in rows}
    if len(race_ids) != 1:
        raise ParseError(f"expected one contest titled {contest_title!r}, found {len(race_ids)}")
    names = [_squash(t.findtext("ContestantName") or "").upper() for t in rows]
    if sorted(names) != ["NO", "YES"]:
        raise ParseError(f"expected one YES and one NO row, found {sorted(names)}")
    votes = {name: whole_number(t.findtext("TotalVotes") or "", f"{name} votes") for name, t in zip(names, rows)}

    return {
        "contest": contest_title,
        "yes": votes["YES"],
        "no": votes["NO"],
        "county_updated": updated.isoformat(),
        "county_ballots_counted": ballots,
    }


# ---------- what a failed read found, for the run's diagnosis ----------

def _of_interest(title):
    t = title.lower()
    return "belmont" in t or re.search(r"\bmeasure\s+y\b", t) is not None


def like_measure_y(title):
    """A contest title that looks like Belmont's Measure Y, even if the county reworded it."""
    t = title.lower()
    return "belmont" in t and re.search(r"\bmeasure\s+y\b", t) is not None


def _yes_no(rows):
    """YES and NO totals from a contest's (name, votes text) rows, or (None, None) unless they're one clear pair."""
    yes = [v for n, v in rows if "YES" in n.split() and "NO" not in n.split()]
    no = [v for n, v in rows if "NO" in n.split() and "YES" not in n.split()]
    if len(rows) != 2 or len(yes) != 1 or len(no) != 1:
        return None, None
    try:
        return whole_number(yes[0] or "", "YES"), whole_number(no[0] or "", "NO")
    except ParseError:
        return None, None


def _page_title(text):
    m = re.search(r"<title[^>]*>(.*?)</title\s*>", text[:20000], re.I | re.S)
    if not m:
        return None
    return _squash(unescape(m.group(1)))[:100] or None


def _findings(text):
    return {
        "size": len(text),        # characters received
        "page_title": None,       # an HTML <title>, e.g. of an error page
        "updated_text": None,     # the county's update time, written the way the county page shows it
        "updated": None,          # the same as ISO 8601, if understood
        "updated_from": None,     # Website Updated, Last-Modified or GeneratedDate
        "ballots": None,          # countywide ballots counted
        "titles": [],             # contest titles with Belmont or Measure Y in them
        "races": [],              # contests titled like Measure Y: {title, rows [(name, votes text)], yes, no}
    }


def inspect(html):
    """What a county results page holds, as far as it can be read. Never raises."""
    found = _findings(html)
    try:
        found["page_title"] = _page_title(html)
        c = _Collector()
        c.feed(html)
        c.close()
    except Exception:
        return found
    raw = c.get("updated")
    if raw:
        found["updated_text"], found["updated_from"] = raw[:60], "Website Updated"
        try:
            found["updated"] = datetime.strptime(raw, PAGE_TIME_FORMAT).replace(tzinfo=PACIFIC).isoformat()
        except ValueError:
            pass
    spans = [c.get(k) for k in c.spans]
    if "Ballots Counted" in spans:
        try:
            found["ballots"] = whole_number(spans[spans.index("Ballots Counted") + 1], "Ballots Counted")
        except (IndexError, ParseError):
            pass
    cells = {}
    for race, contestant, kind, key in c.cells:
        cells.setdefault(race, {}).setdefault(contestant, {})[kind] = c.get(key)
    for race, key in c.race_titles:
        title = c.get(key)
        if _of_interest(title) and title not in found["titles"]:
            found["titles"].append(title)
        if like_measure_y(title):
            rows = [(_row_name(r.get("candidate", "")), r.get("votes")) for r in cells.get(race, {}).values()]
            yes, no = _yes_no(rows)
            found["races"].append({"title": title, "rows": rows, "yes": yes, "no": no})
    return found


def inspect_xml(text, headers=None):
    """What the county's summary XML holds, as far as it can be read. Never raises."""
    found = _findings(text)
    try:
        root = _xml_root(text)
    except Exception:
        found["page_title"] = _page_title(text)
        return found
    try:
        dt, found["updated_from"] = _xml_updated(root, headers)
        found["updated"], found["updated_text"] = dt.isoformat(), county_time(dt)
    except Exception:
        pass
    try:
        found["ballots"] = whole_number(root.findtext("TotalBallotsCast") or "", "TotalBallotsCast")
    except ParseError:
        pass
    races = {}
    for t in root.findall("Table"):
        title = _squash(t.findtext("RaceName") or "")
        if _of_interest(title) and title not in found["titles"]:
            found["titles"].append(title)
        if like_measure_y(title):
            row = (_squash(t.findtext("ContestantName") or "").upper(), t.findtext("TotalVotes"))
            races.setdefault((_squash(t.findtext("RaceID") or ""), title), []).append(row)
    for (_, title), rows in races.items():
        yes, no = _yes_no(rows)
        found["races"].append({"title": title, "rows": rows, "yes": yes, "no": no})
    return found
