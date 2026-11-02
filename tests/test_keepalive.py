"""Tests for ops/keepalive.py: its reading of the schedule lines, and whole runs with a fake clock
and a fake GitHub API (no network).

  python3 -m unittest discover -s tests
"""

import io
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ops"))
import common  # noqa: E402
import keepalive  # noqa: E402

P = common.PACIFIC
UTC = timezone.utc
WF = ROOT / ".github" / "workflows"
TOKEN = "ghs_FAKEtoken_DO_NOT_PRINT_1234"


def pt(*a):
    return datetime(*a, tzinfo=P).timestamp()


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- an independent, minute-by-minute reading of a schedule line ----------

def numbers(spec, lo, hi):
    out = set()
    for part in spec.split(","):
        base, _, step = part.partition("/")
        if base == "*":
            first, last = lo, hi
        elif "-" in base:
            first, last = map(int, base.split("-"))
        else:
            first = int(base)
            last = hi if step else first
        out.update(range(first, last + 1, int(step) if step else 1))
    return out


def fires(text, t0, t1):
    """Every time in [t0, t1) the line fires, checking each minute."""
    m, h, dom, mon, dow = text.split()
    assert dow == "*"
    mm, hh, dd, oo = numbers(m, 0, 59), numbers(h, 0, 23), numbers(dom, 1, 31), numbers(mon, 1, 12)
    t = datetime.fromtimestamp(t0, UTC).replace(second=0, microsecond=0)
    if t.timestamp() < t0:
        t += timedelta(minutes=1)
    out = []
    while t.timestamp() < t1:
        if t.minute in mm and t.hour in hh and t.day in dd and t.month in oo:
            out.append(t.timestamp())
        t += timedelta(minutes=1)
    return out


def lines(name):
    """A workflow's schedule lines (written quoted in this repository)."""
    return re.findall(r'^\s*-\s*cron:\s*"([^"]+)"', (WF / name).read_text(), re.M)


# ---------- fakes ----------

class Stop(BaseException):
    """The fake clock reached the end of a test (not an Exception, so nothing in keepalive.py catches it)."""


class Clock:
    def __init__(self, t):
        self.t = t
        self.until = float("inf")

    def now(self):
        return self.t

    def sleep(self, s):
        assert s >= 0, s
        self.t += s
        if self.t >= self.until:
            raise Stop


class FakeGH:
    """The few GitHub API calls keepalive.py makes, against a fake clock."""

    LENGTH = {"update.yml": 70, "watchdog.yml": 30, "preflight.yml": 45}

    def __init__(self, clock):
        self.clock = clock
        self.runs = {}              # workflow -> runs, newest first
        self.dispatches = []        # (time, workflow, inputs)
        self.fail = []              # functions (method, rest, t) -> HTTP status or None
        self.cache = True           # the Actions cache holds a saved county fetch time
        self.cache_error = False
        self.make_keepalive_runs = True
        self.timer = None           # (lines by workflow, delay): GitHub's timer starts scheduled runs too
        self.timer_done = set()
        self.next_id = 100

    def add_run(self, workflow, t0, event="workflow_dispatch", length=None, status=None):
        self.next_id += 1
        run = {"id": self.next_id, "_t0": t0, "event": event,
               "_len": self.LENGTH.get(workflow) if length is None else length, "_status": status}
        self.runs.setdefault(workflow, []).insert(0, run)
        self.runs[workflow].sort(key=lambda r: r["_t0"], reverse=True)
        return run

    def view(self, run):
        if run["_status"]:
            status = run["_status"]
        else:
            age = self.clock.t - run["_t0"]
            status = "queued" if age < 3 else "in_progress" if age < run["_len"] else "completed"
        return {"id": run["id"], "status": status, "conclusion": "success" if status == "completed" else None,
                "created_at": iso(run["_t0"]), "event": run["event"]}

    def _timer(self):
        if not self.timer:
            return
        by_workflow, delay = self.timer
        for workflow, texts in by_workflow.items():
            for text in texts:
                for at in fires(text, self.clock.t - 3600, self.clock.t - delay + 1):
                    if (workflow, at) not in self.timer_done:
                        self.timer_done.add((workflow, at))
                        self.add_run(workflow, at + delay, event="schedule")

    def _failing(self, method, rest):
        for rule in self.fail:
            status = rule(method, rest, self.clock.t)
            if status:
                raise common.ApiError(f"{method} /repos/x/y{rest}: HTTP {status}", status)

    def get(self, rest, **params):
        self._failing("GET", rest)
        self._timer()
        m = re.fullmatch(r"/actions/workflows/([\w.-]+)/runs", rest)
        if m:
            runs = self.runs.get(m.group(1), [])[: int(params.get("per_page", 30))]
            return {"total_count": len(runs), "workflow_runs": [self.view(r) for r in runs]}
        if rest == "/actions/caches":
            if self.cache_error:
                raise common.ApiError("GET /repos/x/y/actions/caches: HTTP 403", 403)
            assert params.get("key") == "last-fetch-" and params.get("ref") == "refs/heads/main", params
            return {"total_count": 1 if self.cache else 0, "actions_caches": []}
        raise common.ApiError(f"GET {rest}: HTTP 404", 404)

    def call(self, method, rest, body=None, **params):
        if method == "GET":
            return self.get(rest, **params)
        self._failing(method, rest)
        m = re.fullmatch(r"/actions/workflows/([\w.-]+)/dispatches", rest)
        assert m and method == "POST", (method, rest)
        assert body["ref"] == "main", body
        workflow = m.group(1)
        self.dispatches.append((self.clock.t, workflow, body.get("inputs")))
        if workflow == "keepalive.yml":
            if self.make_keepalive_runs:
                self.add_run(workflow, self.clock.t, status="pending")
        else:
            self.add_run(workflow, self.clock.t)
        return None

    def sent(self, workflow):
        return [(t, i) for t, w, i in self.dispatches if w == workflow]


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = (keepalive.now, keepalive.sleep, keepalive.say, os.environ.get("GITHUB_RUN_ID"))
        self.log = []

    def tearDown(self):
        keepalive.now, keepalive.sleep, keepalive.say, run_id = self.saved
        if run_id is None:
            os.environ.pop("GITHUB_RUN_ID", None)
        else:
            os.environ["GITHUB_RUN_ID"] = run_id

    def fake(self, start):
        clock = Clock(start)
        gh = FakeGH(clock)
        keepalive.now, keepalive.sleep, keepalive.say = clock.now, clock.sleep, self.log.append
        os.environ["GITHUB_RUN_ID"] = "1"
        return clock, gh

    def chain(self, clock, gh, until, gap=40):
        """Keep-alive runs one after another, as the hand-overs start them, until `until` or the chain stops."""
        runs = 0
        clock.until = until
        while clock.t < until:
            try:
                keepalive.run(gh)
            except Stop:
                return runs + 1
            runs += 1
            pending = [r for r in gh.runs.get("keepalive.yml", []) if r["_status"] == "pending"]
            if not pending:
                break
            for r in gh.runs["keepalive.yml"]:
                if r["_status"] == "in_progress":
                    r["_status"] = "completed"
            nxt = pending[0]
            for r in pending[1:]:
                r["_status"] = "completed"   # GitHub cancels the older pending run in the group
            nxt["_status"] = "in_progress"
            os.environ["GITHUB_RUN_ID"] = str(nxt["id"])
            clock.t += gap
        return runs

    def expected(self, workflow, t0, t1, texts=None):
        out = set()
        for text in texts or lines(workflow):
            if workflow == "preflight.yml" and text == keepalive.DRILL:
                continue
            out.update(fires(text, t0, t1))
        return sorted(out)

    def assertMatches(self, sent_times, slots, late=180):
        """Each slot got exactly one start, at the slot or up to `late` seconds after it, and nothing else."""
        sent_times = sorted(sent_times)
        self.assertEqual(len(sent_times), len(slots), (len(sent_times), len(slots)))
        for got, slot in zip(sent_times, slots):
            self.assertTrue(slot <= got <= slot + late, (keepalive.stamp(slot), got - slot))


