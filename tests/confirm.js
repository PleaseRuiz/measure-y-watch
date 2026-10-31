// Check of the "confirmed to pass" rule (confirmedPass in js/peanut-model.js).
//
// Runs the made-up election nights of tests/calibrate.js (the same nights, in the same order) and, at every
// county update from Thu Nov 5 on, the forecast at full size, as on the page. It reports how many elections
// Measure Y was called in, whether a call was ever wrong, how often more votes were really left than Peanut's
// "at most" (r.remainingMax), how often every remaining vote being a NO would have sunk YES anyway, the closest
// call, and when passing races were first called, overall and by their final YES share.
//
//   node tests/confirm.js [elections] [world]     (about 1 minute per 300 elections and world)
//   world: matched | wild | late-swing | slow-count | all (default all)
//
// The numbers quoted on the forecast tab ("When Peanut calls it") and in README.md come from
//   node tests/confirm.js 1000 matched
//   node tests/confirm.js 1000 wild
//   node tests/confirm.js 600 late-swing
//   node tests/confirm.js 600 slow-count

'use strict';
var M = require('../js/peanut-model.js');
var P = require('../data/peanut-priors.json');
var C = require('./calibrate.js');

var DAYS = [['Fri Nov 13', '2026-11-13T23:59:00-08:00'], ['Mon Nov 16', '2026-11-16T23:59:00-08:00']];
var BUCKETS = [['50-52.5%', 0.525], ['52.5-55%', 0.55], ['55-60%', 0.6], ['60-65%', 0.65], ['65%+', 1]];
var CERT = Date.parse('2026-12-03T17:00:00-08:00');

function pct(a, b) { return (b ? 100 * a / b : 0).toFixed(1) + '%'; }

function run(worldName, n) {
  var rng = M.makeRng(M.hashString(worldName) ^ 12345);
  var s = { updates: 0, calls: 0, wrong: 0, elections: 0, wrongElections: 0, over: 0, overCalled: 0, sunk: 0, closest: Infinity, passed: 0, called: 0,
    by: DAYS.map(function () { return 0; }), share: [], days: [], buckets: BUCKETS.map(function () { return { n: 0, by16: 0 }; }) };
  for (var e = 0; e < n; e++) {
    var sim = C.simulateElection(rng, C.WORLDS[worldName]), first = null, wrongHere = false;
    if (sim.passed) s.passed++;
    sim.hist.forEach(function (h, i) {
      if (Date.parse(h.county_updated) < M.confirmFrom) return;
      var r = M.forecast({ threshold: 0.5, history: sim.hist }, P, { upTo: i }), left = sim.V - r.counted.n;
      s.updates++;
      if (left > r.remainingMax) s.over++;
      if (!M.confirmedPass(r)) return;
      s.calls++;
      if (!sim.passed) { s.wrong++; wrongHere = true; }
      if (left > r.remainingMax) s.overCalled++;
      // Every vote really left being a NO would have sunk YES (the call was right only because they didn't break that way).
      if (r.counted.yes <= 0.5 * sim.V) s.sunk++;
      // The NO share of the votes really left that would have sunk YES (over 100%: nothing could have).
      if (left > 0) s.closest = Math.min(s.closest, 1 - (0.5 * sim.V - r.counted.yes) / left);
      if (!first) first = { t: r.counted.t, share: r.counted.n / sim.V };
    });
    if (first) s.elections++;
    if (wrongHere) s.wrongElections++;
    if (!sim.passed) continue;
    var bk = 0;
    while (sim.finalShare > BUCKETS[bk][1]) bk++;
    s.buckets[bk].n++;
    if (!first) continue;
    s.called++; s.share.push(first.share); s.days.push((CERT - first.t) / 864e5);
    DAYS.forEach(function (d, k) { if (first.t <= Date.parse(d[1])) s.by[k]++; });
    if (first.t <= Date.parse(DAYS[1][1])) s.buckets[bk].by16++;
  }
  function median(a) { a = a.slice().sort(function (x, y) { return x - y; }); return a.length ? a[a.length >> 1] : NaN; }
  console.log('\n== world: ' + worldName + ' (' + n + ' elections, ' + s.passed + ' passed, ' + s.updates + ' county updates from Thu Nov 5 on)');
  console.log('Called in ' + s.elections + ' of ' + n + ' elections (' + s.calls + ' county updates), wrong in ' + s.wrongElections + ' (' + s.wrong + ' updates)');
  console.log('More votes really left than Peanut\'s "at most": ' + pct(s.over, s.updates) + ' of updates, ' + pct(s.overCalled, s.calls) + ' of calls (' + s.overCalled + ' of ' + s.calls + ')');
  console.log('Calls where every vote really left being a NO would have sunk YES: ' + s.sunk + ' of ' + s.calls);
  console.log('Closest call: YES would have lost only if ' + (s.closest === Infinity ? '-' : (100 * s.closest).toFixed(0) + '%') + ' of the votes really left were NO (over 100%: no way)');
  console.log('Passing races called by ' + DAYS.map(function (d, k) { return d[0] + ' ' + pct(s.by[k], s.passed); }).join(', ') +
    ' (the rest are never called on the county\'s schedule); median first call at ' + (100 * median(s.share)).toFixed(0) + '% counted, ' + median(s.days).toFixed(0) + ' days before Dec 3');
  console.log('Passing races called by Mon Nov 16, by final YES share: ' + BUCKETS.map(function (b, k) { var x = s.buckets[k]; return b[0] + ' ' + (x.n ? pct(x.by16, x.n) + ' (' + x.by16 + ' of ' + x.n + ')' : '-'); }).join(', '));
}

var nElections = parseInt(process.argv[2] || '300', 10);
var which = process.argv[3] || 'all';
(which === 'all' ? Object.keys(C.WORLDS) : [which]).forEach(function (w) { run(w, nElections); });
