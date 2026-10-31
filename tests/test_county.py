"""Tests for the county reader (scraper/county.py) and the update run (scraper/update.py).

Run from the repo root:  python -m unittest discover -s tests
Standard library only. Nothing here reaches the network: every county fetch is a stand-in, and
urllib.request.urlopen is blocked for every run of update.py (in this process and in subprocesses).

Fixtures (tests/fixtures): county_page.html is a synthetic results page (the markup the county page
uses, with made-up numbers). The summary_*.xml files are the county's own summary XMLs, trimmed to a
few measures with their bytes kept (byte order mark, CRLF): summary_20_zero.xml is this election's
zero report (Oct 26), the others are past elections.
"""

import contextlib
import gzip
import http.client
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "scraper"))
import county  # noqa: E402
import update  # noqa: E402

PACIFIC = county.PACIFIC
TITLE = county.CONTEST_TITLE
PAGE = (FIXTURES / "county_page.html").read_text(encoding="utf-8")
ZERO_XML = (FIXTURES / "summary_20_zero.xml").read_bytes().decode("utf-8")  # keeps the byte order mark
LAST_MODIFIED = "Mon, 26 Oct 2026 18:48:46 GMT"  # summary_20.xml's header when it was saved (Oct 29)
T0 = datetime(2026, 11, 3, 20, 12, tzinfo=PACIFIC)  # election night, Tue 8:12 PM: the run after the first report
RUN_ENV = ("EVENT", "MANUAL_YES", "MANUAL_NO", "MANUAL_COUNTY_UPDATED", "ALLOW_DECREASE", "MARK_CERTIFIED",
           "GITHUB_OUTPUT", "PRACTICE", "KEEPALIVE", "CACHE_HIT")
START = {  # data/results.json as it stood before election night
    "status": "pre", "contest": TITLE, "threshold": 0.5, "expected_votes": None, "source_url": county.SOURCE_URL,
    "latest": {"yes": 0, "no": 0, "county_updated": "2026-10-26T11:48:45-07:00", "county_ballots_counted": 0,
               "fetched_at": "2026-10-28T22:00:21-07:00", "source": "county"},
}
START["history"] = [dict(START["latest"])]

# data/status.json reasons, word for word
NOT_RESPONDING = "The county's results site isn't responding to our automatic updates right now."
CANT_READ = "Our automatic reader can't read the county's results right now."
CONFIRMING = "The county revised its numbers. We're confirming the change before showing it."


def sub(text, old, new):
    assert text.count(old) == 1, f"fixture changed: {old!r} found {text.count(old)} times"
    return text.replace(old, new)


def page(yes="3,412", no="2,988", updated="11/3/2026 8:10:45 PM", ballots="98,450"):
    """The synthetic county page with other Measure Y numbers."""
    h = sub(PAGE, 'data-contestant="221" data-votes="true" class="content-data">3,412<',
            f'data-contestant="221" data-votes="true" class="content-data">{yes}<')
    h = sub(h, 'data-contestant="222" data-votes="true" class="content-data">2,988<',
            f'data-contestant="222" data-votes="true" class="content-data">{no}<')
    h = sub(h, ">11/3/2026 8:10:45 PM<", f">{updated}<")
    return sub(h, "<span>Ballots Counted</span>\n</div>\n<div style=\"display: inline-block;\">\n<span>98,450</span>",
               f"<span>Ballots Counted</span>\n</div>\n<div style=\"display: inline-block;\">\n<span>{ballots}</span>")


def renamed(html, new_title="City of Belmont, Measure Y"):
    return sub(html, f'<span class="race-title">{TITLE}</span>', f'<span class="race-title">{new_title}</span>')


def xml(yes=0, no=0, ballots=0, text=ZERO_XML):
    """The trimmed zero report with other Measure Y numbers."""
    for contestant, votes in (("221", yes), ("222", no)):
        text, n = re.subn(rf"(<ContestantID>{contestant}</ContestantID>.*?<TotalVotes>)[^<]*(</TotalVotes>)",
                          lambda m: f"{m.group(1)}{votes}{m.group(2)}", text, count=1, flags=re.S)
        assert n == 1
    return sub(text, "<TotalBallotsCast>0</TotalBallotsCast>", f"<TotalBallotsCast>{ballots}</TotalBallotsCast>")


def http_date(dt):
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def feed_xml(text, at):
    """What fetch_xml returns: the text, and headers with the file's time."""
    return text, {"Content-Type": "text/xml", "Last-Modified": http_date(at)}


def ts(dt):
    return int(dt.timestamp())


def pt(h, m, s=0):
    """A time on election night (Nov 3), Pacific."""
    return datetime(2026, 11, 3, h, m, s, tzinfo=PACIFIC)


def state(feed="html", retry=0, failures=0, last_ok=0, last_fail=None, pending=None):
    return {"feed": feed, "html_retry_after": retry, "failures": failures, "last_ok": last_ok,
            "last_fail": last_fail, "pending_decrease": pending}


def timeout():
    return urllib.error.URLError(socket.timeout("timed out"))


def http_error(code, url=county.SOURCE_URL):
    return urllib.error.HTTPError(url, code, "error", {}, None)


class NoNetwork(unittest.TestCase):
    def setUp(self):
        blocked = mock.patch("urllib.request.urlopen", side_effect=AssertionError("a test tried to use the network"))
        blocked.start()
        self.addCleanup(blocked.stop)


# ---------- county.py: the XML feed ----------