def stamps(sent):
    return [keepalive.stamp(t) for t, _ in sent]


class ScheduleLines(Base):
    def test_every_line_parses(self):
        jobs, notes = keepalive.load_jobs()
        self.assertEqual(notes, [])
        by = {(j.workflow, (j.inputs or {}).get("source")): [c.text for c in j.crons] for j in jobs}
        self.assertEqual(by[("update.yml", None)], lines("update.yml"))
        self.assertEqual(by[("watchdog.yml", None)], lines("watchdog.yml"))
        self.assertEqual(by[("preflight.yml", "html")], ["5 1 4 11 *"])
        self.assertEqual(by[("preflight.yml", "xml")], ["15 1 4 11 *"])
        self.assertNotIn(keepalive.DRILL, [c.text for j in jobs for c in j.crons])

    def test_preflight_inputs_match_the_workflow(self):
        text = (WF / "preflight.yml").read_text()
        self.assertIn("github.event.schedule == '15 1 4 11 *' && 'xml'", text)
        self.assertIn("github.event.schedule == '13 22 1 11 *'", text)
        for line in lines("preflight.yml"):
            self.assertTrue(line == keepalive.DRILL or line in keepalive.PREFLIGHT, line)

    def test_next_and_last_match_minute_by_minute(self):
        t0, t1 = pt(2026, 10, 30, 0, 0), pt(2026, 12, 5, 0, 0)
        samples = [t0 + k * 7919.3 for k in range(int((t1 - t0) / 7919.3))]
        samples += [pt(2026, 11, 3, 19, 57), pt(2026, 11, 3, 19, 56, 59), pt(2026, 11, 3, 20, 2) + 0.5,
                    pt(2026, 11, 4, 4, 57), pt(2026, 11, 17, 7, 7), pt(2026, 11, 17, 7, 9, 59)]
        texts = [x for name in ("update.yml", "watchdog.yml", "preflight.yml", "keepalive.yml") for x in lines(name)]
        # Forms the current lines don't use: *, n/step, comma lists, a range with a step.
        texts += ["*/15 * * * *", "5/20 3 * * *", "0,30 1,13 1-3,10 11,12 *", "* 4 4 11 *", "59 23 31 12 *",
                  "1-5/2,40 */6 */2 11 *"]
        for text in texts:
            cron = keepalive.Cron(text)
            every = fires(text, t0 - 86400 * 2, t1 + 86400 * 2)
            for t in samples:
                after = [x for x in every if t < x <= t1]
                self.assertEqual(cron.next_after(t, t1), after[0] if after else None, (text, keepalive.stamp(t)))
                before = [x for x in every if t - 5400 <= x <= t]
                self.assertEqual(cron.last_at_or_before(t, t - 5400), before[-1] if before else None,
                                 (text, keepalive.stamp(t)))

    def test_bad_lines_are_refused(self):
        for bad in ("* * * *", "61 * * * *", "5 25 * * *", "0 0 * * 1", "a b c d e", "5-1 * * * *", "*/0 * * * *"):
            with self.assertRaises(ValueError, msg=bad):
                keepalive.Cron(bad)

    def test_end_is_after_the_last_hourly_read(self):
        reads = self.expected("update.yml", pt(2026, 11, 1), pt(2026, 12, 5))
        before = [x for x in reads if x < keepalive.END]
        after = [x for x in reads if x >= keepalive.END]
        self.assertEqual(keepalive.stamp(before[-1]), "Tue Nov 17, 7:07 AM")
        self.assertEqual(keepalive.stamp(after[0]), "Tue Nov 17, 5:07 PM")

    def test_lead_leaves_room_for_the_gap_guard(self):
        jobs = {j.name: j for j in keepalive.load_jobs()[0]}
        update, watchdog = jobs["Update results"], jobs["Watchdog"]
        self.assertEqual(update.lead(pt(2026, 11, 3, 20, 2)), 60)        # election night: every 5 minutes
        self.assertEqual(update.lead(pt(2026, 11, 3, 19, 57)), 300)      # the first read: nothing just before
        self.assertEqual(update.lead(pt(2026, 11, 6, 10, 4)), 300)       # daytime: every 10 minutes
        self.assertEqual(update.lead(pt(2026, 11, 6, 19, 7)), 300)       # hourly
        self.assertEqual(watchdog.lead(pt(2026, 11, 3, 20, 5)), 300)
        self.assertEqual(jobs["Preflight (the county results page)"].lead(pt(2026, 11, 3, 17, 5)), 5)


