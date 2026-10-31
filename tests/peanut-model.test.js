// Unit tests for the forecast engine. Run with: node --test tests/
'use strict';
var test = require('node:test');
var assert = require('node:assert');
var M = require('../js/peanut-model.js');
var P = require('../data/peanut-priors.json');

function data(rows, extra) {
  var d = { threshold: 0.5, history: rows.map(function (r) { return { yes: r[0], no: r[1], county_updated: r[2], source: r[3] || 'county' }; }) };
  return Object.assign(d, extra || {});
}
var FIRST = '2026-11-03T20:12:00-08:00', NIGHT = '2026-11-03T23:05:00-08:00', THU = '2026-11-05T16:28:00-08:00',
  MON = '2026-11-09T16:25:00-08:00', LAST = '2026-11-16T16:25:00-08:00';

test('waits until there are votes', function () {
  assert.strictEqual(M.forecast(data([]), P).status, 'waiting');
  assert.strictEqual(M.forecast(data([[0, 0, FIRST]]), P).status, 'waiting');
  assert.strictEqual(M.forecast({ history: [] }, P).status, 'waiting');
});

test('same input gives the same answer (seeded)', function () {
  var d = data([[3400, 3000, FIRST], [3900, 3500, NIGHT]]);
  assert.deepStrictEqual(M.forecast(d, P), M.forecast(d, P));
});

test('certified results are certain', function () {
  var pass = M.forecast(data([[6001, 6000, LAST]], { status: 'final' }), P);
  var fail = M.forecast(data([[6000, 6000, LAST]], { status: 'final' }), P);
  assert.strictEqual(pass.pYes, 1);
  assert.strictEqual(fail.pYes, 0, 'a tie fails: the measure needs 50% plus one vote');
});

test('a bigger lead means a higher chance', function () {
  var last = -1;
  [2900, 3100, 3200, 3300, 3500].forEach(function (y) {
    var p = M.forecast(data([[y, 6400 - y, FIRST]]), P).pYes;
    assert.ok(p >= last, 'pYes should not drop as YES grows: ' + p + ' after ' + last);
    last = p;
  });
});

test('flipping YES and NO mirrors the chance when the assumptions are symmetric', function () {
  var sym = JSON.parse(JSON.stringify(P));
  sym.leans.election_night.mean = 0; sym.leans.post_election.mean = 0;
  var a = M.forecast(data([[3300, 3100, FIRST], [3700, 3600, NIGHT]]), sym).pYes;
  var b = M.forecast(data([[3100, 3300, FIRST], [3600, 3700, NIGHT]]), sym).pYes;
  assert.ok(Math.abs(a + b - 1) < 0.03, 'expected about 1, got ' + (a + b));
});

test('re-stamped reports with the same numbers are ignored', function () {
  var a = M.forecast(data([[3400, 3000, FIRST], [3900, 3500, NIGHT]]), P);
  var b = M.forecast(data([[3400, 3000, FIRST], [3400, 3000, '2026-11-03T21:00:00-08:00'], [3900, 3500, NIGHT]]), P);
  assert.strictEqual(a.pYes, b.pYes);
});

test('little is left after the last scheduled update', function () {
  var r = M.forecast(data([[3400, 3000, FIRST], [4200, 3900, NIGHT], [5000, 4600, THU], [5600, 5100, MON], [5900, 5400, LAST]]), P);
  assert.ok(r.remaining[0.5] < 0.04 * r.totalVotes[0.5], 'remaining ' + r.remaining[0.5]);
  assert.ok(r.pYes > 0.99);
});

test('the trailing side needs the right share of what is left', function () {
  var r = M.forecast(data([[3400, 3000, FIRST]]), P);
  var R = r.remaining[0.5], need = r.trailingNeeds.share;
  assert.strictEqual(r.trailingNeeds.side, 'NO');
  // NO gets need*R, YES gets (1-need)*R, and that exactly erases the 400-vote margin.
  assert.ok(Math.abs((3000 + need * R) - (3400 + (1 - need) * R)) < 1e-6);
});

test('forecast ranges are ordered and inside 0-100%', function () {
  var r = M.forecast(data([[3300, 3100, FIRST], [3700, 3600, NIGHT], [4500, 4400, THU]]), P);
  var q = r.finalShare;
  assert.ok(q[0.05] <= q[0.1] && q[0.1] <= q[0.25] && q[0.25] <= q[0.5] && q[0.5] <= q[0.75] && q[0.75] <= q[0.9] && q[0.9] <= q[0.95]);
  assert.ok(q[0.05] > 0 && q[0.95] < 1);
  assert.ok(r.pYes >= 0 && r.pYes <= 1);
  assert.ok(r.remaining[0.1] >= 0 && r.remaining[0.1] <= r.remaining[0.5] && r.remaining[0.5] <= r.remaining[0.9]);
});

test('demo and rehearsal data can be placed on the release schedule', function () {
  var d = data([[3412, 2988, '2026-10-29T05:20:24Z'], [4105, 3702, '2026-10-29T05:22:43Z'], [4950, 4301, '2026-10-29T05:25:04Z']]);
  var plain = M.forecast(d, P), mapped = M.forecast(d, P, { remapTimes: true });
  assert.strictEqual(plain.phase, 0, 'raw rehearsal times all fall before election night');
  assert.strictEqual(mapped.phase, 1, 'remapped to 8:10 PM, 10 PM and 1 AM');
});

