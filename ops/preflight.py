"""Preflight for election night: one county read and a check of everything else, then an FYI
status issue. Also the alert drill.

  SOURCE=html|xml python ops/preflight.py     the check
  DRILL=true python ops/preflight.py          the drill: no county read

The check (preflight.yml: Tue Nov 3 at 5:05 PM with the county results page and 5:15 PM with the
county's backup data feed, or by hand):
1. One county read of SOURCE through a gap guard: refused when the county was read less than 5
   minutes ago. .last-fetch is written before the read and the workflow saves it to the Actions
   cache, so the read counts for the next Update results run's guard. .county-state.json is left
   as it is (only scraper/update.py writes it).
2. The live site serves the same data/results.json and data/status.json as main.
3. ops/engine_check.js passes on main's results.json.
4. Update results, Keep-alive and Watchdog are active.
A pass posts a closed FYI issue with the county numbers read (status.py note) and closes an open
preflight issue. A failure opens the preflight issue: what failed and what the automation does
about it; the run then fails too.

The drill (Sun Nov 1 at 2:13 PM, or by hand) posts a closed test issue: "DRILL: test alert,
nothing is wrong, no action needed." The run fails only if it couldn't be posted.
Prints status lines only.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import common
import status

sys.path.insert(0, str(common.ROOT / "scraper"))
import county  # noqa: E402  (scraper/county.py: the same reader Update results uses)

MIN_GAP = 300
WORKFLOWS = (("update.yml", "Update results"), ("keepalive.yml", "Keep-alive"), ("watchdog.yml", "Watchdog"))
# What still runs when a workflow is turned off.
WITHOUT = {
    "update.yml": "Without it no county reads happen; the watchdog's FYI issues will say so.",
    "keepalive.yml": "Scheduled Update results runs and the watchdog still run without it.",
    "watchdog.yml": "Update results and the keep-alive still run without it.",
}
SOURCES = {"html": "the county results page", "xml": "the county's backup data feed (XML)"}
# What the automation does when a county source can't be read.
COUNTY_FALLBACK = {
    "html": "Update results switches to the county's backup data feed when the results page can't be read, "
            "and after 2 failed reads in a row the site sends readers to the county's official results page.",
    "xml": "Update results reads the county results page first; the backup feed is only used when the page "
           "can't be read. After 2 failed reads in a row the site sends readers to the county's official results page.",
}


def now():
    return time.time()


def say(message):
    print(message, flush=True)


def drill(t):
    ok = status.post("note", "drill", f"Posted by the alert drill at {common.clock(t)} Pacific ({common.day(t)}).")
    say("Drill issue posted and closed" if ok else "Drill issue could NOT be posted")
    return 0 if ok else 1


def read_county(source, t):
    """One read through the gap guard: (reading, None, None), (None, None, why skipped) or
    (None, problem, None)."""
    last = common.last_fetch(t) or 0.0
    gap = t - last
    if gap < MIN_GAP:
        return None, None, (f"not read: the county was read {max(gap, 0):.0f} s ago, and preflight reads it at most "
                            "once every 5 minutes")
    if source == "xml" and not (hasattr(county, "fetch_xml") and hasattr(county, "parse_xml")):
        return None, "scraper/county.py has no reader for the backup data feed (fetch_xml and parse_xml)", None
    common.LAST_FETCH.write_text(f"{t:.0f}\n")
    what = SOURCES[source]
    try:
        if source == "xml":
            text, headers = county.fetch_xml()
        else:
            text, headers = county.fetch(), None
    except Exception as e:
        code, reason = getattr(e, "code", None), getattr(e, "reason", None)
        why = (f"HTTP {code}" if isinstance(code, int) else
               type(reason).__name__ if isinstance(reason, BaseException) else type(e).__name__)
        return None, f"{what} couldn't be fetched ({why})", None
    try:
        reading = county.parse_xml(text, headers) if source == "xml" else county.parse(text)
    except county.ParseError as e:
        return None, f"{what} was fetched, but Measure Y couldn't be read from it ({e})", None
    except Exception as e:
        return None, f"{what} was fetched, but the reader stopped on an unexpected {type(e).__name__}", None
    return reading, None, None


def county_line(source, r):
    parts = [f"YES {r['yes']:,}", f"NO {r['no']:,}"]
    ballots = r.get("county_ballots_counted")
    if isinstance(ballots, int):
        parts.append(f"Ballots Counted {ballots:,}")
    stamp = common.parse_iso(r.get("county_updated"))
    if stamp:
        parts.append(f"county update {common.clock(stamp.timestamp(), seconds=True)} ({common.day(stamp.timestamp())})")
    return f"Read {SOURCES[source]}: " + ", ".join(parts)


def check(gh, source, t):
    """Returns (problems, passed, notes): problems are (what failed, what the automation does)."""
    problems, passed, notes = [], [], []

    r, problem, skipped = read_county(source, t)
    if problem:
        problems.append((problem[0].upper() + problem[1:] + ".", COUNTY_FALLBACK[source]))
        say(f"County: {problem}")
    elif skipped:
        notes.append(f"County: {skipped}.")
        say(notes[-1])
    else:
        passed.append(county_line(source, r))
        say(passed[-1])

    try:
        compared = common.compare_live(gh)
    except common.ApiError as e:
        compared = None
        problems.append((f"Couldn't read the saved data files on main ({e.status or 'network error'}).",
                         "Nothing else depends on this check."))
    if compared is not None:
        verdict = common.overall(compared)
        if verdict == "same":
            passed.append("Live page matches the saved results and site status")
        elif verdict == "unknown":
            problems.append(("Couldn't read the live page.",
                             "Update results republishes whenever the live page is behind, from its first run at 7:57 PM."))
        else:
            behind = ", ".join(p for p, v in compared.items() if v[0] == "behind")
            problems.append((f"The live page is behind the saved data ({behind}).",
                             "The first Update results run at 7:57 PM republishes it, and the watchdog checks every "
                             "10 minutes from 7:55 PM."))
        raw = compared["data/results.json"][1]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "results.json"
            path.write_bytes(raw)
            ok, line = common.engine_check(path)
        if ok:
            passed.append(f"Forecast engine: {line}")
        elif ok is False:
            problems.append((f"Forecast engine: {line}.",
                             "The Live count tab doesn't use the engine and isn't affected. Update results and the "
                             "watchdog check the engine again after each update."))
        else:
            problems.append((f"The forecast engine check didn't run ({line}).", "The watchdog runs it again from 7:55 PM."))

    all_on = True
    for file, name in WORKFLOWS:
        try:
            state = common.workflow_state(gh, file)
        except common.ApiError as e:
            all_on = False
            problems.append((f"Couldn't check whether {name} is on ({e.status or 'network error'}).",
                             "Nothing else depends on this check."))
            continue
        if state != "active":
            all_on = False
            problems.append((f"{name} is turned off ({str(state).replace('_', ' ')}).",
                             f"Nothing in the automation can turn a workflow back on. {WITHOUT[file]}"))
    if all_on:
        passed.append("Update results, Keep-alive and Watchdog are on")
    if os.environ.get("KEEPALIVE_SETTING", "").strip().lower() == "off":
        notes.append("Keep-alive is switched off by the repository variable KEEPALIVE=off; scheduled Update "
                     "results runs and the watchdog still run.")
    return problems, passed, notes


def report(t, source, problems, passed, notes):
    link = common.run_url() or "(no run link)"
    run_line = f"Preflight run {link} at {common.clock(t)} Pacific ({common.day(t)}), reading {SOURCES[source]}."
    if problems:
        lines = [f"Preflight found {len(problems)} problem{'s' if len(problems) > 1 else ''}:", ""]
        for i, (what, doing) in enumerate(problems, 1):
            lines += [f"{i}. {what}", f"   What the automation does: {doing}"]
        if passed:
            lines += ["", "Passed:"] + [f"- {p}" for p in passed]
        if notes:
            lines += ["", "Notes:"] + [f"- {n}" for n in notes]
        lines += ["", run_line]
        for what, _ in problems:
            say(f"Problem: {what}")
        status.post("open", "preflight", "\n".join(lines))
        return 1
    lines = ["Checked:"] + [f"- {p}" for p in passed]
    if notes:
        lines += ["", "Notes:"] + [f"- {n}" for n in notes]
    lines += ["", run_line]
    say("Preflight passed")
    status.post("resolve", "preflight", f"Preflight passed at {common.clock(t)} Pacific.")
    if not status.post("note", "preflight", "\n".join(lines)):
        say("Preflight passed, but its FYI issue couldn't be posted")
        return 1
    return 0


def main():
    t = now()
    gh = common.GitHub()
    if not gh.ready():
        say("GITHUB_TOKEN or GITHUB_REPOSITORY not set; nothing checked")
        return 1
    if os.environ.get("DRILL", "").strip().lower() == "true":
        say(f"Drill at {common.clock(t)} Pacific: no county read")
        return drill(t)
    source = os.environ.get("SOURCE", "").strip().lower() or "html"
    if source not in SOURCES:
        say(f"Unknown source {source!r}; use html or xml")
        return 1
    say(f"Preflight at {common.clock(t)} Pacific ({common.day(t)}): one read of {SOURCES[source]}")
    problems, passed, notes = check(gh, source, t)
    return report(t, source, problems, passed, notes)


if __name__ == "__main__":
    sys.exit(main())
