"""Election-night status issues on GitHub: FYI notes for the owner. No action is ever needed.

  python ops/status.py open <kind> <markdown-file>
  python ops/status.py resolve <kind> "<one line>"
  python ops/status.py note <kind> <markdown-file>

open     opens the kind's issue (mentioning the owner), or comments on the one already open: at
         once when it says something the issue hasn't said yet ("New details."), otherwise only
         when 30 minutes passed since the last post ("Still the same after 30 minutes."). So a
         problem that flips between two descriptions isn't posted on every run.
resolve  comments the line on the kind's open issue and closes it; nothing happens if none is open
note     posts an issue and closes it at once (preflight results, the drill)

Kinds: update-failed, run-failed, overdue, site-behind, engine, preflight, drill. At most one open
issue per kind, labeled election-night-status and kind:<kind> (the labels are created if missing). Every
post starts "No action needed." and says what the automation is doing about it, and, when
data/status.json says broken, that the site is sending readers to the county's results.

Each post ends with a hidden marker holding a hash of its content. What changes from run to run
doesn't count toward the hash: lines with a run link (/actions/runs/), times written as a weekday
and a clock ("Tue 8:42 PM"; county times like "11/3/2026 8:10:45 PM" still count) and "in a row"
counters.

Issues are public like the run logs: callers post only county numbers, contest titles and times
from the county's pages, and run status. Text from callers can't mention anyone.

Environment: GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID, GITHUB_API_URL
(Actions sets all but the token); ALERT_MENTION overrides who is mentioned (default: the
repository owner; empty: nobody); STATUS_QUICK=1 (Update results runs, which publish after it)
gives each API call one try of at most 8 seconds: a missed post is made by the next run.
Always exits 0. Prints status lines only, never the token.
"""

import hashlib
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import common