test('counts far from the turnout prior still give sensible ranges', function () {
  // A huge, a tiny and an on-schedule first report: the estimate of total votes should follow the data.
  var big = M.forecast(data([[4600, 4400, FIRST]]), P), small = M.forecast(data([[520, 480, FIRST]]), P);
  assert.ok(big.totalVotes[0.5] > 12500, 'big total ' + big.totalVotes[0.5]);
  assert.ok(small.totalVotes[0.5] < 9000, 'small total ' + small.totalVotes[0.5]);
  [big, small].forEach(function (r) { assert.ok(r.finalShare[0.9] - r.finalShare[0.1] > 0.005, 'range collapsed'); });
  // Late in the count, the total is pinned near what has been counted.
  var late = M.forecast(data([[1700, 1500, FIRST], [2500, 2300, NIGHT], [3100, 2900, LAST]]), P);
  assert.ok(late.totalVotes[0.5] < 6400 && late.totalVotes[0.5] > 6000, 'late total ' + late.totalVotes[0.5]);
});

test('the trajectory has one point per distinct report', function () {
  var t = M.trajectory(data([[3400, 3000, FIRST], [3400, 3000, NIGHT], [3900, 3500, THU]]), P, { sims: 2000 });
  assert.strictEqual(t.length, 2);
});

test('a first report stamped late is still the early-mail baseline', function () {
  var onTime = M.forecast(data([[3380, 3120, FIRST]]), P).pYes;
  var late = M.forecast(data([[3380, 3120, '2026-11-03T21:05:00-08:00']]), P).pYes;
  assert.ok(Math.abs(onTime - late) < 0.05, 'on time ' + onTime + ' vs late ' + late);
});

test('a razor-thin finish is not called early', function () {
  // A 10-vote YES lead after the last scheduled update, every batch breaking about even,
  // and a few hundred votes possibly left: too close for the needle to be sure either way.
  var r = M.forecast(data([[3200, 3200, FIRST], [3600, 3600, NIGHT], [4400, 4400, THU], [5000, 5000, MON], [5205, 5195, LAST]]), P);
  assert.ok(r.pYes < 0.9 && r.pYes > 0.1, 'chance ' + r.pYes);
});

test('a correction below several earlier reports keeps the counts growing', function () {
  var h = M.cleanHistory(data([[3400, 3000, FIRST], [3600, 3100, NIGHT], [3700, 3200, THU], [3500, 3050, MON]]), P);
  for (var i = 1; i < h.length; i++) assert.ok(h[i].yes >= h[i - 1].yes && h[i].no >= h[i - 1].no);
  assert.strictEqual(h[h.length - 1].yes, 3500);
});

test('counts stored as text are read as numbers', function () {
  var a = M.forecast(data([[3400, 3000, FIRST]]), P).pYes;
  var b = M.forecast(data([['3400', '3000', FIRST]]), P).pYes;
  assert.strictEqual(a, b);
  assert.strictEqual(M.forecast(data([['3,400', '3000', FIRST]]), P).status, 'waiting', 'unparseable counts are ignored');
});

test('an overnight count stamped Wednesday morning is still election night', function () {
  var night = M.forecast(data([[3300, 3100, FIRST], [3750, 3620, '2026-11-04T01:58:00-08:00']]), P).pYes;
  var morning = M.forecast(data([[3300, 3100, FIRST], [3750, 3620, '2026-11-04T09:20:00-08:00']]), P).pYes;
  assert.strictEqual(night, morning);
});

test('one more YES vote never lowers the chance', function () {
  var last = -1;
  for (var y = 3180; y <= 3220; y++) {
    var p = M.forecast(data([[y, 3200, FIRST]]), P).pYes;
    assert.ok(p >= last, y + ' YES gives ' + p + ' after ' + last);
    last = p;
  }
});

test('a later report on election night is not folded into the early-mail baseline', function () {
  var a = M.forecast(data([[3300, 3100, FIRST], [3420, 3260, '2026-11-03T20:54:00-08:00']]), P).pYes;
  var b = M.forecast(data([[3300, 3100, FIRST], [3420, 3260, '2026-11-03T20:56:00-08:00']]), P).pYes;
  assert.strictEqual(a, b);
});

test('missing or blank counts are skipped, not read as zero', function () {
  var h = M.cleanHistory(data([[3300, 3100, FIRST], [null, 3200, NIGHT], ['', 3200, THU], [true, 3200, MON]]), P);
  assert.strictEqual(h.length, 1);
});

test('nothing is left to count on certification day', function () {
  var r = M.forecast(data([[5701, 5700, '2026-12-03T17:05:00-08:00']]), P);
  assert.strictEqual(r.remaining[0.9], 0);
  assert.strictEqual(r.pYes, 1);
});

test('the trailing side follows the pass line, and YES trails at a tie', function () {
  var tie = M.forecast(data([[3200, 3200, FIRST]]), P).trailingNeeds;
  assert.deepStrictEqual([tie.side, tie.share], ['YES', 0.5]);
  var higher = M.forecast(data([[3500, 3000, FIRST]], { threshold: 0.55 }), P);
  assert.strictEqual(higher.trailingNeeds.side, 'YES', 'YES is below 55% even though it leads');
});

test('final-share ranges stay inside 0-100% for one-sided counts', function () {
  [[3400, 0], [0, 3000]].forEach(function (c) {
    var q = M.forecast(data([[c[0], c[1], FIRST]]), P).finalShare;
    assert.ok(q[0.05] >= 0 && q[0.95] <= 1, JSON.stringify(q));
  });
});

test('the wobble band changes only when the count does', function () {
  var rows = [[3300, 3100, FIRST], [3750, 3620, NIGHT], [4400, 4250, THU]];
  var a = M.wobble(data(rows), P);
  var withRestamp = M.wobble(data(rows.concat([[4400, 4250, MON]])), P);
  var d = data(rows); d.history[2].fetched_at = '2026-11-05T16:39:00-08:00';
  assert.deepStrictEqual(withRestamp, a);
  assert.deepStrictEqual(M.wobble(d, P), a);
  assert.ok(a.lo <= a.hi);
});

