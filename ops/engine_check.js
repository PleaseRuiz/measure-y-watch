#!/usr/bin/env node
/* Checks that the forecast engine still works on a results file.
 *
 *   node ops/engine_check.js data/results.json
 *
 * Loads js/peanut-model.js and data/peanut-priors.json and runs forecast() on the file, then
 * wobble() while the count is live (the forecast tab draws the wobble band only then). Prints
 * "engine OK" and exits 0, or prints one line starting "engine check failed:" and exits 1 when the
 * file can't be read, something throws, a number in the result isn't finite, a chance is outside
 * 0-1, or a band (the wobble band or the final-share range) has lo above hi.
 *
 * Not run, and still "engine OK" (a note goes to stderr): no forecast.html (the forecast tab was
 * removed) or "paused": true in the priors (the tab shows nothing from the engine then).
 *
 * It never prints forecast numbers: workflow logs and issues are public.
 */
'use strict';
var fs = require('fs');
var path = require('path');

var ROOT = path.resolve(__dirname, '..');

function Failure(reason) { this.reason = reason; }
function fail(reason) { throw new Failure(reason); }

// Error text without decimals, so a forecast number can't slip into a public log.
function errorText(e) {
  var text = (e && e.name ? e.name + ': ' : '') + String(e && e.message || e);
  return text.replace(/\d+\.\d+(e-?\d+)?/gi, '#').replace(/\s+/g, ' ').slice(0, 160);
}

function readJson(file, label) {
  var text;
  try { text = fs.readFileSync(file, 'utf8'); } catch (e) { fail(label + ' could not be read'); }
  try { return JSON.parse(text); } catch (e) { fail(label + ' is not valid JSON'); }
}

// The first top-level field holding a number that isn't finite (draws are back-testing only).
function nonFinite(obj) {
  var stack = Object.keys(obj).filter(function (k) { return k !== 'draws'; }).map(function (k) { return [obj[k], k]; });
  while (stack.length) {
    var item = stack.pop(), v = item[0], top = item[1];
    if (typeof v === 'number') { if (!isFinite(v)) return top; }
    else if (v && typeof v === 'object') Object.keys(v).forEach(function (k) { stack.push([v[k], top]); });
  }
  return null;
}

function isChance(x) { return typeof x === 'number' && x >= 0 && x <= 1; }

function checkForecast(r) {
  if (!r || typeof r !== 'object') fail('forecast() returned nothing');
  if (r.status === 'waiting') return;
  var bad = nonFinite(r);
  if (bad) fail('forecast() returned a non-finite ' + bad);
  if (!isChance(r.pYes)) fail('forecast() returned a chance outside 0-1');
  var q = r.finalShare;
  if (q && typeof q === 'object') {
    var keys = Object.keys(q).map(Number).sort(function (a, b) { return a - b; });
    keys.forEach(function (k, i) {
      if (i && q[k] < q[keys[i - 1]]) fail('forecast() returned a final-share range with lo above hi');
    });
  }
}

function checkBand(b) {
  if (!b || typeof b !== 'object') fail('wobble() returned nothing');
  if (typeof b.lo !== 'number' || typeof b.hi !== 'number' || !isFinite(b.lo) || !isFinite(b.hi)) fail('wobble() returned a non-finite band');
  if (!isChance(b.lo) || !isChance(b.hi)) fail('wobble() returned a chance outside 0-1');
  if (b.lo > b.hi) fail('wobble() returned a band with lo above hi');
}

function check(file) {
  if (!fs.existsSync(path.join(ROOT, 'forecast.html'))) return 'no forecast tab; engine not run';
  var data = readJson(path.resolve(file), path.basename(file));
  if (!data || typeof data !== 'object' || Array.isArray(data)) fail(path.basename(file) + ' is not a results object');
  var priors = readJson(path.join(ROOT, 'data', 'peanut-priors.json'), 'data/peanut-priors.json');
  if (!priors || typeof priors !== 'object') fail('data/peanut-priors.json is not an object');
  if (priors.paused === true) return 'forecast tab paused; engine not run';
  var M;
  try { M = require(path.join(ROOT, 'js', 'peanut-model.js')); } catch (e) { fail('js/peanut-model.js did not load (' + errorText(e) + ')'); }
  if (!M || typeof M.forecast !== 'function' || typeof M.wobble !== 'function') fail('js/peanut-model.js has no forecast() or wobble()');
  var r;
  try { r = M.forecast(data, priors); } catch (e) { fail('forecast() threw ' + errorText(e)); }
  checkForecast(r);
  if (r.status !== 'live') return 'count is ' + (r.status === 'waiting' ? 'waiting for votes' : 'not live') + '; wobble not run';
  var band;
  try { band = M.wobble(data, priors); } catch (e) { fail('wobble() threw ' + errorText(e)); }
  checkBand(band);
  return '';
}

function main() {
  var file = process.argv[2];
  if (!file) { console.log('engine check failed: usage: node ops/engine_check.js <results.json>'); return 1; }
  try {
    var note = check(file);
    console.log('engine OK');
    if (note) console.error('(' + note + ')');
    return 0;
  } catch (e) {
    console.log('engine check failed: ' + (e instanceof Failure ? e.reason : 'unexpected ' + errorText(e)));
    return 1;
  }
}

process.exitCode = main();
