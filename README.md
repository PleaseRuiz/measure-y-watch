# Measure Y Watch

Live count for Belmont's Measure Y (Nov 3, 2026), from San Mateo County's results page.
Page: https://pleaseruiz.github.io/measure-y-watch/

- `index.html` reads `data/results.json` and re-checks every 60 seconds. `?demo=1` shows sample numbers.
- `.github/workflows/update.yml` reads the county page on a schedule, saves new counts to
  `data/results.json` and publishes the site. It saves only the county's counts; the forecast
  tab works out its numbers in each visitor's browser.
- `config.json` holds `expected_votes` (null hides the progress bar).
- `forecast.html` is the just-for-fun forecast tab, PEANUT (Peninsula Election Analysis & Needle,
  Unofficial Tally). The model is `js/peanut-model.js`
  (it runs in the browser, in `js/peanut-worker.js` when it can), every assumption is in
  `data/peanut-priors.json`, and `node --test tests/peanut-model.test.js` runs its tests.
- `ops/` holds the election-night helpers: FYI status issues on GitHub (`status.py`), the keep-alive,
  the watchdog, preflight and the forecast engine check. See "Election night is automatic".

## Schedule (Pacific)

- Tue Nov 3, 7:57 PM to Wed Nov 4, 4:57 AM: every 5 minutes
- Thu Nov 5 to Mon Nov 16, 8 AM to 6 PM: every 10 minutes

GitHub can start scheduled runs late or skip them. On election night the keep-alive starts a run
every 5 minutes when none is waiting or running, and in both windows the watchdog checks every 10
minutes that county reads keep coming and the page is current. A run can always be started by hand.

## Run it by hand (phone)

GitHub app or github.com: **Actions → Update results → Run workflow** (branch `main`).

- **Leave every field blank** to read the county page now.
- **Manual entry** if reading the county page breaks: type the YES and NO totals from the
  county page and its "Website Updated" time exactly as shown (e.g. `11/3/2026 8:10:45 PM`).
  The time is required: a made-up time could make the county's next update look old and get
  skipped. Saved with `"source": "manual"`.
- **Allow decrease**: tick only when the county really lowered a count, or to fix a
  manual typo. Applies to that one run. If the county itself lowered a count while you were
  typing entries by hand, also turn the forecast tab off (see below).
- **Certified**: tick once, after the county certifies the count (Dec 3). The page then shows
  CERTIFIED and the forecast tab shows the final result. It's refused before Nov 17. If it was
  ticked by mistake, edit `data/results.json` on `main` (github.com, pencil icon) and change
  `"status": "final"` to `"status": "counting"`; the site republishes in a couple of minutes.
- **Keepalive**: leave it unticked. The keep-alive and the watchdog tick it, so their runs republish
  only when the live page is behind; a hand-started run always republishes.

After a hand-started run, check that it turned green. If it says Cancelled (another run took its
place in line), start it again.

**Turn the forecast tab off**: edit `data/peanut-priors.json` on `main` and change
`"paused": false` to `"paused": true`. Within a couple of minutes the tab says Peanut is taking a
break, even on pages already open. Change it back to turn it on. The Live count tab isn't affected.

**Mid-count rules for the forecast tab** (from the 8:10 PM report until the county certifies):
- Change `data/peanut-priors.json` only to fix a real error, and list every number change at the
  end of the forecast page's methodology ("Changes to the assumptions after the first report").
  An edit that only changes wording keeps `"version"` as it is, so no number moves. A change to
  any number must also change `"version"`.
- Pause only for broken data or a model bug (for example a county correction), never because of an
  unwelcome number.
- After pushing a change to that file, check that the run it started turned green. If it says
  Cancelled, start Update results by hand: a hand-started run always republishes.

A run fails (red, and GitHub emails you) when the contest title changes, a number isn't a
whole number, a manual entry lowers YES or NO without **Allow decrease** or has a time that isn't
newer than the saved one, or the county's results can't be read. Nothing is saved to
`data/results.json` when a run fails. When the county itself lowers YES or NO, the run doesn't fail:
the next read saves the new numbers if the county still shows them (see below).

## Election night is automatic

Nobody needs to do anything on election night. The workflows read the county, publish the page,
watch themselves and fix what they can. If updates break, the site says why and sends readers to
the county's official results.

### What runs when (Pacific)
- **Sun Nov 1, 2:13 PM: the drill.** Preflight posts a test issue, "DRILL: test alert, nothing is
  wrong, no action needed", and closes it. It doesn't read the county.
