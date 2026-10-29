# Measure Y Watch

Live count for Belmont's Measure Y (Nov 3, 2026), from San Mateo County's results page.
Page: https://pleaseruiz.github.io/measure-y-watch/

- `index.html` reads `data/results.json` and re-checks every 60 seconds. `?demo=1` shows sample numbers.
- `.github/workflows/update.yml` reads the county page on a schedule, saves new counts to
  `data/results.json` and publishes the page. It never estimates or forecasts.
- `config.json` holds `expected_votes` (null hides the progress bar).

## Schedule (Pacific)

- Tue Nov 3, 7:57 PM to Wed Nov 4, 2:57 AM: every 5 minutes
- Thu Nov 5 to Mon Nov 16, 8 AM to 6 PM: every 10 minutes

GitHub can start scheduled runs late or skip them. A run can always be started by hand.

## Run it by hand (phone)

GitHub app or github.com: **Actions → Update results → Run workflow** (branch `main`).

- **Leave every field blank** to read the county page now.
- **Manual entry** if reading the county page breaks: type the YES and NO totals from the
  county page, and optionally its "Website Updated" time exactly as shown
  (e.g. `11/3/2026 8:10:45 PM`; blank uses the current time). Saved with `"source": "manual"`.
- **Allow decrease**: tick only when the county really lowered a count, or to fix a
  manual typo. Applies to that one run.

A run fails (red, and GitHub emails you) when the contest title changes, a number isn't a
whole number, YES or NO goes down, a manual time isn't newer than the saved one, or the
county page can't be read. Nothing is saved when a run fails.
