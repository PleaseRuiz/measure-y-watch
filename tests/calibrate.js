// Calibration check for Peanut's Needle.
//
// Simulates many made-up election nights, runs the forecast after every county
// release, and checks that "70% chance" outcomes happen about 70% of the time and
// that the 80% ranges contain the final result about 80% of the time. Worlds can
// match the model's assumptions or deliberately break them.
//
//   node tests/calibrate.js [elections] [world]
//   world: matched | wild | late-swing | slow-count | all (default all)

'use strict';
var M = require('../js/peanut-model.js');
var P = require('../data/peanut-priors.json');

var RELEASES = [
  '2026-11-03T20:10:00-08:00', '2026-11-03T21:00:00-08:00', '2026-11-03T22:00:00-08:00',
  '2026-11-03T23:00:00-08:00', '2026-11-04T00:00:00-08:00', '2026-11-04T01:00:00-08:00',
  '2026-11-04T02:00:00-08:00', '2026-11-05T16:30:00-08:00', '2026-11-06T16:30:00-08:00',
  '2026-11-09T16:30:00-08:00', '2026-11-12T09:30:00-08:00', '2026-11-13T16:30:00-08:00',
  '2026-11-16T16:30:00-08:00'
];

var WORLDS = {
  // Drawn from the model's own assumptions: calibration should be near perfect.
  matched: { leanScale: 1, leanShift: [0, 0], vScale: 1, schedScale: 1, schedShift: 0, batchSd: 0.012 },
  // Everything 1.6x more uncertain than the model thinks.
  wild: { leanScale: 1.6, leanShift: [0, 0], vScale: 1.6, schedScale: 1.6, schedShift: 0, batchSd: 0.02 },
  // Late ballots lean 4 points more YES than the model expects.
  'late-swing': { leanScale: 1, leanShift: [0, 0.04], vScale: 1, schedScale: 1, schedShift: 0, batchSd: 0.012 },
  // The county counts slower than usual (less done at every release).
  'slow-count': { leanScale: 1, leanShift: [0, 0], vScale: 1, schedScale: 1, schedShift: -0.07, batchSd: 0.012 }
};

function simulateElection(rng, W) {
  var V = Math.round(P.turnout.y_votes_median * Math.exp(P.turnout.y_votes_log_sd * W.vScale * rng.normal()));
  var se = 0.40 + 0.20 * rng.uniform();
  var L = P.leans;
  var z1 = rng.normal(), z2 = L.correlation * z1 + Math.sqrt(1 - L.correlation * L.correlation) * rng.normal();
  var d1 = L.election_night.mean + W.leanShift[0] + L.election_night.sd * W.leanScale * z1;
  var d2 = L.post_election.mean + W.leanShift[1] + L.post_election.sd * W.leanScale * z2;
  // Same "counting speed" model as the engine: log shares share one factor (correlation rho).
  var z = rng.normal(), rho = P.schedule_correlation;
  var prev = 0;
  var cum = RELEASES.map(function (iso) {
    var s = M.scheduleAt(Date.parse(iso), P);
    var zz = Math.sqrt(rho) * z + Math.sqrt(1 - rho) * rng.normal();
    var c = (s.mean + W.schedShift) * Math.exp(s.sd / s.mean * W.schedScale * zz);
    c = Math.min(0.995, Math.max(prev + 0.003, c));
    prev = c;
    return c;
  });
  // Election night adds in-person ballots only: their share of all votes comes from the model's own
  // prior and is spread over the six night releases, instead of falling out of two noisy schedule draws.
  var VC = P.phases.vote_center_share, vcs = Math.min(0.3, Math.max(0.01, VC.mean + VC.sd * W.leanScale * rng.normal()));
  for (var i0 = 1; i0 <= 6; i0++) cum[i0] = cum[0] + vcs * i0 / 6;
  for (var i1 = 7; i1 < cum.length; i1++) cum[i1] = Math.min(0.995, Math.max(cum[i1 - 1] + 0.003, cum[i1]));
  var hist = [], yes = 0, no = 0, counted = 0;
  cum.forEach(function (c, i) {
    var target = Math.round(c * V), dn = target - counted;
    if (dn <= 0) return;
    var phase = M.phaseOf(Date.parse(RELEASES[i]), P);
    var p = phase === 0 ? se : se + (phase === 1 ? d1 : d2) + W.batchSd * rng.normal();
    p = Math.min(0.99, Math.max(0.01, p));
    var dy = 0;
    for (var k = 0; k < dn; k++) if (rng.uniform() < p) dy++;
    yes += dy; no += dn - dy; counted = target;
    hist.push({ yes: yes, no: no, county_updated: RELEASES[i] });
  });
  // The rest of the count, for the truth.
  var rest = V - counted, pr = Math.min(0.99, Math.max(0.01, se + d2 + W.batchSd * rng.normal())), ry = 0;
  for (var j = 0; j < rest; j++) if (rng.uniform() < pr) ry++;
  var finalShare = (yes + ry) / V;
  return { hist: hist, finalShare: finalShare, passed: finalShare > 0.5 };
}