test('each chart point is exactly what the needle showed after that report', function () {
  var rows = [[3300, 3100, FIRST], [3750, 3620, NIGHT], [4400, 4250, THU]];
  var t = M.trajectory(data(rows), P);
  rows.forEach(function (_, i) {
    assert.strictEqual(t[i].pYes, M.forecast(data(rows.slice(0, i + 1)), P).pYes);
  });
  var fin = M.trajectory(data(rows, { status: 'final' }), P);
  assert.strictEqual(fin[fin.length - 1].pYes, 1);
});

test('a late-mail update on Thursday morning counts as late ballots', function () {
  assert.strictEqual(M.phaseOf(Date.parse('2026-11-05T11:30:00-08:00'), P), 2);
  assert.strictEqual(M.phaseOf(Date.parse('2026-11-04T16:30:00-08:00'), P), 2);
  assert.strictEqual(M.phaseOf(Date.parse('2026-11-04T08:00:00-08:00'), P), 1);
});

test('the counts behind each later group add up', function () {
  var r = M.forecast(data([[3300, 3100, FIRST], [3480, 3290, NIGHT], [4100, 3880, THU]]), P);
  assert.deepStrictEqual(r.observed, { night: { yes: 180, n: 370 }, post: { yes: 620, n: 1210 } });
});

test('the wobble band sits around the needle', function () {
  [
    [[3300, 3100, FIRST]],
    [[3300, 3100, FIRST], [3750, 3620, NIGHT]],
    [[3300, 3100, FIRST], [3750, 3620, NIGHT], [4400, 4250, THU]],
    [[3220, 3180, FIRST], [3600, 3610, NIGHT], [4250, 4240, THU], [4900, 4880, MON]]
  ].forEach(function (rows) {
    var d = data(rows), p = M.forecast(d, P).pYes, w = M.wobble(d, P);
    assert.ok(w.lo <= p + 0.005 && p - 0.005 <= w.hi, 'needle ' + p + ' outside ' + JSON.stringify(w));
  });
});

test('the wobble band moves by the needle minus the short-run chance', function () {
  var d = data([[3300, 3100, FIRST]]);
  var shift = M.forecast(d, P).pYes - M.forecast(d, P, { sims: 800 }).pYes;
  // With sims passed, wobble() uses the same 800-sim redraws without the shift.
  var raw = M.wobble(d, P, { sims: 800 }), w = M.wobble(d, P);
  assert.ok(Math.abs(shift) > 0.002, 'pick a count with a visible short-run error, got ' + shift);
  assert.ok(Math.abs(w.lo - (raw.lo + shift)) < 1e-12 && Math.abs(w.hi - (raw.hi + shift)) < 1e-12);
});

// County corrections and fixed typos (saved with Allow decrease). A fixed typo in a manual entry should land where
// the right numbers would have. A county correction may fix an error from any earlier report, so the needle should
// stay close to the readings for wherever the error might have been, and never collapse.
var WED = '2026-11-04T16:30:00-08:00';
function near(a, b, tol, what) { assert.ok(Math.abs(a - b) < tol, what + ': ' + a + ' vs ' + b); }

test('a fixed typo in a manual entry barely moves the needle', function () {
  var first = [3300, 3100, FIRST], fixed = [3600, 3400, '2026-11-03T22:01:00-08:00'];
  var typo = M.forecast(data([first, [3840, 3290, '2026-11-03T21:01:00-08:00', 'manual'], fixed]), P);
  var right = M.forecast(data([first, [3480, 3290, '2026-11-03T21:01:00-08:00'], fixed]), P);
  near(typo.pYes, right.pYes, 0.03, 'typo vs no typo');
  assert.strictEqual(typo.observed.night.n, 600);
});

test('a swap fixed on its own after Thursday\'s update counts with that update', function () {
  var rows = [[2897, 2544, FIRST], [2951, 2614, '2026-11-03T21:01:00-08:00'], [3015, 2682, '2026-11-03T22:01:00-08:00'],
    [3054, 2727, '2026-11-03T23:01:00-08:00'], [3103, 2764, '2026-11-04T00:01:00-08:00'], [3140, 2806, '2026-11-04T01:01:00-08:00'],
    [3232, 2856, '2026-11-04T02:01:00-08:00']];
  var posted = M.forecast(data(rows.concat([[3722, 3140, '2026-11-05T16:31:00-08:00'], [3667, 3195, '2026-11-05T17:31:00-08:00']])), P);
  var source = M.forecast(data(rows.concat([[3667, 3195, '2026-11-05T16:31:00-08:00']])), P);
  near(posted.pYes, source.pYes, 0.03, 'fix posted alone vs fixed at its source');
});

test('a Wednesday fix of election-night counts stays election night', function () {
  var posted = M.forecast(data([[2821, 2603, FIRST], [3168, 3206, NIGHT], [3108, 3266, WED]]), P);
  assert.strictEqual(posted.phase, 1);
  assert.strictEqual(posted.observed.post.n, 0, 'no late ballots have been counted');
  // The 60 votes moved from YES to NO were wrong either in the 8:10 PM report or in the night's updates.
  var inBase = M.forecast(data([[2761, 2663, FIRST], [3108, 3266, NIGHT]]), P).pYes;
  var inNight = M.forecast(data([[2821, 2603, FIRST], [3108, 3266, NIGHT]]), P).pYes;
  assert.ok(posted.pYes > Math.min(inBase, inNight) - 0.1 && posted.pYes < Math.max(inBase, inNight) + 0.1,
    posted.pYes + ' outside ' + inNight + ' to ' + inBase);
});

