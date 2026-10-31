"""Run the forecast engine check on a results file, then open or close the engine status issue.

  python ops/engine_alert.py data/results.json

Used by update.yml after it commits new results, and by the watchdog on main's results.json. Both
post the same text for the same problem, so the issue isn't repeated just because the other one
saw it too. Always exits 0: the issue is the signal, not the run. Prints status lines only.
"""

import sys

import common
import status


def issue_text(reason):
    return f"The forecast engine check failed on the saved results (`data/results.json`):\n\n> {reason}\n"


def check_and_post(path):
    """Returns (ok, line) as common.engine_check does, after opening or closing the engine issue."""
    ok, line = common.engine_check(path)
    print(line, flush=True)
    if ok is True:
        status.post("resolve", "engine", "The forecast engine check passes again.")
    elif ok is False:
        status.post("open", "engine", issue_text(line))
    else:
        print("Engine check didn't run; the engine issue is left as it is", flush=True)
    return ok, line


def main(argv):
    if len(argv) != 1:
        print("usage: python ops/engine_alert.py <results.json>")
        return 0
    try:
        check_and_post(argv[0])
    except Exception as e:  # never break the run that called it
        print(f"engine check: warning: {type(e).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
