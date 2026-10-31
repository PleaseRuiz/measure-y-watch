"""Watchdog for the counting windows: county reads keep coming, the live page matches main and the
forecast engine works.

watchdog.yml runs this every 10 minutes, between Update results' runs: election night (Tue Nov 3
7:55 PM to Wed Nov 4 5:00 AM Pacific) and Nov 5-16, 8:00 AM to 6:00 PM. It never reads the county
site. It uses the last county fetch time from the Actions cache (.last-fetch and
.county-state.json, restored by the workflow and never saved), the Update results runs, and the
live site's data/results.json and data/status.json against main's:

1. Overdue: no county fetch for more than 12 minutes (25 in the daytime window) and no Update
   results run queued or in progress: starts Update results with keepalive=true. Past 25 minutes
   (45 in the daytime) it opens the overdue status issue, and closes it once fetches are on time.
2. Site behind: the live data files differ from main's for more than 10 minutes (since main's
   newest change to them) and no Update results run queued or in progress: starts Update results
   with keepalive=false, which always republishes. Past 25 minutes it opens the site-behind status
   issue, and closes it once they match.
3. Engine: ops/engine_check.js on main's results.json opens or closes the engine status issue.

It starts at most one run per check. Outside the windows (a hand-started check) it reports, and
may close issues, but starts nothing and opens nothing. Prints status lines only.
"""

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import common
import engine_alert
import status

P = common.PACIFIC
NIGHT = (datetime(2026, 11, 3, 19, 55, tzinfo=P), datetime(2026, 11, 4, 5, 0, tzinfo=P))
DAYS = [(datetime(2026, 11, d, 8, 0, tzinfo=P), datetime(2026, 11, d, 18, 0, tzinfo=P)) for d in range(5, 17)]
# Per window: start Update results after this long without a county fetch, open the issue after
# this long, and how many minutes apart the scheduled runs are.
LIMITS = {"night": (12 * 60, 25 * 60, 5), "day": (25 * 60, 45 * 60, 10)}
SITE_GRACE = 10 * 60   # a deploy normally lands a couple of minutes after the commit
SITE_ALERT = 25 * 60
SCOPE = {"night": "election night", "day": "daytime counting window",
         None: "outside the counting windows (report only: nothing is started or opened)"}


def now():
    return time.time()


def say(message):
    print(message, flush=True)


def window(t):
    """('night' or 'day', the window's start in epoch seconds), or (None, None) outside them."""
    if NIGHT[0].timestamp() <= t < NIGHT[1].timestamp():
        return "night", NIGHT[0].timestamp()
    for start, end in DAYS:
        if start.timestamp() <= t < end.timestamp():
            return "day", start.timestamp()
    return None, None