test('a correction of an error in the 8:10 PM report does not collapse the needle', function () {
  var posted = M.forecast(data([[3255, 2634, FIRST], [3357, 2712, '2026-11-03T21:01:00-08:00'], [3494, 2825, '2026-11-03T22:01:00-08:00'],
    [3271, 3048, '2026-11-03T23:01:00-08:00']]), P);
  var source = M.forecast(data([[3032, 2857, FIRST], [3134, 2935, '2026-11-03T21:01:00-08:00'], [3271, 3048, '2026-11-03T22:01:00-08:00']]), P);
  near(posted.pYes, source.pYes, 0.1, 'error fixed in the 8:10 PM report');
  assert.strictEqual(posted.observed.night.n, 0, 'the take leaves out a batch the correction made unsure');
});

// Every way an error of (dY, dN) votes could have got in at one report and stayed until the fix (the last row),
// as fixed-at-source histories. The posted needle should sit among their needles.
function readings(rows, dY, dN) {
  var out = [];
  for (var i = 0; i < rows.length - 1; i++) {
    var h = rows.slice(0, -1).map(function (r, j) { return j >= i ? [r[0] - dY, r[1] - dN, r[2]] : r; });
    var ok = h.every(function (r, j) { return r[0] >= 0 && r[1] >= 0 && (!j || (r[0] >= h[j - 1][0] && r[1] >= h[j - 1][1])); });
    if (ok) out.push(M.forecast(data(h), P).pYes);
  }
  return out;
}
function among(p, ps, tol, what) {
  var lo = Math.min.apply(null, ps), hi = Math.max.apply(null, ps);
  assert.ok(p > lo - tol && p < hi + tol, what + ': ' + p + ' outside ' + lo + ' to ' + hi);
}
var T = function (d, hm) { return '2026-11-0' + d + 'T' + hm + ':00-08:00'; };

test('a lone county fix after Thursday\'s release of an error from election night lands among the readings', function () {
  var rows = [[2727, 2673, T(3, '20:12')], [2794, 2756, T(3, '21:03')], [2950, 2850, T(3, '22:02')], [3021, 2939, T(3, '23:05')],
    [3101, 3039, T(4, '00:04')], [3168, 3122, T(4, '01:02')], [3230, 3200, T(4, '02:05')], [3778, 3782, T(5, '16:31')], [3698, 3782, T(5, '18:00')]];
  among(M.forecast(data(rows), P).pYes, readings(rows, 80, 0), 0.05, 'Thursday fix of +80 YES');
});

test('an election-night fix of an earlier error lands among the readings, and the take says nothing about those ballots', function () {
  var rows = [[2727, 2673, T(3, '20:12')], [2794, 3056, T(3, '21:03')], [2870, 3150, T(3, '22:02')], [2941, 3239, T(3, '23:05')],
    [3021, 3339, T(4, '00:04')], [3088, 3422, T(4, '01:02')], [3150, 3200, T(4, '02:05')]];
  var r = M.forecast(data(rows), P);
  among(r.pYes, readings(rows, 0, 300), 0.05, '2 AM fix of +300 NO');
  assert.strictEqual(r.observed.night.n, 0);
});

test('a Wednesday-afternoon fix that adds a few votes stays election night', function () {
  var night = [[2727, 2673, T(3, '20:12')], [2794, 2756, T(3, '21:03')], [2870, 2850, T(3, '22:02')], [2941, 2939, T(3, '23:05')],
    [3021, 3039, T(4, '00:04')], [3088, 3122, T(4, '01:02')], [3150, 3200, T(4, '02:05')]];
  var posted = M.forecast(data(night.concat([[3148, 3207, T(4, '16:00')]])), P);
  var source = M.forecast(data(night.slice(0, -1).concat([[3148, 3207, T(4, '02:05')]])), P);
  assert.strictEqual(posted.phase, 1);
  assert.strictEqual(posted.observed.post.n, 0);
  near(posted.pYes, source.pYes, 0.03, 'Wednesday fix vs fixed at 2 AM');
});

test('a fixed re-post of the 8:10 PM report stays early mail', function () {
  var posted = M.forecast(data([[2727, 2673, T(3, '20:12')], [3131, 3069, T(3, '20:25')], [3091, 3069, T(3, '20:50')], [3158, 3152, T(3, '21:03')]]), P);
  var source = M.forecast(data([[2727, 2673, T(3, '20:12')], [3091, 3069, T(3, '20:25')], [3158, 3152, T(3, '21:03')]]), P);
  near(posted.pYes, source.pYes, 0.03, 'fixed re-post vs fixed at its source');
});

test('a removed duplicate batch does not collapse the needle', function () {
  var D = [[2509, 2629, FIRST], [2587, 2721, '2026-11-03T21:01:00-08:00'], [2671, 2802, '2026-11-03T22:01:00-08:00']];
  var posted = M.forecast(data(D.concat([[2500, 2692, '2026-11-03T23:01:00-08:00']])), P);
  var source = M.forecast(data([[2297, 2450, FIRST], [2375, 2542, '2026-11-03T21:01:00-08:00'], [2459, 2623, '2026-11-03T22:01:00-08:00'],
    [2500, 2692, '2026-11-03T23:01:00-08:00']]), P);
  near(posted.pYes, source.pYes, 0.2, 'duplicate removed later vs at its source');
});

test('Thursday\'s votes stay late ballots when they also fix a typo in the last night entry', function () {
  var r = M.forecast(data([[3300, 3100, FIRST], [4500, 3400, NIGHT, 'manual'], [3900, 3700, THU]]), P);
  assert.strictEqual(r.phase, 2);
  assert.strictEqual(r.observed.post.n, 3900 + 3700 - 3300 - 3100);
});

