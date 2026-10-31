"""Shared helpers for the election-night scripts in ops/. Standard library only.

GitHub API calls use the run's GITHUB_TOKEN. Errors name only the API path and the HTTP status:
never the token, never a response body. Workflow logs and issues are public, so everything these
scripts print or post is county numbers, times and run status.
"""

import http.client
import json
import math
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PACIFIC = ZoneInfo("America/Los_Angeles")
USER_AGENT = "measure-y-watch ops"
LAST_FETCH = ROOT / ".last-fetch"          # restored from the Actions cache by the workflow
COUNTY_STATE = ROOT / ".county-state.json"  # written only by scraper/update.py
STATUS_FILE = ROOT / "data" / "status.json"  # written only by scraper/update.py; missing means ok
DATA_FILES = ("data/results.json", "data/status.json")  # what the live page must match on main
MAX_CLOCK_AHEAD = 300  # a saved fetch time further ahead than this is a broken file, not a fetch
# Run states that mean an Update results run is waiting or working.
BUSY = {"queued", "in_progress", "waiting", "pending", "requested"}
RETRY_DELAY = 3


class ApiError(Exception):
    """A GitHub API call that failed. str() is safe to print: method, path and status only."""

    def __init__(self, what, status=None):
        super().__init__(what)
        self.status = status


def _why(e):
    """A short printable name for a network error (no URL, no headers)."""
    reason = getattr(e, "reason", None)
    return type(reason).__name__ if isinstance(reason, BaseException) else type(e).__name__


