"""Keep-alive: starts every scheduled county read, check and watchdog run itself, so the counting
schedule doesn't depend on GitHub's timer.

GitHub's timer starts scheduled runs in this repository hours late, if at all (it started the
Sun Nov 1, 2:13 PM drill at 5:29 PM). So this reads the schedule lines (cron, UTC) of update.yml,
watchdog.yml and preflight.yml when it starts and, at each of those times, starts that workflow
with workflow_dispatch, as GitHub's timer should:

- Update results, with keepalive=true. Not when an Update results run (other than a deploy after a
  push) is queued or in progress, or one was already started for that time, for example by GitHub's
  late timer or by the watchdog. A run counts for a time when it was created at
  most the gap to the time before less 240 s back (1 minute at night, 5 in the daytime), so a run
  whose county read update.py's gap guard skipped never stands in for one. Its own starts stay at
  least 290 s apart, and update.py's gap guard keeps county reads at least 4 minutes apart.
  A keepalive=true run skips its county read when the Actions cache has no saved county fetch time,
  so when main's cache has none, this starts the run with keepalive unticked (at most once every 20
  minutes in one keep-alive run); that run reads the county and saves one.
- Watchdog, unless one is queued or in progress or was already started for that time.
- Preflight at Tue Nov 3, 5:05 PM (the county results page) and 5:15 PM (the backup XML feed), each
  unless a Preflight run was started with workflow_dispatch at or after its time (a run GitHub's
  timer starts does nothing), and at least 330 s after the Preflight before it (preflight.py reads
  the county at most every 5 minutes). Never the Sun Nov 1 drill.

A time missed by up to an hour (a late start, the hand-over between runs, API errors) is caught up
once; when two times of one workflow are owed at once, only the later one is started. A failed
start is tried again each minute.

A job can run for at most 6 hours, so after 5.5 hours this starts a fresh Keep-alive run (the
concurrency group holds it until this one ends), checks that it's queued, then exits. If it can't,
it goes on starting runs and tries again each minute for up to 8 more minutes, then ends red
(or, that close to the end, carries on to it). It stops for good at Tue Nov 17, 7:10 AM Pacific,
just after the last hourly read (7:07 AM). Claude's daily check starts the daily reads after that.

It starts when a push to main changes it or a schedule file, on its own schedule lines (when
GitHub's timer gets to them), or by hand (Actions tab, or Claude's checks). A change to the schedule lines or to
this code reaches a running keep-alive at its next hand-over; to switch at once, cancel the running
Keep-alive run (the push queued a fresh one). Off switch: the repository variable KEEPALIVE=off
skips the job. Prints status lines only.
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import common

UTC = timezone.utc
WORKFLOWS = common.ROOT / ".github" / "workflows"
END = datetime(2026, 11, 17, 7, 10, tzinfo=common.PACIFIC).timestamp()
MAX_RUN = 330 * 60          # start a fresh run after 5.5 hours; keepalive.yml's timeout is 355 minutes
HANDOFF_UNTIL = 338 * 60    # stop trying to hand over after this long, well inside the timeout
MIN_HANDOFF = 10 * 60       # when 5.5 hours ends this close to END, carry on to END instead (at most 340 minutes)
REASK = 3 * 60              # while the fresh run isn't showing as queued, start another this often
SHOW_TRIES = 4              # checks that the fresh run is queued, SHOW_WAIT seconds apart
SHOW_WAIT = 5
MIN_LIFE = 10 * 60          # after an unexpected error, hand over only if this run lasted this long
CATCH_UP = 60 * 60          # a time missed by up to this long still gets its run, once
LEAD_MIN, LEAD_MAX = 60, 5 * 60   # bounds on how long before a time a run may be created and count for it
UPDATE_SPACING = 290        # Update results starts at least this far apart
MIN_READ_GAP = 240          # update.py's MIN_FETCH_GAP: a run this soon after a read skips its own
PREFLIGHT_SPACING = 330     # a Preflight start at least this long after the one before
RETRY = 60                  # seconds before trying a failed start or hand-over again
NAP = 60                    # the loop wakes at least this often
HEARTBEAT = 60 * 60         # a "still on" line this often
UNTICKED_EVERY = 20 * 60    # at most one keepalive-unticked Update results start this often (per run)

DRILL = "13 22 1 11 *"
PREFLIGHT = {"5 1 4 11 *": {"source": "html"}, "15 1 4 11 *": {"source": "xml"}}
NAMES = {"update.yml": "Update results", "watchdog.yml": "Watchdog", "preflight.yml": "Preflight"}
SOURCES = {"html": "the county results page", "xml": "the county's backup data feed"}
CRON_LINE = re.compile(r"""^\s*-\s*cron:\s*(["']?)([^"'#]+?)\1\s*(?:#.*)?$""")
READ_EVENTS = {"schedule", "workflow_dispatch"}   # runs that can read the county (not deploys after a push)
RESTART = "If none is waiting or running in the Actions tab, start one there (Actions, Keep-alive, Run workflow)."
STOPPED = "Keep-alive stops here: it couldn't start a fresh Keep-alive run. " + RESTART
NEVER_SHOWED = "Keep-alive stops here: a fresh Keep-alive run was started but never showed as queued. " + RESTART


def now():
    return time.time()


def sleep(seconds):
    time.sleep(seconds)


def say(message):
    print(message, flush=True)


def stamp(ts):
    """'Tue Nov 3, 8:02 PM' (Pacific)."""
    return f"{common.day(ts)}, {common.clock(ts)}"


# ---------- schedule lines ----------

def field(spec, lo, hi):
    """The numbers a cron field names: *, n, a-b, each with an optional /step, in comma lists."""
    out = set()
    for part in spec.split(","):
        rng, slash, step = part.partition("/")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = int(rng)
            b = hi if slash else a
        n = int(step) if slash else 1
        if not (lo <= a <= b <= hi) or n < 1:
            raise ValueError(f"out of range: {part!r}")
        out.update(range(a, b + 1, n))
    return out


class Cron:
    """One schedule line as GitHub reads it (UTC). The day-of-week field must be *."""

    def __init__(self, text):
        self.text = text
        parts = text.split()
        if len(parts) != 5:
            raise ValueError("not 5 fields")
        if parts[4] != "*":
            raise ValueError("a day-of-week field isn't supported")
        self.minutes = sorted(field(parts[0], 0, 59))
        self.hours = sorted(field(parts[1], 0, 23))
        self.days = field(parts[2], 1, 31)
        self.months = field(parts[3], 1, 12)

    def _day_ok(self, d):
        return d.month in self.months and d.day in self.days

    def next_after(self, t, until):
        """The first time after epoch t, and not after until, that this fires (epoch seconds), or None."""
        start = datetime.fromtimestamp(t, UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
        stop = datetime.fromtimestamp(until, UTC)
        d = start.date()
        while d <= stop.date():
            if self._day_ok(d):
                for h in self.hours:
                    if d == start.date() and h < start.hour:
                        continue
                    for m in self.minutes:
                        at = datetime(d.year, d.month, d.day, h, m, tzinfo=UTC)
                        if at >= start:
                            return at.timestamp() if at <= stop else None
            d += timedelta(days=1)
        return None

    def last_at_or_before(self, t, since):
        """The latest time at or before epoch t, and not before since, that this fired (epoch seconds), or None."""
        end = datetime.fromtimestamp(t, UTC).replace(second=0, microsecond=0)
        first = datetime.fromtimestamp(since, UTC)
        d = end.date()
        while d >= first.date():
            if self._day_ok(d):
                for h in reversed(self.hours):
                    if d == end.date() and h > end.hour:
                        continue
                    for m in reversed(self.minutes):
                        at = datetime(d.year, d.month, d.day, h, m, tzinfo=UTC)
                        if at <= end:
                            return at.timestamp() if at >= first else None
            d -= timedelta(days=1)
        return None


def schedule_lines(workflow):
    """The cron lines of one workflow file, in order, and the lines that name a cron but can't be read."""
    found, odd = [], []
    for line in (WORKFLOWS / workflow).read_text().splitlines():
        if "cron:" not in line or line.lstrip().startswith("#"):
            continue
        m = CRON_LINE.match(line)
        if m:
            found.append(m.group(2).strip())
        else:
            odd.append(line.strip())
    return found, odd


class Job:
    """One kind of run this starts: a workflow plus its inputs (Preflight has two, by source)."""

    def __init__(self, workflow, inputs=None):
        self.workflow, self.inputs = workflow, inputs
        self.crons = []
        self.due = None        # the scheduled time this still owes a start for
        self.seen = None       # the latest scheduled time seen (so each time is owed at most once)
        self.retry_at = 0.0    # after a failed or held start: not before this

    @property
    def name(self):
        source = (self.inputs or {}).get("source")
        return NAMES[self.workflow] + (f" ({SOURCES[source]})" if source in SOURCES else "")

    def next_time(self, t):
        """The next scheduled time after t, up to END, or None."""
        times = [x for x in (c.next_after(t, END) for c in self.crons) if x is not None]
        return min(times) if times else None

    def last_time(self, t):
        """The latest scheduled time within CATCH_UP before t (or at t), or None."""
        times = [x for x in (c.last_at_or_before(t, t - CATCH_UP) for c in self.crons) if x is not None]
        return max(times) if times else None

    def lead(self, due):
        """How long before `due` a run may have been created and still count for it."""
        if self.workflow == "preflight.yml":
            return 5           # the runs list can't tell the two Preflight sources apart; 5 s for clock skew
        before = [x for x in (c.last_at_or_before(due - 60, due - 2 * LEAD_MAX) for c in self.crons) if x is not None]
        gap = due - max(before) if before else 2 * LEAD_MAX
        return max(LEAD_MIN, min(gap - MIN_READ_GAP, LEAD_MAX))   # a run that counts is one update.py lets read


def load_jobs():
    """The jobs to start, from the workflow files' schedule lines, and notes on lines left out."""
    jobs, notes = {}, []
    for workflow in ("update.yml", "watchdog.yml", "preflight.yml"):
        try:
            lines, odd = schedule_lines(workflow)
        except OSError as e:
            notes.append(f"couldn't read {workflow} ({type(e).__name__})")
            continue
        notes.extend(f"left out {workflow} line {line!r}: can't read it" for line in odd)
        for text in lines:
            if workflow == "preflight.yml":
                if text == DRILL:
                    continue
                inputs = PREFLIGHT.get(text)
                if inputs is None:
                    notes.append(f"left out {workflow} line {text!r}: not a known preflight time")
                    continue
            else:
                inputs = {"keepalive": "true"} if workflow == "update.yml" else None
            try:
                cron = Cron(text)
            except ValueError as e:
                notes.append(f"left out {workflow} line {text!r}: {e}")
                continue
            key = (workflow, tuple(sorted((inputs or {}).items())))
            jobs.setdefault(key, Job(workflow, inputs)).crons.append(cron)
    return list(jobs.values()), notes


# ---------- GitHub ----------

def runs_of(gh, workflow, per_page=10):
    """The newest runs of a workflow, newest first."""
    return (gh.get(f"/actions/workflows/{workflow}/runs", per_page=per_page) or {}).get("workflow_runs") or []


def created(run):
    dt = common.parse_iso(run.get("created_at"))
    return dt.timestamp() if dt else None


def saved_fetch(gh):
    """True when main's Actions cache holds a saved county fetch time, False when it holds none, None when unknown."""
    try:
        got = gh.get("/actions/caches", key="last-fetch-", ref="refs/heads/main", per_page=1) or {}
    except common.ApiError:
        return None
    count = got.get("total_count") if isinstance(got, dict) else None
    return count > 0 if isinstance(count, int) and not isinstance(count, bool) else None


class Pacer:
    def __init__(self, gh, jobs):
        self.gh, self.jobs = gh, jobs
        self.last_unticked = None
        self.last_update = None   # when this run last started Update results

    def start(self, job, t):
        """Start job for job.due unless it isn't needed. Returns None when that time is dealt with,
        or the time to try again."""
        at, when = common.clock(t), common.clock(job.due)
        try:
            # A Preflight run GitHub's timer started does nothing (preflight.py), so it never stands in for a check.
            events = {"workflow_dispatch"} if job.workflow == "preflight.yml" else READ_EVENTS
            runs = [r for r in runs_of(self.gh, job.workflow) if r.get("event") in events]
        except common.ApiError as e:
            say(f"{at}: couldn't list {job.name} runs ({e}); trying again in a minute")
            return t + RETRY
        lead = job.lead(job.due)
        for r in runs:
            c = created(r)
            if c is not None and c >= job.due - lead:
                say(f"{at}: {job.name} for {when} already started ({common.describe_run(r)}); not starting another")
                return None
        if job.workflow != "preflight.yml":
            busy = common.busy_runs(runs)
            if busy:
                say(f"{at}: {job.name} is {busy[0]['status'].replace('_', ' ')}; not starting another for {when}")
                return None
        spacing = {"update.yml": UPDATE_SPACING, "preflight.yml": PREFLIGHT_SPACING}.get(job.workflow, 0)
        newest = (self.last_update if job.workflow == "update.yml"   # its own starts, not runs that may skip their read
                  else max((c for c in map(created, runs) if c is not None), default=None))
        if spacing and newest is not None and t < newest + spacing:
            say(f"{at}: the last {NAMES[job.workflow]} run was started {max(t - newest, 0):.0f} s ago; "
                f"{job.name} for {when} can start at {common.clock(newest + spacing, seconds=True)}")
            return newest + spacing
        inputs, unticked = job.inputs, False
        if job.workflow == "update.yml" and saved_fetch(self.gh) is False:
            if self.last_unticked is None or t - self.last_unticked >= UNTICKED_EVERY:
                inputs, unticked = {"keepalive": "false"}, True
                say(f"{at}: the Actions cache has no saved county fetch time, so the Update results run started "
                    "now has keepalive unticked, to read the county and save one")
            else:
                say(f"{at}: still no saved county fetch time in the Actions cache; the Update results run started "
                    "now may skip its county read")
        try:
            common.dispatch(self.gh, job.workflow, inputs)
        except common.ApiError as e:
            say(f"{at}: couldn't start {job.name} ({e}); trying again in a minute")
            return t + RETRY
        if unticked:
            self.last_unticked = t
        if job.workflow == "update.yml":
            self.last_update = t
        say(f"{at}: started {job.name} for {when}")
        return None

    def step(self, t):
        """Start whatever is due at t."""
        for job in self.jobs:
            last = job.last_time(t)
            if last is not None and (job.seen is None or last > job.seen):
                if job.due is not None:
                    say(f"{common.clock(t)}: {job.name} for {common.clock(job.due)} not started from here; "
                        f"moving on to {common.clock(last)}")
                job.due, job.seen, job.retry_at = last, last, 0.0
        for workflow in sorted({j.workflow for j in self.jobs}):
            owed = [j for j in self.jobs if j.workflow == workflow and j.due is not None]
            keep = max(owed, key=lambda j: j.due, default=None)
            for j in owed:
                if j is not keep:
                    say(f"{common.clock(t)}: checking only the later {NAMES[workflow]} time, "
                        f"{common.clock(keep.due)} ({keep.name}), not {common.clock(j.due)}")
                    j.due = None
        for job in self.jobs:
            if job.due is None or t < job.retry_at:
                continue
            if t - job.due > CATCH_UP:
                say(f"{common.clock(t)}: gave up starting {job.name} for {common.clock(job.due)}")
                job.due = None
                continue
            try:
                retry = self.start(job, t)
            except Exception as e:  # noqa: BLE001 - one job's bug must not stop the others
                say(f"{common.clock(t)}: unexpected error starting {job.name} ({type(e).__name__}); "
                    "trying again in a minute")
                retry = t + RETRY
            if retry is None:
                job.due = None
            else:
                job.retry_at = retry

    def next_wake(self, t):
        times = [j.next_time(t) for j in self.jobs] + [j.retry_at for j in self.jobs if j.due is not None]
        times = [x for x in times if x is not None]
        return min(times) if times else None

    def next_job(self, t):
        upcoming = [(j.next_time(t), j.name) for j in self.jobs]
        upcoming = [u for u in upcoming if u[0] is not None]
        return min(upcoming) if upcoming else None


# ---------- hand-over ----------

def successor_queued(gh):
    """True when a Keep-alive run other than this one is waiting or running."""
    me = os.environ.get("GITHUB_RUN_ID", "")
    for attempt in range(SHOW_TRIES):
        if attempt:
            sleep(SHOW_WAIT)
        try:
            runs = runs_of(gh, "keepalive.yml", per_page=5)
        except common.ApiError:
            continue
        if any(str(r.get("id")) != me and r.get("status") in common.BUSY for r in runs):
            return True
    return False


class HandOver:
    def __init__(self):
        self.asked = None      # when this run last started a fresh Keep-alive run

    def attempt(self, gh):
        """One try: start a fresh Keep-alive run (again only after REASK) and check that it's queued."""
        t = now()
        if self.asked is None or t - self.asked >= REASK:
            try:
                common.dispatch(gh, "keepalive.yml")
            except common.ApiError as e:
                say(f"{common.clock(t)}: couldn't start a fresh Keep-alive run ({e})")
                return False
            self.asked = t
        if successor_queued(gh):
            say(f"{common.clock(now())}: a fresh Keep-alive run is queued to carry on")
            return True
        say(f"{common.clock(now())}: the fresh Keep-alive run isn't showing as queued yet")
        return False


def run(gh):
    started = now()
    if started >= END:
        say("The counting schedule is over (after Tue Nov 17, 7:10 AM Pacific); nothing started")
        try:
            say(f"API check OK: {len(common.update_runs(gh))} recent Update results runs listed")
        except common.ApiError as e:
            say(f"API check failed ({e})")
        return 0
    jobs, notes = load_jobs()
    for note in notes:
        say(f"Schedule: {note}")
    counts = {}
    for job in jobs:
        counts[NAMES[job.workflow]] = counts.get(NAMES[job.workflow], 0) + len(job.crons)
    deadline = started + MAX_RUN
    if END - deadline <= MIN_HANDOFF:
        deadline = END    # too close to the end for a fresh run: carry on to END
    until = ("the end of the counting schedule" if deadline >= END else "then hands over to a fresh one")
    say(f"This Keep-alive run goes until {stamp(min(deadline, END))} Pacific, {until}; it follows the schedule "
        "lines of " + ", ".join(f"{name} ({n})" for name, n in counts.items()))
    pacer = Pacer(gh, jobs)
    nxt = pacer.next_job(started)
    if nxt:
        say(f"Next: {nxt[1]} at {stamp(nxt[0])} Pacific")
    handing = HandOver()
    beat = started
    while True:
        t = now()
        if t >= END:
            say(f"{common.clock(t)}: the counting schedule is over; stopping")
            return 0
        if t >= deadline:
            if handing.attempt(gh):
                return 0
            t = now()
            if t < started + HANDOFF_UNTIL:
                deadline = t + RETRY      # go on starting runs; try the hand-over again in a minute
            elif END - t <= MIN_HANDOFF:
                deadline = END            # this close to the end, carry on to it without one
            else:
                say(STOPPED if handing.asked is None else NEVER_SHOWED)
                return 1
        wake = None
        try:
            pacer.step(t)
            t = now()
            wake = pacer.next_wake(t)
            if t - beat >= HEARTBEAT:
                beat = t
                nxt = pacer.next_job(t)
                say(f"{common.clock(t)}: still on" + (f"; next: {nxt[1]} at {stamp(nxt[0])} Pacific" if nxt else ""))
        except Exception as e:  # noqa: BLE001 - one bad check must not end the keep-alive
            say(f"{common.clock(t)}: unexpected error ({type(e).__name__}); carrying on")
        target = min(x for x in (wake, deadline, END, now() + NAP) if x is not None)
        sleep(max(1.0, target - now()))


def main():
    gh = common.GitHub()
    if not gh.ready():
        say("GITHUB_TOKEN or GITHUB_REPOSITORY not set; nothing to do")
        return 1
    started = now()
    try:
        return run(gh)
    except Exception as e:  # noqa: BLE001 - keep the chain going after a bug, but not in a tight loop
        say(f"Keep-alive stopped on an unexpected error ({type(e).__name__})")
        if now() - started >= MIN_LIFE and now() < END - MIN_HANDOFF:
            handing = HandOver()
            try:
                for attempt in range(5):
                    if attempt:
                        sleep(RETRY)
                    if handing.attempt(gh):
                        break
                else:
                    say(STOPPED if handing.asked is None else NEVER_SHOWED)
            except Exception as e2:  # noqa: BLE001
                say(f"Keep-alive couldn't hand over ({type(e2).__name__}). " + RESTART)
        elif now() < END:
            say("Keep-alive stops here without starting a fresh run. " + RESTART)
        return 1


if __name__ == "__main__":
    sys.exit(main())
