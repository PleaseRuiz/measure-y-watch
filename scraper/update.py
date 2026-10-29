"""Update data/results.json from the county page, or from a manual entry.

Run by .github/workflows/update.yml. If any check fails it writes nothing and
exits non-zero, so the run goes red and GitHub sends an email.

Modes, picked from the environment:
  EVENT=push                      no fetch; only copies expected_votes from config.json
  MANUAL_YES / MANUAL_NO set      manual entry (MANUAL_COUNTY_UPDATED required)
  otherwise                       one fetch of the county page
ALLOW_DECREASE=true (only set by a hand-started run) lets this run's reading go below the saved YES or NO.
PRACTICE=1 (rehearsal only) writes data/rehearsal.json instead and never fetches the county page.
MARK_CERTIFIED=true (hand-started run) marks the count final once the county has certified it.
It is refused before CERTIFY_AFTER, so a mis-tap on election night can't end the count early.
"""

import http.client
import json
import os
import re
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import county

ROOT = Path(__file__).resolve().parent.parent
PRACTICE = os.environ.get("PRACTICE") == "1"
RESULTS = ROOT / "data" / ("rehearsal.json" if PRACTICE else "results.json")
CONFIG = ROOT / "config.json"
LAST_FETCH = ROOT / ".last-fetch"  # restored from the Actions cache between runs

# The county certifies on Dec 3. Nothing can be marked certified before the last scheduled
# update (Nov 16) is over.
CERTIFY_AFTER = datetime(2026, 11, 17, tzinfo=county.PACIFIC)

# Skip a fetch if the last one was this recent. Scheduled runs are 5 minutes apart
# but GitHub starts them a little early or late, so a strict 300 would skip on-time runs.
MIN_FETCH_GAP = 240


class CheckFailed(Exception):
    pass


class Skip(Exception):
    pass


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


def county_reading(now):
    try:
        last = float(LAST_FETCH.read_text())
    except (FileNotFoundError, ValueError):
        last = 0.0
    gap = now.timestamp() - last
    if gap < MIN_FETCH_GAP:
        raise Skip(f"county page was fetched {gap:.0f} s ago; waiting for the next run")
    LAST_FETCH.write_text(f"{now.timestamp():.0f}\n")

    try:
        html = county.fetch()
    except urllib.error.HTTPError as e:
        raise CheckFailed(f"county page fetch failed: HTTP {e.code}")
    except (OSError, http.client.HTTPException) as e:
        raise CheckFailed(f"county page fetch failed: {type(e).__name__}")
    try:
        r = county.parse(html)
    except county.ParseError as e:
        raise CheckFailed(f"county page check failed: {e}")
    return {"yes": r["yes"], "no": r["no"], "county_updated": r["county_updated"],
            "county_ballots_counted": r["county_ballots_counted"], "source": "county"}


def apply_reading(results, reading, now, allow_decrease=False):
    """Add the reading if it's a new county update. Returns True if it was added."""
    latest = results.get("latest") or {}
    saved = latest.get("county_updated")
    if saved and datetime.fromisoformat(reading["county_updated"]) <= datetime.fromisoformat(saved):
        if reading["source"] == "manual":
            raise CheckFailed(f"county_updated must be newer than the saved {saved}")
        print(f"No new county update (still {saved})")
        return False
    for side in ("yes", "no"):
        before = latest.get(side) or 0
        if reading[side] < before:
            if not allow_decrease:
                raise CheckFailed(f"{side.upper()} went down: {before:,} -> {reading[side]:,}")
            print(f"{side.upper()} went down: {before:,} -> {reading[side]:,} (allowed for this run)")

    entry = {
        "yes": reading["yes"],
        "no": reading["no"],
        "county_updated": reading["county_updated"],
        "county_ballots_counted": reading["county_ballots_counted"],
        "fetched_at": now.isoformat(),
        "source": reading["source"],
    }
    results["latest"] = entry
    results.setdefault("history", []).append(dict(entry))
    if results.get("status") != "final":
        results["status"] = "counting" if entry["yes"] + entry["no"] > 0 else "pre"
    return True


def write_outputs(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


def main():
    now = datetime.now(county.PACIFIC).replace(microsecond=0)
    event = os.environ.get("EVENT", "")
    try:
        results = load_json(RESULTS, f"data/{RESULTS.name}")
        before = json.dumps(results)
        was_final = results.get("status") == "final"
        expected_votes = read_expected_votes()

        reading = None
        if event != "push":
            reading = manual_reading(now)
            if reading is None and PRACTICE:
                raise CheckFailed("practice runs need YES and NO numbers")
            if reading is None:
                try:
                    reading = county_reading(now)
                except Skip as e:
                    print(f"Skipped fetch: {e}")

        added = False
        if reading:
            print(f"Source:           {reading['source']}")
            print(f"Website Updated:  {reading['county_updated']}")
            if reading["county_ballots_counted"] is not None:
                print(f"Ballots Counted:  {reading['county_ballots_counted']:,}")
            print(f"YES:              {reading['yes']:,}")
            print(f"NO:               {reading['no']:,}")
            added = apply_reading(results, reading, now, os.environ.get("ALLOW_DECREASE") == "true")

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
    except CheckFailed as e:
        print(f"Check failed: {e}")
        print("Nothing written.")
        return 1

    if json.dumps(results) == before:
        print(f"{RESULTS.name} unchanged")
        write_outputs(changed="false")
        return 0

    tmp = RESULTS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(results, indent=2) + "\n")
    os.replace(tmp, RESULTS)
    certified_now = results.get("status") == "final" and not was_final
    if added:
        message = f"Update results: {reading['source']} reading, county update {reading['county_updated']}"
        if certified_now:
            message += ", marked certified"
    elif certified_now:
        message = "Mark the count as certified"
    else:
        message = "Update expected_votes from config.json"
    print(f"Saved {RESULTS.name} ({message})")
    write_outputs(changed="true", message=message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
