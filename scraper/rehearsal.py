"""Rehearsal only: writes made-up numbers to data/rehearsal.json. Never touches results.json.

  python scraper/rehearsal.py write N   N=0 resets to the pre-count state; N=1..5 adds fake update N
  python scraper/rehearsal.py verify    waits until the live page serves the local rehearsal.json

Delete this file, .github/workflows/rehearsal.yml and data/rehearsal.json after the rehearsal.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import county

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "data" / "results.json"
REHEARSAL = ROOT / "data" / "rehearsal.json"
CONFIG = ROOT / "config.json"
PAGE_URL = os.environ.get("PAGE_URL", "https://pleaseruiz.github.io/measure-y-watch")

# Made-up cumulative YES, NO and countywide ballots counted, one row per fake update.
FAKE = [
    (3412, 2988, 98450),
    (4105, 3702, 121880),
    (4950, 4301, 150312),
    (5391, 5240, 171940),
    (5802, 5905, 188215),
]


def write(n):
    base = json.loads(RESULTS.read_text())
    expected_votes = json.loads(CONFIG.read_text()).get("expected_votes")
    if n == 0 or not REHEARSAL.exists():
        data = {
            "status": "pre",
            "contest": base["contest"],
            "threshold": base["threshold"],
            "expected_votes": expected_votes,
            "source_url": base["source_url"],
            "latest": {"yes": 0, "no": 0, "county_updated": None, "county_ballots_counted": 0,
                       "fetched_at": None, "source": "rehearsal"},
            "history": [],
        }
    else:
        data = json.loads(REHEARSAL.read_text())
        data["expected_votes"] = expected_votes
    if n > 0:
        yes, no, ballots = FAKE[n - 1]
        now = datetime.now(county.PACIFIC).replace(microsecond=0).isoformat()
        entry = {"yes": yes, "no": no, "county_updated": now, "county_ballots_counted": ballots,
                 "fetched_at": now, "source": "rehearsal"}
        data["latest"] = entry
        data["history"].append(dict(entry))
        data["status"] = "counting"
    REHEARSAL.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Rehearsal update {n}: YES {data['latest']['yes']:,}, NO {data['latest']['no']:,}")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "measure-y-watch rehearsal check"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read()


def verify():
    want = REHEARSAL.read_bytes()
    pushed_at = float(os.environ.get("PUSHED_AT") or time.time())
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            if get(f"{PAGE_URL}/data/rehearsal.json?t={time.time_ns()}") == want:
                print(f"Live page serves this update {time.time() - pushed_at:.0f} s after the commit "
                      "(an open page picks it up within 60 s more)")
                try:
                    plain = get(f"{PAGE_URL}/data/rehearsal.json") == want
                    print("Same file without the cache-busting query string: "
                          + ("also up to date" if plain else "still the old copy"))
                except OSError:
                    pass
                return 0
        except OSError:
            pass
        time.sleep(5)
    print("Live page did not show this update within 5 minutes of the deploy")
    return 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["write"] and len(sys.argv) == 3 and sys.argv[2] in list("012345"):
        write(int(sys.argv[2]))
    elif sys.argv[1:] == ["verify"]:
        sys.exit(verify())
    else:
        sys.exit(__doc__)
