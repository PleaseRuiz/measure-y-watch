"""Update data/results.json from the county's results, or from a manual entry.

Run by .github/workflows/update.yml. Nothing here waits for a person: a failed county read is
retried by the next run (from the other feed), data/status.json tells the site when to send
readers to the county's own results page, and a county correction is saved once a second read
shows the same numbers.

Modes, picked from the environment:
  EVENT=push                      no fetch; only copies expected_votes from config.json
  MANUAL_YES / MANUAL_NO set      manual entry (MANUAL_COUNTY_UPDATED required)
  otherwise                       one fetch of the county's results, from one feed
ALLOW_DECREASE=true (only set by a hand-started run) lets this run's reading go below the saved YES or NO.
PRACTICE=1 (rehearsal only) writes data/rehearsal.json instead and never fetches the county page.
KEEPALIVE=true with CACHE_HIT=false (a run the keep-alive or the watchdog started, when the Actions
cache didn't restore .last-fetch) skips the county read: without the last fetch time the gap guard
can't work, so only scheduled runs read the county then.
MARK_CERTIFIED=true (hand-started run) marks the count final once the county has certified it.
It is refused before CERTIFY_AFTER, so a mis-tap on election night can't end the count early.

Feeds: the county results page (HTML) normally. After the page fails (can't be fetched, read or
checked), the NEXT run reads the county's summary XML instead: still one fetch per run, behind the
same gap guard. 30 minutes after switching, one run tries the page again and stays on it if it
reads; a failed retry doesn't count as a failed read. One XML failure is read again from the XML;
an XML failure right after another failed read goes back to the page. Both feeds go through the
same checks, and saved entries say which one they came from ("feed": "html" or "xml"; "source"
stays "county").

.county-state.json (repo root, git-ignored, kept between runs by the Actions cache with
.last-fetch; written only here; a missing or unreadable file or field counts as the default):
  feed              "html" | "xml"   the feed the next run reads (xml until html_retry_after)
  html_retry_after  epoch | 0        on the XML feed: when to try the page again
  failures          county reads that failed in a row
  last_ok           epoch | 0        the last county read that worked (held decreases included)
  last_fail         {"feed", "kind", "at"} | null   the last county read that failed; kind is
                    fetch (HTTP error, timeout, connection, or a reply that isn't the county's
                    page or file), parse (the reader couldn't read the county's page or file)
                    or check (it was read but failed a check)
  pending_decrease  {"yes", "no", "county_updated", "seen_at"} | null   a county reading below the
                    saved YES or NO, held until the next read (seen_at is epoch seconds)
Manual entries and push runs leave it as it is.

data/status.json (public, deployed with the site; written only here, and only when its state or
reason changes; missing means ok):
  {"state": "ok" | "broken" | "confirming", "reason": "<one sentence for readers>", "since": "<ISO time>"}
ok after a county read that passes every check; broken after 2 or more failed county reads in a
row, with a reason that depends on what failed (a new reason needs two failed reads in a row that
agree on it, so it doesn't flip between runs); confirming while a county decrease waits for its
second read. A single failed read leaves it as it is (the next read usually works).

County decreases: a county reading below the saved YES or NO is held, not saved (read=hold). The
next county read saves it if the county still shows the same YES and NO (a correction, saved
exactly as ALLOW_DECREASE=true would), drops it if the numbers are no longer below the saved ones,
and holds the new numbers instead if they're a different decrease. ALLOW_DECREASE=true still saves
a decrease at once. Manual entries are checked as before: a lower YES or NO needs ALLOW_DECREASE.

Outputs for the workflow (GITHUB_OUTPUT):
  changed         true | false   data/results.json was written
  status_changed  true | false   data/status.json was written (a failed run can write it too)
  message         a commit message, when either was written
  read            ok | failed | skipped | hold | none   none: no read was due (push runs)
  alert           now | wait | no   now: 2 or more county reads failed in a row (the site sends
                  readers to the county), or the run failed outside a county read; wait: one
                  county read failed (the next run usually reads); no: nothing failed
  feed            html | xml | manual | none   what this run read
  failure         read | run | none   what made the run fail: a county read, or something else (a
                  repository file, a manual entry or a bug)
A failed run exits 1 and writes diagnosis.md (repo root, git-ignored): a short FYI summary, public
like the log (county numbers, contest titles and times from the county's pages, run status). It's
printed to the log too.
"""

import http.client
import json
import math
import os
import re
import socket
import ssl
import sys
import traceback
import urllib.error
import zlib
from datetime import datetime, timedelta
from pathlib import Path

