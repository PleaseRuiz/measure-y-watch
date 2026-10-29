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