class ParseXmlTest(NoNetwork):
    def test_current_zero_report(self):
        r = county.parse_xml(ZERO_XML, {"Last-Modified": LAST_MODIFIED})
        self.assertEqual(r, {"contest": TITLE, "yes": 0, "no": 0, "county_updated": "2026-10-26T11:48:46-07:00",
                             "county_ballots_counted": 0})

    def test_same_shape_as_the_page_reader(self):
        self.assertEqual(set(county.parse_xml(ZERO_XML, {"Last-Modified": LAST_MODIFIED})), set(county.parse(PAGE)))

    def test_counts_come_through(self):
        r = county.parse_xml(xml(yes=3412, no=2988, ballots=98450), {"Last-Modified": LAST_MODIFIED})
        self.assertEqual((r["yes"], r["no"], r["county_ballots_counted"]), (3412, 2988, 98450))

    def test_past_elections(self):
        cases = [  # file, contest, YES, NO, TotalBallotsCast, GeneratedDate (seconds)
            ("summary_4_2020-11.xml", "City of San Mateo, Measure Y (Majority Approval Required)", 23038, 22995, 380193,
             "2020-12-03T11:09:25-08:00"),
            ("summary_4_2020-11.xml", "Proposition 14 (Majority Approval Required)", 207225, 147817, 380193,
             "2020-12-03T11:09:25-08:00"),
            ("summary_11_2022-11.xml", "City of Belmont, Measure K (Majority Approval Required)", 8352, 2180, 252233,
             "2022-12-08T15:44:17-08:00"),
            ("summary_11_2022-11.xml", "City of Pacifica, Measure Y (Majority Approval Required)", 9577, 6554, 252233,
             "2022-12-08T15:44:17-08:00"),
            ("summary_16_2024-11.xml", "City of Belmont, Measure DD (Majority Approval Required)", 10945, 1980, 337241,
             "2024-12-04T19:42:25-08:00"),
            ("summary_16_2024-11.xml", "Woodside Elementary School District, Measure Y (2/3 Approval Required)", 1536, 449,
             337241, "2024-12-04T19:42:25-08:00"),
            ("summary_17_2025-04.xml", "County of San Mateo, Measure A (Majority Approval Required)", 90900, 17318, 108346,
             "2025-04-03T15:19:12-07:00"),
        ]
        for name, title, yes, no, ballots, generated in cases:
            with self.subTest(name=name, contest=title):
                text = (FIXTURES / name).read_bytes().decode("utf-8")
                r = county.parse_xml(text, None, contest_title=title)
                self.assertEqual(r, {"contest": title, "yes": yes, "no": no, "county_updated": generated,
                                     "county_ballots_counted": ballots})

    def test_rows_must_be_exactly_yes_and_no(self):
        text = (FIXTURES / "summary_16_2024-11.xml").read_bytes().decode("utf-8")
        title = "Belmont-Redwood Shores School District, Measure P (55 Percent Approval Required)"  # BONDS YES / BONDS NO
        with self.assertRaisesRegex(county.ParseError, r"expected one YES and one NO row, found \['BONDS NO', 'BONDS YES'\]"):
            county.parse_xml(text, None, contest_title=title)

    def test_last_modified_is_preferred_over_generated_date(self):
        self.assertEqual(county.parse_xml(ZERO_XML, {"Last-Modified": LAST_MODIFIED})["county_updated"],
                         "2026-10-26T11:48:46-07:00")  # the page said 11:48:45 AM; GeneratedDate says 11:41:33
        self.assertEqual(county.parse_xml(ZERO_XML, {"last-modified": LAST_MODIFIED})["county_updated"],
                         "2026-10-26T11:48:46-07:00")  # any header case
        message = http.client.HTTPMessage()  # what urllib's response headers are
        message["Last-Modified"] = LAST_MODIFIED
        self.assertEqual(county.parse_xml(ZERO_XML, message)["county_updated"], "2026-10-26T11:48:46-07:00")

    def test_generated_date_when_last_modified_is_missing_or_broken(self):
        for headers in (None, {}, {"Last-Modified": "yesterday"}, {"Content-Type": "text/xml"}):
            with self.subTest(headers=headers):
                self.assertEqual(county.parse_xml(ZERO_XML, headers)["county_updated"], "2026-10-26T11:41:33-07:00")

    def test_times_are_pacific_after_daylight_saving_ends(self):
        r = county.parse_xml(ZERO_XML, {"Last-Modified": "Wed, 04 Nov 2026 04:10:46 GMT"})
        self.assertEqual(r["county_updated"], "2026-11-03T20:10:46-08:00")

    def test_no_time_at_all(self):
        text = re.sub(r"\s*<GeneratedDate>[^<]*</GeneratedDate>", "", ZERO_XML)
        with self.assertRaisesRegex(county.ParseError, "no usable Last-Modified header and no GeneratedDate"):
            county.parse_xml(text, None)
        text = sub(ZERO_XML, "<GeneratedDate>2026-10-26T11:41:33.447-07:00</GeneratedDate>",
                   "<GeneratedDate>Oct 26</GeneratedDate>")
        with self.assertRaisesRegex(county.ParseError, "XML GeneratedDate not understood: 'Oct 26'"):
            county.parse_xml(text, None)

    def test_byte_order_mark(self):
        self.assertTrue(ZERO_XML.startswith("\ufeff"))
        for text in (ZERO_XML, ZERO_XML.lstrip("\ufeff")):
            self.assertEqual(county.parse_xml(text, {"Last-Modified": LAST_MODIFIED})["yes"], 0)

    def test_missing_contest(self):
        text = ZERO_XML.replace(f"<RaceName>{TITLE}</RaceName>", "<RaceName>City of Belmont, Measure Y</RaceName>")
        with self.assertRaisesRegex(county.ParseError, "expected one contest titled .*, found 0"):
            county.parse_xml(text, {"Last-Modified": LAST_MODIFIED})

    def test_duplicate_contest(self):
        block = re.search(r"  <Table>\r?\n    <ElectionID>20</ElectionID>\r?\n    <RaceID>116</RaceID>.*?</Table>\r?\n",
                          ZERO_XML, re.S).group(0)
        copy = block.replace("<RaceID>116</RaceID>", "<RaceID>916</RaceID>")
        with self.assertRaisesRegex(county.ParseError, "expected one contest titled .*, found 2"):
            county.parse_xml(ZERO_XML.replace(block, block + copy), {"Last-Modified": LAST_MODIFIED})

    def test_duplicate_yes_row(self):
        block = re.search(r"  <Table>\r?\n(?:(?!</Table>).)*?<ContestantID>221</ContestantID>.*?</Table>\r?\n",
                          ZERO_XML, re.S).group(0)
        with self.assertRaisesRegex(county.ParseError, r"expected one YES and one NO row, found \['NO', 'YES', 'YES'\]"):
            county.parse_xml(ZERO_XML.replace(block, block + block), {"Last-Modified": LAST_MODIFIED})

    def test_missing_yes_row(self):
        block = re.search(r"  <Table>\r?\n(?:(?!</Table>).)*?<ContestantID>221</ContestantID>.*?</Table>\r?\n",
                          ZERO_XML, re.S).group(0)
        with self.assertRaisesRegex(county.ParseError, r"expected one YES and one NO row, found \['NO'\]"):
            county.parse_xml(ZERO_XML.replace(block, ""), {"Last-Modified": LAST_MODIFIED})

    def test_votes_must_be_whole_numbers(self):
        for bad in ("-", "12.5", "1,2x", ""):
            with self.subTest(votes=bad):
                text = re.sub(r"(<ContestantID>221</ContestantID>.*?<TotalVotes>)[^<]*(</TotalVotes>)",
                              lambda m: m.group(1) + bad + m.group(2), ZERO_XML, count=1, flags=re.S)
                with self.assertRaisesRegex(county.ParseError, "YES votes is not a whole number"):
                    county.parse_xml(text, {"Last-Modified": LAST_MODIFIED})
        text = re.sub(r"(<ContestantID>222</ContestantID>.*?)<TotalVotes>[^<]*</TotalVotes>", r"\1", ZERO_XML,
                      count=1, flags=re.S)
        with self.assertRaisesRegex(county.ParseError, "NO votes is not a whole number: ''"):
            county.parse_xml(text, {"Last-Modified": LAST_MODIFIED})
        text = sub(ZERO_XML, "<TotalBallotsCast>0</TotalBallotsCast>", "<TotalBallotsCast>-</TotalBallotsCast>")
        with self.assertRaisesRegex(county.ParseError, "TotalBallotsCast is not a whole number"):
            county.parse_xml(text, {"Last-Modified": LAST_MODIFIED})

    def test_missing_ballots_cast(self):
        text = sub(ZERO_XML, "<TotalBallotsCast>0</TotalBallotsCast>", "")
        with self.assertRaisesRegex(county.ParseError, "'TotalBallotsCast' not found"):
            county.parse_xml(text, {"Last-Modified": LAST_MODIFIED})

    def test_not_xml(self):
        for text in ("<html><head><title>Service Unavailable</title></head><body><p>Busy<br></body></html>", "", "\ufeff"):
            with self.subTest(text=text), self.assertRaisesRegex(county.ParseError, "not well-formed"):
                county.parse_xml(text, {"Last-Modified": LAST_MODIFIED})

    def test_refuses_entity_declarations(self):
        evil = ('<?xml version="1.0"?><!DOCTYPE NewDataSet [<!ENTITY e SYSTEM "file:///etc/hostname">]>'
                "<NewDataSet><TotalBallotsCast>&e;</TotalBallotsCast></NewDataSet>")
        with self.assertRaisesRegex(county.ParseError, "DOCTYPE or ENTITY"):
            county.parse_xml(evil, {"Last-Modified": LAST_MODIFIED})
        laughs = '<!DOCTYPE d [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><NewDataSet>&b;</NewDataSet>'
        with self.assertRaisesRegex(county.ParseError, "DOCTYPE or ENTITY"):
            county.parse_xml(laughs, {"Last-Modified": LAST_MODIFIED})

    def test_a_namespace_is_ignored(self):
        text = sub(ZERO_XML, "<NewDataSet>", '<NewDataSet xmlns="http://example.org/results">')
        self.assertEqual(county.parse_xml(text, {"Last-Modified": LAST_MODIFIED})["county_updated"],
                         "2026-10-26T11:48:46-07:00")

    def test_county_time_round_trips_through_manual_entry(self):
        for dt in (datetime(2026, 11, 3, 20, 10, 45, tzinfo=PACIFIC), datetime(2026, 11, 4, 0, 5, 9, tzinfo=PACIFIC),
                   datetime(2026, 11, 9, 12, 0, 0, tzinfo=PACIFIC), datetime(2026, 10, 26, 11, 48, 46, tzinfo=PACIFIC)):
            with self.subTest(dt=dt):
                self.assertEqual(update.parse_time(county.county_time(dt)), dt)
        self.assertEqual(county.county_time(datetime(2026, 11, 3, 20, 10, 45, tzinfo=PACIFIC)), "11/3/2026 8:10:45 PM")

    def test_like_measure_y(self):
        for title, want in (("City of Belmont, Measure Y", True), ("CITY OF BELMONT MEASURE Y", True), (TITLE, True),
                            ("City of San Mateo, Measure Y (Majority Approval Required)", False),
                            ("Belmont-Redwood Shores School District, Measure P", False),
                            ("City of Belmont, Measure YY", False)):
            with self.subTest(title=title):
                self.assertIs(county.like_measure_y(title), want)

    def test_inspect_never_raises(self):
        for text in ("", "\ufeff", "<<<>>>", "<!DOCTYPE x>", "<NewDataSet><Table><RaceName>", ZERO_XML[:500]):
            with self.subTest(text=text[:20]):
                county.inspect(text)
                county.inspect_xml(text, None)


class FakeResponse:
    def __init__(self, body, headers):
        self.body = body
        self.headers = http.client.HTTPMessage()
        for k, v in headers.items():
            self.headers[k] = v

    def read(self, size=-1):
        if size < 0:
            size = len(self.body)
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetchTest(unittest.TestCase):
    def fake(self, body, headers):
        seen = []

        def urlopen(req, timeout=None):
            seen.append((req, timeout))
            return FakeResponse(body, headers)
        return seen, mock.patch("urllib.request.urlopen", urlopen)

    def test_fetch_xml_asks_for_gzip_only_and_names_the_project(self):
        raw = (FIXTURES / "summary_20_zero.xml").read_bytes()
        seen, patch = self.fake(gzip.compress(raw), {"Content-Type": "text/xml", "Content-Encoding": "gzip",
                                                     "Last-Modified": LAST_MODIFIED})
        with patch:
            text, headers = county.fetch_xml()
        req, timeout = seen[0]
        self.assertEqual(len(seen), 1)
        self.assertEqual(req.full_url, "https://www.livevoterturnout.com/ENR/sanmateocaenr/20/summary_20.xml")
        self.assertEqual(req.get_header("User-agent"), "measure-y-watch (+https://github.com/PleaseRuiz/measure-y-watch)")
        self.assertEqual(req.get_header("Accept-encoding"), "gzip")
        self.assertEqual(timeout, 60)
        self.assertEqual(county.parse_xml(text, headers)["county_updated"], "2026-10-26T11:48:46-07:00")

    def test_fetch_xml_without_compression(self):
        raw = (FIXTURES / "summary_20_zero.xml").read_bytes()
        seen, patch = self.fake(raw, {"Content-Type": "text/xml", "Last-Modified": LAST_MODIFIED})
        with patch:
            text, headers = county.fetch_xml(timeout=5)
        self.assertEqual(seen[0][1], 5)
        self.assertEqual(county.parse_xml(text, headers)["contest"], TITLE)

    def test_byte_order_mark_decides_the_encoding(self):
        raw = (FIXTURES / "summary_20_zero.xml").read_bytes()
        seen, patch = self.fake(raw, {"Content-Type": "text/xml; charset=iso-8859-1"})
        with patch:
            text, _ = county.fetch_xml()
        self.assertTrue(text.startswith("\ufeff<?xml"))

    def test_refuses_compression_it_cannot_read(self):
        for fetch in (county.fetch, county.fetch_xml):
            seen, patch = self.fake(b"\x8b\x05\x80", {"Content-Encoding": "br"})
            with self.subTest(fetch=fetch.__name__), patch, self.assertRaisesRegex(county.ParseError, "'br'-compressed"):
                fetch()

    def test_a_reply_that_trickles_in_times_out(self):
        raw = (FIXTURES / "summary_20_zero.xml").read_bytes() * 30  # more than one 64 KB chunk
        clock = iter(range(0, 10000, 50))  # each look at the clock is 50 s later
        for fetch in (county.fetch, county.fetch_xml):
            seen, patch = self.fake(raw, {"Content-Type": "text/xml"})
            with self.subTest(fetch=fetch.__name__), patch, mock.patch("time.monotonic", lambda: next(clock)), \
                    self.assertRaisesRegex(TimeoutError, "took more than 120 s"):
                fetch()
        self.assertEqual(update.fetch_problem(TimeoutError("the response took more than 120 s")), "timed out")

    def test_a_reply_cut_short_is_an_error(self):
        class Socket:  # a whole HTTP reply, already received
            def __init__(self, data):
                self.data = data

            def makefile(self, *args, **kwargs):
                return io.BytesIO(self.data)

        def response(data):
            res = http.client.HTTPResponse(Socket(data), method="GET")
            res.begin()
            return res
        body = b"x" * 150000
        whole = b"HTTP/1.1 200 OK\r\nContent-Length: 150000\r\n\r\n" + body
        self.assertEqual(county._read_all(response(whole), 120), body)
        with self.assertRaises(http.client.IncompleteRead):
            county._read_all(response(whole[:-1000]), 120)
        chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + b"".join(
            b"%x\r\n%s\r\n" % (len(body[i:i + 70000]), body[i:i + 70000]) for i in range(0, len(body), 70000)) + b"0\r\n\r\n"
        self.assertEqual(county._read_all(response(chunked), 120), body)
        with self.assertRaises(http.client.IncompleteRead):
            county._read_all(response(chunked[:-5000]), 120)
        self.assertEqual(update.fetch_problem(http.client.IncompleteRead(b"", 1000)), "connection problem (IncompleteRead)")

    def test_page_fetch_unchanged(self):
        seen, patch = self.fake(gzip.compress(PAGE.encode("utf-8")), {"Content-Type": "text/html; charset=utf-8",
                                                                     "Content-Encoding": "gzip"})
        with patch:
            html = county.fetch()
        self.assertEqual(seen[0][0].full_url, county.SOURCE_URL)
        self.assertEqual(seen[0][0].get_header("Accept-encoding"), "gzip")
        self.assertEqual(county.parse(html)["yes"], 3412)