import county

ROOT = Path(__file__).resolve().parent.parent
PRACTICE = os.environ.get("PRACTICE") == "1"
RESULTS = ROOT / "data" / ("rehearsal.json" if PRACTICE else "results.json")
STATUS = ROOT / "data" / "status.json"  # what the site tells readers (never written in practice runs)
CONFIG = ROOT / "config.json"
LAST_FETCH = ROOT / ".last-fetch"  # restored from the Actions cache between runs
STATE = ROOT / ".county-state.json"  # restored from the Actions cache with .last-fetch
DIAGNOSIS = ROOT / "diagnosis.md"  # written only when a run fails

# The county certifies on Dec 3. Nothing can be marked certified before the last scheduled
# update (Nov 16) is over.
CERTIFY_AFTER = datetime(2026, 11, 17, tzinfo=county.PACIFIC)

# Skip a fetch if the last one was this recent. Scheduled runs are 5 minutes apart
# but GitHub starts them a little early or late, so a strict 300 would skip on-time runs.
MIN_FETCH_GAP = 240
# A saved fetch time further ahead than this is not a real fetch (a broken cache file), so it can't block reads.
MAX_CLOCK_AHEAD = 300

HTML_RETRY_AFTER = 30 * 60  # on the XML feed, try the results page again this long after switching
BROKEN_AFTER = 2  # failed county reads in a row before the site sends readers to the county
# A county update time this far past the run's clock can't be real; saving it would make every
# later county update look old.
COUNTY_AHEAD_LIMIT = 60 * 60
# The results page's Website Updated and the XML file's time for the same county update differ by about a
# second (by about 7 minutes when the XML has no Last-Modified and its GeneratedDate is used).
SAME_UPDATE_SLACK = 10 * 60
# Text every real reply of each feed has. A reply without it (an error page sent as 200, an empty
# body) means the county's site isn't answering properly, not that its format changed.
FEED_MARKERS = {"html": "lblLastPublishedDateTime", "xml": "<NewDataSet"}

FEEDS = {"html": "the county results page", "xml": "the county XML feed"}
KINDS = ("fetch", "parse", "check")
DEFAULT_STATE = {"feed": "html", "html_retry_after": 0, "failures": 0, "last_ok": 0,
                 "last_fail": None, "pending_decrease": None}

# What data/status.json tells readers. Public: calm, short, and the same every time.
NOT_RESPONDING = "The county's results site isn't responding to our automatic updates right now."
CANT_READ = "Our automatic reader can't read the county's results right now."
CONFIRMING = "The county revised its numbers. We're confirming the change before showing it."
BROKEN_REASON = {"fetch": NOT_RESPONDING, "parse": CANT_READ, "check": CANT_READ}
STATES = ("ok", "broken", "confirming")


class CheckFailed(Exception):
    pass


class Skip(Exception):
    pass


class ReadFailed(CheckFailed):
    """A county read that failed. kind: fetch, parse or check (see the module docstring)."""

    def __init__(self, message, kind, findings=None):
        super().__init__(message)
        self.kind = kind
        self.findings = findings  # what the reader found, for diagnosis.md


def load_json(path, what):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise CheckFailed(f"{what} not found")
    except json.JSONDecodeError as e:
        raise CheckFailed(f"{what} is not valid JSON ({e.msg}, line {e.lineno})")


def read_expected_votes():
    ev = load_json(CONFIG, "config.json").get("expected_votes")
    if ev is not None and (isinstance(ev, bool) or not isinstance(ev, int) or ev <= 0):
        raise CheckFailed("config.json expected_votes must be null or a positive whole number")
    return ev


def parse_time(text):
    """County format ("11/3/2026 8:10:45 PM", seconds optional) or ISO 8601. Pacific if no offset.

    Text around a county-format time is ignored, so "Website Updated: 11/3/2026 8:10:45 PM"
    pasted from the county page works too.
    """
    text = " ".join(text.split()).upper()
    found = re.search(r"\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}(:\d{2})? ?[AP]M", text)
    if found:
        stamp = found.group(0).replace("AM", " AM").replace("PM", " PM").replace("  ", " ")
        for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p"):
            try:
                return datetime.strptime(stamp, fmt).replace(tzinfo=county.PACIFIC)
            except ValueError:
                pass
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise CheckFailed(f"county_updated not understood: {text!r} (example: 11/3/2026 8:10:45 PM)")
    return dt if dt.tzinfo else dt.replace(tzinfo=county.PACIFIC)


