// Back-test Peanut's Needle on real San Mateo County measures.
//
// tests/history-cases.json holds 23 local measures with a partial count from election
// night (or the next days) and the certified result, mapped onto this year's release times.
// For each partial count we run the forecast and check where the real result landed.
// Each measure has its own size, so the turnout assumption is centered 10% away from the
// true total (alternating high and low), roughly as wrong as a real turnout guess.
//
//   node tests/backtest.js [verbose]

'use strict';
var M = require('../js/peanut-model.js');
var P = require('../data/peanut-priors.json');
var cases = require('./history-cases.json');
var verbose = process.argv[2] === 'verbose';

var rows = [];
cases.forEach(function (c, ci) {
  var total = c.final[0] + c.final[1], actual = c.final[0] / total;
  var priors = JSON.parse(JSON.stringify(P));
  priors.turnout.y_votes_median = total * (ci % 2 ? 1.1 : 0.9);
  c.reports.forEach(function (_, i) {
    var hist = c.reports.slice(0, i + 1).map(function (r) { return { yes: r[0], no: r[1], county_updated: r[2] }; });
    var r = M.forecast({ threshold: c.threshold, history: hist }, priors, { sims: 20000, keepDraws: true });
    var below = 0;
    r.draws.forEach(function (d) { if (d < actual) below++; });
    var q = r.finalShare, last = c.reports[i];
    rows.push({
      measure: c.measure, kind: c.kind, stage: i === 0 ? 'first update we have' : 'later update',
      counted: (last[0] + last[1]) / total, now: last[0] / (last[0] + last[1]), actual: actual,
      q: q, pit: below / r.draws.length, p: r.pYes, passed: actual > c.threshold ? 1 : 0, threshold: c.threshold
    });
  });
});

function summarize(sel, name) {
  if (!sel.length) return;
  var n = sel.length, c50 = 0, c80 = 0, c90 = 0, bias = 0, brier = 0, width = 0;
  sel.forEach(function (x) {
    if (x.actual >= x.q[0.25] && x.actual <= x.q[0.75]) c50++;
    if (x.actual >= x.q[0.1] && x.actual <= x.q[0.9]) c80++;
    if (x.actual >= x.q[0.05] && x.actual <= x.q[0.95]) c90++;
    bias += x.actual - x.q[0.5]; width += x.q[0.9] - x.q[0.1];
    brier += (x.p - x.passed) * (x.p - x.passed);
  });
  console.log((name + '                                  ').slice(0, 32) + ' n=' + String(n).padStart(2) +
    ' | 50% range holds ' + (100 * c50 / n).toFixed(0).padStart(3) + '% | 80%: ' + (100 * c80 / n).toFixed(0).padStart(3) + '% | 90%: ' + (100 * c90 / n).toFixed(0).padStart(3) +
    '% | final minus forecast ' + (bias >= 0 ? '+' : '') + (100 * bias / n).toFixed(1) + ' pts | 80% width ' + (100 * width / n).toFixed(1) + ' pts | Brier ' + (brier / n).toFixed(3));
}

if (verbose) rows.forEach(function (x) {
  console.log('  ' + (x.measure + '                                                  ').slice(0, 58) + ' counted ' + (100 * x.counted).toFixed(0).padStart(3) + '% | then ' + (100 * x.now).toFixed(1) +
    '% -> final ' + (100 * x.actual).toFixed(1) + '% | forecast ' + (100 * x.q[0.5]).toFixed(1) + ' [' + (100 * x.q[0.1]).toFixed(1) + '-' + (100 * x.q[0.9]).toFixed(1) + '] | chance > ' + (100 * x.threshold).toFixed(0) + '%: ' + (100 * x.p).toFixed(0) + '% (' + (x.passed ? 'passed' : 'failed') + ')');
});
console.log('priors ' + P.version);
summarize(rows, 'All forecasts');
summarize(rows.filter(function (x) { return x.kind === 'property tax or school bond'; }), 'Property taxes and bonds');
summarize(rows.filter(function (x) { return x.kind !== 'property tax or school bond'; }), 'Other measures');
summarize(rows.filter(function (x) { return x.stage === 'first update we have'; }), 'At the first update');
summarize(rows.filter(function (x) { return x.stage !== 'first update we have'; }), 'At a later update');