class PageFixtureTest(NoNetwork):
    def test_synthetic_page_reads_like_the_county_page(self):
        self.assertEqual(county.parse(PAGE), {"contest": TITLE, "yes": 3412, "no": 2988,
                                              "county_updated": "2026-11-03T20:10:45-08:00",
                                              "county_ballots_counted": 98450})

    def test_broken_variants_fail(self):
        no_yes = re.sub(r'<tr class="content-row"><td style="width: 35px;"><div class="contestant-color"></div></td>'
                        r'<td data-race="116" data-contestant="221".*?</tr>', "", PAGE, count=1, flags=re.S)
        cases = {
            "expected one contest titled": renamed(PAGE),
            r"expected YES and NO rows, found \['NO'\]": no_yes,
            "YES votes is not a whole number": page(yes="3,41x"),
            "'Website Updated' time not understood": page(updated="Nov 3, 2026 8:10 PM"),
            "'Ballots Counted' not found": PAGE.replace("<span>Ballots Counted</span>", "<span>Ballots Cast</span>"),
        }
        for message, html in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(county.ParseError, message):
                county.parse(html)


# ---------- update.py: one run at a time ----------

class FakeCounty:
    """Stands in for the county site: what each feed returns (or raises) next, and every fetch made."""

    def __init__(self):
        self.html = PAGE
        self.xml = feed_xml(ZERO_XML, datetime(2026, 10, 26, 11, 48, 46, tzinfo=PACIFIC))
        self.calls = []

    def fetch(self):
        self.calls.append("html")
        if isinstance(self.html, BaseException):
            raise self.html
        return self.html

    def fetch_xml(self):
        self.calls.append("xml")
        if isinstance(self.xml, BaseException):
            raise self.xml
        return self.xml


class RunTest(NoNetwork):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "data").mkdir()
        (self.dir / "data" / "results.json").write_text(json.dumps(START, indent=2) + "\n")
        (self.dir / "config.json").write_text('{"expected_votes": null}\n')
        paths = {"RESULTS": "data/results.json", "STATUS": "data/status.json", "CONFIG": "config.json",
                 "LAST_FETCH": ".last-fetch", "STATE": ".county-state.json", "DIAGNOSIS": "diagnosis.md"}
        for name, rel in paths.items():
            p = mock.patch.object(update, name, self.dir / rel)
            p.start()
            self.addCleanup(p.stop)
        self.county = FakeCounty()
        for name in ("fetch", "fetch_xml"):
            p = mock.patch.object(county, name, getattr(self.county, name))
            p.start()
            self.addCleanup(p.stop)

    def run_update(self, at, **env):
        """One run of update.py at `at`. Returns (exit code, workflow outputs, log)."""
        outputs = self.dir / "github_output"
        outputs.write_text("")
        environ = {k: v for k, v in os.environ.items() if k not in RUN_ENV}
        environ.update({"EVENT": "schedule", "GITHUB_OUTPUT": str(outputs)}, **env)
        log = io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=True), contextlib.redirect_stdout(log), \
                contextlib.redirect_stderr(log):
            code = update.main(now=at)
        lines = outputs.read_text().splitlines()
        out = dict(line.split("=", 1) for line in lines)
        self.assertEqual(len(out), len(lines), "an output was written twice")
        return code, out, log.getvalue()

    def results(self):
        return json.loads((self.dir / "data" / "results.json").read_text())

    def state(self):
        return json.loads((self.dir / ".county-state.json").read_text())

    def set_state(self, **fields):
        (self.dir / ".county-state.json").write_text(json.dumps(state(**fields)))

    def status(self):
        path = self.dir / "data" / "status.json"
        return json.loads(path.read_text()) if path.exists() else None

    def set_status(self, state_, reason, since):
        (self.dir / "data" / "status.json").write_text(json.dumps({"state": state_, "reason": reason, "since": since}))

    def diagnosis(self):
        return (self.dir / "diagnosis.md").read_text()

    def assertRun(self, result, code, read, feed, alert):
        self.assertEqual((result[0], result[1]["read"], result[1]["feed"], result[1]["alert"]), (code, read, feed, alert),
                         result[2])


class FeedStateTest(RunTest):
    def test_html_ok_then_xml_after_a_failure_then_back(self):
        # 1. The results page reads.
        t1 = T0
        self.assertRun(self.run_update(t1), 0, "ok", "html", "no")
        self.assertEqual(self.state(), state(last_ok=ts(t1)))
        latest = self.results()["latest"]
        self.assertEqual((latest["yes"], latest["no"], latest["source"], latest["feed"]), (3412, 2988, "county", "html"))

        # 2. The page changes and can't be read: this run fails; the next one reads the XML.
        t2 = t1 + timedelta(minutes=5)
        self.county.html = renamed(page(yes="4,105", no="3,702", updated="11/3/2026 8:15:09 PM", ballots="121,880"))
        self.assertRun(self.run_update(t2), 1, "failed", "html", "wait")
        self.assertEqual(self.state(), state("xml", ts(t2) + 1800, 1, ts(t1), {"feed": "html", "kind": "parse", "at": ts(t2)}))
        self.assertEqual(self.results()["latest"]["yes"], 3412)  # nothing saved

        # 3. The XML has the new county update.
        t3 = t2 + timedelta(minutes=5)
        self.county.xml = feed_xml(xml(yes=4105, no=3702, ballots=121880), pt(20, 15, 10))
        code, out, log = self.run_update(t3)
        self.assertRun((code, out, log), 0, "ok", "xml", "no")
        self.assertEqual(out["changed"], "true")
        self.assertEqual(out["message"], "Update results: county reading (XML feed), county update 2026-11-03T20:15:10-08:00")
        latest = self.results()["latest"]
        self.assertEqual((latest["yes"], latest["no"], latest["county_ballots_counted"], latest["feed"], latest["source"]),
                         (4105, 3702, 121880, "xml", "county"))
        self.assertEqual(self.state(), state("xml", ts(t2) + 1800, 0, ts(t3), {"feed": "html", "kind": "parse", "at": ts(t2)}))

        # 4. Still on the XML; nothing new.
        t4 = t3 + timedelta(minutes=5)
        code, out, log = self.run_update(t4)
        self.assertRun((code, out, log), 0, "ok", "xml", "no")
        self.assertEqual(out["changed"], "false")

        # 5. 30 minutes after the switch, one run tries the page again; it reads, so it's back on the page.
        t5 = t2 + timedelta(minutes=30)
        self.county.html = page(yes="4,105", no="3,702", updated="11/3/2026 8:15:09 PM", ballots="121,880")
        code, out, log = self.run_update(t5)
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertEqual(out["changed"], "false")  # the same county update the XML gave, a second earlier
        self.assertEqual(self.state(), state(last_ok=ts(t5), last_fail={"feed": "html", "kind": "parse", "at": ts(t2)}))

        # 6. And stays there.
        self.run_update(t5 + timedelta(minutes=5))
        self.assertEqual(self.county.calls, ["html", "html", "xml", "xml", "html", "html"])
        self.assertEqual([h.get("feed") for h in self.results()["history"]], [None, "html", "xml"])
        self.assertIsNone(self.status())  # one failure at a time never changed what readers see

    def test_page_fetch_failure_also_switches(self):
        self.county.html = http_error(503)
        self.assertRun(self.run_update(T0), 1, "failed", "html", "wait")
        self.assertEqual(self.state(), state("xml", ts(T0) + 1800, 1, 0, {"feed": "html", "kind": "fetch", "at": ts(T0)}))
        self.assertRun(self.run_update(T0 + timedelta(minutes=5)), 0, "ok", "xml", "no")

    def test_page_still_broken_at_the_retry_stays_on_xml(self):
        self.set_state(feed="xml", retry=ts(T0), last_ok=ts(T0) - 600)
        self.county.html = renamed(PAGE)
        self.assertRun(self.run_update(T0), 1, "failed", "html", "wait")
        # The failed retry doesn't count as a failed read: the XML is still reading.
        self.assertEqual(self.state(), state("xml", ts(T0) + 1800, 0, ts(T0) - 600, {"feed": "html", "kind": "parse",
                                                                                    "at": ts(T0)}))
        self.assertIn("- Readers: the site still shows the last saved numbers\n", self.diagnosis() + "\n")
        self.assertRun(self.run_update(T0 + timedelta(minutes=5)), 0, "ok", "xml", "no")
        self.assertEqual(self.county.calls, ["html", "xml"])

    def test_one_xml_failure_reads_the_xml_again(self):
        self.set_state(feed="xml", retry=ts(T0) + 1200, last_ok=ts(T0) - 300)
        self.county.html = renamed(PAGE)  # the page is still unreadable
        self.county.xml = timeout()
        self.assertRun(self.run_update(T0), 1, "failed", "xml", "wait")
        self.assertEqual(self.state(), state("xml", ts(T0) + 1200, 1, ts(T0) - 300, {"feed": "xml", "kind": "fetch", "at": ts(T0)}))
        self.county.xml = feed_xml(xml(yes=4105, no=3702, ballots=121880), pt(20, 15, 10))
        self.assertRun(self.run_update(T0 + timedelta(minutes=5)), 0, "ok", "xml", "no")
        self.assertEqual(self.county.calls, ["xml", "xml"])
        self.assertIsNone(self.status())  # the backup kept working: no banner

    def test_an_xml_failure_right_after_another_failed_read_goes_back_to_the_page(self):
        self.set_state(feed="xml", retry=ts(T0) + 1200, failures=1, last_ok=ts(T0) - 600)
        self.county.xml = http_error(404, county.XML_URL)
        self.assertRun(self.run_update(T0), 1, "failed", "xml", "now")
        self.assertEqual(self.state(), state("html", 0, 2, ts(T0) - 600, {"feed": "xml", "kind": "fetch", "at": ts(T0)}))
        self.assertRun(self.run_update(T0 + timedelta(minutes=5)), 0, "ok", "html", "no")
        self.assertEqual(self.county.calls, ["xml", "html"])

    def test_xml_that_parses_but_is_wrong_counts_as_an_xml_failure(self):
        self.set_state(feed="xml", retry=ts(T0) + 1200)
        self.county.xml = feed_xml(xml(yes=0, no=0).replace(TITLE, "City of Belmont, Measure Y"), T0)
        self.assertRun(self.run_update(T0), 1, "failed", "xml", "wait")
        self.assertEqual((self.state()["feed"], self.state()["failures"], self.state()["last_fail"]["kind"]),
                         ("xml", 1, "parse"))

    def test_unreadable_state_counts_as_the_default(self):
        for junk in ("", "not json", "[]", '{"feed": "ftp", "failures": "many", "last_ok": -5}', '{"html_retry_after": NaN}',
                     '{"last_fail": {"feed": "html", "kind": "oops", "at": 5}}',
                     '{"last_fail": "yesterday", "pending_decrease": {"yes": -1, "no": 2, "county_updated": "x", "seen_at": 1}}',
                     '{"pending_decrease": {"yes": 1, "no": 2, "county_updated": "2026-11-03T20:15:10", "seen_at": 1}}',
                     '{"pending_decrease": {"yes": true, "no": 2, "county_updated": "2026-11-03T20:15:10-08:00", "seen_at": 1}}'):
            with self.subTest(state=junk):
                (self.dir / ".county-state.json").write_text(junk)
                self.assertEqual(update.load_state(), update.DEFAULT_STATE)

    def test_valid_saved_fields_are_kept(self):
        saved = state("xml", 1762230000, 3, 1762229000, {"feed": "xml", "kind": "fetch", "at": 1762229500},
                      {"yes": 3400, "no": 2988, "county_updated": "2026-11-03T20:15:10-08:00", "seen_at": 1762229400})
        (self.dir / ".county-state.json").write_text(json.dumps(dict(saved, extra="ignored")))
        self.assertEqual(update.load_state(), saved)

    def test_retry_time_impossibly_far_off_counts_as_due(self):
        self.set_state(feed="xml", retry=ts(T0) + 30 * 86400)
        self.assertRun(self.run_update(T0), 0, "ok", "html", "no")

    def test_unexpected_reader_crash_counts_as_a_parse_failure(self):
        with mock.patch.object(county, "parse", side_effect=TypeError("boom")):
            code, out, log = self.run_update(T0)
        self.assertRun((code, out, log), 1, "failed", "html", "wait")
        self.assertEqual((self.state()["feed"], self.state()["last_fail"]["kind"]), ("xml", "parse"))
        self.assertIn("the reader stopped on an unexpected TypeError", self.diagnosis())

    def test_compression_the_reader_cannot_unpack_is_a_parse_failure(self):
        self.county.html = county.ParseError("the county server sent 'br'-compressed data, which this reader can't unpack")
        self.assertRun(self.run_update(T0), 1, "failed", "html", "wait")
        self.assertEqual(self.state()["last_fail"], {"feed": "html", "kind": "parse", "at": ts(T0)})
        self.assertIn("county page couldn't be read: the county server sent 'br'-compressed data", self.diagnosis())