class LoadJobs(unittest.TestCase):
    def setUp(self):
        self.saved = keepalive.WORKFLOWS
        self.dir = Path(tempfile.mkdtemp())
        for f in ("update.yml", "watchdog.yml", "preflight.yml"):
            shutil.copy(WF / f, self.dir / f)
        keepalive.WORKFLOWS = self.dir

    def tearDown(self):
        keepalive.WORKFLOWS = self.saved
        shutil.rmtree(self.dir)

    def edit(self, name, old, new):
        p = self.dir / name
        text = p.read_text()
        self.assertIn(old, text)
        p.write_text(text.replace(old, new))

    def test_bad_unknown_and_missing_are_noted(self):
        self.edit("update.yml", '- cron: "57 3 4 11 *"', '- cron: "61 3 4 11 *"')
        self.edit("preflight.yml", '- cron: "15 1 4 11 *"', '- cron: "20 1 4 11 *"')
        (self.dir / "watchdog.yml").unlink()
        jobs, notes = keepalive.load_jobs()
        self.assertIn("left out update.yml line '61 3 4 11 *': out of range: '61'", notes)
        self.assertIn("left out preflight.yml line '20 1 4 11 *': not a known preflight time", notes)
        self.assertIn("couldn't read watchdog.yml (FileNotFoundError)", notes)
        self.assertEqual(sorted(j.workflow for j in jobs), ["preflight.yml", "update.yml"])

    def test_single_quotes_unquoted_and_trailing_comments_are_read(self):
        self.edit("update.yml", '- cron: "57 3 4 11 *"', "- cron: '57 3 4 11 *'  # first read")
        self.edit("watchdog.yml", '- cron: "55 3 4 11 *"', "- cron: 55 3 4 11 *  # first check")
        jobs, notes = keepalive.load_jobs()
        texts = [c.text for j in jobs for c in j.crons]
        self.assertIn("57 3 4 11 *", texts)
        self.assertIn("55 3 4 11 *", texts)
        self.assertEqual(notes, [])

    def test_a_cron_line_it_cant_read_is_noted(self):
        self.edit("update.yml", '- cron: "57 3 4 11 *"', '- cron: "57 3 4 11 *\'')
        jobs, notes = keepalive.load_jobs()
        self.assertEqual(len(notes), 1)
        self.assertIn("left out update.yml line", notes[0])
        self.assertIn("can't read it", notes[0])


class WholeCount(Base):
    def test_every_scheduled_start_happens_once(self):
        start = pt(2026, 11, 1, 17, 0)
        clock, gh = self.fake(start)
        runs = self.chain(clock, gh, keepalive.END + 3600)
        end = keepalive.END
        self.assertMatches([t for t, _ in gh.sent("update.yml")], self.expected("update.yml", start, end))
        self.assertMatches([t for t, _ in gh.sent("watchdog.yml")], self.expected("watchdog.yml", start, end))
        self.assertEqual([(keepalive.stamp(t), i) for t, i in gh.sent("preflight.yml")],
                         [("Tue Nov 3, 5:05 PM", {"source": "html"}), ("Tue Nov 3, 5:15 PM", {"source": "xml"})])
        self.assertTrue(all(i == {"keepalive": "true"} for _, i in gh.sent("update.yml")))
        self.assertTrue(all(i is None for _, i in gh.sent("watchdog.yml")))
        # Hand-overs: one per run but the last, about every 5.5 hours.
        handoffs = [t for t, _ in gh.sent("keepalive.yml")]
        self.assertEqual(len(handoffs), runs - 1)
        self.assertGreater(runs, 60)
        gaps = [b - a for a, b in zip(handoffs, handoffs[1:])]
        self.assertTrue(all(keepalive.MAX_RUN <= g <= keepalive.MAX_RUN + 300 for g in gaps), gaps[:3])
        self.assertLessEqual(clock.t, end + 120)
        self.assertEqual(self.log[-1], "7:10 AM: the counting schedule is over; stopping")
        text = "\n".join(self.log)
        self.assertNotIn("unexpected error", text)
        self.assertNotIn("not started from here", text)
        self.assertNotIn("gave up", text)

    def test_every_start_with_the_chain_in_another_phase(self):
        # The same, with the hand-overs falling at other times of day (and one within 10 minutes of END).
        for start in (pt(2026, 11, 1, 19, 37, 20), pt(2026, 11, 1, 22, 3, 50)):
            with self.subTest(start=keepalive.stamp(start)):
                self.log.clear()
                clock, gh = self.fake(start)
                self.chain(clock, gh, keepalive.END + 3600)
                self.assertMatches([t for t, _ in gh.sent("update.yml")],
                                   self.expected("update.yml", start, keepalive.END))
                self.assertMatches([t for t, _ in gh.sent("watchdog.yml")],
                                   self.expected("watchdog.yml", start, keepalive.END))
                self.assertEqual(self.log[-1], "7:10 AM: the counting schedule is over; stopping")

    def test_starts_land_on_the_minute(self):
        start = pt(2026, 11, 3, 19, 30)
        clock, gh = self.fake(start)
        self.chain(clock, gh, pt(2026, 11, 3, 22, 0))
        slots = self.expected("update.yml", start, pt(2026, 11, 3, 22, 0))
        self.assertMatches([t for t, _ in gh.sent("update.yml")], slots, late=2)
        self.assertEqual(keepalive.stamp(slots[0]), "Tue Nov 3, 7:57 PM")

    def test_start_lines_and_heartbeat(self):
        clock, gh = self.fake(pt(2026, 11, 3, 19, 50))
        self.chain(clock, gh, pt(2026, 11, 3, 22, 30))
        self.assertEqual(self.log[:2], [
            "This Keep-alive run goes until Wed Nov 4, 1:20 AM Pacific, then hands over to a fresh one; it follows "
            "the schedule lines of Update results (9), Watchdog (4), Preflight (2)",
            "Next: Watchdog at Tue Nov 3, 7:55 PM Pacific"])
        beats = [x for x in self.log if ": still on" in x]
        self.assertEqual(beats, ["8:50 PM: still on; next: Update results at Tue Nov 3, 8:52 PM Pacific",
                                 "9:50 PM: still on; next: Update results at Tue Nov 3, 9:52 PM Pacific"])