test('a lower count posted on certification day leaves nothing to count', function () {
  var d = data([[3300, 3100, FIRST], [3600, 3400, NIGHT], [4400, 4250, THU], [5205, 5195, LAST], [5203, 5194, '2026-12-03T17:05:00-08:00']]);
  var r = M.forecast(d, P), w = M.wobble(d, P);
  assert.strictEqual(r.remaining[0.9], 0);
  assert.strictEqual(r.pYes, 1);
  assert.deepStrictEqual(w, { lo: 1, hi: 1 }, 'no wobble once the count is complete');
});

test('the page\'s plan gives the same needle, band and last chart point as the engine', function () {
  var d = data([[3300, 3100, FIRST], [3750, 3620, NIGHT]]);
  var steps = M.plan(d, P, {}, {}), msgs = [];
  while (steps.length) { var m = steps.shift()(); if (m) msgs.push(m); }
  var byType = function (t) { return msgs.filter(function (x) { return x.type === t; })[0]; };
  var r = M.forecast(d, P);
  assert.strictEqual(byType('needle').r.pYes, r.pYes);
  assert.deepStrictEqual(byType('band').band, M.wobble(d, P));
  var pts = byType('points').points;
  assert.strictEqual(pts[pts.length - 1].pYes, r.pYes);
});

test('what Peanut expects from late ballots after election night', function () {
  var none = M.forecast(data([[3300, 3100, FIRST], [4400, 4250, THU]]), P);
  near(none.lateExpected, P.leans.post_election.mean, 1e-12, 'no in-person updates: the starting assumption');
  var d = data([[3300, 3100, FIRST], [3750, 3620, NIGHT], [4400, 4250, THU]]);
  assert.strictEqual(M.forecast(d, P).lateExpected, M.forecast(d, P, { upTo: 1 }).leans.postElection.mean);
});

test('the worker redraws every chart point when a number in the priors changes', function () {
  var fs = require('fs'), vm = require('vm'), path = require('path'), dir = path.join(__dirname, '..', 'js');
  var queue = [], posted = [];
  var ctx = { self: { postMessage: function (m) { posted.push(JSON.parse(JSON.stringify(m))); } }, JSON: JSON, Math: Math, Date: Date,
    setTimeout: function (f) { queue.push(f); } };
  ctx.importScripts = function (f) { vm.runInContext(fs.readFileSync(path.join(dir, f), 'utf8'), ctx); };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(dir, 'peanut-worker.js'), 'utf8'), ctx);
  function run(priors) {
    ctx.self.onmessage({ data: JSON.parse(JSON.stringify({ key: 'k', data: d, priors: priors, remapTimes: false })) });
    while (queue.length) queue.shift()();
    var pts = posted.splice(0).filter(function (m) { return m.out && m.out.type === 'points'; })[0].out.points;
    return pts.map(function (x) { return x.pYes; });
  }
  var d = data([[3300, 3100, FIRST], [3750, 3620, NIGHT], [4400, 4250, THU]]);
  var before = run(P), P2 = JSON.parse(JSON.stringify(P));
  P2.leans.post_election.mean = 0; // same version, as a mistaken mid-count edit would leave it
  var after = run(P2);
  assert.deepStrictEqual(after, M.trajectory(d, P2).map(function (x) { return x.pYes; }));
  assert.notDeepStrictEqual(after.slice(0, -1), before.slice(0, -1), 'earlier points were redrawn too');
});

test('a county correction keeps the night\'s votes in the night', function () {
  var night = [[2821, 2603, FIRST], [3168, 3206, NIGHT]];
  var plain = M.forecast(data(night), P);
  var fixed = M.forecast(data(night.concat([[3160, 3200, '2026-11-04T16:30:00-08:00']])), P);
  assert.strictEqual(fixed.observed.post.n, 0, 'no late ballots have been counted');
  assert.strictEqual(fixed.observed.night.n, 3160 + 3200 - 2821 - 2603);
  assert.ok(Math.abs(fixed.pYes - plain.pYes) < 0.03, plain.pYes + ' vs ' + fixed.pYes);
  // A correction that also adds votes keeps the earlier report's time for what it already counted.
  var mixed = M.forecast(data(night.concat([[3160, 3200, '2026-11-04T16:30:00-08:00'], [3700, 3600, THU]])), P);
  assert.strictEqual(mixed.observed.night.n, 3160 + 3200 - 2821 - 2603);
  assert.strictEqual(mixed.observed.post.n, 3700 + 3600 - 3160 - 3200);
});

// ---------- confirmed to pass ----------
// Measure Y is confirmed to pass when a count posted from Thu Nov 5 on leaves YES above the pass line even if every
// vote in Peanut's worst case for the votes still out (r.remainingMax, M.boundZ standard deviations above its turnout
// estimate) were a NO.
var FRI13 = '2026-11-13T16:22:00-08:00', NOV16 = '2026-11-16T16:20:00-08:00';
var CALL = [[3780, 2620, FIRST], [4560, 3290, '2026-11-04T01:02:00-08:00'], [5250, 3700, THU], [6120, 4210, MON], [6640, 4500, FRI13], [6870, 4640, NOV16]];
// What the rule compares, without the date: YES stays above the line if every vote that could be left is a NO.
function evenIfAllNo(r) { return r.counted.yes > r.threshold * (r.counted.n + r.remainingMax); }

test('the bound on the votes still out is a whole number at or above the usual range', function () {
  [data(CALL.slice(0, 3)), data(CALL), data([[3300, 3100, FIRST]]), data([[5701, 5700, '2026-12-03T17:05:00-08:00']])].forEach(function (d) {
    var r = M.forecast(d, P);
    assert.ok(Number.isInteger(r.remainingMax) && r.remainingMax >= r.remaining[0.9], r.remainingMax + ' vs ' + r.remaining[0.9]);
    assert.ok(r.remainingMax > 0, 'the bound is never 0 for a live count');
  });
});