class OneFetchTest(RunTest):
    def test_one_fetch_per_run_and_the_gap_guard(self):
        self.assertRun(self.run_update(T0), 0, "ok", "html", "no")
        self.assertEqual((self.dir / ".last-fetch").read_text(), f"{ts(T0)}\n")
        saved = (self.dir / ".county-state.json").read_text()
        for seconds in (1, 60, 239):
            with self.subTest(seconds=seconds):
                code, out, log = self.run_update(T0 + timedelta(seconds=seconds))
                self.assertRun((code, out, log), 0, "skipped", "none", "no")
                self.assertEqual((out["changed"], out["status_changed"]), ("false", "false"))
                self.assertIn("Skipped fetch", log)
        self.assertEqual(self.county.calls, ["html"])
        self.assertEqual((self.dir / ".last-fetch").read_text(), f"{ts(T0)}\n")
        self.assertEqual((self.dir / ".county-state.json").read_text(), saved)
        self.run_update(T0 + timedelta(seconds=240))
        self.assertEqual(self.county.calls, ["html", "html"])
        self.assertEqual((self.dir / ".last-fetch").read_text(), f"{ts(T0) + 240}\n")

    def test_a_failed_read_does_not_try_the_other_feed_in_the_same_run(self):
        self.county.html = timeout()
        self.assertRun(self.run_update(T0), 1, "failed", "html", "wait")
        self.assertEqual(self.county.calls, ["html"])
        self.assertEqual((self.dir / ".last-fetch").read_text(), f"{ts(T0)}\n")  # saved before the fetch
        # A hand-started run 2 minutes later is still held back by the gap guard.
        self.assertRun(self.run_update(T0 + timedelta(minutes=2), EVENT="workflow_dispatch"), 0, "skipped", "none", "no")
        self.assertEqual(self.county.calls, ["html"])
        self.run_update(T0 + timedelta(minutes=5))
        self.assertEqual(self.county.calls, ["html", "xml"])

    def test_a_saved_fetch_time_far_in_the_future_is_ignored(self):
        (self.dir / ".last-fetch").write_text(f"{ts(T0) + 86400}\n")
        self.assertRun(self.run_update(T0), 0, "ok", "html", "no")
        (self.dir / ".last-fetch").write_text(f"{ts(T0) + 400 + 100}\n")  # still ahead after a fetch 400 s later
        self.assertRun(self.run_update(T0 + timedelta(seconds=400)), 0, "skipped", "none", "no")
        (self.dir / ".last-fetch").write_text("inf\n")
        self.assertRun(self.run_update(T0 + timedelta(minutes=10)), 0, "ok", "html", "no")
        self.assertEqual(self.county.calls, ["html", "html"])

    def test_a_held_decrease_waits_for_the_gap_guard_too(self):
        self.run_update(T0)
        self.county.html = page(yes="3,400", updated="11/3/2026 8:15:10 PM")
        self.assertRun(self.run_update(T0 + timedelta(minutes=5)), 0, "hold", "html", "no")
        code, out, log = self.run_update(T0 + timedelta(minutes=7))  # too soon: no second read, nothing saved
        self.assertRun((code, out, log), 0, "skipped", "none", "no")
        self.assertEqual((self.results()["latest"]["yes"], self.status()["state"]), (3412, "confirming"))
        self.assertEqual(self.county.calls, ["html", "html"])


    def test_a_keepalive_run_without_the_cache_skips_the_county(self):
        # Without .last-fetch from the cache the gap guard can't work, so only scheduled runs read.
        code, out, log = self.run_update(T0, EVENT="workflow_dispatch", KEEPALIVE="true", CACHE_HIT="false")
        self.assertRun((code, out, log), 0, "skipped", "none", "no")
        self.assertIn("only scheduled runs read the county", log)
        self.assertEqual(self.county.calls, [])
        self.assertRun(self.run_update(T0, EVENT="workflow_dispatch", KEEPALIVE="true", CACHE_HIT="true"),
                       0, "ok", "html", "no")
        self.assertRun(self.run_update(T0 + timedelta(minutes=5), CACHE_HIT="false"), 0, "ok", "html", "no")
        self.assertRun(self.run_update(T0 + timedelta(minutes=10), EVENT="workflow_dispatch", CACHE_HIT="false"),
                       0, "ok", "html", "no")  # a hand-started run reads too
        self.assertEqual(self.county.calls, ["html", "html", "html"])

    def test_the_failure_output_tells_a_county_read_from_the_rest(self):
        code, out, log = self.run_update(T0)
        self.assertEqual((code, out["failure"]), (0, "none"))
        self.county.html = http_error(503)
        code, out, log = self.run_update(T0 + timedelta(minutes=5))
        self.assertEqual((code, out["failure"], out["alert"]), (1, "read", "wait"))
        (self.dir / "config.json").write_text("{not json")
        code, out, log = self.run_update(T0 + timedelta(minutes=10))
        self.assertEqual((code, out["failure"], out["alert"]), (1, "run", "now"))

