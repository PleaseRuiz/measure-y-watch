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

## Schedule (Pacific)

- Tue Nov 3, 7:57 PM to Wed Nov 4, 4:57 AM: every 5 minutes
- Thu Nov 5 to Mon Nov 16, 8 AM to 6 PM: every 10 minutes

GitHub can start scheduled runs late or skip them. A run can always be started by hand.

## Run it by hand (phone)

GitHub app or github.com: **Actions → Update results → Run workflow** (branch `main`).

- **Leave every field blank** to read the county page now.
- **Manual entry** if reading the county page breaks: type the YES and NO totals from the
  county page and its "Website Updated" time exactly as shown (e.g. `11/3/2026 8:10:45 PM`).
  The time is required: a made-up time could make the county's next update look old and get
  skipped. Saved with `"source": "manual"`.
- **Allow decrease**: tick only when the county really lowered a count, or to fix a
  manual typo. Applies to that one run.
- **Certified**: tick once, after the county certifies the count (Dec 3). The page then shows
  CERTIFIED and the forecast tab shows the final result. It's refused before Nov 17. If it was
  ticked by mistake, edit `data/results.json` on `main` (github.com, pencil icon) and change
  `"status": "final"` to `"status": "counting"`; the site republishes in a couple of minutes.

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
whole number, YES or NO goes down, a manual time isn't newer than the saved one, or the
county page can't be read. Nothing is saved when a run fails.

## Election night (Tue Nov 3)

### Monday
- Open both tabs on your phone. Live count says "First results Tue, Nov 3 at 8:10 PM" and Peanut is asleep.
- Actions tab: the latest "Update results" run is green.

### 7:57 PM
The first automatic run. Then every 5 minutes until 5 AM. Most runs say "No new county update". That's normal.

### 8:10 PM
The county posts its first report: mail ballots received through Sunday, probably about half of all votes.
Expect it on the page by about 8:20 PM: up to 5 minutes until the next run (GitHub can start a few minutes late),
then about a minute for the page.

Check:
- The page's YES and NO match the county's Measure Y line.
- The page's "County update" time matches the county's "Website Updated".
- Actions: green is fine. Red means GitHub emails you; the last lines of the run's log say why.

### If the page is still empty at 8:25 PM
1. Actions → Update results → Run workflow, every field blank. Wait 2 minutes.
2. Still nothing, or red: do a manual entry.

### Manual entry from the phone
1. In the phone's browser: github.com/PleaseRuiz/measure-y-watch → Actions → Update results → Run workflow (branch `main`).
2. YES and NO: the Measure Y totals on the county page. Commas are fine.
3. county_updated: copy the county's "Website Updated" time exactly, e.g. `11/3/2026 8:10:45 PM`.
   Pasting the whole "Website Updated: …" line works too. The run stops with a message if the time is missing.
4. Run workflow. It turns green in about a minute, and the page shows the numbers within a minute after that.
5. Later runs go back to reading the county page by themselves. Repeat manual entries only while they keep failing.

### Through the night and after
- 9 PM, then about hourly until 2 or 3 AM: in-person (vote-center) ballots only. YES often slips a little, because in-person voters have been less supportive of local measures.
- Thu Nov 5 onward, around 4:30 PM on weekdays: late mail, provisional ballots and signature fixes. On property taxes, YES has usually gained in this stretch.
- The Live count tab only ever shows counted votes. The forecast tab is the just-for-fun one.
- Dec 3: after the county certifies, run Update results once with **Certified** ticked.
