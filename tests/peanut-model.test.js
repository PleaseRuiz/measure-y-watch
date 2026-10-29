// Unit tests for the forecast engine. Run with: node --test tests/
'use strict';
var test = require('node:test');
var assert = require('node:assert');
var M = require('../js/peanut-model.js');
var P = require('../data/peanut-priors.json');

function data(rows, extra) {
  var d = { threshold: 0.5, history: rows.map(function (r) { return { yes: r[0], no: r[1], county_updated: r[2] }; }) };
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