class StatusTest(RunTest):
    def test_a_good_read_leaves_a_missing_status_missing(self):
        code, out, log = self.run_update(T0)
        self.assertEqual((out["changed"], out["status_changed"]), ("true", "false"))
        self.assertIsNone(self.status())  # missing means ok

    def test_one_failure_does_not_change_it(self):
        self.county.html = http_error(503)
        code, out, log = self.run_update(T0)
        self.assertEqual((code, out["status_changed"], out["changed"]), (1, "false", "false"))
        self.assertNotIn("message", out)
        self.assertIsNone(self.status())
        # Nor an existing status.
        for i, (st, reason) in enumerate((("ok", ""), ("confirming", CONFIRMING), ("broken", CANT_READ))):
            with self.subTest(status=st):
                self.set_state()
                self.set_status(st, reason, "2026-11-03T20:00:00-08:00")
                self.county.html = renamed(PAGE)
                code, out, log = self.run_update(T0 + timedelta(minutes=10 * (i + 1)))
                self.assertEqual((code, out["read"], out["status_changed"]), (1, "failed", "false"))
                self.assertEqual(self.status(), {"state": st, "reason": reason, "since": "2026-11-03T20:00:00-08:00"})

    def test_two_fetch_failures_in_a_row_break_it(self):
        self.county.html = http_error(503)
        self.county.xml = timeout()
        self.assertRun(self.run_update(T0), 1, "failed", "html", "wait")
        code, out, log = self.run_update(T0 + timedelta(minutes=5))
        self.assertRun((code, out, log), 1, "failed", "xml", "now")
        self.assertEqual((out["status_changed"], out["changed"], out["message"]), ("true", "false", "Update site status: broken"))
        self.assertEqual(self.status(), {"state": "broken", "reason": NOT_RESPONDING, "since": "2026-11-03T20:17:00-08:00"})
        self.assertIn("Site status: broken", log)
        # A third failure: still broken, nothing to write.
        code, out, log = self.run_update(T0 + timedelta(minutes=10))
        self.assertRun((code, out, log), 1, "failed", "html", "now")
        self.assertEqual(out["status_changed"], "false")
        self.assertEqual(self.state()["failures"], 3)

    def test_two_format_failures_break_it_with_the_other_reason(self):
        self.county.html = renamed(PAGE)
        self.county.xml = feed_xml(xml().replace(TITLE, "City of Belmont, Measure Y"), T0)
        self.run_update(T0)
        code, out, log = self.run_update(T0 + timedelta(minutes=5))
        self.assertRun((code, out, log), 1, "failed", "xml", "now")
        self.assertEqual(self.status(), {"state": "broken", "reason": CANT_READ, "since": "2026-11-03T20:17:00-08:00"})

    def test_a_new_reason_needs_two_failed_reads_in_a_row_that_agree(self):
        self.county.html = http_error(503)
        self.county.xml = timeout()
        self.run_update(T0)
        self.run_update(T0 + timedelta(minutes=5))
        self.assertEqual(self.status()["reason"], NOT_RESPONDING)
        self.county.html = renamed(PAGE)  # the site answers again, in a form the reader can't read
        code, out, log = self.run_update(T0 + timedelta(minutes=10))
        self.assertEqual(out["status_changed"], "false")  # one read doesn't change the reason
        self.assertEqual(self.status()["reason"], NOT_RESPONDING)
        self.county.xml = feed_xml(xml().replace(TITLE, "City of Belmont, Measure Y"), T0 + timedelta(minutes=14))
        code, out, log = self.run_update(T0 + timedelta(minutes=15))  # a second unreadable read in a row
        self.assertEqual((out["status_changed"], out["message"]), ("true", "Update site status: broken"))
        self.assertEqual(self.status(), {"state": "broken", "reason": CANT_READ, "since": "2026-11-03T20:17:00-08:00"})

    def test_feeds_failing_in_different_ways_dont_flip_the_reason(self):
        self.county.html = renamed(PAGE)
        self.county.xml = http_error(503, county.XML_URL)
        written = []
        for i in range(12):
            code, out, log = self.run_update(T0 + timedelta(minutes=5 * i))
            if out["status_changed"] == "true":
                written.append(self.status()["reason"])
        self.assertEqual(written, [NOT_RESPONDING])
        self.assertEqual(self.status()["state"], "broken")

    def test_a_reply_that_isnt_the_countys_is_not_responding(self):
        error_page = "<html><head><title>Service Unavailable</title></head><body>Try later</body></html>"
        self.county.html = error_page
        self.county.xml = ("", {"Content-Type": "text/xml"})
        self.assertRun(self.run_update(T0), 1, "failed", "html", "wait")
        self.assertEqual(self.state()["last_fail"]["kind"], "fetch")
        self.assertRun(self.run_update(T0 + timedelta(minutes=5)), 1, "failed", "xml", "now")
        self.assertEqual((self.state()["last_fail"]["kind"], self.status()["reason"]), ("fetch", NOT_RESPONDING))
        # The county's own page without Measure Y is still unreadable, not unresponsive.
        self.set_state()
        self.county.html = renamed(PAGE)
        self.run_update(T0 + timedelta(minutes=10))
        self.assertEqual(self.state()["last_fail"]["kind"], "parse")

    def test_a_good_read_after_a_break_makes_it_ok(self):
        self.county.html = http_error(503)
        self.county.xml = timeout()
        self.run_update(T0)
        self.run_update(T0 + timedelta(minutes=5))
        self.county.html = PAGE
        code, out, log = self.run_update(T0 + timedelta(minutes=10))
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertEqual((out["changed"], out["status_changed"]), ("true", "true"))
        self.assertEqual(out["message"], "Update results: county reading, county update 2026-11-03T20:10:45-08:00; site status ok")
        self.assertEqual(self.status(), {"state": "ok", "reason": "", "since": "2026-11-03T20:22:00-08:00"})
        # A good read with nothing new also makes it ok.
        self.set_status("broken", NOT_RESPONDING, "2026-11-03T20:17:00-08:00")
        code, out, log = self.run_update(T0 + timedelta(minutes=15))
        self.assertEqual((out["changed"], out["status_changed"], out["message"]), ("false", "true", "Update site status: ok"))
        self.assertEqual(self.status()["state"], "ok")
        # And the next good read has nothing to write.
        code, out, log = self.run_update(T0 + timedelta(minutes=20))
        self.assertEqual((out["status_changed"], self.status()["since"]), ("false", "2026-11-03T20:27:00-08:00"))

    def test_an_unreadable_status_is_rewritten_by_a_good_read(self):
        for junk in ("", "{", "[]", '{"state": "sideways", "reason": ""}', '{"state": "ok"}'):
            with self.subTest(status=junk):
                (self.dir / "data" / "status.json").write_text(junk)
                (self.dir / ".last-fetch").unlink(missing_ok=True)
                code, out, log = self.run_update(T0)
                self.assertEqual(out["status_changed"], "true")
                self.assertEqual(self.status(), {"state": "ok", "reason": "", "since": "2026-11-03T20:12:00-08:00"})

    def test_skipped_push_and_manual_runs_leave_it_alone(self):
        self.set_state(failures=5)
        self.set_status("broken", NOT_RESPONDING, "2026-11-03T20:17:00-08:00")
        before = (self.dir / "data" / "status.json").read_text()
        (self.dir / ".last-fetch").write_text(f"{ts(T0) - 30}\n")
        for env in ({}, {"EVENT": "push"}, {"MANUAL_YES": "3412", "MANUAL_NO": "2988",
                                            "MANUAL_COUNTY_UPDATED": "11/3/2026 8:10:45 PM"}):
            with self.subTest(env=env):
                code, out, log = self.run_update(T0, **env)
                self.assertEqual((code, out["status_changed"]), (0, "false"))
                self.assertEqual((self.dir / "data" / "status.json").read_text(), before)
        self.assertEqual(self.county.calls, [])

    def test_practice_runs_never_write_it(self):
        with mock.patch.object(update, "PRACTICE", True):
            code, out, log = self.run_update(T0, MANUAL_YES="3412", MANUAL_NO="2988",
                                             MANUAL_COUNTY_UPDATED="11/3/2026 8:10:45 PM")
            self.assertEqual((code, out["status_changed"]), (0, "false"))
            self.assertFalse(update.set_status("broken", NOT_RESPONDING, T0))
        self.assertIsNone(self.status())
        self.assertEqual(self.county.calls, [])

    def test_the_file_format(self):
        self.county.html = http_error(503)
        self.county.xml = timeout()
        self.run_update(T0)
        self.run_update(T0 + timedelta(minutes=5))
        text = (self.dir / "data" / "status.json").read_text()
        self.assertEqual(text, '{\n  "state": "broken",\n  "reason": "' + NOT_RESPONDING.replace('"', '\\"')
                         + '",\n  "since": "2026-11-03T20:17:00-08:00"\n}\n')
        self.assertEqual(list(json.loads(text)), ["state", "reason", "since"])



class DecreaseTest(RunTest):
    """A county reading below the saved YES or NO waits for a second read."""

    def setUp(self):
        super().setUp()
        self.assertRun(self.run_update(T0), 0, "ok", "html", "no")  # saved: 3,412 / 2,988 at 8:10:45 PM
        self.lower = page(yes="3,400", updated="11/3/2026 8:15:10 PM", ballots="98,600")
        self.held = {"yes": 3400, "no": 2988, "county_updated": "2026-11-03T20:15:10-08:00",
                     "seen_at": ts(T0 + timedelta(minutes=5))}

    def hold(self, at=None):
        self.county.html = self.lower
        code, out, log = self.run_update(at or T0 + timedelta(minutes=5))
        self.assertRun((code, out, log), 0, "hold", "html", "no")
        return out, log

    def test_held_then_saved_on_the_second_read(self):
        before = (self.dir / "data" / "results.json").read_text()
        out, log = self.hold()
        self.assertEqual((out["changed"], out["status_changed"], out["message"]),
                         ("false", "true", "Update site status: confirming"))
        self.assertEqual((self.dir / "data" / "results.json").read_text(), before)  # nothing saved
        self.assertEqual(self.status(), {"state": "confirming", "reason": CONFIRMING, "since": "2026-11-03T20:17:00-08:00"})
        self.assertEqual(self.state()["pending_decrease"], self.held)
        self.assertEqual(self.state()["failures"], 0)
        self.assertIn("YES went down: 3,412 -> 3,400", log)
        self.assertFalse((self.dir / "diagnosis.md").exists())

        code, out, log = self.run_update(T0 + timedelta(minutes=10))
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertEqual((out["changed"], out["status_changed"]), ("true", "true"))
        self.assertEqual(out["message"], "Update results: county reading, county update 2026-11-03T20:15:10-08:00, "
                                         "a county correction confirmed on a second read; site status ok")
        self.assertIn("YES went down: 3,412 -> 3,400 (confirmed on a second read)", log)
        latest = self.results()["latest"]
        self.assertEqual((latest["yes"], latest["no"], latest["county_ballots_counted"], latest["source"], latest["feed"]),
                         (3400, 2988, 98600, "county", "html"))
        self.assertEqual(self.status(), {"state": "ok", "reason": "", "since": "2026-11-03T20:22:00-08:00"})
        self.assertIsNone(self.state()["pending_decrease"])

    def test_dropped_when_the_numbers_are_no_longer_lower(self):
        self.hold()
        self.county.html = page(yes="3,450", no="3,001", updated="11/3/2026 8:20:00 PM", ballots="99,000")
        code, out, log = self.run_update(T0 + timedelta(minutes=10))
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertEqual((self.results()["latest"]["yes"], self.results()["latest"]["no"]), (3450, 3001))
        self.assertEqual(out["message"], "Update results: county reading, county update 2026-11-03T20:20:00-08:00; site status ok")
        self.assertIsNone(self.state()["pending_decrease"])
        self.assertEqual(self.status()["state"], "ok")

    def test_dropped_when_the_county_shows_the_saved_update_again(self):
        self.hold()
        self.county.html = PAGE
        code, out, log = self.run_update(T0 + timedelta(minutes=10))
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertEqual((out["changed"], out["status_changed"], out["message"]), ("false", "true", "Update site status: ok"))
        self.assertIsNone(self.state()["pending_decrease"])
        self.assertEqual(self.results()["latest"]["yes"], 3412)

    def test_replaced_when_it_changes(self):
        self.hold()
        self.county.html = page(yes="3,390", updated="11/3/2026 8:19:00 PM")
        code, out, log = self.run_update(T0 + timedelta(minutes=10))
        self.assertRun((code, out, log), 0, "hold", "html", "no")
        self.assertEqual(out["status_changed"], "false")  # still confirming, since the first hold
        self.assertEqual(self.status()["since"], "2026-11-03T20:17:00-08:00")
        self.assertEqual(self.state()["pending_decrease"], {"yes": 3390, "no": 2988, "county_updated": "2026-11-03T20:19:00-08:00",
                                                           "seen_at": ts(T0 + timedelta(minutes=10))})
        self.assertEqual(self.results()["latest"]["yes"], 3412)
        code, out, log = self.run_update(T0 + timedelta(minutes=15))
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertEqual(self.results()["latest"]["yes"], 3390)

    def test_the_second_read_must_match_both_yes_and_no(self):
        self.hold()  # YES 3,400, NO 2,988
        self.county.html = page(yes="3,400", no="2,980", updated="11/3/2026 8:19:00 PM")
        code, out, log = self.run_update(T0 + timedelta(minutes=10))
        self.assertRun((code, out, log), 0, "hold", "html", "no")
        self.assertEqual((self.state()["pending_decrease"]["no"], self.results()["latest"]["yes"]), (2980, 3412))
        self.county.html = page(yes="3,401", no="2,980", updated="11/3/2026 8:24:00 PM")
        code, out, log = self.run_update(T0 + timedelta(minutes=15))
        self.assertRun((code, out, log), 0, "hold", "html", "no")
        self.assertEqual(self.state()["pending_decrease"]["yes"], 3401)

    def test_a_restamp_of_the_same_lower_numbers_still_confirms(self):
        self.hold()
        self.county.html = page(yes="3,400", updated="11/3/2026 8:21:00 PM", ballots="98,700")
        self.run_update(T0 + timedelta(minutes=10))
        latest = self.results()["latest"]
        self.assertEqual((latest["yes"], latest["county_updated"]), (3400, "2026-11-03T20:21:00-08:00"))

    def test_allow_decrease_saves_at_once(self):
        self.county.html = self.lower
        code, out, log = self.run_update(T0 + timedelta(minutes=5), EVENT="workflow_dispatch", ALLOW_DECREASE="true")
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertIn("YES went down: 3,412 -> 3,400 (allowed for this run)", log)
        self.assertEqual(self.results()["latest"]["yes"], 3400)
        self.assertIsNone(self.status())
        self.assertIsNone(self.state()["pending_decrease"])

    def test_allow_decrease_also_ends_a_hold(self):
        self.hold()
        self.county.html = page(yes="3,395", updated="11/3/2026 8:19:00 PM")
        code, out, log = self.run_update(T0 + timedelta(minutes=10), EVENT="workflow_dispatch", ALLOW_DECREASE="true")
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertEqual((self.results()["latest"]["yes"], self.status()["state"]), (3395, "ok"))
        self.assertIsNone(self.state()["pending_decrease"])

    def test_a_held_decrease_survives_a_failed_read_and_confirms_from_the_other_feed(self):
        self.hold()
        self.county.html = timeout()
        self.assertRun(self.run_update(T0 + timedelta(minutes=10)), 1, "failed", "html", "wait")
        self.assertEqual(self.state()["pending_decrease"], self.held)
        self.assertEqual(self.status()["state"], "confirming")  # one failure changes nothing
        self.county.xml = feed_xml(xml(yes=3400, no=2988, ballots=98600), pt(20, 15, 11))
        code, out, log = self.run_update(T0 + timedelta(minutes=15))
        self.assertRun((code, out, log), 0, "ok", "xml", "no")
        latest = self.results()["latest"]
        self.assertEqual((latest["yes"], latest["feed"], latest["county_updated"]), (3400, "xml", "2026-11-03T20:15:11-08:00"))
        self.assertEqual(self.status()["state"], "ok")

    def test_two_failures_while_held_break_it_then_the_hold_resumes(self):
        self.hold()
        self.county.html = timeout()
        self.county.xml = http_error(503, county.XML_URL)
        self.run_update(T0 + timedelta(minutes=10))
        code, out, log = self.run_update(T0 + timedelta(minutes=15))
        self.assertRun((code, out, log), 1, "failed", "xml", "now")
        self.assertEqual(self.status(), {"state": "broken", "reason": NOT_RESPONDING, "since": "2026-11-03T20:27:00-08:00"})
        self.county.html = self.lower  # the same lower numbers: the second read that confirms them
        code, out, log = self.run_update(T0 + timedelta(minutes=20))
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertEqual((self.results()["latest"]["yes"], self.status()["state"]), (3400, "ok"))

    def test_a_decrease_on_the_xml_feed_is_held_too(self):
        self.set_state(feed="xml", retry=ts(T0) + 1500, last_ok=ts(T0))
        self.county.xml = feed_xml(xml(yes=3412, no=2900, ballots=99000), pt(20, 15, 11))
        code, out, log = self.run_update(T0 + timedelta(minutes=5))
        self.assertRun((code, out, log), 0, "hold", "xml", "no")
        self.assertIn("NO went down: 2,988 -> 2,900", log)
        self.assertEqual(self.state()["feed"], "xml")
        self.assertEqual(self.status()["state"], "confirming")

    def test_an_old_lower_reading_is_not_a_decrease(self):
        self.county.html = page(yes="3,000", updated="11/3/2026 8:05:00 PM")  # older than the saved update
        code, out, log = self.run_update(T0 + timedelta(minutes=5))
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertIn("No new county update (still 2026-11-03T20:10:45-08:00)", log)
        self.assertIsNone(self.status())