def manual_requested():
    return any(os.environ.get(k, "").strip() for k in ("MANUAL_YES", "MANUAL_NO", "MANUAL_COUNTY_UPDATED"))


def manual_reading(now):
    yes = os.environ.get("MANUAL_YES", "").strip()
    no = os.environ.get("MANUAL_NO", "").strip()
    updated = os.environ.get("MANUAL_COUNTY_UPDATED", "").strip()
    if not (yes or no or updated):
        return None
    if not (yes and no):
        raise CheckFailed("manual entry needs both yes and no")
    if not updated:
        # A made-up time could hide the county's next update, so copy the county's own.
        raise CheckFailed("manual entry needs county_updated: the county page's Website Updated time "
                          "(example: 11/3/2026 8:10:45 PM)")
    try:
        yes, no = county.whole_number(yes, "yes"), county.whole_number(no, "no")
    except county.ParseError as e:
        raise CheckFailed(str(e))
    updated = parse_time(updated)
    if updated > now + timedelta(minutes=5):
        raise CheckFailed(f"county_updated {updated.isoformat()} is in the future")
    return {"yes": yes, "no": no, "county_updated": updated.isoformat(),
            "county_ballots_counted": None, "source": "manual"}


# ---------- .county-state.json: which feed to read, and how the last reads went ----------

def _seconds(v):
    """A saved time or count: a finite number >= 0, as whole seconds. None if it isn't one."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0:
        return int(v)
    return None


def _whole(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _aware_iso(v):
    """An ISO 8601 time with an offset, or None."""
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return v if dt.tzinfo else None


def load_state():
    """The county state; a missing or unreadable file (or field) counts as the default."""
    state = dict(DEFAULT_STATE)
    try:
        saved = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return state
    if not isinstance(saved, dict):
        return state
    if saved.get("feed") in FEEDS:
        state["feed"] = saved["feed"]
    for key in ("html_retry_after", "failures", "last_ok"):
        v = _seconds(saved.get(key))
        if v is not None:
            state[key] = v
    fail = saved.get("last_fail")
    if isinstance(fail, dict) and fail.get("feed") in FEEDS and fail.get("kind") in KINDS \
            and _seconds(fail.get("at")) is not None:
        state["last_fail"] = {"feed": fail["feed"], "kind": fail["kind"], "at": _seconds(fail["at"])}
    held = saved.get("pending_decrease")
    if isinstance(held, dict) and _whole(held.get("yes")) is not None and _whole(held.get("no")) is not None \
            and _aware_iso(held.get("county_updated")) and _seconds(held.get("seen_at")) is not None:
        state["pending_decrease"] = {"yes": held["yes"], "no": held["no"], "county_updated": held["county_updated"],
                                     "seen_at": _seconds(held["seen_at"])}
    return state


def save_state(state):
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(state) + "\n")
    os.replace(tmp, STATE)


def pick_feed(state, now):
    """The XML feed until its retry time comes (a retry time impossibly far off counts as due)."""
    t = now.timestamp()
    if state["feed"] == "xml" and t < state["html_retry_after"] <= t + HTML_RETRY_AFTER + 60:
        return "xml"
    return "html"


def next_state(state, feed, outcome, now, pending=None):
    """The state after one read of `feed`. outcome: ok or hold (the read worked), or the kind of
    failure (fetch, parse, check). pending: the held decrease after a read that worked."""
    t = int(now.timestamp())
    new = dict(state)
    worked = outcome in ("ok", "hold")
    if worked:
        new["failures"], new["last_ok"], new["pending_decrease"] = 0, t, pending
    else:  # a held decrease waits for the next read that works
        # The 30-minute retry of a page that already failed doesn't count: on the XML feed, only XML reads do.
        retry = feed == "html" and state["feed"] == "xml"
        new["failures"] = state["failures"] + (0 if retry else 1)
        new["last_fail"] = {"feed": feed, "kind": outcome, "at": t}
    if feed == "xml":
        if not worked and state["failures"]:  # the XML failed right after another failed read: back to the page
            new["feed"], new["html_retry_after"] = "html", 0
        else:  # stay on the XML feed (one XML failure is read again) until the retry time set when it switched
            new["feed"] = "xml"
    elif worked:
        new["feed"], new["html_retry_after"] = "html", 0
    else:  # the results page failed: the XML feed next, the page again in 30 minutes
        new["feed"], new["html_retry_after"] = "xml", t + HTML_RETRY_AFTER
    return new


# ---------- data/status.json: what the site tells readers ----------

def read_status():
    """data/status.json as a dict. Missing counts as ok; None if it can't be read."""
    try:
        data = json.loads(STATUS.read_text())
    except FileNotFoundError:
        return {"state": "ok", "reason": "", "since": None}
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("state") not in STATES or not isinstance(data.get("reason"), str):
        return None
    return data