class Situations(Base):
    def night(self, start, until, setup=None):
        """A keep-alive from start to until, with the hour before start already run (as by an earlier run)."""
        clock, gh = self.fake(start)
        for workflow in ("update.yml", "watchdog.yml", "preflight.yml"):
            for slot in self.expected(workflow, start - 3600, start + 1):
                gh.add_run(workflow, slot + 1, length=20)
        if setup:
            setup(gh, clock)
        self.chain(clock, gh, until)
        return clock, gh

    def test_github_timer_on_time_means_no_extra_starts(self):
        timer = ({"update.yml": lines("update.yml"), "watchdog.yml": lines("watchdog.yml")}, 0)
        clock, gh = self.night(pt(2026, 11, 3, 19, 50), pt(2026, 11, 3, 23, 0),
                               lambda gh, clock: setattr(gh, "timer", timer))
        self.assertEqual(gh.sent("update.yml"), [])
        self.assertEqual(gh.sent("watchdog.yml"), [])
        self.assertTrue(any("already started" in line and "by schedule" in line for line in self.log))

    def test_github_timer_late_at_night_still_one_start_per_slot_on_time(self):
        # GitHub's timer 150 s late: each late run comes 150 s after a read, so update.py's gap guard skips
        # it; it doesn't count for the next time, and the keep-alive starts every time on time.
        timer = ({"update.yml": lines("update.yml")}, 150)
        start, until = pt(2026, 11, 3, 19, 50), pt(2026, 11, 3, 21, 0)
        clock, gh = self.night(start, until, lambda gh, clock: setattr(gh, "timer", timer))
        self.assertMatches([t for t, _ in gh.sent("update.yml")], self.expected("update.yml", start, until), late=2)

    def test_github_timer_five_minutes_late_in_the_daytime_doesnt_double_reads(self):
        timer = ({"update.yml": lines("update.yml")}, 300)
        start, until = pt(2026, 11, 6, 9, 0), pt(2026, 11, 6, 12, 0)
        clock, gh = self.night(start, until, lambda gh, clock: setattr(gh, "timer", timer))
        made = sorted(r["_t0"] for r in gh.runs["update.yml"] if r["_t0"] >= start)
        gaps = [round(b - a) for a, b in zip(made, made[1:])]
        self.assertLessEqual(len(made), 19, [keepalive.stamp(x) for x in made])    # 18 times, plus one start early on
        self.assertTrue(all(g >= 290 for g in gaps), gaps)

    def test_busy_update_run_means_no_start(self):
        def setup(gh, clock):
            gh.add_run("update.yml", pt(2026, 11, 3, 19, 58), length=600)   # in progress at 8:02 and 8:07
        clock, gh = self.night(pt(2026, 11, 3, 19, 59), pt(2026, 11, 3, 20, 10), setup)
        self.assertEqual(stamps(gh.sent("update.yml")), [])
        self.assertTrue(any("Update results is in progress; not starting another for 8:02 PM" in x for x in self.log))
        clock2, gh2 = self.night(pt(2026, 11, 3, 19, 59), pt(2026, 11, 3, 20, 13), setup)
        self.assertEqual(stamps(gh2.sent("update.yml")), ["Tue Nov 3, 8:12 PM"])

    def test_busy_watchdog_means_no_start(self):
        def setup(gh, clock):
            gh.add_run("watchdog.yml", pt(2026, 11, 3, 19, 59, 30), length=400)   # in progress at 8:05
        clock, gh = self.night(pt(2026, 11, 3, 20, 2), pt(2026, 11, 3, 20, 16), setup)
        self.assertEqual(stamps(gh.sent("watchdog.yml")), ["Tue Nov 3, 8:15 PM"])
        self.assertTrue(any("Watchdog is in progress; not starting another for 8:05 PM" in x for x in self.log))

    def test_deploy_after_push_is_not_busy(self):
        def setup(gh, clock):
            gh.add_run("update.yml", pt(2026, 11, 3, 20, 1, 30), event="push", length=200)
        clock, gh = self.night(pt(2026, 11, 3, 20, 0), pt(2026, 11, 3, 20, 3), setup)
        self.assertEqual(stamps(gh.sent("update.yml")), ["Tue Nov 3, 8:02 PM"])

    def test_a_run_shortly_before_the_time_counts(self):
        def setup(gh, clock):
            gh.add_run("update.yml", pt(2026, 11, 3, 20, 1, 10), length=20)   # e.g. a hand-started run
        clock, gh = self.night(pt(2026, 11, 3, 19, 59), pt(2026, 11, 3, 20, 8), setup)
        self.assertEqual(stamps(gh.sent("update.yml")), ["Tue Nov 3, 8:07 PM"])
        self.assertTrue(any("Update results for 8:02 PM already started" in x for x in self.log))

    def test_a_run_that_skips_its_read_doesnt_cancel_the_next_time(self):
        # A watchdog republish or a hand-started run 3.5 minutes after a read: update.py's gap guard skips
        # its county read, so the next time still gets its own start, on time.
        def setup(gh, clock):
            real_get, at = gh.get, pt(2026, 11, 3, 20, 25, 30)

            def get(rest, **params):
                if clock.t >= at and not any(r["_t0"] == at for r in gh.runs["update.yml"]):
                    gh.add_run("update.yml", at, length=40)
                return real_get(rest, **params)
            gh.get = get
        clock, gh = self.night(pt(2026, 11, 3, 20, 20), pt(2026, 11, 3, 20, 35), setup)
        self.assertEqual([keepalive.stamp(t)[-7:] for t, _ in gh.sent("update.yml")],
                         ["8:22 PM", "8:27 PM", "8:32 PM"])
        self.assertTrue(all(t % 60 < 2 for t, _ in gh.sent("update.yml")))

    def test_starts_stay_at_least_290_seconds_apart(self):
        # A fresh run at 8:02:30 catches up 8:02; its next start keeps 290 s from that one.
        clock, gh = self.fake(pt(2026, 11, 3, 20, 2, 30))
        gh.add_run("update.yml", pt(2026, 11, 3, 19, 57, 1), length=40)
        self.chain(clock, gh, pt(2026, 11, 3, 20, 20))
        got = [round(t - pt(2026, 11, 3, 20, 2)) for t, _ in gh.sent("update.yml")]
        self.assertEqual(got, [30, 320, 610, 900])      # 8:07:20, 8:12:10, then 8:17 on time
        self.assertTrue(any("Update results for 8:07 PM can start at 8:07:20 PM" in x for x in self.log))

    def test_failed_starts_are_tried_again_each_minute(self):
        def setup(gh, clock):
            gh.fail.append(lambda m, rest, t: 502 if m == "POST" and "update.yml" in rest
                           and pt(2026, 11, 3, 20, 2) <= t < pt(2026, 11, 3, 20, 4, 30) else None)
        clock, gh = self.night(pt(2026, 11, 3, 20, 0), pt(2026, 11, 3, 20, 13), setup)
        got = [round(t - pt(2026, 11, 3, 20, 2)) for t, _ in gh.sent("update.yml")]
        # 8:05 (after 8:02, 8:03 and 8:04 failed); the next keeps 290 s from it: 8:09:50.
        self.assertEqual(got, [180, 470])
        self.assertEqual(sum("couldn't start Update results" in x for x in self.log), 3)

    def test_failed_listing_is_tried_again(self):
        def setup(gh, clock):
            gh.fail.append(lambda m, rest, t: 500 if m == "GET" and rest.endswith("/watchdog.yml/runs")
                           and t < pt(2026, 11, 3, 20, 7) else None)
        clock, gh = self.night(pt(2026, 11, 3, 20, 4), pt(2026, 11, 3, 20, 16), setup)
        self.assertEqual(stamps(gh.sent("watchdog.yml")), ["Tue Nov 3, 8:07 PM", "Tue Nov 3, 8:15 PM"])

    def test_a_newer_time_replaces_one_still_failing(self):
        def setup(gh, clock):
            gh.fail.append(lambda m, rest, t: 500 if m == "POST" and "update.yml" in rest
                           and t < pt(2026, 11, 3, 20, 13) else None)
        clock, gh = self.night(pt(2026, 11, 3, 20, 1), pt(2026, 11, 3, 20, 20), setup)
        self.assertEqual(stamps(gh.sent("update.yml")), ["Tue Nov 3, 8:13 PM", "Tue Nov 3, 8:17 PM"])
        self.assertTrue(any("for 8:02 PM not started from here; moving on to 8:07 PM" in x for x in self.log))

    def test_a_newer_time_is_tried_at_once_not_at_the_old_retry_time(self):
        start = pt(2026, 11, 3, 20, 6, 30)                     # catches up 8:02 PM, which fails
        clock, gh = self.fake(start)
        gh.fail.append(lambda m, rest, t: 502 if m == "POST" and "update.yml" in rest and t < start + 30 else None)
        self.chain(clock, gh, pt(2026, 11, 3, 20, 8))
        self.assertEqual([round(t - pt(2026, 11, 3, 20, 7)) for t, _ in gh.sent("update.yml")], [0])

    def test_gives_up_on_a_time_after_an_hour(self):
        clock, gh = self.night(pt(2026, 11, 3, 17, 0), pt(2026, 11, 3, 18, 30),
                               lambda gh, clock: gh.fail.append(
                                   lambda m, rest, t: 500 if m == "POST" and "preflight" in rest else None))
        self.assertEqual(gh.sent("preflight.yml"), [])
        self.assertTrue(any("gave up starting Preflight (the county's backup data feed) for 5:15 PM" in x for x in self.log))

    def test_no_saved_fetch_time_starts_unticked_runs_sparingly(self):
        clock, gh = self.night(pt(2026, 11, 3, 19, 50), pt(2026, 11, 3, 20, 45),
                               lambda gh, clock: setattr(gh, "cache", False))
        got = [(keepalive.stamp(t), i["keepalive"]) for t, i in gh.sent("update.yml")]
        self.assertEqual(got[:6], [("Tue Nov 3, 7:57 PM", "false"), ("Tue Nov 3, 8:02 PM", "true"),
                                   ("Tue Nov 3, 8:07 PM", "true"), ("Tue Nov 3, 8:12 PM", "true"),
                                   ("Tue Nov 3, 8:17 PM", "false"), ("Tue Nov 3, 8:22 PM", "true")])

    def test_cache_question_failing_keeps_runs_ticked(self):
        clock, gh = self.night(pt(2026, 11, 3, 19, 50), pt(2026, 11, 3, 20, 10),
                               lambda gh, clock: setattr(gh, "cache_error", True))
        self.assertTrue(all(i == {"keepalive": "true"} for _, i in gh.sent("update.yml")))
        self.assertEqual(len(gh.sent("update.yml")), 3)

    def test_saved_fetch_odd_answers(self):
        class G:
            def __init__(self, answer):
                self.answer = answer

            def get(self, rest, **params):
                return self.answer
        for answer, want in (({"total_count": 2}, True), ({"total_count": 0}, False), ({"total_count": True}, None),
                             ({"total_count": "1"}, None), ([], None), (None, None), ({}, None)):
            self.assertIs(keepalive.saved_fetch(G(answer)), want, answer)

    def test_catches_up_a_time_missed_before_it_started(self):
        clock, gh = self.fake(pt(2026, 11, 3, 20, 4, 10))
        self.chain(clock, gh, pt(2026, 11, 3, 20, 6))
        self.assertEqual([round(t - pt(2026, 11, 3, 20, 4, 10)) for t, _ in gh.sent("update.yml")], [0])
        self.assertEqual([round(t - pt(2026, 11, 3, 20, 4, 10)) for t, _ in gh.sent("watchdog.yml")], [0])

    def test_catch_up_runs_only_the_later_preflight(self):
        clock, gh = self.fake(pt(2026, 11, 3, 17, 40))
        self.chain(clock, gh, pt(2026, 11, 3, 17, 45))
        self.assertEqual([i for _, i in gh.sent("preflight.yml")], [{"source": "xml"}])
        self.assertTrue(any("checking only the later Preflight time" in x for x in self.log))

    def test_restart_after_both_preflights_ran_starts_and_claims_nothing(self):
        clock, gh = self.fake(pt(2026, 11, 3, 17, 30))
        gh.add_run("preflight.yml", pt(2026, 11, 3, 17, 5, 1), length=45)
        gh.add_run("preflight.yml", pt(2026, 11, 3, 17, 15, 1), length=45)
        self.chain(clock, gh, pt(2026, 11, 3, 17, 32))
        self.assertEqual(gh.sent("preflight.yml"), [])
        self.assertEqual([x for x in self.log if "wasn't started" in x or "missed" in x], [])

    def test_xml_preflight_starts_while_the_html_one_still_runs(self):
        clock, gh = self.fake(pt(2026, 11, 3, 17, 0))
        gh.LENGTH = dict(gh.LENGTH, **{"preflight.yml": 700})
        self.chain(clock, gh, pt(2026, 11, 3, 17, 20))
        self.assertEqual([(keepalive.stamp(t), i) for t, i in gh.sent("preflight.yml")],
                         [("Tue Nov 3, 5:05 PM", {"source": "html"}), ("Tue Nov 3, 5:15 PM", {"source": "xml"})])

    def test_a_late_html_preflight_doesnt_stand_in_for_the_xml_one(self):
        clock, gh = self.fake(pt(2026, 11, 3, 17, 0))
        gh.fail.append(lambda m, rest, t: 502 if m == "POST" and "preflight" in rest
                       and t < pt(2026, 11, 3, 17, 13, 30) else None)
        self.chain(clock, gh, pt(2026, 11, 3, 17, 25))
        got = [(round(t - pt(2026, 11, 3, 17, 0)), i["source"]) for t, i in gh.sent("preflight.yml")]
        self.assertEqual(got, [(14 * 60, "html"), (14 * 60 + 330, "xml")])   # xml 330 s after html: 5:19:30 PM

    def test_a_preflight_github_timer_started_doesnt_stand_in_for_the_check(self):
        # preflight.py does nothing in a run GitHub's timer started; here its 5:05 PM run comes 7 minutes
        # late and its 5:15 PM run 1 minute late. The keep-alive still starts both checks on time.
        clock, gh = self.fake(pt(2026, 11, 3, 17, 0))
        late = [pt(2026, 11, 3, 17, 12), pt(2026, 11, 3, 17, 16)]
        real_get = gh.get

        def get(rest, **params):
            while late and clock.t >= late[0]:
                gh.add_run("preflight.yml", late.pop(0), event="schedule", length=10)
            return real_get(rest, **params)
        gh.get = get
        self.chain(clock, gh, pt(2026, 11, 3, 17, 30))
        self.assertEqual([(keepalive.stamp(t), i["source"]) for t, i in gh.sent("preflight.yml")],
                         [("Tue Nov 3, 5:05 PM", "html"), ("Tue Nov 3, 5:15 PM", "xml")])

    def test_nothing_older_than_an_hour_is_caught_up(self):
        clock, gh = self.fake(pt(2026, 11, 3, 18, 20))
        self.chain(clock, gh, pt(2026, 11, 3, 18, 25))
        self.assertEqual(gh.dispatches, [])

    def test_unexpected_error_carries_on(self):
        real = keepalive.Pacer.start
        calls = {"n": 0}

        def flaky(self_, job, t):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real(self_, job, t)
        keepalive.Pacer.start = flaky
        try:
            clock, gh = self.night(pt(2026, 11, 3, 20, 1), pt(2026, 11, 3, 20, 13))
        finally:
            keepalive.Pacer.start = real
        self.assertTrue(any("unexpected error starting" in x and "(RuntimeError); trying again in a minute" in x
                            for x in self.log))
        self.assertGreaterEqual(len(gh.sent("update.yml")), 2)
        self.assertNotIn("boom", "\n".join(self.log))

    def test_one_job_that_keeps_failing_doesnt_block_the_others(self):
        real = keepalive.Pacer.start

        def start(self_, job, t):
            if job.workflow == "update.yml":
                raise RuntimeError("a bug on the Update results path only")
            return real(self_, job, t)
        keepalive.Pacer.start = start
        try:
            clock, gh = self.fake(pt(2026, 11, 3, 19, 50))
            self.chain(clock, gh, pt(2026, 11, 3, 20, 30))
        finally:
            keepalive.Pacer.start = real
        self.assertEqual(stamps(gh.sent("watchdog.yml")),
                         ["Tue Nov 3, 7:55 PM", "Tue Nov 3, 8:05 PM", "Tue Nov 3, 8:15 PM", "Tue Nov 3, 8:25 PM"])

    def test_token_never_printed_through_the_real_client(self):
        clock, _ = self.fake(pt(2026, 11, 3, 20, 1, 50))
        gh = common.GitHub(token=TOKEN, repo="o/r", api="https://api.invalid")
        seen = []

        def urlopen(req, timeout=None):
            seen.append(req.get_header("Authorization"))
            body = io.BytesIO(b'{"message": "Bad credentials ' + TOKEN.encode() + b'"}')
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, body)
        real = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        clock.until = pt(2026, 11, 3, 20, 3)
        try:
            keepalive.run(gh)
        except Stop:
            pass
        finally:
            urllib.request.urlopen = real
        text = "\n".join(self.log)
        self.assertTrue(seen and all(a == f"Bearer {TOKEN}" for a in seen))
        self.assertIn("HTTP 401", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("Bearer", text)


class HandOver(Base):
    def test_hands_over_after_five_and_a_half_hours(self):
        clock, gh = self.fake(pt(2026, 11, 2, 9, 0))
        self.assertEqual(keepalive.run(gh), 0)
        self.assertEqual([round((t - pt(2026, 11, 2, 9, 0)) / 60) for t, _ in gh.sent("keepalive.yml")], [330])
        self.assertTrue(any("a fresh Keep-alive run is queued to carry on" in x for x in self.log))
        self.assertEqual(gh.sent("update.yml"), [])

    def test_keeps_starting_runs_while_the_hand_over_fails(self):
        start = pt(2026, 11, 3, 15, 0)                        # hand-over due at 8:30 PM
        clock, gh = self.fake(start)
        gh.fail.append(lambda m, rest, t: 503 if m == "POST" and "keepalive.yml" in rest
                       and t < pt(2026, 11, 3, 20, 35) else None)
        self.assertEqual(keepalive.run(gh), 0)
        self.assertEqual(stamps(gh.sent("keepalive.yml")), ["Tue Nov 3, 8:35 PM"])
        self.assertIn("Tue Nov 3, 8:32 PM", stamps(gh.sent("update.yml")))
        self.assertTrue(any("queued to carry on" in x for x in self.log))
        self.assertEqual(sum("couldn't start a fresh Keep-alive run" in x for x in self.log), 5)

    def test_ends_red_when_no_fresh_run_can_be_started(self):
        start = pt(2026, 11, 3, 15, 0)
        clock, gh = self.fake(start)
        gh.fail.append(lambda m, rest, t: 503 if m == "POST" and "keepalive.yml" in rest else None)
        self.assertEqual(keepalive.run(gh), 1)
        self.assertEqual(self.log[-1], keepalive.STOPPED)
        self.assertGreaterEqual(clock.t - start, keepalive.HANDOFF_UNTIL)
        self.assertLess(clock.t - start, keepalive.HANDOFF_UNTIL + 120)
        self.assertIn("Tue Nov 3, 8:37 PM", stamps(gh.sent("update.yml")))   # still starting runs at the end

    def test_starts_again_only_every_few_minutes_when_the_run_doesnt_show(self):
        start = pt(2026, 11, 2, 9, 0)
        clock, gh = self.fake(start)
        gh.make_keepalive_runs = False
        self.assertEqual(keepalive.run(gh), 1)
        sent = [t - start for t, _ in gh.sent("keepalive.yml")]
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0], keepalive.MAX_RUN)
        self.assertTrue(all(b - a >= keepalive.REASK for a, b in zip(sent, sent[1:])), sent)
        self.assertLess(sent[-1], keepalive.HANDOFF_UNTIL)
        self.assertIn("never showed as queued", self.log[-1])

    def test_hand_over_when_listing_keepalive_runs_fails(self):
        clock, gh = self.fake(pt(2026, 11, 2, 9, 0))
        gh.fail.append(lambda m, rest, t: 500 if m == "GET" and rest.endswith("/keepalive.yml/runs") else None)
        self.assertEqual(keepalive.run(gh), 1)
        self.assertEqual(len(gh.sent("keepalive.yml")), 3)
        self.assertIn("never showed as queued", self.log[-1])

    def test_successor_seen_on_a_later_check_means_one_dispatch(self):
        clock, gh = self.fake(pt(2026, 11, 2, 9, 0))
        handoff = pt(2026, 11, 2, 14, 30)
        gh.fail.append(lambda m, rest, t: 500 if m == "GET" and rest.endswith("/keepalive.yml/runs")
                       and handoff <= t < handoff + 2 * keepalive.SHOW_WAIT else None)
        self.assertEqual(keepalive.run(gh), 0)
        self.assertEqual(len(gh.sent("keepalive.yml")), 1)
        self.assertLess(clock.t - handoff, keepalive.RETRY)      # seen on a later check of the same try

    def test_this_run_is_not_its_own_successor(self):
        clock, gh = self.fake(pt(2026, 11, 2, 9, 0))
        me = gh.add_run("keepalive.yml", pt(2026, 11, 2, 9, 0), status="in_progress")
        os.environ["GITHUB_RUN_ID"] = str(me["id"])
        gh.make_keepalive_runs = False
        self.assertEqual(keepalive.run(gh), 1)
        self.assertEqual(len(gh.sent("keepalive.yml")), 3)

    def test_carries_on_to_the_end_when_its_time_runs_out_just_before(self):
        clock, gh = self.fake(pt(2026, 11, 17, 1, 35))       # 5.5 hours later is 7:05 AM, 5 minutes before END
        self.assertEqual(keepalive.run(gh), 0)
        self.assertEqual(gh.sent("keepalive.yml"), [])
        self.assertIn("Tue Nov 17, 7:07 AM", stamps(gh.sent("update.yml")))
        self.assertEqual(self.log[-1], "7:10 AM: the counting schedule is over; stopping")

    def test_hand_over_failing_near_the_end_still_starts_the_last_read(self):
        # 5.5 hours end at 6:55 AM on Nov 17; the hand-over keeps failing, so the run carries on to END.
        clock, gh = self.fake(keepalive.END - keepalive.MAX_RUN - 15 * 60)
        gh.fail.append(lambda m, rest, t: 503 if m == "POST" and "keepalive.yml" in rest else None)
        self.assertEqual(keepalive.run(gh), 0)
        self.assertIn("Tue Nov 17, 7:07 AM", stamps(gh.sent("update.yml")))
        self.assertEqual(self.log[-1], "7:10 AM: the counting schedule is over; stopping")

    def test_main_crash_asks_again_when_the_fresh_run_doesnt_show(self):
        real_run, real_gh = keepalive.run, common.GitHub
        clock, gh = self.fake(pt(2026, 11, 3, 21, 0))
        gh.make_keepalive_runs = False

        def run(_gh):
            clock.t += 30 * 60
            raise KeyError("x")
        try:
            common.GitHub = lambda: gh
            gh.ready = lambda: True
            keepalive.run = run
            self.assertEqual(keepalive.main(), 1)
        finally:
            keepalive.run, common.GitHub = real_run, real_gh
        self.assertEqual(len(gh.sent("keepalive.yml")), 2)      # once, then again after REASK
        self.assertEqual(self.log[-1], keepalive.NEVER_SHOWED)

    def test_after_the_end_nothing_starts(self):
        clock, gh = self.fake(keepalive.END + 60)
        gh.add_run("update.yml", keepalive.END - 1000)
        self.assertEqual(keepalive.run(gh), 0)
        self.assertEqual(gh.dispatches, [])
        self.assertIn("The counting schedule is over", self.log[0])
        self.assertEqual(self.log[1], "API check OK: 1 recent Update results runs listed")

    def test_main_hands_over_after_a_late_crash_only(self):
        real_run, real_gh = keepalive.run, common.GitHub
        clock, gh = self.fake(pt(2026, 11, 3, 21, 0))

        def crash_after(minutes):
            def run(_gh):
                clock.t += minutes * 60
                raise KeyError("x")
            return run
        try:
            common.GitHub = lambda: gh
            gh.ready = lambda: True
            keepalive.run = crash_after(30)
            self.assertEqual(keepalive.main(), 1)
            self.assertEqual(len(gh.sent("keepalive.yml")), 1)
            keepalive.run = crash_after(1)
            self.assertEqual(keepalive.main(), 1)
            self.assertEqual(len(gh.sent("keepalive.yml")), 1)
        finally:
            keepalive.run, common.GitHub = real_run, real_gh
        self.assertTrue(any("Keep-alive stopped on an unexpected error (KeyError)" in x for x in self.log))

    def test_main_no_hand_over_after_a_crash_near_the_end(self):
        real_run, real_gh = keepalive.run, common.GitHub
        clock, gh = self.fake(keepalive.END - 30 * 60)

        def run(_gh):
            clock.t += 25 * 60
            raise KeyError("x")
        try:
            common.GitHub = lambda: gh
            gh.ready = lambda: True
            keepalive.run = run
            self.assertEqual(keepalive.main(), 1)
        finally:
            keepalive.run, common.GitHub = real_run, real_gh
        self.assertEqual(gh.sent("keepalive.yml"), [])