class AlertTest(RunTest):
    def test_fetch_problems(self):
        err = urllib.error
        cases = [
            (http_error(503), "HTTP 503"),
            (http_error(404), "HTTP 404"),
            (timeout(), "timed out"),
            (TimeoutError("The read operation timed out"), "timed out"),
            (err.URLError(ConnectionRefusedError(111, "Connection refused")), "connection problem (ConnectionRefusedError)"),
            (ConnectionResetError(104, "reset"), "connection problem (ConnectionResetError)"),
            (http.client.RemoteDisconnected("closed"), "connection problem (RemoteDisconnected)"),
            (http.client.IncompleteRead(b"partial"), "connection problem (IncompleteRead)"),
            (EOFError("Compressed file ended before the end-of-stream marker was reached"), "connection problem (EOFError)"),
            (gzip.BadGzipFile("Not a gzipped file"), "network problem (BadGzipFile)"),
            (err.URLError(socket.gaierror(-3, "Temporary failure in name resolution")), "address lookup failed"),
            (err.URLError(ssl.SSLCertVerificationError("certificate verify failed")), "certificate check failed"),
            (err.URLError("no host given"), "URLError"),
            (ValueError("unknown url type"), "ValueError"),
        ]
        for e, problem in cases:
            with self.subTest(error=repr(e)):
                self.assertEqual(update.fetch_problem(e), problem)

    def test_one_failed_read_waits_and_two_alert(self):
        for first, second in ((http_error(503), timeout()), (renamed(PAGE), http_error(404, county.XML_URL)),
                              (http_error(403), feed_xml("<html>busy</html>", T0))):
            with self.subTest(first=repr(first)[:40]):
                self.set_state()
                (self.dir / ".last-fetch").unlink(missing_ok=True)
                self.county.html, self.county.xml = first, second
                self.assertRun(self.run_update(T0), 1, "failed", "html", "wait")
                self.assertRun(self.run_update(T0 + timedelta(minutes=5)), 1, "failed", "xml", "now")
                self.county.html = PAGE
                self.assertRun(self.run_update(T0 + timedelta(minutes=10)), 0, "ok", "html", "no")
                self.assertEqual(self.state()["failures"], 0)

    def test_failures_outside_a_county_read_alert_now(self):
        cases = [
            ("config.json", '{"expected_votes": "12000"}', {}, "config.json expected_votes must be null"),
            ("data/results.json", "{", {}, "data/results.json is not valid JSON"),
            (None, None, {"MANUAL_YES": "3412", "MANUAL_NO": "2988"}, "manual entry needs county_updated"),
            (None, None, {"MANUAL_YES": "12.5", "MANUAL_NO": "2988", "MANUAL_COUNTY_UPDATED": "11/3/2026 8:10:45 PM"},
             "yes is not a whole number"),
        ]
        for rel, text, env, message in cases:
            with self.subTest(message=message):
                self.setUp()
                if rel:
                    (self.dir / rel).write_text(text)
                code, out, log = self.run_update(T0, **env)
                self.assertEqual((code, out["read"], out["alert"], out["status_changed"]), (1, "failed", "now", "false"), log)
                self.assertIn(message, self.diagnosis())
                self.assertEqual(self.county.calls, [])
                self.assertFalse((self.dir / ".county-state.json").exists())
                self.assertIsNone(self.status())

    def test_certify_too_early_alerts_now_but_the_county_read_is_kept_in_the_state(self):
        code, out, log = self.run_update(T0, MARK_CERTIFIED="true", EVENT="workflow_dispatch")
        self.assertRun((code, out, log), 1, "failed", "html", "now")
        self.assertIn("can't mark the count certified", self.diagnosis())
        self.assertEqual(self.results(), START)  # nothing saved
        self.assertEqual(self.state()["failures"], 0)

    def test_push_runs(self):
        code, out, log = self.run_update(T0, EVENT="push")
        self.assertRun((code, out, log), 0, "none", "none", "no")
        self.assertEqual(self.county.calls, [])
        self.assertFalse((self.dir / ".county-state.json").exists())
        (self.dir / "config.json").write_text('{"expected_votes": 0}\n')
        code, out, log = self.run_update(T0, EVENT="push")
        self.assertRun((code, out, log), 1, "none", "none", "now")

    def test_practice_runs_never_fetch(self):
        with mock.patch.object(update, "PRACTICE", True):
            code, out, log = self.run_update(T0)
        self.assertRun((code, out, log), 1, "failed", "none", "now")
        self.assertIn("practice runs need YES and NO numbers", log)
        self.assertEqual(self.county.calls, [])

    def test_a_bug_while_checking_a_reading_counts_as_a_failed_read(self):
        broken = dict(START, latest=dict(START["latest"], county_updated="not a time"))
        (self.dir / "data" / "results.json").write_text(json.dumps(broken))
        code, out, log = self.run_update(T0)
        self.assertRun((code, out, log), 1, "failed", "html", "wait")
        self.assertEqual(self.state()["last_fail"], {"feed": "html", "kind": "check", "at": ts(T0)})
        self.assertIn("unexpected ValueError while checking the reading", self.diagnosis())
        self.assertIn("Traceback", log)
        code, out, log = self.run_update(T0 + timedelta(minutes=5))
        self.assertRun((code, out, log), 1, "failed", "xml", "now")
        self.assertEqual(self.status()["reason"], CANT_READ)


class CertifiedTest(RunTest):
    """The Certified run records when the count was marked final; nothing else writes certified_at."""

    DEC3 = datetime(2026, 12, 3, 17, 30, tzinfo=PACIFIC)  # the hand-started run after the county certifies

    def test_normal_runs_never_write_certified_at(self):
        code, out, log = self.run_update(T0)
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertNotIn("certified_at", self.results())
        self.county.html = page(yes="6,005", no="5,675", updated="12/3/2026 4:45:00 PM", ballots="192,720")
        code, out, log = self.run_update(self.DEC3)
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        results = self.results()
        self.assertEqual(results["status"], "counting")
        self.assertNotIn("certified_at", results)

    def test_the_certified_run_writes_certified_at_and_later_reads_keep_it(self):
        self.run_update(T0)
        self.county.html = page(yes="6,005", no="5,675", updated="12/3/2026 4:45:00 PM", ballots="192,720")
        code, out, log = self.run_update(self.DEC3, MARK_CERTIFIED="true", EVENT="workflow_dispatch")
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertEqual(out["message"],
                         "Update results: county reading, county update 2026-12-03T16:45:00-08:00, marked certified")
        results = self.results()
        self.assertEqual((results["status"], results["certified_at"]), ("final", "2026-12-03T17:30:00-08:00"))
        self.assertEqual(results["latest"]["county_updated"], "2026-12-03T16:45:00-08:00")
        # A read that finds nothing new, one that adds a count, and a second Certified tick all keep the time.
        code, out, log = self.run_update(self.DEC3 + timedelta(minutes=10))
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertIn("results.json unchanged", log)
        self.county.html = page(yes="6,006", no="5,675", updated="12/3/2026 5:35:00 PM", ballots="192,721")
        code, out, log = self.run_update(self.DEC3 + timedelta(minutes=20))
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        code, out, log = self.run_update(self.DEC3 + timedelta(minutes=30), MARK_CERTIFIED="true", EVENT="workflow_dispatch")
        self.assertRun((code, out, log), 0, "ok", "html", "no")
        self.assertIn("results.json unchanged", log)
        results = self.results()
        self.assertEqual((results["status"], results["certified_at"], results["latest"]["yes"], len(results["history"])),
                         ("final", "2026-12-03T17:30:00-08:00", 6006, 4))