LABEL = "election-night-status"
BOT = "github-actions[bot]"
KINDS = ("update-failed", "run-failed", "overdue", "site-behind", "engine", "preflight", "drill")
TITLES = {
    "update-failed": "FYI: the county's results can't be read right now",
    "run-failed": "FYI: an Update results run failed",
    "overdue": "FYI: county reads are running late",
    "site-behind": "FYI: the live page is behind",
    "engine": "FYI: the forecast engine check failed",
    "preflight": "FYI: preflight found a problem",
    "drill": "DRILL: test alert, nothing is wrong, no action needed",
}
NOTE_TITLES = {"preflight": "FYI: preflight passed"}  # a note about a problem keeps the kind's TITLES title
# What the automation is doing about each kind (the first line of every post).
DOING = {
    "update-failed": "Update results keeps trying on every run and switches to the county's backup data feed "
                     "when the results page can't be read. This closes by itself after the next good read.",
    "run-failed": "The site keeps showing the last saved numbers, and Update results runs again on its schedule. "
                  "This closes by itself after the next run that works.",
    "overdue": "The watchdog starts Update results by itself when county reads are late. "
               "This closes by itself once reads are on time.",
    "site-behind": "The watchdog republishes the page by itself. This closes by itself once the live page "
                   "matches the saved results.",
    "engine": "The Live count tab doesn't use the forecast engine and isn't affected. The check runs again "
              "after each saved update and on every watchdog run, and this closes by itself when it passes.",
    "preflight": "This check changes nothing by itself. The election-night workflows run on their own schedule, "
                 "and each problem below says what the automation does about it.",
}
NOTE_DOING = {"preflight": "Preflight passed. Election night runs on its own from here."}
DRILL_LINE = "DRILL: test alert, nothing is wrong, no action needed."
REPEAT_AFTER = 30 * 60
MAX_TEXT = 60000
MARK = "<!-- election-night-status kind={kind} hash={hash} -->"
MARK_RE = re.compile(r"<!-- election-night-status kind=([a-z-]+) hash=([0-9a-f]{16}) -->")
DONE_MARK = "<!-- election-night-status kind={kind} closed -->"
RUN_TIME_RE = re.compile(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,? \d{1,2}:\d{2}(?::\d{2})? ?[AP]M\b")
IN_A_ROW_RE = re.compile(r"(in a row\W{0,3})\d+", re.IGNORECASE)
HANDLE_RE = re.compile(r"@(?=[A-Za-z0-9])")
ZWSP_AT = "@" + chr(0x200B)  # an @ followed by a zero-width space: shown as @, mentions nobody
SAYS_SITE_RE = re.compile(r"sending readers to the county", re.IGNORECASE)  # the caller already says what readers see


def say(message):
    print(f"status.py: {message}", flush=True)


def quiet_mentions(text):
    """Text from a caller can't mention anyone: an @ outside `code` gets a zero-width space after it.
    The only mention is the one this script adds for the owner."""
    parts = text.split("`")
    for i in range(0, len(parts), 2):
        parts[i] = HANDLE_RE.sub(ZWSP_AT, parts[i])
    return "`".join(parts)


def content_hash(text):
    """Hash of what a post says, without what changes from run to run (see the top of this file)."""
    kept = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "/actions/runs/" in line or line.startswith("<!--"):
            continue
        kept.append(IN_A_ROW_RE.sub(r"\1#", RUN_TIME_RE.sub("<time>", line)))
    return hashlib.sha256("\n".join(kept).encode()).hexdigest()[:16]


def mention():
    who = os.environ.get("ALERT_MENTION")
    if who is None:
        who = os.environ.get("GITHUB_REPOSITORY", "").partition("/")[0]
    who = who.strip().lstrip("@")
    return f"@{who}" if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", who) else ""


def site_line():
    """What readers see, from this checkout's data/status.json, when it isn't ok."""
    s = common.local_status()
    reason = quiet_mentions(s["reason"].replace("`", "'"))
    if s["state"] == "broken":
        return ("**On the site:** readers are being sent to the county's official results page."
                + (f' The site says: "{reason}"' if reason else ""))
    if s["state"] == "confirming":
        return "**On the site:** " + (f'readers see: "{reason}"' if reason else "a note that a county correction is being confirmed.")
    return ""


def compose(kind, text, first, lead="", note=False):
    """A post: the FYI line, what readers see, the caller's text, the mention (new issues only),
    the run link and the hidden marker. note: worded as the kind's all-clear note."""
    if kind == "drill":
        head = f"**{DRILL_LINE}**"
    else:
        doing = NOTE_DOING.get(kind, DOING[kind]) if note else DOING[kind]
        head = "**No action needed.** " + (f"{lead} " if lead else "") + doing
    run = common.run_url()
    who = mention() if first else ""
    parts = [
        head,
        site_line() if kind != "drill" and not SAYS_SITE_RE.search(text) else "",
        quiet_mentions(text.strip()[:MAX_TEXT]),
        f"FYI {who}" if who else "",
        f"Run: {run}" if run and run not in text else "",
        MARK.format(kind=kind, hash=content_hash(text)),
    ]
    return "\n\n".join(p for p in parts if p)


def _epoch(stamp):
    parsed = common.parse_iso(stamp)
    return parsed.timestamp() if parsed else None


def open_issues(gh, kind):
    """The kind's open issues, oldest first: found by its labels, or by the marker on an issue this
    workflow's bot opened (if labels couldn't be set). Issues opened by anyone else never count."""
    found = []
    newest_first = gh.get("/issues", state="open", per_page=100, sort="created", direction="desc") or []
    for issue in reversed(newest_first):
        if "pull_request" in issue:
            continue
        labels = {lab.get("name") for lab in issue.get("labels") or [] if isinstance(lab, dict)}
        mark = MARK_RE.search(issue.get("body") or "")
        by_bot = (issue.get("user") or {}).get("login") == BOT
        if {LABEL, f"kind:{kind}"} <= labels or (by_bot and mark and mark.group(1) == kind):
            found.append(issue)
    return found


def posts(gh, issue):
    """[(hash, epoch seconds)] of the marked posts on an issue: the issue itself and its newest comments."""
    found = []
    mark = MARK_RE.search(issue.get("body") or "")
    if mark:
        found.append((mark.group(2), _epoch(issue.get("created_at"))))
    count = issue.get("comments") or 0
    if count:
        page = (count - 1) // 100 + 1
        for comment in gh.get(f"/issues/{issue['number']}/comments", per_page=100, page=page) or []:
            if (comment.get("user") or {}).get("login") not in (BOT, None):
                continue
            mark = MARK_RE.search(comment.get("body") or "")
            if mark:
                found.append((mark.group(2), _epoch(comment.get("created_at"))))
    return [(h, t) for h, t in found if t]


def ensure_label(gh, name, color, description):
    try:
        gh.get(f"/labels/{urllib.parse.quote(name, safe='')}")
        return
    except common.ApiError as e:
        if e.status != 404:
            raise
    try:
        gh.post("/labels", {"name": name, "color": color, "description": description})
    except common.ApiError as e:
        if e.status != 422:  # 422: another run created it a moment ago
            raise


def create_issue(gh, kind, title, body):
    labels = [LABEL, f"kind:{kind}"]
    try:
        ensure_label(gh, LABEL, "c5def5", "Election-night FYI posted by the workflows")
        ensure_label(gh, f"kind:{kind}", "ededed", "Which election-night FYI")
    except common.ApiError as e:
        say(f"warning: couldn't set up labels ({e}); posting without them")
        labels = []
    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    return gh.post("/issues", payload)["number"]


def close_issue(gh, kind, number, line, reason="completed"):
    run = common.run_url()
    body = "\n\n".join(p for p in (line, f"Run: {run}" if run else "", DONE_MARK.format(kind=kind)) if p)
    gh.post(f"/issues/{number}/comments", {"body": body})
    gh.patch(f"/issues/{number}", {"state": "closed", "state_reason": reason})


def do_open(gh, kind, text):
    issues = open_issues(gh, kind)
    digest = content_hash(text)
    if not issues:
        number = create_issue(gh, kind, TITLES[kind], compose(kind, text, first=True))
        say(f"opened issue #{number} ({kind})")
        return True
    keep = issues[0]
    for extra in issues[1:]:
        close_issue(gh, kind, extra["number"], f"Duplicate of #{keep['number']}.", reason="not_planned")
        say(f"closed duplicate issue #{extra['number']} ({kind})")
    now = time.time()
    seen = posts(gh, keep)
    last_at = max(t for _, t in seen) if seen else None
    said = {h for h, _ in seen}
    # Something the issue already said is posted again only once 30 minutes have passed since its
    # last post, whatever that post said: a problem that alternates between two descriptions (one
    # per county feed) isn't repeated on every run.
    if digest in said and last_at is not None and now - last_at < REPEAT_AFTER:
        say(f"issue #{keep['number']} ({kind}) already says this; last post {(now - last_at) / 60:.0f} min ago, no new comment")
        return True
    changed = digest not in said
    lead = "New details." if changed else "Still the same after 30 minutes."
    gh.post(f"/issues/{keep['number']}/comments", {"body": compose(kind, text, first=False, lead=lead)})
    say(f"commented on issue #{keep['number']} ({kind}): " + ("content changed" if changed else "30 minutes since the last post"))
    return True


def do_resolve(gh, kind, line):
    line = quiet_mentions(" ".join(line.split())[:500]) or "Back to normal."
    issues = open_issues(gh, kind)
    if not issues:
        say(f"no open {kind} issue; nothing to close")
        return True
    for issue in issues:
        close_issue(gh, kind, issue["number"], f"**Resolved:** {line}")
        say(f"resolved issue #{issue['number']} ({kind})")
    return True


def do_note(gh, kind, text, problem=False):
    """Post an issue and close it at once. problem: it reports a problem (the kind's title and
    opening line), not an all-clear (NOTE_TITLES and NOTE_DOING)."""
    title = TITLES[kind] if problem else NOTE_TITLES.get(kind, TITLES[kind])
    number = create_issue(gh, kind, title, compose(kind, text, first=True, note=not problem))
    try:
        gh.patch(f"/issues/{number}", {"state": "closed", "state_reason": "completed"})
    except common.ApiError as e:
        say(f"posted issue #{number} ({kind}) but couldn't close it ({e})")
        return True
    say(f"posted issue #{number} ({kind}) and closed it")
    return True


def post(command, kind, text, problem=False):
    """One open, resolve or note (problem: see do_note). Returns True if it went through. Never raises."""
    undone = f"{kind} issue not closed" if command == "resolve" else f"{kind} issue not posted"
    try:
        if kind not in KINDS:
            say(f"warning: unknown kind {kind!r}; nothing posted")
            return False
        action = {"open": do_open, "resolve": do_resolve, "note": do_note}.get(command)
        if action is None:
            say(f"warning: unknown command {command!r}; nothing posted")
            return False
        quick = os.environ.get("STATUS_QUICK") == "1"
        gh = common.GitHub(timeout=8, get_tries=1) if quick else common.GitHub()
        if not gh.ready():
            say(f"warning: GITHUB_TOKEN or GITHUB_REPOSITORY not set; {undone}")
            return False
        return do_note(gh, kind, text, problem) if command == "note" else action(gh, kind, text)
    except common.ApiError as e:
        say(f"warning: GitHub API error ({e}); {undone}")
    except Exception as e:  # a status post must never break a run
        say(f"warning: {type(e).__name__}; {undone}")
    return False


def run(argv):
    if len(argv) != 3 or argv[0] not in ("open", "resolve", "note"):
        say('warning: usage: status.py open|note <kind> <markdown-file>  or  status.py resolve <kind> "<one line>"')
        return False
    command, kind, arg = argv
    if command == "resolve":
        return post(command, kind, arg)
    try:
        text = Path(arg).read_text(encoding="utf-8", errors="replace")
    except OSError:
        say(f"warning: {arg} not found; posting a short message instead")
        text = ""
    if not text.strip():
        text = "The run log has the details."
    return post(command, kind, text)


def main(argv=None):
    try:
        run(sys.argv[1:] if argv is None else argv)
    except Exception as e:
        say(f"warning: {type(e).__name__}; nothing posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