def set_status(state, reason, now):
    """Write data/status.json if its state or reason changes. Returns True if it was written."""
    if PRACTICE:
        return False
    current = read_status()
    if current is not None and (current["state"], current["reason"]) == (state, reason):
        return False
    since = now.isoformat()
    if current is not None and current["state"] == state and _aware_iso(current.get("since")):
        since = current["since"]  # the same state with a new reason: it began when it began
    tmp = STATUS.with_name(STATUS.name + ".tmp")
    tmp.write_text(json.dumps({"state": state, "reason": reason, "since": since}, indent=2) + "\n")
    os.replace(tmp, STATUS)
    was = "unreadable" if current is None else current["state"]
    print(f"Site status: {state} (was {was})" + (f". Readers see: {reason}" if reason else ""))
    return True


# ---------- one county read ----------

def fetch_problem(e):
    """What went wrong with a download, in a few words (no server text)."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    if isinstance(reason, TimeoutError):  # socket.timeout is TimeoutError
        return "timed out"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "certificate check failed"
    if isinstance(reason, socket.gaierror):
        return "address lookup failed"
    if isinstance(reason, (ConnectionError, http.client.HTTPException, EOFError, zlib.error, ssl.SSLError)):
        return f"connection problem ({type(reason).__name__})"
    if isinstance(reason, OSError):
        return f"network problem ({type(reason).__name__})"
    return type(e).__name__


def county_reading(now, feed="html"):
    """One fetch of the chosen county feed, if the last county fetch was long enough ago."""
    if os.environ.get("KEEPALIVE") == "true" and os.environ.get("CACHE_HIT") == "false":
        raise Skip("the Actions cache didn't restore the last county fetch time, so only scheduled runs "
                   "read the county")
    try:
        last = float(LAST_FETCH.read_text())
    except (OSError, ValueError):
        last = 0.0
    if not math.isfinite(last) or last > now.timestamp() + MAX_CLOCK_AHEAD:
        print("Ignoring a saved county fetch time that isn't a real time")
        last = 0.0
    gap = now.timestamp() - last
    if gap < MIN_FETCH_GAP:
        raise Skip(f"county page was fetched {gap:.0f} s ago; waiting for the next run")
    LAST_FETCH.write_text(f"{now.timestamp():.0f}\n")

    what = "county XML feed" if feed == "xml" else "county page"
    try:
        if feed == "xml":
            text, headers = county.fetch_xml()
        else:
            text, headers = county.fetch(), None
    except county.ParseError as e:  # it answered, in a form this reader can't unpack
        raise ReadFailed(f"{what} couldn't be read: {e}", "parse")
    except Exception as e:
        raise ReadFailed(f"{what} fetch failed: {fetch_problem(e)}", "fetch")
    try:
        r = county.parse_xml(text, headers) if feed == "xml" else county.parse(text)
    except Exception as e:
        problem = str(e) if isinstance(e, county.ParseError) else f"the reader stopped on an unexpected {type(e).__name__}"
        try:
            found = county.inspect_xml(text, headers) if feed == "xml" else county.inspect(text)
        except Exception:  # the diagnosis is a bonus; the failed read must still be recorded
            found = None
        # Not the county's page or file at all (an error page sent as 200, an empty reply): the site
        # isn't answering properly. Only a reply that looks like the county's counts as unreadable.
        kind = "parse" if FEED_MARKERS[feed] in (text or "") else "fetch"
        raise ReadFailed(f"{what} check failed: {problem}", kind, findings=found)
    return {"yes": r["yes"], "no": r["no"], "county_updated": r["county_updated"],
            "county_ballots_counted": r["county_ballots_counted"], "source": "county", "feed": feed}


def same_update(latest, reading):
    """True when a county reading from the other feed (or after a manual entry) has the saved numbers,
    stamped a few minutes off: the same county update, which would otherwise be saved twice. Ballots
    counted don't decide it: the two feeds can total them differently, and they aren't shown."""
    feed = reading.get("feed")
    if reading.get("source") != "county" or feed not in FEEDS:
        return False
    saved_feed = latest.get("feed") or ("html" if latest.get("source") == "county" else latest.get("source"))
    if saved_feed == feed or (reading["yes"], reading["no"]) != (latest.get("yes"), latest.get("no")):
        return False
    gap = datetime.fromisoformat(reading["county_updated"]) - datetime.fromisoformat(latest["county_updated"])
    return abs(gap.total_seconds()) <= SAME_UPDATE_SLACK


def is_new_update(latest, reading):
    """Whether the reading is a county update the saved results don't have yet."""
    saved = latest.get("county_updated")
    if not saved:
        return True
    if datetime.fromisoformat(reading["county_updated"]) <= datetime.fromisoformat(saved):
        return False
    return not same_update(latest, reading)