class ManualEntryTest(RunTest):
    """Manual entries work as before, and leave the county state and data/status.json alone."""

    def test_manual_entry(self):
        self.set_state(feed="xml", retry=ts(T0) + 900, failures=3, last_ok=ts(T0) - 3600)
        self.set_status("broken", NOT_RESPONDING, "2026-11-03T19:57:00-08:00")
        saved_state = (self.dir / ".county-state.json").read_bytes()
        code, out, log = self.run_update(T0, EVENT="workflow_dispatch", MANUAL_YES="3,412", MANUAL_NO="2988",
                                         MANUAL_COUNTY_UPDATED="Website Updated: 11/3/2026 8:10:45 PM")
        self.assertRun((code, out, log), 0, "ok", "manual", "no")
        self.assertEqual((out["changed"], out["status_changed"]), ("true", "false"))
        self.assertEqual(out["message"], "Update results: manual reading, county update 2026-11-03T20:10:45-08:00")
        latest = self.results()["latest"]
        self.assertEqual(latest, {"yes": 3412, "no": 2988, "county_updated": "2026-11-03T20:10:45-08:00",
                                  "county_ballots_counted": None, "fetched_at": "2026-11-03T20:12:00-08:00",
                                  "source": "manual"})
        self.assertEqual((self.dir / ".county-state.json").read_bytes(), saved_state)
        self.assertEqual(self.status()["state"], "broken")
        self.assertEqual(self.county.calls, [])

    def test_a_manual_decrease_still_needs_allow_decrease(self):
        self.run_update(T0, MANUAL_YES="3412", MANUAL_NO="2988", MANUAL_COUNTY_UPDATED="11/3/2026 8:10:45 PM")
        code, out, log = self.run_update(T0 + timedelta(minutes=5), MANUAL_YES="3400", MANUAL_NO="2988",
                                         MANUAL_COUNTY_UPDATED="11/3/2026 8:15:10 PM")
        self.assertRun((code, out, log), 1, "failed", "manual", "now")
        self.assertIn("YES went down: 3,412 -> 3,400", log)
        self.assertEqual(self.results()["latest"]["yes"], 3412)
        self.assertFalse((self.dir / ".county-state.json").exists())  # no held decrease: manual entries aren't county reads
        self.assertIsNone(self.status())
        code, out, log = self.run_update(T0 + timedelta(minutes=6), MANUAL_YES="3400", MANUAL_NO="2988",
                                         MANUAL_COUNTY_UPDATED="11/3/2026 8:15:10 PM", ALLOW_DECREASE="true")
        self.assertRun((code, out, log), 0, "ok", "manual", "no")
        self.assertEqual(self.results()["latest"]["yes"], 3400)

    def test_manual_time_checks(self):
        self.run_update(T0, MANUAL_YES="3412", MANUAL_NO="2988", MANUAL_COUNTY_UPDATED="11/3/2026 8:10:45 PM")
        for when, message in (("11/3/2026 8:10:45 PM", "county_updated must be newer than the saved"),
                              ("11/3/2026 9:30 PM", "is in the future"), ("tonight", "county_updated not understood")):
            with self.subTest(when=when):
                code, out, log = self.run_update(T0 + timedelta(minutes=1), MANUAL_YES="3500", MANUAL_NO="3000",
                                                 MANUAL_COUNTY_UPDATED=when)
                self.assertEqual((code, out["read"], out["alert"]), (1, "failed", "now"))
                self.assertIn(message, log)

    def test_after_a_manual_entry_the_county_copy_of_it_is_not_saved_again(self):
        self.run_update(T0, MANUAL_YES="3412", MANUAL_NO="2988", MANUAL_COUNTY_UPDATED="11/3/2026 8:10:45 PM")
        self.set_state(feed="xml", retry=ts(T0) + 1500)
        self.county.xml = feed_xml(xml(yes=3412, no=2988, ballots=98450), pt(20, 10, 46))
        code, out, log = self.run_update(T0 + timedelta(minutes=5))
        self.assertRun((code, out, log), 0, "ok", "xml", "no")
        self.assertEqual(out["changed"], "false")
        self.assertEqual(self.results()["latest"]["source"], "manual")


class ChecksTest(RunTest):
    def test_xml_readings_pass_the_same_checks(self):
        self.run_update(T0)  # the page: 3,412 / 2,988 at 8:10:45 PM
        self.set_state(feed="xml", retry=ts(T0) + 1500)
        # Older county time, other numbers: not a new update.
        self.county.xml = feed_xml(xml(yes=3000, no=2000, ballots=90000), pt(20, 5))
        code, out, log = self.run_update(T0 + timedelta(minutes=5))
        self.assertRun((code, out, log), 0, "ok", "xml", "no")
        self.assertEqual(out["changed"], "false")
        self.assertIn("No new county update", log)
        # Newer, but YES went down: held, not saved.
        self.county.xml = feed_xml(xml(yes=3400, no=3000, ballots=99000), pt(20, 16))
        code, out, log = self.run_update(T0 + timedelta(minutes=10))
        self.assertRun((code, out, log), 0, "hold", "xml", "no")
        self.assertEqual(self.results()["latest"]["yes"], 3412)

    def test_the_same_county_update_from_the_other_feed_is_not_saved_twice(self):
        self.run_update(T0)  # the page: 3,412 / 2,988 at 8:10:45 PM
        self.county.html = timeout()
        self.run_update(T0 + timedelta(minutes=5))  # -> the XML next
        self.county.xml = feed_xml(xml(yes=3412, no=2988, ballots=98450), pt(20, 10, 46))
        code, out, log = self.run_update(T0 + timedelta(minutes=10))
        self.assertRun((code, out, log), 0, "ok", "xml", "no")
        self.assertEqual(out["changed"], "false")
        self.assertEqual(len(self.results()["history"]), 2)
        # Ballots counted don't decide it: the feeds may total them differently.
        self.county.xml = feed_xml(xml(yes=3412, no=2988, ballots=98451), pt(20, 10, 47))
        code, out, log = self.run_update(T0 + timedelta(minutes=15))
        self.assertEqual(out["changed"], "false")
        self.assertEqual(len(self.results()["history"]), 2)
        # The same Measure Y numbers more than 10 minutes later are a new county update, and are saved.
        self.county.xml = feed_xml(xml(yes=3412, no=2988, ballots=99001), pt(20, 21))
        code, out, log = self.run_update(T0 + timedelta(minutes=20))
        self.assertEqual(out["changed"], "true")
        self.assertEqual(len(self.results()["history"]), 3)

    def test_a_restamp_from_the_same_feed_is_still_saved(self):
        self.run_update(T0)
        self.county.html = page(updated="11/3/2026 8:15:00 PM")
        code, out, log = self.run_update(T0 + timedelta(minutes=5))
        self.assertEqual(out["changed"], "true")  # as before this change

    def test_a_county_time_far_ahead_of_the_clock_fails_the_check(self):
        self.county.html = page(updated="11/4/2026 8:10:45 PM")  # a day ahead
        code, out, log = self.run_update(T0)
        self.assertRun((code, out, log), 1, "failed", "html", "wait")
        self.assertEqual(self.state()["last_fail"], {"feed": "html", "kind": "check", "at": ts(T0)})
        self.assertEqual(self.state()["feed"], "xml")
        self.assertIn("the county update time 2026-11-04T20:10:45-08:00 is more than an hour after this run's time",
                      self.diagnosis())
        self.assertEqual(self.results(), START)
        # A few minutes ahead (clocks differ) is fine.
        (self.dir / ".last-fetch").unlink()
        self.county.html = page(updated="11/3/2026 8:20:00 PM")
        self.set_state()
        self.assertRun(self.run_update(T0), 0, "ok", "html", "no")

    def test_results_entries_are_unchanged_but_for_feed(self):
        self.run_update(T0)
        latest = self.results()["latest"]
        self.assertEqual(latest, {"yes": 3412, "no": 2988, "county_updated": "2026-11-03T20:10:45-08:00",
                                  "county_ballots_counted": 98450, "fetched_at": "2026-11-03T20:12:00-08:00",
                                  "source": "county", "feed": "html"})
        self.assertEqual(list(self.results()), list(START))  # no field added or renamed at the top
        self.assertEqual(self.results()["status"], "counting")