test('the bound reads the fitted turnout distribution, not the random draws', function () {
  [data(CALL.slice(0, 3)), data(CALL), data([[3300, 3100, FIRST]])].forEach(function (d) {
    var r = M.forecast(d, P), P2 = JSON.parse(JSON.stringify(P));
    P2.version = P.version + '-another-seed'; P2.sims = 3000;
    assert.strictEqual(M.forecast(d, P2).remainingMax, r.remainingMax, 'another seed and fewer simulations');
    assert.strictEqual(M.forecast(d, P, { sims: 2000 }).remainingMax, r.remainingMax, 'fewer simulations');
  });
});

test('the bound is 4.5 standard deviations above the turnout estimate', function () {
  // One report: log(votes counted) = log(share of the count done) + log(total votes), with the turnout prior, the
  // schedule prior at that time and the count noise. Conditioning on it by hand gives the posterior for the total.
  var n = 6400, r = M.forecast(data([[3300, 3100, FIRST]]), P);
  var T = P.turnout, sch = M.scheduleAt(Date.parse(FIRST), P), tau = P.noise.count_log_sd;
  var vx = T.y_votes_log_sd * T.y_votes_log_sd, vw = Math.pow(sch.sd / sch.mean, 2), vObs = vx + vw + tau * tau;
  var resid = Math.log(n) - Math.log(T.y_votes_median) - Math.log(sch.mean);
  var meanLog = Math.log(T.y_votes_median) + vx / vObs * resid, sdLog = Math.sqrt(vx - vx * vx / vObs);
  assert.strictEqual(M.boundZ, 4.5);
  assert.strictEqual(r.remainingMax, Math.ceil(Math.exp(meanLog + 4.5 * sdLog) - n));
  assert.ok(r.remainingMax > r.remaining[0.9] * 1.3, 'well above the usual range: ' + r.remainingMax + ' vs ' + r.remaining[0.9]);
});

test('Measure Y is confirmed once YES leads by more than the most votes that could be left', function () {
  var friday = M.forecast(data(CALL), P, { upTo: 4 }), monday = M.forecast(data(CALL), P);
  assert.ok(friday.pYes > 0.99 && !evenIfAllNo(friday), 'Friday: very likely, but the lead ' + (6640 - 4500) + ' is under ' + friday.remainingMax);
  assert.strictEqual(M.confirmedPass(friday), false);
  assert.ok(monday.counted.yes - monday.counted.no > monday.remainingMax, 'Monday: the lead tops the bound');
  assert.strictEqual(M.confirmedPass(monday), true);
  assert.ok(monday.pYes > 0.999, 'a confirmed pass is never shown next to a lower chance: ' + monday.pYes);
});

test('a lead equal to the bound is not enough: all NO would tie, and a tie fails', function () {
  // The bound depends only on how many votes were counted when, so moving votes between YES and NO keeps it. A total
  // of 11,001 or 11,002 votes makes the lead and the bound the same parity, so a count with lead = bound exists.
  var n, B, at = function (yes) { return M.forecast(data([[3400, 3000, FIRST], [4900, 4200, THU], [yes, n - yes, NOV16]]), P); };
  for (n = 11001; (n + (B = at(6100).remainingMax)) % 2; n++);
  var y = (n + B) / 2, tie = at(y), win = at(y + 1);
  assert.strictEqual(win.remainingMax, B);
  assert.strictEqual(tie.counted.yes - tie.counted.no, B, 'the lead is exactly the bound');
  assert.strictEqual(M.confirmedPass(tie), false);
  assert.strictEqual(M.confirmedPass(win), true);
  assert.strictEqual(M.confirmedPass(at(y - 1)), false);
});

test('nothing is confirmed before Thu Nov 5, Pacific time, even when the math holds', function () {
  var night = [[11000, 1500, FIRST], [12350, 1800, '2026-11-04T02:01:00-08:00']];
  var r = M.forecast(data(night), P);
  assert.ok(evenIfAllNo(r), 'an election-night landslide where the math holds');
  assert.strictEqual(M.confirmedPass(r), false, 'election night');
  var wed = M.forecast(data(night.concat([[13900, 2000, '2026-11-04T23:59:59-08:00']])), P);
  var thu = M.forecast(data(night.concat([[13900, 2000, '2026-11-05T00:00:00-08:00']])), P);
  assert.ok(evenIfAllNo(wed) && evenIfAllNo(thu));
  assert.strictEqual(M.confirmedPass(wed), false, 'one second before Thursday');
  assert.strictEqual(M.confirmedPass(thu), true, 'Thursday 12:00 AM');
  assert.strictEqual(M.confirmFrom, Date.parse('2026-11-05T00:00:00-08:00'));
});