class GitHub:
    """The repository's REST API, as the run's GITHUB_TOKEN."""

    def __init__(self, token=None, repo=None, api=None, timeout=20):
        self.token = os.environ.get("GITHUB_TOKEN", "") if token is None else token
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
        self.api = (api or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.timeout = timeout

    def ready(self):
        return bool(self.token) and "/" in self.repo

    def call(self, method, rest, body=None, raw=False, **params):
        """One call to /repos/<repo><rest>. Returns parsed JSON (bytes with raw=True).

        GETs are tried twice on a server or network error; writes once, so nothing is posted twice.
        """
        path = f"/repos/{self.repo}{rest}"
        url = self.api + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        tries = 2 if method == "GET" else 1
        for attempt in range(1, tries + 1):
            try:
                req = urllib.request.Request(url, data=data, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as res:
                    payload = res.read()
                break
            except urllib.error.HTTPError as e:
                e.close()
                if e.code >= 500 and attempt < tries:
                    time.sleep(RETRY_DELAY)
                    continue
                raise ApiError(f"{method} {path}: HTTP {e.code}", e.code) from None
            except (OSError, http.client.HTTPException) as e:
                if attempt < tries:
                    time.sleep(RETRY_DELAY)
                    continue
                raise ApiError(f"{method} {path}: {_why(e)}") from None
        if raw:
            return payload
        if not payload.strip():
            return None
        try:
            return json.loads(payload)
        except ValueError:
            raise ApiError(f"{method} {path}: answer is not JSON") from None

    def get(self, rest, **params):
        return self.call("GET", rest, **params)

    def post(self, rest, body):
        return self.call("POST", rest, body=body)

    def patch(self, rest, body):
        return self.call("PATCH", rest, body=body)


# ---------- runs ----------

def update_runs(gh, per_page=10):
    """The newest Update results runs, newest first."""
    return (gh.get("/actions/workflows/update.yml/runs", per_page=per_page) or {}).get("workflow_runs") or []


def busy_runs(runs):
    return [r for r in runs if r.get("status") in BUSY]


def dispatch(gh, workflow, inputs=None):
    """Start a workflow on main."""
    body = {"ref": "main"}
    if inputs:
        body["inputs"] = inputs
    gh.call("POST", f"/actions/workflows/{workflow}/dispatches", body=body)


def workflow_state(gh, workflow):
    """'active', 'disabled_manually', ... for a workflow file."""
    return (gh.get(f"/actions/workflows/{workflow}") or {}).get("state")


def describe_run(run):
    """'completed (failure), created 8:31 PM by schedule' for a run object."""
    state = run.get("status") or "unknown"
    if state == "completed" and run.get("conclusion"):
        state += f" ({run['conclusion']})"
    created = parse_iso(run.get("created_at"))
    when = f", created {clock(created.timestamp())}" if created else ""
    return f"{state.replace('_', ' ')}{when} by {run.get('event') or 'unknown'}"


def run_url():
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo, run = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", "")
    return f"{server}/{repo}/actions/runs/{run}" if repo and run else ""


# ---------- times ----------

def parse_iso(text):
    """An ISO 8601 time (GitHub's or results.json's) as an aware datetime, or None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=PACIFIC)


def clock(ts, seconds=False):
    """'8:15 PM' (Pacific) for epoch seconds."""
    dt = datetime.fromtimestamp(ts, PACIFIC)
    sec = f":{dt.second:02d}" if seconds else ""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}{sec} {'AM' if dt.hour < 12 else 'PM'}"


def day(ts):
    """'Tue Nov 3' (Pacific) for epoch seconds."""
    dt = datetime.fromtimestamp(ts, PACIFIC)
    return f"{dt:%a %b} {dt.day}"


def when(ts):
    """'8:15 PM Pacific (Tue Nov 3)'."""
    return f"{clock(ts)} Pacific ({day(ts)})"


# ---------- the cached county fetch state ----------

def last_fetch(t=None):
    """Epoch seconds of the last county fetch (.last-fetch), or None.

    A time further ahead than MAX_CLOCK_AHEAD isn't a real fetch (update.py ignores it too)."""
    try:
        value = float(LAST_FETCH.read_text().strip())
    except (OSError, ValueError):
        return None
    t = time.time() if t is None else t
    if not math.isfinite(value) or value <= 0 or value > t + MAX_CLOCK_AHEAD:
        return None
    return value


def county_state():
    """.county-state.json with defaults for anything missing or unreadable."""
    state = {"feed": "html", "html_retry_after": 0, "failures": 0, "last_ok": 0,
             "last_fail": None, "pending_decrease": None}
    try:
        data = json.loads(COUNTY_STATE.read_text())
    except (OSError, ValueError):
        return state
    if not isinstance(data, dict):
        return state
    if data.get("feed") in ("html", "xml"):
        state["feed"] = data["feed"]
    for key in ("html_retry_after", "failures", "last_ok"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            state[key] = value
    for key in ("last_fail", "pending_decrease"):
        if isinstance(data.get(key), dict):
            state[key] = data[key]
    return state


# ---------- data/status.json ----------

def read_status(raw):
    """data/status.json (bytes or text) as {state, reason, since}. Missing or unreadable means ok."""
    out = {"state": "ok", "reason": "", "since": ""}
    if raw is None:
        return out
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return out
    if isinstance(data, dict) and data.get("state") in ("ok", "broken", "confirming"):
        out["state"] = data["state"]
        out["reason"] = " ".join(str(data.get("reason") or "").split())[:300]
        out["since"] = str(data.get("since") or "")[:40]
    return out


def local_status():
    """This checkout's data/status.json."""
    try:
        return read_status(STATUS_FILE.read_bytes())
    except OSError:
        return read_status(None)


def status_summary(raw):
    """'ok' or 'broken ("reason")' for a status.json, or 'none (counts as ok)'."""
    if raw is None:
        return "none (counts as ok)"
    s = read_status(raw)
    return s["state"] + (f' ("{s["reason"]}")' if s["reason"] else "")


# ---------- the live page and main ----------

def page_url():
    url = os.environ.get("PAGE_URL")
    if url:
        return url.rstrip("/")
    owner, _, name = os.environ.get("GITHUB_REPOSITORY", "/").partition("/")
    return f"https://{owner.lower()}.github.io/{name}"


def fetch_live(path, timeout=20):
    """A file from the live site, past any cache: (bytes or None, problem, HTTP status or None)."""
    url = f"{page_url()}/{path}?t={time.time_ns()}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read(), None, res.status
    except urllib.error.HTTPError as e:
        e.close()
        return None, f"HTTP {e.code}", e.code
    except (OSError, http.client.HTTPException) as e:
        return None, _why(e), None


def main_file(gh, path):
    """A file as it is on main right now (bytes), or None if main doesn't have it."""
    try:
        return gh.call("GET", f"/contents/{path}", raw=True, ref="main")
    except ApiError as e:
        if e.status == 404:
            return None
        raise


def compare_live(gh):
    """The live page's data files against main's.

    Returns {path: (verdict, main bytes or None, live bytes or None, problem)} with verdict 'same',
    'behind' or 'unknown' (the live file couldn't be read). Main without status.json matches a
    live page without one. Raises ApiError if main's files can't be read.
    """
    out = {}
    for path in DATA_FILES:
        mine = main_file(gh, path)
        if mine is None and path == "data/results.json":
            raise ApiError(f"GET {path} on main: missing", 404)
        live, problem, code = fetch_live(path)
        if mine is None:
            verdict = "same" if code == 404 else "behind" if live is not None else "unknown"
        elif live is not None:
            verdict = "same" if live == mine else "behind"
        else:
            verdict = "behind" if code == 404 else "unknown"
        out[path] = (verdict, mine, live, problem)
    return out


def overall(compared):
    """'same', 'behind' or 'unknown' for compare_live's answer."""
    verdicts = {v[0] for v in compared.values()}
    return "behind" if "behind" in verdicts else "unknown" if "unknown" in verdicts else "same"


def data_changed_at(gh):
    """Epoch seconds of the newest commit on main that changed a data file the page reads, or None."""
    newest = None
    for path in DATA_FILES:
        commits = gh.get("/commits", sha="main", path=path, per_page=1) or []
        if commits:
            stamp = parse_iso(((commits[0].get("commit") or {}).get("committer") or {}).get("date"))
            if stamp and (newest is None or stamp.timestamp() > newest):
                newest = stamp.timestamp()
    return newest


def summary(raw):
    """'county update 8:10:45 PM (Tue Nov 3), YES 3,412, NO 2,988' for a results.json, or a short problem."""
    try:
        latest = json.loads(raw).get("latest") or {}
        yes, no = latest.get("yes"), latest.get("no")
        stamp = parse_iso(latest.get("county_updated"))
        head = f"county update {clock(stamp.timestamp(), seconds=True)} ({day(stamp.timestamp())})" if stamp else "no county update time"
        if isinstance(yes, int) and isinstance(no, int):
            return f"{head}, YES {yes:,}, NO {no:,}"
        return f"{head}, counts not readable"
    except (ValueError, AttributeError, TypeError):
        return "not readable as results"


# ---------- the forecast engine ----------

def engine_check(path, timeout=120):
    """Runs ops/engine_check.js on a results file.

    Returns (True, 'engine OK'), (False, 'engine check failed: ...'), or (None, why) when the check
    itself couldn't run (no node, a timeout, or a crash before it could report).
    """
    try:
        p = subprocess.run(["node", str(ROOT / "ops" / "engine_check.js"), str(path)],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "node not found"
    except subprocess.TimeoutExpired:
        return None, "engine check timed out"
    lines = [line.strip() for line in p.stdout.splitlines() if line.strip()]
    first = lines[0] if lines else ""
    if p.returncode == 0 and first == "engine OK":
        return True, first
    if p.returncode == 1 and first.startswith("engine check failed"):
        return False, first
    return None, f"engine check didn't run properly (exit {p.returncode})"