class DiagnosisTest(RunTest):
    INSTRUCTIONS = re.compile(r"Run workflow|Actions →|allow_decrease|\btick\b|\bcopy\b|\btype\b|\byou\b|please|"
                              r"manual entry|should|check that", re.I)

    def assertFyi(self, text):
        """Short, public-safe and FYI: no instructions, nothing that could mention or command anyone."""
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 25, text)
        for line in lines:
            self.assertLessEqual(len(line), 320, line)
            self.assertFalse(line.startswith("::"), line)
        outside_code = re.sub(r"`[^`]*`", "", text)
        self.assertNotIn("@", outside_code)
        self.assertNotIn("http", outside_code)
        self.assertIsNone(self.INSTRUCTIONS.search(outside_code), text)
        self.assertNotIn("No action needed", text)  # ops/status.py leads with it

    def test_renamed_contest(self):
        self.county.html = renamed(PAGE)
        code, out, log = self.run_update(T0)
        d = self.diagnosis()
        self.assertFyi(d)
        for want in ("**County read failed** at Tue 8:12 PM",
                     "- What failed: `county page check failed: expected one contest titled",
                     "- This run: read the county results page",
                     "- Next county read: the county XML feed (the backup); the county results page again after Tue 8:42 PM",
                     "- Failed county reads in a row: 1",
                     "- Readers: the site still shows the last saved numbers. If the next county read fails too, it sends "
                     "readers to the county's results page",
                     "**What the reader found** (page title `Election Results (test page)`)",
                     "- Website Updated: `11/3/2026 8:10:45 PM`",
                     "- Ballots counted: 98,450",
                     "- Contests with Belmont or Measure Y: `City of Belmont Mayor`, `City of Belmont, Measure Y`",
                     "- `City of Belmont, Measure Y`: YES 3,412, NO 2,988"):
            self.assertIn(want, d)
        # The same summary is in the log, without the markdown.
        self.assertIn("- Website Updated: 11/3/2026 8:10:45 PM", log)
        self.assertIn("County read failed at Tue 8:12 PM", log)

    def test_second_failure_says_readers_are_sent_to_the_county(self):
        self.run_update(T0 - timedelta(minutes=5))  # a good read first
        self.county.html = renamed(PAGE)
        self.county.xml = feed_xml(xml(yes=4105, no=3702, ballots=121880).replace(TITLE, "CITY OF BELMONT MEASURE Y"),
                                   pt(20, 11, 2))
        self.run_update(T0)
        code, out, log = self.run_update(T0 + timedelta(minutes=5))
        d = self.diagnosis()
        self.assertFyi(d)
        for want in ("**County read failed** at Tue 8:17 PM",
                     "- What failed: `county XML feed check failed: expected one contest titled",
                     "- This run: read the county XML feed (the backup)",
                     "- Next county read: the county results page",
                     "- Failed county reads in a row: 2; last good read Tue 8:07 PM",
                     "- Readers: the site is sending readers to the county's results page, with this note: " + CANT_READ,
                     "- Last-Modified: `11/3/2026 8:11:02 PM`",
                     "- `CITY OF BELMONT MEASURE Y`: YES 4,105, NO 3,702"):
            self.assertIn(want, d)

    def test_fetch_timeout(self):
        self.run_update(T0 - timedelta(minutes=5))  # a good read first
        self.county.html = timeout()
        self.run_update(T0)
        d = self.diagnosis()
        self.assertFyi(d)
        self.assertIn("- What failed: `county page fetch failed: timed out`", d)
        self.assertIn("- Next county read: the county XML feed (the backup)", d)
        self.assertNotIn("What the reader found", d)

    def test_retry_of_the_page_while_on_the_backup(self):
        self.set_state(feed="xml", retry=ts(T0), failures=0, last_ok=ts(T0) - 300)
        self.county.html = http_error(502)
        self.run_update(T0)
        self.assertIn("- Next county read: the county XML feed (the backup); the county results page again after "
                      "Tue 8:42 PM", self.diagnosis())
        self.set_state(feed="xml", retry=ts(T0) + 300, failures=1, last_ok=ts(T0) - 300)
        (self.dir / "config.json").write_text('{"expected_votes": "x"}')  # fails before reading; the retry is due
        self.run_update(T0 + timedelta(minutes=10))
        self.assertIn("- Next county read: the county results page (a retry; the county XML feed is the backup)",
                      self.diagnosis())

    def test_a_held_decrease_is_mentioned(self):
        self.run_update(T0)
        self.county.html = page(yes="3,400", updated="11/3/2026 8:15:10 PM")
        self.run_update(T0 + timedelta(minutes=5))
        self.county.html = timeout()
        self.run_update(T0 + timedelta(minutes=10))
        d = self.diagnosis()
        self.assertFyi(d)
        self.assertIn("- Waiting to confirm a lower county count: YES 3,400, NO 2,988 (county update "
                      "`11/3/2026 8:15:10 PM`)", d)
        self.assertIn("- Readers: the site shows the last saved numbers, with a note that a county revision is being "
                      "confirmed", d)

    def test_a_failure_outside_a_county_read(self):
        (self.dir / "config.json").write_text('{"expected_votes": -3}\n')
        self.run_update(T0)
        d = self.diagnosis()
        self.assertFyi(d)
        for want in ("**Update run failed** at Tue 8:12 PM",
                     "- What failed: `config.json expected_votes must be null or a positive whole number`",
                     "- This run: no county read",
                     "- Next county read: the county results page",
                     "- Readers: the site still shows the last saved numbers"):
            self.assertIn(want, d)
        self.assertNotIn("Failed county reads", d)

    def test_a_failed_manual_entry(self):
        self.set_state(feed="xml", retry=ts(T0) + 900, failures=1, last_ok=ts(T0) - 3600)
        self.run_update(T0, MANUAL_YES="3412", MANUAL_NO="2988")
        d = self.diagnosis()
        for want in ("- What failed: `manual entry needs county_updated", "- This run: a manual entry",
                     "- Next county read: the county XML feed (the backup); the county results page again after Tue 8:27 PM",
                     "- Failed county reads in a row: 1; last good read Tue 7:12 PM"):
            self.assertIn(want, d)

    def test_county_text_cannot_mention_anyone(self):
        at = "\x40"  # an at sign, written so this file has no mention-like text
        self.county.html = renamed(PAGE, f"City of Belmont, Measure Y {at}_x_ `x`")  # no GitHub name has _
        self.run_update(T0)
        d = self.diagnosis()
        self.assertFyi(d)
        self.assertIn(f"`City of Belmont, Measure Y {at}_x_ 'x'`", d)

    def test_an_error_page_instead_of_results(self):
        self.county.html = "<html><head><title>Service Unavailable</title></head><body>Try later</body></html>"
        self.run_update(T0)
        d = self.diagnosis()
        self.assertFyi(d)
        self.assertIn("**What the reader found** (only 82 characters, page title `Service Unavailable`)", d)
        self.assertIn("- No county update time (Website Updated)", d)
        self.assertIn("- No contest with Belmont or Measure Y in its title", d)

    def test_a_missing_yes_row(self):
        self.county.html = re.sub(r'<tr class="content-row"><td style="width: 35px;"><div class="contestant-color"></div></td>'
                                  r'<td data-race="116" data-contestant="221".*?</tr>', "", PAGE, count=1, flags=re.S)
        self.run_update(T0)
        d = self.diagnosis()
        self.assertFyi(d)
        self.assertIn("expected YES and NO rows, found ['NO']", d)
        self.assertIn(f"- `{TITLE}`: NO 2,988 (no YES row)", d)

    def test_the_same_problem_reads_the_same(self):
        """Repeat alerts post only when the text changes; what changes every run is the run's clock times and the
        count of failed reads in a row."""
        self.county.html = renamed(PAGE)
        self.county.xml = feed_xml(xml().replace(TITLE, "City of Belmont, Measure Y"), pt(20, 11, 2))
        texts = []
        for i in range(5):
            self.run_update(T0 + timedelta(minutes=5 * i))
            texts.append(self.diagnosis())

        def steady(text):
            text = re.sub(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2}:\d\d [AP]M\b", "<clock>", text)
            return re.sub(r"in a row: \d+", "in a row: <n>", text)
        self.assertEqual(steady(texts[2]), steady(texts[4]))  # both read the page, 2+ failures in
        self.assertEqual(steady(texts[1]), steady(texts[3]))  # both read the XML
        self.assertNotEqual(steady(texts[0]), steady(texts[2]))  # the first one says readers aren't sent away yet

    def test_no_diagnosis_after_a_good_run(self):
        (self.dir / "diagnosis.md").write_text("old")
        self.run_update(T0)
        self.assertFalse((self.dir / "diagnosis.md").exists())


# A stand-in county for update.py run as a script: the fetches are replaced, and the network is blocked.
DRIVER = r"""
import runpy, socket, sys, urllib.error, urllib.request
root, mode = sys.argv[1], sys.argv[2]
def no_network(*args, **kwargs):
    raise AssertionError("tried to use the network")
urllib.request.urlopen = no_network
sys.path.insert(0, root + "/scraper")
import county
def fetch():
    if mode == "down":
        raise urllib.error.URLError(socket.timeout("timed out"))
    return open(root + "/page.html", encoding="utf-8").read()
def fetch_xml():
    raise urllib.error.HTTPError(county.XML_URL, 503, "Service Unavailable", {}, None)
county.fetch, county.fetch_xml = fetch, fetch_xml
sys.argv = [root + "/scraper/update.py"]
runpy.run_path(root + "/scraper/update.py", run_name="__main__")
"""


class ScriptTest(unittest.TestCase):
    """update.py run the way the workflow runs it: a fresh process, outputs in GITHUB_OUTPUT."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copytree(ROOT / "scraper", self.dir / "scraper", ignore=shutil.ignore_patterns("__pycache__"))
        (self.dir / "data").mkdir()
        (self.dir / "data" / "results.json").write_text(json.dumps(START, indent=2) + "\n")
        (self.dir / "config.json").write_text('{"expected_votes": null}\n')
        # The run uses the real clock, so the page's update time is 10 minutes ago.
        self.updated = datetime.now(PACIFIC).replace(microsecond=0) - timedelta(minutes=10)
        (self.dir / "page.html").write_text(page(updated=county.county_time(self.updated)), encoding="utf-8")

    def run_script(self, mode, event="schedule"):
        outputs = self.dir / "github_output"
        outputs.write_text("")
        env = {k: v for k, v in os.environ.items() if k not in RUN_ENV}
        env.update(EVENT=event, GITHUB_OUTPUT=str(outputs), PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, "-c", DRIVER, str(self.dir), mode], cwd=self.dir, env=env,
                           capture_output=True, text=True, timeout=120)
        out = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
        return p.returncode, out, p.stdout + p.stderr

    def age_last_fetch(self):
        (self.dir / ".last-fetch").write_text(f"{time.time() - 600:.0f}\n")

    def test_push_run(self):
        code, out, log = self.run_script("up", event="push")
        self.assertEqual((code, out), (0, {"changed": "false", "status_changed": "false", "read": "none", "alert": "no",
                                           "feed": "none", "failure": "none"}), log)
        self.assertFalse((self.dir / ".county-state.json").exists())

    def test_a_good_read_then_a_skip(self):
        code, out, log = self.run_script("up")
        self.assertEqual(code, 0, log)
        self.assertEqual({k: out[k] for k in ("changed", "status_changed", "read", "alert", "feed")},
                         {"changed": "true", "status_changed": "false", "read": "ok", "alert": "no", "feed": "html"})
        self.assertEqual(out["message"], f"Update results: county reading, county update {self.updated.isoformat()}")
        self.assertEqual(json.loads((self.dir / ".county-state.json").read_text())["feed"], "html")
        code, out, log = self.run_script("up")
        self.assertEqual((code, out["read"]), (0, "skipped"), log)
        self.assertIn("Skipped fetch", log)

    def test_two_failed_reads(self):
        code, out, log = self.run_script("down")
        self.assertEqual((code, out["read"], out["alert"], out["feed"], out["status_changed"]),
                         (1, "failed", "wait", "html", "false"), log)
        self.assertTrue((self.dir / "diagnosis.md").exists())
        self.age_last_fetch()
        code, out, log = self.run_script("down")
        self.assertEqual((code, out["read"], out["alert"], out["feed"], out["status_changed"], out["message"]),
                         (1, "failed", "now", "xml", "true", "Update site status: broken"), log)
        status = json.loads((self.dir / "data" / "status.json").read_text())
        self.assertEqual((status["state"], status["reason"]), ("broken", NOT_RESPONDING))
        self.assertRegex(status["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d-0[78]:00$")
        self.assertIn("county XML feed fetch failed: HTTP 503", (self.dir / "diagnosis.md").read_text())
        self.age_last_fetch()
        code, out, log = self.run_script("up")
        self.assertEqual((code, out["read"], out["status_changed"]), (0, "ok", "true"), log)
        self.assertEqual(json.loads((self.dir / "data" / "status.json").read_text())["state"], "ok")
        self.assertFalse((self.dir / "diagnosis.md").exists())


class EngineIgnoresFeedTest(unittest.TestCase):
    def test_forecast_is_the_same_with_feed_fields(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        script = r"""
        var M = require(process.argv[1]), P = require(process.argv[2]);
        var rows = [[3412, 2988, '2026-11-03T20:10:45-08:00'], [4105, 3702, '2026-11-03T21:05:00-08:00']];
        function data(withFeed) {
          var history = rows.map(function (r, i) {
            var h = { yes: r[0], no: r[1], county_updated: r[2], county_ballots_counted: 0, fetched_at: r[2], source: 'county' };
            if (withFeed) h.feed = i ? 'xml' : 'html';
            return h;
          });
          return { status: 'counting', threshold: 0.5, latest: history[history.length - 1], history: history };
        }
        var a = JSON.stringify(M.forecast(data(false), P)), b = JSON.stringify(M.forecast(data(true), P));
        var c = JSON.stringify(M.wobble(data(false), P)), d = JSON.stringify(M.wobble(data(true), P));
        process.stdout.write(a === b && c === d ? 'same' : 'different');
        """
        out = subprocess.run(["node", "-e", script, str(ROOT / "js" / "peanut-model.js"),
                              str(ROOT / "data" / "peanut-priors.json")], capture_output=True, text=True, timeout=120)
        self.assertEqual((out.returncode, out.stdout), (0, "same"), out.stderr)


if __name__ == "__main__":
    unittest.main()