- **Mon Nov 2, 6 PM: code freeze** (below).
- **Tue Nov 3, 5:05 PM and 5:15 PM: preflight.** One county read each (the results page, then the
  county's backup data feed), plus a check that the live page matches `main`, the forecast engine
  works, and Update results, Keep-alive and Watchdog are on. It posts a closed FYI issue with the
  numbers it read, or an open one that says what failed.
- **Tue 7:45 PM to Wed 5 AM: the keep-alive.** Every 5 minutes it starts Update results unless a run
  is already waiting or running, in case GitHub starts scheduled runs late. The county is still read
  at most once every 4 minutes; an extra run just says "Skipped fetch". After 5.5 hours it starts a
  fresh Keep-alive run to carry on.
- **Update results** runs on the schedule above. It reads the county's results page. When the page
  can't be read, the next run reads the county's backup data feed (XML) instead, and goes back to
  the page within 30 minutes.
- **The watchdog** runs every 10 minutes, Tue 7:55 PM to Wed 5 AM and Nov 5-16, 8 AM to 6 PM. It never
  reads the county site. It starts Update results when county reads are late (12 minutes at night,
  25 in the daytime), republishes when the live page has been behind `main` for 10 minutes, and runs
  the forecast engine check.

### What readers see when something breaks
- After 2 failed county reads in a row, the site shows a banner that sends readers to the county's
  official results page and says why: "The county's results site isn't responding to our automatic
  updates right now." or "The county changed how it publishes results, and our automatic reader
  can't read it yet." It goes away after the next good read.
- When the county lowers YES or NO, the page keeps the saved numbers and says "The county revised
  its numbers. We're confirming the change before showing it." The next read, about 5 minutes later,
  saves the new numbers if the county still shows them.
- The banner comes from `data/status.json`, which only Update results writes and publishes with the
  page. No file means all is well.
- Both tabs also show "Live updates are running late" when a scheduled county update hasn't reached
  the page 30 minutes after it was due on election night (through the midnight update), or 45 minutes
  after on Nov 5-16. This works even if GitHub itself stalls.
- They show "We can't load the latest numbers right now" after the page fails to load the numbers 3
  times in a row.
- While the banner says updates are paused, the forecast tab rests ("Peanut is taking a break") and
  comes back by itself. Sample numbers (`?demo=1`, `?rehearsal=1`) and a certified count never show
  the banner.

### FYI issues
The workflows post status issues on GitHub (label `election-night-status`) that mention the
repository owner, so the GitHub app can buzz. They're FYI only: each starts "No action needed.",
says what the automation is doing about it, and closes by itself once things are fine. A problem
that goes on gets a comment when something changes, or after 30 minutes. Issues are public like run
logs: they only show county numbers, times and run status.

| Issue | When | What the automation does |
|---|---|---|
| The county's results can't be read | 2 failed county reads in a row | Each run tries again, switching to the backup data feed; the site sends readers to the county |
| County reads are running late | No county read for 25 minutes (45 in the daytime) | The watchdog starts Update results |
| The live page is behind | The live page still differs from `main` 25 minutes after it changed | The watchdog republishes the page |
| The forecast engine check failed | The engine fails on the saved results | Checks again after each update; the Live count tab doesn't use the engine |
| Preflight | Tue 5:05 and 5:15 PM | Lists what it checked, and what the automation does about any problem |
| DRILL | Sun 2:13 PM | Opens and closes; nothing is wrong |

A failed Update results run is still red in the Actions tab, and GitHub may email about it. That's FYI too.

### The keep-alive switch
The repository variable `KEEPALIVE` set to `off` (Settings → Secrets and variables → Actions →
Variables) makes Keep-alive runs skip. One that's already running stops when it's cancelled in the
Actions tab. Scheduled Update results runs and the watchdog go on either way.

### What to expect
- 8:10 PM: the county's first report, mail ballots received through Sunday, probably about half of
  all votes. It's on the page by about 8:20 PM.
- 9 PM, then about hourly until 2 or 3 AM: in-person (vote-center) ballots only. YES often slips a
  little, because in-person voters have been less supportive of local measures.
- Thu Nov 5 onward, around 4:30 PM on weekdays: late mail, provisional ballots and signature fixes.
  On property taxes, YES has usually gained in this stretch.
- The Live count tab only ever shows counted votes. The forecast tab is the just-for-fun one.
- Dec 3: after the county certifies, Update results runs once with **Certified** ticked.

### Code freeze: Mon Nov 2, 6 PM Pacific
After Monday 6 PM Pacific, code and workflows change only to fix something that's broken.