def apply_reading(results, reading, now, allow_decrease=False, allowed_by="allowed for this run"):
    """Add the reading if it's a new county update. Returns True if it was added."""
    latest = results.get("latest") or {}
    saved = latest.get("county_updated")
    if saved and datetime.fromisoformat(reading["county_updated"]) <= datetime.fromisoformat(saved):
        if reading["source"] == "manual":
            raise CheckFailed(f"county_updated must be newer than the saved {saved}")
        print(f"No new county update (still {saved})")
        return False
    if saved and same_update(latest, reading):
        print(f"No new county update (still {saved}; the same numbers, stamped {reading['county_updated']} in this feed)")
        return False
    for side in ("yes", "no"):
        before = latest.get(side) or 0
        if reading[side] < before:
            if not allow_decrease:
                raise CheckFailed(f"{side.upper()} went down: {before:,} -> {reading[side]:,}")
            print(f"{side.upper()} went down: {before:,} -> {reading[side]:,} ({allowed_by})")

    entry = {
        "yes": reading["yes"],
        "no": reading["no"],
        "county_updated": reading["county_updated"],
        "county_ballots_counted": reading["county_ballots_counted"],
        "fetched_at": now.isoformat(),
        "source": reading["source"],
    }
    if reading.get("feed"):
        entry["feed"] = reading["feed"]
    results["latest"] = entry
    results.setdefault("history", []).append(dict(entry))
    if results.get("status") != "final":
        results["status"] = "counting" if entry["yes"] + entry["no"] > 0 else "pre"
    return True


def judge_reading(results, reading, held, now, allow_decrease):
    """Check a county reading and save it, or hold it if it lowers the count.

    held: the decrease held by an earlier read, or None.
    Returns (added, outcome, held after this read, corrected); outcome is ok or hold.
    """
    ahead = (datetime.fromisoformat(reading["county_updated"]) - now).total_seconds()
    if ahead > COUNTY_AHEAD_LIMIT:
        raise CheckFailed(f"the county update time {reading['county_updated']} is more than an hour "
                          "after this run's time")
    latest = results.get("latest") or {}
    lower = [s for s in ("yes", "no") if reading[s] < (latest.get(s) or 0)] if is_new_update(latest, reading) else []
    if lower and not allow_decrease:
        if held and (held["yes"], held["no"]) == (reading["yes"], reading["no"]):
            print(f"The county still shows YES {reading['yes']:,} and NO {reading['no']:,} on a second read: "
                  "saving it as a county correction")
            added = apply_reading(results, reading, now, allow_decrease=True, allowed_by="confirmed on a second read")
            return added, "ok", None, True
        for side in lower:
            print(f"{side.upper()} went down: {latest.get(side) or 0:,} -> {reading[side]:,}")
        if held:
            print(f"(a different lower count than the one held before: YES {held['yes']:,}, NO {held['no']:,})")
        print("Held, not saved: the next read saves it if the county still shows these numbers (a correction)")
        new_held = {"yes": reading["yes"], "no": reading["no"], "county_updated": reading["county_updated"],
                    "seen_at": int(now.timestamp())}
        return False, "hold", new_held, False
    if held:
        print("The county no longer shows the lower count held before; it's dropped")
    return apply_reading(results, reading, now, allow_decrease), "ok", None, False


