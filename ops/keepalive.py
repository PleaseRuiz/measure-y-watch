"""Election-night keep-alive: Update results keeps running about every 5 minutes even if GitHub's
schedule slips.

GitHub can start scheduled runs late or drop them, most often on busy evenings. From Tue Nov 3
7:45 PM to Wed Nov 4 5:00 AM Pacific, every 5 minutes this starts Update results with
keepalive=true, unless an Update results run is already queued or in progress. A started run reads
the county through update.py's gap guard (never within 4 minutes of the last read; a run that comes
too soon only skips its read) and republishes only if the live page is behind.

A job can run for at most 6 hours, so after 5.5 hours this starts a fresh Keep-alive run to carry
on (the concurrency group holds it until this one ends), then exits. Outside the window it starts
nothing.

Off switch: the repository variable KEEPALIVE=off skips the job (keepalive.yml); cancelling a run
in the Actions tab stops it. Prints status lines only.
"""

import sys
import time
from datetime import datetime

import common

WINDOW_START = datetime(2026, 11, 3, 19, 45, tzinfo=common.PACIFIC).timestamp()
WINDOW_END = datetime(2026, 11, 4, 5, 0, tzinfo=common.PACIFIC).timestamp()
EVERY = 300                # seconds between checks
MAX_RUN = 330 * 60         # hand over to a fresh run after 5.5 hours; keepalive.yml's timeout is 350 minutes
MIN_HANDOFF = 10 * 60      # no hand-over when less than this is left of the window
HANDOFF_TRIES = 3
HANDOFF_WAIT = 20


def now():
    return time.time()


def sleep(seconds):
    time.sleep(seconds)


def say(message):
    print(message, flush=True)


def tick(gh, t):
    """One check: start Update results unless one is queued or in progress. Returns what it did."""
    at = common.clock(t)
    try:
        busy = common.busy_runs(common.update_runs(gh))
    except common.ApiError as e:
        say(f"{at}: couldn't list Update results runs ({e}); trying again at the next check")
        return "error"
    if busy:
        say(f"{at}: Update results is {busy[0]['status'].replace('_', ' ')}; not starting another")
        return "busy"
    try:
        common.dispatch(gh, "update.yml", {"keepalive": "true"})
    except common.ApiError as e:
        say(f"{at}: couldn't start Update results ({e}); trying again at the next check")
        return "error"
    say(f"{at}: started Update results (keep-alive)")
    return "started"


def hand_over(gh):
    for attempt in range(1, HANDOFF_TRIES + 1):
        try:
            common.dispatch(gh, "keepalive.yml")
            say(f"{common.clock(now())}: near the job's time limit; started a fresh Keep-alive run to carry on")
            return True
        except common.ApiError as e:
            say(f"{common.clock(now())}: couldn't start the next Keep-alive run ({e})")
            if attempt < HANDOFF_TRIES:
                sleep(HANDOFF_WAIT)
    say("Keep-alive stops here. Scheduled Update results runs go on, and the watchdog starts one when reads are late.")
    return False


def run(gh):
    started = now()
    if not WINDOW_START <= started < WINDOW_END:
        say("Outside the election-night window (Tue Nov 3 7:45 PM to Wed Nov 4 5:00 AM Pacific); nothing started")
        try:
            say(f"API check OK: {len(common.update_runs(gh))} recent Update results runs listed")
        except common.ApiError as e:
            say(f"API check failed ({e})")
        return 0
    deadline = started + MAX_RUN
    say(f"Keep-alive on until {common.clock(min(deadline, WINDOW_END))} Pacific, checking every {EVERY // 60} minutes")
    while True:
        t = now()
        if t >= WINDOW_END:
            say(f"{common.clock(t)}: the election-night window is over; stopping")
            return 0
        if t >= deadline:
            if WINDOW_END - t > MIN_HANDOFF:
                hand_over(gh)
            else:
                say(f"{common.clock(t)}: the window ends soon; stopping without a hand-over")
            return 0
        tick(gh, t)
        sleep(max(1.0, min(t + EVERY, deadline, WINDOW_END) - now()))


def main():
    gh = common.GitHub()
    if not gh.ready():
        say("GITHUB_TOKEN or GITHUB_REPOSITORY not set; nothing to do")
        return 1
    return run(gh)


if __name__ == "__main__":
    sys.exit(main())