test('a complete count keeps the schedule\'s last-point uncertainty: a lead of a handful of votes is not called', function () {
  var DEC3 = '2026-12-03T17:05:00-08:00';
  var ahead = M.forecast(data([[5701, 5700, DEC3]]), P);
  assert.strictEqual(ahead.status, 'live');
  assert.deepStrictEqual([ahead.remaining[0.1], ahead.remaining[0.5], ahead.remaining[0.9]], [0, 0, 0], 'the simulations treat the count as complete');
  assert.ok(ahead.remainingMax > 100 && ahead.remainingMax < 0.03 * ahead.counted.n, 'the bound keeps the schedule noise: ' + ahead.remainingMax);
  assert.strictEqual(M.confirmedPass(ahead), false, 'a one-vote lead on certification day is not called');
  assert.strictEqual(M.confirmedPass(M.forecast(data([[5700, 5700, DEC3]]), P)), false, 'a tie');
  var clear = M.forecast(data([[6000, 5400, DEC3]]), P);
  assert.ok(clear.counted.yes - clear.counted.no > clear.remainingMax, 'a 600-vote lead tops the bound ' + clear.remainingMax);
  assert.strictEqual(M.confirmedPass(clear), true);
  assert.strictEqual(clear.remaining[0.9], 0);
  // The same count an hour before the schedule says complete has a slightly bigger bound, and no call either.
  var before = M.forecast(data([[5701, 5700, '2026-12-03T08:00:00-08:00']]), P);
  assert.ok(before.remainingMax >= ahead.remainingMax && before.remaining[0.9] > 0, before.remainingMax + ' vs ' + ahead.remainingMax);
  assert.strictEqual(M.confirmedPass(before), false);
});

test('a certified count is the county\'s result, not a confirmation', function () {
  // The page shows the certified result (r.status 'final') on its own, so the helper says false there.
  var pass = M.forecast(data(CALL, { status: 'final' }), P);
  assert.strictEqual(pass.status, 'final');
  assert.strictEqual(pass.pYes, 1);
  assert.strictEqual(pass.remainingMax, 0);
  assert.strictEqual(pass.threshold, 0.5);
  assert.strictEqual(M.confirmedPass(pass), false);
  assert.strictEqual(M.confirmedPass(M.forecast(data([]), P)), false, 'waiting');
  assert.strictEqual(M.confirmedPass(null), false);
});

test('the pass line comes from the data', function () {
  var twoThirds = M.forecast(data(CALL, { threshold: 0.6667 }), P);
  assert.strictEqual(twoThirds.threshold, 0.6667);
  assert.strictEqual(M.confirmedPass(twoThirds), false, '59.7% YES is short of two thirds');
  var big = [[4800, 1600, FIRST], [5700, 2150, '2026-11-04T01:02:00-08:00'], [6700, 2250, THU], [7800, 2530, MON], [8500, 2640, FRI13], [9200, 2810, NOV16]];
  var r = M.forecast(data(big, { threshold: 0.6667 }), P);
  assert.ok(r.counted.yes > 0.6667 * (r.counted.n + r.remainingMax));
  assert.strictEqual(M.confirmedPass(r), true);
  // Just under the line plus the bound is not confirmed.
  var y = Math.floor(0.6667 * (12010 + r.remainingMax));
  var under = M.forecast(data(big.slice(0, -1).concat([[y, 12010 - y, NOV16]]), { threshold: 0.6667 }), P);
  assert.strictEqual(under.remainingMax, r.remainingMax);
  assert.strictEqual(M.confirmedPass(under), false);
});

test('a county correction that lowers YES takes the call back', function () {
  var called = [[3400, 3000, FIRST], [4900, 4200, THU], [6133, 4868, NOV16]];
  assert.strictEqual(M.confirmedPass(M.forecast(data(called), P)), true);
  var fixed = M.forecast(data(called.concat([[6050, 4868, '2026-11-16T18:00:00-08:00']])), P);
  assert.ok(fixed.counted.yes - fixed.counted.no <= fixed.remainingMax);
  assert.strictEqual(M.confirmedPass(fixed), false);
});

test('the worker hands the page the same call as the engine', function () {
  var fs = require('fs'), vm = require('vm'), path = require('path'), dir = path.join(__dirname, '..', 'js');
  var queue = [], posted = [];
  var ctx = { self: { postMessage: function (m) { posted.push(JSON.parse(JSON.stringify(m))); } }, JSON: JSON, Math: Math, Date: Date,
    setTimeout: function (f) { queue.push(f); } };
  ctx.importScripts = function (f) { vm.runInContext(fs.readFileSync(path.join(dir, f), 'utf8'), ctx); };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(dir, 'peanut-worker.js'), 'utf8'), ctx);
  [data(CALL), data(CALL.slice(0, 5)), data(CALL, { status: 'final' })].forEach(function (d) {
    ctx.self.onmessage({ data: JSON.parse(JSON.stringify({ key: 'k', data: d, priors: P, remapTimes: false })) });
    while (queue.length) queue.shift()();
    var r = posted.splice(0).filter(function (m) { return m.out && m.out.type === 'needle'; })[0].out.r, e = M.forecast(d, P);
    assert.deepStrictEqual([r.remainingMax, r.threshold, r.counted.t], [e.remainingMax, e.threshold, e.counted.t]);
    assert.strictEqual(M.confirmedPass(r), M.confirmedPass(e));
  });
});