def write_outputs(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


# ---------- diagnosis.md: what a failed run found (FYI; public like the log) ----------

def clock(when):
    """e.g. Tue 8:42 PM (Pacific). when: a datetime or epoch seconds."""
    try:
        dt = datetime.fromtimestamp(when, county.PACIFIC) if isinstance(when, (int, float)) else when.astimezone(county.PACIFIC)
    except (OverflowError, OSError, ValueError):
        return "an unknown time"
    return f"{dt:%a} {dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _code(text, limit=120):
    """County or error text as inline code: shown as it is, and a mention in it can't notify anyone."""
    text = "".join(ch for ch in " ".join(str(text).split()) if ch.isprintable()).replace("`", "'")
    if len(text) > limit:
        text = text[:limit - 3] + "..."
    return f"`{text}`"


def _shown_votes(text):
    try:
        return f"{county.whole_number(text or '', 'votes'):,}"
    except county.ParseError:
        return _code("(none)" if text is None else text, 30)


def _county_time(iso):
    try:
        return county.county_time(datetime.fromisoformat(iso))
    except (TypeError, ValueError):
        return str(iso)


def reading_findings(reading):
    """A reading that failed a check, as findings."""
    try:
        updated_text = _county_time(reading["county_updated"])
        rows = [("YES", str(reading["yes"])), ("NO", str(reading["no"]))]
        return {"size": None, "page_title": None, "updated_text": updated_text, "updated": reading["county_updated"],
                "updated_from": "County update", "ballots": reading["county_ballots_counted"], "titles": None,
                "races": [{"title": county.CONTEST_TITLE, "rows": rows, "yes": reading["yes"], "no": reading["no"]}]}
    except (KeyError, TypeError):
        return None


def found_lines(found, feed):
    about = []
    # Only a short reply's size: a full results page's size changes from run to run, and alerts post again
    # only when the text changes.
    if found.get("size") is not None and found["size"] < 2000:
        about.append(f"only {found['size']:,} characters")
    if found.get("page_title"):
        about.append(f"page title {_code(found['page_title'], 60)}")
    lines = ["", "**What the reader found**" + (f" ({', '.join(about)})" if about else "")]
    if found.get("updated_text"):
        lines.append(f"- {found['updated_from'] or 'County update'}: {_code(found['updated_text'], 40)}"
                     + ("" if found.get("updated") else " (not understood)"))
    else:
        lines.append("- No county update time" + (" in the XML" if feed == "xml" else " (Website Updated)"))
    if found.get("ballots") is not None:
        lines.append(f"- Ballots counted: {found['ballots']:,}")
    titles = found.get("titles")
    if titles:
        shown = ", ".join(_code(t, 80) for t in titles[:6])
        lines.append(f"- Contests with Belmont or Measure Y: {shown}" + (f" and {len(titles) - 6} more" if len(titles) > 6 else ""))
    elif titles is not None:
        lines.append("- No contest with Belmont or Measure Y in its title")
    for race in (found.get("races") or [])[:3]:
        rows = race["rows"]
        shown = ", ".join(f"{n if n in ('YES', 'NO') else _code(n, 30)} {_shown_votes(v)}" for n, v in rows[:4]) or "no rows"
        missing = [s for s in ("YES", "NO") if rows and not any(s in n.split() for n, _ in rows)]
        lines.append(f"- {_code(race['title'], 80)}: {shown}" + "".join(f" (no {s} row)" for s in missing))
    return lines


def next_read(state, now):
    """Which feed the next county read uses, for the diagnosis."""
    if state["feed"] == "xml" and pick_feed(state, now) == "xml":
        return f"{FEEDS['xml']} (the backup); {FEEDS['html']} again after {clock(state['html_retry_after'])}"
    if state["feed"] == "xml":
        return f"{FEEDS['html']} (a retry; {FEEDS['xml']} is the backup)"
    return FEEDS["html"]


def readers_line(status, state, read_failed):
    """What readers see on the site after this run."""
    if status is not None and status["state"] == "broken":
        return f"- Readers: the site is sending readers to the county's results page, with this note: {status['reason']}"
    if status is not None and status["state"] == "confirming":
        return "- Readers: the site shows the last saved numbers, with a note that a county revision is being confirmed"
    if read_failed and state["failures"] == BROKEN_AFTER - 1:
        return ("- Readers: the site still shows the last saved numbers. If the next county read fails too, "
                "it sends readers to the county's results page")
    return "- Readers: the site still shows the last saved numbers"


def diagnosis(run, err, now):
    """Short FYI markdown: what failed, what the reader found, what the automation does next."""
    state = run.state or load_state()
    read_failed = isinstance(err, ReadFailed)
    this = {"html": "read " + FEEDS["html"], "xml": "read " + FEEDS["xml"] + " (the backup)",
            "manual": "a manual entry"}.get(run.feed, "no county read" + (" (a push)" if run.event == "push" else ""))
    lines = [f"**{'County read failed' if read_failed else 'Update run failed'}** at {clock(now)}",
             "",
             f"- What failed: {_code(err, 200)}",
             f"- This run: {this}",
             f"- Next county read: {next_read(state, now)}"]
    if state["failures"]:
        lines.append(f"- Failed county reads in a row: {state['failures']}"
                     + (f"; last good read {clock(state['last_ok'])}" if state["last_ok"] else ""))
    held = state.get("pending_decrease")
    if held:
        lines.append(f"- Waiting to confirm a lower county count: YES {held['yes']:,}, NO {held['no']:,} "
                     f"(county update {_code(_county_time(held['county_updated']), 40)})")
    lines.append(readers_line(read_status(), state, read_failed))
    found = getattr(err, "findings", None)
    if found:
        lines += found_lines(found, run.feed)
    return "\n".join(lines)


# ---------- one run ----------

class Run:
    """What this run did: the workflow outputs, and what the diagnosis needs."""

    def __init__(self, event):
        self.event = event
        self.read = "none"   # ok | failed | skipped | hold | none
        self.alert = "no"    # now | wait | no
        self.feed = "none"   # html | xml | manual | none
        self.failure = "none"  # read | run | none: what made the run fail
        self.state = None    # the county state after this run's county read, if it read the county
        self.changed = False         # data/results.json written
        self.status_changed = False  # data/status.json written
        self.status_written = None   # the state it was written with
        self.prev_kind = None        # the kind of the read before this one, if it failed too
        self.message = ""

    def after_read(self, state, feed, outcome, now, pending=None):
        self.prev_kind = (state["last_fail"] or {}).get("kind") if state["failures"] else None
        self.state = next_state(state, feed, outcome, now, pending)
        try:
            save_state(self.state)
        except OSError as e:
            print(f"Could not save {STATE.name}: {type(e).__name__}")

    def set_status(self, state, reason, now):
        try:
            if set_status(state, reason, now):
                self.status_changed, self.status_written = True, state
        except OSError as e:
            print(f"Could not save data/{STATUS.name}: {type(e).__name__}")

    def fail(self, err, now):
        if self.event != "push":
            self.read = "failed"
        self.failure = "read" if isinstance(err, ReadFailed) else "run"
        if isinstance(err, ReadFailed) and self.state is not None:
            if self.state["failures"] >= BROKEN_AFTER:
                self.alert = "now"
                reason, current = BROKEN_REASON[err.kind], read_status()
                if current and current["state"] == "broken" and current["reason"] in BROKEN_REASON.values() \
                        and BROKEN_REASON.get(self.prev_kind) != reason:
                    reason = current["reason"]  # a new reason needs two failed reads in a row that agree on it
                self.set_status("broken", reason, now)
            else:  # one failed read (or a failed page retry): the next read usually works
                self.alert = "wait"
        else:  # not a county read: a file, an entry or a bug, which the next run won't fix by itself
            self.alert = "now"
        if self.status_changed:
            self.message = f"Update site status: {self.status_written}"
        try:
            text = diagnosis(self, err, now)
        except Exception as e:  # the alert matters more than its details
            text = f"**Update run failed**\n\n- What failed: {_code(err, 200)}\n- (no more details: {type(e).__name__})"
        try:
            DIAGNOSIS.write_text(text + "\n")
        except OSError as e:
            print(f"Could not write {DIAGNOSIS.name}: {type(e).__name__}")
        print()
        print(text.replace("**", "").replace("`", ""))

    def write_outputs(self):
        values = {"changed": str(self.changed).lower(), "status_changed": str(self.status_changed).lower()}
        if self.message:
            values["message"] = self.message
        values.update(read=self.read, alert=self.alert, feed=self.feed, failure=self.failure)
        write_outputs(**values)


def show_reading(reading):
    print(f"Source:           {reading['source']}")
    if reading.get("feed"):
        print(f"Feed:             {FEEDS[reading['feed']]}")
    label = "XML file time:" if reading.get("feed") == "xml" else "Website Updated:"
    print(f"{label:<18}{reading['county_updated']}")
    if reading["county_ballots_counted"] is not None:
        print(f"Ballots Counted:  {reading['county_ballots_counted']:,}")
    print(f"YES:              {reading['yes']:,}")
    print(f"NO:               {reading['no']:,}")


def read_county(run, results, now, allow_decrease):
    """One county read from the feed the state picks; updates the state.
    Returns (reading, added, corrected)."""
    state = load_state()
    feed = pick_feed(state, now)
    if state["feed"] == "xml" and feed == "html":
        print("Trying the county results page again (the XML feed is the backup)")
    try:
        reading = county_reading(now, feed)
    except Skip as e:
        print(f"Skipped fetch: {e}")
        run.read = "skipped"
        return None, False, False
    except ReadFailed as e:
        run.feed = feed
        run.after_read(state, feed, e.kind, now)
        raise
    run.feed = feed
    show_reading(reading)
    try:
        added, outcome, held, corrected = judge_reading(results, reading, state["pending_decrease"], now, allow_decrease)
    except Exception as e:  # the reading failed a check (or checking it crashed): a failed county read
        if not isinstance(e, CheckFailed):
            traceback.print_exc()
            e = CheckFailed(f"unexpected {type(e).__name__} while checking the reading; the run log has the details")
        run.after_read(state, feed, "check", now)
        raise ReadFailed(str(e), "check", findings=reading_findings(reading))
    run.after_read(state, feed, outcome, now, held)
    run.read = outcome
    if feed == "html" and state["feed"] == "xml":
        print("The county results page reads again; the XML feed is only the backup now")
    if run.state["feed"] == "xml":
        print(f"Next run reads {FEEDS['xml']} (the results page again after {clock(run.state['html_retry_after'])})")
    return reading, added, corrected


def update(run, now):
    results = load_json(RESULTS, f"data/{RESULTS.name}")
    before = json.dumps(results)
    was_final = results.get("status") == "final"
    expected_votes = read_expected_votes()
    allow_decrease = os.environ.get("ALLOW_DECREASE") == "true"

    reading, added, corrected = None, False, False
    if run.event != "push":
        if manual_requested():
            run.feed = "manual"
            reading = manual_reading(now)
            show_reading(reading)
            added = apply_reading(results, reading, now, allow_decrease)
            run.read = "ok"
        elif PRACTICE:
            raise CheckFailed("practice runs need YES and NO numbers")
        else:
            reading, added, corrected = read_county(run, results, now, allow_decrease)

    results["expected_votes"] = expected_votes

    if os.environ.get("MARK_CERTIFIED") == "true":
        latest = results.get("latest") or {}
        if now < CERTIFY_AFTER:
            raise CheckFailed(f"can't mark the count certified before {CERTIFY_AFTER:%b} {CERTIFY_AFTER.day}; "
                              "the county certifies on Dec 3")
        if (latest.get("yes") or 0) + (latest.get("no") or 0) == 0:
            raise CheckFailed("can't mark the count certified before any votes are counted")
        results["status"] = "final"
        print("Marked the count as certified")

    message = ""
    if json.dumps(results) == before:
        print(f"{RESULTS.name} unchanged")
    else:
        tmp = RESULTS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(results, indent=2) + "\n")
        os.replace(tmp, RESULTS)
        run.changed = True
        certified_now = results.get("status") == "final" and not was_final
        if added:
            via = " (XML feed)" if reading.get("feed") == "xml" else ""
            message = f"Update results: {reading['source']} reading{via}, county update {reading['county_updated']}"
            if corrected:
                message += ", a county correction confirmed on a second read"
            if certified_now:
                message += ", marked certified"
        elif certified_now:
            message = "Mark the count as certified"
        else:
            message = "Update expected_votes from config.json"
        print(f"Saved {RESULTS.name} ({message})")

    # Only a county read changes what the site tells readers; manual entries and push runs don't.
    if run.feed in FEEDS and run.read == "ok":
        run.set_status("ok", "", now)
    elif run.read == "hold":
        run.set_status("confirming", CONFIRMING, now)
    if run.status_changed:
        state = run.status_written
        message = f"{message}; site status {state}" if message else f"Update site status: {state}"
    run.message = message
    return 0


def main(now=None):
    now = now or datetime.now(county.PACIFIC).replace(microsecond=0)
    run = Run(os.environ.get("EVENT", ""))
    try:
        DIAGNOSIS.unlink()  # only a failed run leaves one
    except OSError:
        pass
    try:
        code = update(run, now)
    except Exception as e:
        if not isinstance(e, CheckFailed):  # a bug or a file problem: keep the details
            traceback.print_exc()
            e = CheckFailed(f"unexpected {type(e).__name__}; the run log has the details")
        print(f"Check failed: {e}")
        print(f"Nothing saved to {RESULTS.name}.")
        run.fail(e, now)
        code = 1
    run.write_outputs()
    return code


if __name__ == "__main__":
    sys.exit(main())