class WorkflowFile(unittest.TestCase):
    def test_keepalive_workflow(self):
        text = (WF / "keepalive.yml").read_text()
        push = re.search(r"\n  push:\n    branches: \[main\]\n    paths:\n((?:      - .+\n)+)", text)
        self.assertTrue(push, "push trigger")
        paths = re.findall(r"- (\S+)", push.group(1))
        self.assertEqual(sorted(paths), [".github/workflows/keepalive.yml", ".github/workflows/preflight.yml",
                                         ".github/workflows/update.yml", ".github/workflows/watchdog.yml",
                                         "ops/common.py", "ops/keepalive.py"])
        self.assertIn("workflow_dispatch:", text)
        self.assertRegex(text, r"concurrency:\n  group: keepalive\n  cancel-in-progress: false")
        self.assertRegex(text, r"permissions:\n  actions: write\n  contents: read")
        self.assertIn("if: vars.KEEPALIVE != 'off'", text)
        self.assertIn("run: python ops/keepalive.py", text)
        # The longest run must end well inside the job's timeout. One API call can take 43 s (two tries
        # of 20 s, 3 s apart); a step can make 4 calls per job (runs, cache, start), and a hand-over try
        # 1 start and SHOW_TRIES listings.
        timeout = int(re.search(r"timeout-minutes: (\d+)", text).group(1)) * 60
        call = 2 * 20 + 3
        step = 4 * 3 * call
        handover_try = 20 + keepalive.SHOW_TRIES * call + (keepalive.SHOW_TRIES - 1) * keepalive.SHOW_WAIT
        self.assertGreater(timeout, keepalive.HANDOFF_UNTIL + handover_try + step + 60)
        self.assertGreater(timeout, keepalive.MAX_RUN + keepalive.MIN_HANDOFF + step + 60)
        self.assertLessEqual(timeout, 360 * 60)

    def test_update_keepalive_input_exists(self):
        text = (WF / "update.yml").read_text()
        self.assertRegex(text, r"\n      keepalive:\n")
        self.assertIn("KEEPALIVE: ${{ inputs.keepalive }}", text)
        self.assertIn("restore-keys: last-fetch-", text)


if __name__ == "__main__":
    unittest.main()