function run(worldName, n) {
  var W = WORLDS[worldName], rng = M.makeRng(M.hashString(worldName) ^ 12345);
  var bins = [], brier = 0, count = 0, cover80 = 0, cover50 = 0, byRelease = {};
  for (var b = 0; b < 10; b++) bins.push({ n: 0, p: 0, hit: 0 });
  for (var e = 0; e < n; e++) {
    var sim = simulateElection(rng, W);
    sim.hist.forEach(function (_, i) {
      var r = M.forecast({ threshold: 0.5, history: sim.hist }, P, { upTo: i, sims: 3000 });
      var y = sim.passed ? 1 : 0;
      brier += (r.pYes - y) * (r.pYes - y); count++;
      var bin = Math.min(9, Math.floor(r.pYes * 10));
      bins[bin].n++; bins[bin].p += r.pYes; bins[bin].hit += y;
      if (sim.finalShare >= r.finalShare[0.1] && sim.finalShare <= r.finalShare[0.9]) cover80++;
      if (sim.finalShare >= r.finalShare[0.25] && sim.finalShare <= r.finalShare[0.75]) cover50++;
      var key = i === 0 ? 'first' : r.phase === 1 ? 'night' : 'post';
      var br = byRelease[key] || (byRelease[key] = { n: 0, brier: 0, c80: 0 });
      br.n++; br.brier += (r.pYes - y) * (r.pYes - y);
      if (sim.finalShare >= r.finalShare[0.1] && sim.finalShare <= r.finalShare[0.9]) br.c80++;
    });
  }
  console.log('\n== world: ' + worldName + ' (' + n + ' elections, ' + count + ' forecasts)');
  console.log('Brier ' + (brier / count).toFixed(4) + ' | 80% range covers ' + (100 * cover80 / count).toFixed(1) + '% | 50% range covers ' + (100 * cover50 / count).toFixed(1) + '%');
  Object.keys(byRelease).forEach(function (k) {
    var br = byRelease[k];
    console.log('  ' + k.padEnd(6) + ' Brier ' + (br.brier / br.n).toFixed(4) + '  80% covers ' + (100 * br.c80 / br.n).toFixed(1) + '%  (n=' + br.n + ')');
  });
  console.log('  said      happened   n');
  bins.forEach(function (bn, i) {
    if (!bn.n) return;
    console.log('  ' + (100 * bn.p / bn.n).toFixed(0).padStart(4) + '%    ' + (100 * bn.hit / bn.n).toFixed(0).padStart(5) + '%   ' + bn.n);
  });
}

var nElections = parseInt(process.argv[2] || '300', 10);
var which = process.argv[3] || 'all';
(which === 'all' ? Object.keys(WORLDS) : [which]).forEach(function (w) { run(w, nElections); });