class Watch:
    def __init__(self, gh, t):
        self.gh, self.t = gh, t
        self.kind, self.opened = window(t)
        self.started = None      # 'keep-alive' or 'republish' once this check has started a run
        self.main_results = None
        try:
            self.runs, self.runs_ok = common.update_runs(gh), True
        except common.ApiError as e:
            say(f"Couldn't list Update results runs ({e}); won't start one")
            self.runs, self.runs_ok = [], False
        self.busy = common.busy_runs(self.runs)
        if self.runs:
            say(f"Newest Update results run: {common.describe_run(self.runs[0])}")

    def run_line(self, did):
        """The part of an issue that changes from check to check. Its run link keeps it out of the hash."""
        newest = f" Newest Update results run: {common.describe_run(self.runs[0])}." if self.runs else ""
        return f"Watchdog run {common.run_url() or '(no run link)'} at {common.clock(self.t)}: {did}.{newest}"

    def why_not_start(self):
        if self.kind is None:
            return "outside the counting windows a check never starts runs"
        if self.started:
            return "this check already started one"
        if not self.runs_ok:
            return "couldn't see whether one is already queued or running"
        if self.busy:
            return f"an Update results run is already {self.busy[0]['status'].replace('_', ' ')}"
        return None

    def update_off(self):
        """'disabled manually' (say) if Update results is turned off, else ''. Unknown counts as on."""
        try:
            state = common.workflow_state(self.gh, "update.yml")
        except common.ApiError:
            return ""
        return "" if state in (None, "active") else str(state).replace("_", " ")

    def start(self, keepalive):
        try:
            common.dispatch(self.gh, "update.yml", {"keepalive": "true" if keepalive else "false"})
        except common.ApiError as e:
            say(f"Couldn't start Update results ({e})")
            return f"tried to start Update results, but GitHub refused ({e.status or 'network error'})"
        self.started = "keep-alive" if keepalive else "republish"
        say(f"Started Update results ({self.started})")
        return "started Update results" + (" (keep-alive)" if keepalive else " to republish the page")

    def overdue(self):
        """1. County fetches."""
        last = common.last_fetch(self.t)
        state = common.county_state()
        if last is None and state["last_ok"]:
            last = float(state["last_ok"])
        feed = "the county's backup data feed" if state["feed"] == "xml" else "the county results page"
        say(f"Last county fetch: {common.when(last) if last else 'none recorded'}; next read from {feed}; "
            f"failed reads in a row: {state['failures']}")
        if self.kind is None:
            say("Outside the counting windows: county fetch times aren't judged")
            status.post("resolve", "overdue", "The counting window is closed; no county reads are due until the next one.")
            return
        start_after, alert_after, every = LIMITS[self.kind]
        fresh_in_window = last is not None and last >= self.opened
        age = self.t - (last if fresh_in_window else self.opened)
        if age <= start_after:
            say(f"County fetches on time ({age / 60:.0f} min since the last one or the window's start)")
            status.post("resolve", "overdue", f"County reads are on time again (last read {common.when(last)})."
                        if fresh_in_window else "County reads are on schedule.")
            return
        off = self.update_off()
        reason = self.why_not_start() or (f"Update results is turned off ({off})" if off else None)
        did = self.start(keepalive=True) if reason is None else f"didn't start Update results: {reason}"
        say(f"County fetch overdue ({age / 60:.0f} min)" + (f"; {did}" if reason else ""))
        if age > alert_after:
            since = (f"since {common.when(last)}" if fresh_in_window
                     else f"since the counting window opened at {common.clock(self.opened)} Pacific")
            lines = [f"No county read {since}. In this window Update results reads the county every {every} minutes.",
                     "",
                     f"- Next read from: {feed}",
                     f"- Failed county reads in a row: {state['failures']}"]
            if off:
                lines.append(f"- Update results is turned off ({off}), so no runs can start until it's turned back on.")
            status.post("open", "overdue", "\n".join(lines + ["", self.run_line(did)]))

    def site(self):
        """2. The live page against main."""
        try:
            compared = common.compare_live(self.gh)
        except common.ApiError as e:
            say(f"Couldn't read main's data files ({e}); live page not checked")
            return
        self.main_results = compared["data/results.json"][1]
        verdict = common.overall(compared)
        for path, (v, _, _, problem) in compared.items():
            say(f"Live {path}: {v}" + (f" ({problem})" if problem and v != "same" else ""))
        if verdict == "same":
            say("Live page matches main")
            status.post("resolve", "site-behind", "The live page matches the saved results again.")
            return
        if verdict == "unknown":
            say("Couldn't read the live page; not judged this time")
            return
        try:
            changed = common.data_changed_at(self.gh)
        except common.ApiError as e:
            say(f"Live page differs from main; couldn't tell when main changed ({e})")
            return
        if changed is None:
            say("Live page differs from main; when main changed is unknown")
            return
        age = self.t - changed
        say(f"Live page differs from main; main's data changed {age / 60:.0f} min ago")
        if age <= SITE_GRACE:
            say("Giving the deploy time")
            return
        reason = self.why_not_start()
        if reason is None:
            did = self.start(keepalive=False)
        elif self.started == "keep-alive":
            did = "the Update results run it just started also republishes when the page is behind"
            say("The keep-alive run just started also republishes when the page is behind")
        else:
            did = f"didn't start a republish: {reason}"
            say(f"Live page behind; {did}")
        if age > SITE_ALERT and self.kind is not None:
            res, sta = compared["data/results.json"], compared["data/status.json"]
            live_res = common.summary(res[2]) if res[2] is not None else f"couldn't be read ({res[3]})"
            live_sta = (common.status_summary(sta[2]) if sta[2] is not None
                        else "none (counts as ok)" if sta[3] == "HTTP 404" else f"couldn't be read ({sta[3]})")
            status.post("open", "site-behind", "\n".join([
                f"The live page still serves older data than the saved results on main, which changed at {common.when(changed)}.",
                "",
                f"- Saved: {common.summary(res[1])}; site status {common.status_summary(sta[1])}",
                f"- Live page: {live_res}; site status {live_sta}",
                "",
                self.run_line(did),
            ]))

    def engine(self):
        """3. The forecast engine on main's results."""
        raw = self.main_results
        if raw is None:
            try:
                raw = common.main_file(self.gh, "data/results.json")
            except common.ApiError as e:
                say(f"Couldn't read main's results.json ({e}); engine not checked")
                return
        if raw is None:
            say("main has no data/results.json; engine not checked")
            return
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "results.json"
            path.write_bytes(raw)
            if self.kind is None:  # report only: close a stale issue, never open one
                ok, line = common.engine_check(path)
                say(line)
                if ok is True:
                    status.post("resolve", "engine", "The forecast engine check passes again.")
                return
            engine_alert.check_and_post(path)


def main():
    gh = common.GitHub()
    if not gh.ready():
        say("GITHUB_TOKEN or GITHUB_REPOSITORY not set; nothing checked")
        return 1
    t = now()
    say(f"Watchdog at {common.when(t)}: {SCOPE[window(t)[0]]}")
    watch = Watch(gh, t)
    broken = []
    for check in (watch.overdue, watch.site, watch.engine):
        try:
            check()
        except Exception as e:  # one broken check mustn't hide the others
            say(f"The {check.__name__} check crashed ({type(e).__name__})")
            broken.append(check.__name__)
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
