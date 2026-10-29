/* Runs Peanut's Needle off the page's main thread, so the page never stutters while Peanut thinks.
 * The page posts {key, data, priors, remapTimes}; the worker answers with {key, out} messages
 * (the needle, then the wobble band, then the chart), or {key, error}. Newer results replace
 * any half-finished work.
 */
importScripts('peanut-model.js');

var cache = {}, cachedPriors = null, job = null, busy = false;

self.onmessage = function (e) {
  var m = e.data, pj = JSON.stringify(m.priors);
  // Chart points are kept per priors version; a fix that keeps the version must still redraw them.
  if (pj !== cachedPriors) { cache = {}; cachedPriors = pj; }
  job = { key: m.key, steps: self.PeanutModel.plan(m.data, m.priors, { remapTimes: m.remapTimes }, cache) };
  if (!busy) { busy = true; setTimeout(step, 0); }
};

function step() {
  if (!job || !job.steps.length) { busy = false; return; }
  var key = job.key;
  try {
    var out = job.steps.shift()();
    if (out) self.postMessage({ key: key, out: out });
  } catch (err) {
    self.postMessage({ key: key, error: String(err && err.message || err) });
    job = null;
  }
  setTimeout(step, 0);
}