test('adding the call changed no other number', function () {
  // The engine's numbers before the call was added, with these assumptions frozen so a later edit of
  // data/peanut-priors.json can't break the check. Order: pYes, final-share quantiles (7), votes left (3),
  // total votes (3), counted fraction, then for live forecasts the baseline, late expectation, both leans
  // (mean, sd) and the share the trailing side needs.
  var FROZEN = {"version":"2026-10-29.5","sims":20000,"turnout":{"y_votes_median":11300,"y_votes_log_sd":0.11},"schedule":[{"at":"2026-11-03T20:10:00-08:00","counted_share":0.48,"sd":0.1},{"at":"2026-11-04T03:00:00-08:00","counted_share":0.565,"sd":0.1},{"at":"2026-11-05T16:30:00-08:00","counted_share":0.66,"sd":0.09},{"at":"2026-11-06T16:30:00-08:00","counted_share":0.72,"sd":0.08},{"at":"2026-11-09T16:30:00-08:00","counted_share":0.82,"sd":0.06},{"at":"2026-11-12T09:30:00-08:00","counted_share":0.89,"sd":0.045},{"at":"2026-11-13T16:30:00-08:00","counted_share":0.93,"sd":0.035},{"at":"2026-11-16T16:30:00-08:00","counted_share":0.96,"sd":0.02},{"at":"2026-12-03T17:00:00-08:00","counted_share":1,"sd":0.002}],"schedule_correlation":0.7,"phases":{"first_report_ends":"2026-11-03T20:55:00-08:00","election_night_ends":"2026-11-04T12:00:00-08:00","vote_center_share":{"mean":0.085,"sd":0.025}},"leans":{"election_night":{"mean":-0.06,"sd":0.06},"post_election":{"mean":0.04,"sd":0.045},"correlation":0.6,"tail_df":5},"noise":{"increment_sd":0.02,"min_increment_votes":50,"remaining_batch_sd":0.01,"lean_drift_sd":0.02,"model_sd":0.01,"count_log_sd":0.005},"demo_release_times":["2026-11-03T20:10:00-08:00","2026-11-03T22:00:00-08:00","2026-11-04T01:00:00-08:00","2026-11-05T16:30:00-08:00","2026-11-09T16:30:00-08:00","2026-11-13T16:30:00-08:00"]};
  var L16 = '2026-11-16T16:25:00-08:00', CASES = {
    night: [[[3300, 3100, FIRST], [3750, 3620, NIGHT]],
      [0.9245,0.495338037031,0.503214118643,0.513886058082,0.525499751521,0.537923740377,0.550104439728,0.558475748115,2984.55876298,4339.8265451,5909.88063148,10354.558763,11709.8265451,13279.8806315,0.629385924003,0.515625,0.0431561704988,-0.0529862877803,0.0235619619679,0.0431561704988,0.037528937175,0.514977557127]],
    thu: [[[3300, 3100, FIRST], [3750, 3620, NIGHT], [4400, 4250, THU]],
      [0.9029,0.496101313858,0.500174220101,0.506217254059,0.512376114452,0.518922467485,0.525466045584,0.529983425472,2095.89846619,3324.2359598,4726.13592808,10745.8984662,11974.2359598,13376.1359281,0.722384294834,0.515625,0.0431561704988,-0.0593411928774,0.0228914233263,0.007329521835,0.0204553326595,0.522561575323]],
    nov16: [[[3354, 3096, FIRST], [5363, 5187, '2026-11-04T01:02:00-08:00'], [6055, 5795, THU], [6691, 6359, MON], [7015, 6655, L16]],
      [1,0.51117209195,0.511876805331,0.512816466537,0.513688165693,0.514762448109,0.515926100999,0.51674417877,253.71459077,549.955967431,859.512210268,13923.7145908,14219.9559674,14529.5122103,0.961325058341,0.52,0.0519676394453,-0.0378641166464,0.0197221864817,0.00956534212161,0.0196747013254,0.827298930569]],
    fix: [[[3300, 3100, FIRST], [3600, 3400, NIGHT], [4400, 4250, THU], [4300, 4250, MON]],
      [0.51805,0.488984972678,0.491877515905,0.496246073769,0.500241675664,0.504035227749,0.507688393474,0.510314666327,1026.55982529,1737.73907398,2519.89975521,9576.55982529,10287.739074,11069.8997552,0.831086396974,0.515625,0.0562764150943,-0.0419315235824,0.0249115178837,-0.030185165193,0.020058946474,0.514386509675]],
    line55: [[[3500, 3000, FIRST]],
      [0.4569,0.505241822628,0.515939867383,0.531547999498,0.54744804157,0.563629041325,0.580526849829,0.591872140107,3889.6482157,5249.50871933,6824.88339347,10389.6482157,11749.5087193,13324.8833935,0.553214619885,0.538461538462,0.04,-0.06,0.06,0.04,0.045,0.564287051229], { threshold: 0.55 }],
    final: [[[6001, 6000, L16]],
      [1,0.500041663195,0.500041663195,0.500041663195,0.500041663195,0.500041663195,0.500041663195,0.500041663195,0,0,0,12001,12001,12001,1], { status: 'final' }]
  };
  var LIVE = 'status,phase,baseMixed,counted,pYes,finalShare,remaining,totalVotes,countedFraction,baseShare,observed,lateExpected,leans,trailingNeeds,sims,draws';
  Object.keys(CASES).forEach(function (k) {
    var c = CASES[k], r = M.forecast(data(c[0], c[2]), FROZEN), q = r.finalShare, got = [r.pYes];
    [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95].forEach(function (p) { got.push(q[p]); });
    [0.1, 0.5, 0.9].forEach(function (p) { got.push(r.remaining[p]); });
    [0.1, 0.5, 0.9].forEach(function (p) { got.push(r.totalVotes[p]); });
    got.push(r.countedFraction);
    if (r.status === 'live') got.push(r.baseShare, r.lateExpected, r.leans.electionNight.mean, r.leans.electionNight.sd, r.leans.postElection.mean, r.leans.postElection.sd, r.trailingNeeds.share);
    assert.strictEqual(got.length, c[1].length, k);
    got.forEach(function (x, i) { near(x, c[1][i], 1e-9 * Math.max(1, Math.abs(x)), k + ' number ' + i); });
    // Every old field is still there; the call only adds remainingMax, threshold and counted.t.
    var old = r.status === 'live' ? LIVE.split(',') : 'status,phase,pYes,finalShare,remaining,totalVotes,countedFraction,counted,trailingNeeds'.split(',');
    assert.deepStrictEqual(Object.keys(r).filter(function (x) { return old.indexOf(x) < 0; }).sort(), ['remainingMax', 'threshold']);
    assert.deepStrictEqual(Object.keys(r.counted), ['yes', 'no', 'n', 'share', 'when', 't']);
  });
});
