/* Peanut's Needle: the forecast engine behind "NYT forecast mode".
 *
 * Pure functions, no DOM. Runs in the browser (window.PeanutModel) and in Node
 * (require) for tests. Given the county reports saved in results.json and the
 * assumptions in data/peanut-priors.json, it simulates how the uncounted
 * ballots could break and returns the chance Measure Y passes, with ranges.
 *
 * The idea: the first county report (8:10 PM) is early mail ballots, and its YES
 * share is the baseline. Ballots counted later come in two batches that can lean
 * differently from that baseline: election-night updates (vote-center ballots
 * and more mail) and post-election updates (mail dropped off on Election Day or
 * arriving late, provisionals). Each batch's lean starts from a prior built from
 * San Mateo County history and is updated by how the actual increments break.
 * How many votes are left comes from a turnout prior plus the share of the final
 * count the county usually has finished by each release.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.PeanutModel = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // ---------- random numbers (seeded, so every viewer sees the same numbers) ----------

  function hashString(s) {
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }

  function makeRng(seed) {
    var a = seed >>> 0, spare = null;
    function uniform() {
      a = (a + 0x6D2B79F5) >>> 0;
      var t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    }
    function normal() {
      if (spare !== null) { var s = spare; spare = null; return s; }
      var u = 0;
      while (u === 0) u = uniform();
      var r = Math.sqrt(-2 * Math.log(u)), th = 2 * Math.PI * uniform();
      spare = r * Math.sin(th);
      return r * Math.cos(th);
    }
    return { uniform: uniform, normal: normal };
  }

  function clamp(x, lo, hi) { return x < lo ? lo : x > hi ? hi : x; }
  function toMs(iso) { var t = Date.parse(iso); return isNaN(t) ? null : t; }

  // ---------- reading the county history ----------

  // Reports with votes, oldest first, one per distinct count. With remapTimes, report i is
  // treated as if it arrived at the i-th scheduled county release (used for demo and rehearsal data).
  function cleanHistory(data, priors, remapTimes) {
    var rows = ((data && data.history) || []).map(function (h) {
      return h ? { yes: Number(h.yes), no: Number(h.no), t: toMs(h.county_updated), when: h.county_updated } : null;
    }).filter(function (h) {
      return h && Number.isInteger(h.yes) && Number.isInteger(h.no) && h.yes >= 0 && h.no >= 0 && h.yes + h.no > 0 && h.t !== null;
    }).map(function (h) { h.n = h.yes + h.no; return h; }).sort(function (a, b) { return a.t - b.t; });
    var out = [];
    rows.forEach(function (r) {
      var prev = out[out.length - 1];
      if (prev && prev.yes === r.yes && prev.no === r.no) return; // a re-stamp, not new votes
      // A correction replaces every earlier report it undercuts, so the counts only ever grow.
      while (out.length && (r.yes < out[out.length - 1].yes || r.no < out[out.length - 1].no)) out.pop();
      out.push(r);
    });
    if (remapTimes && priors && priors.demo_release_times) {
      out.forEach(function (r, i) {
        var list = priors.demo_release_times;
        r.t = toMs(list[Math.min(i, list.length - 1)]);
      });
    }
    return out;
  }

  function phaseOf(ms, priors) {
    if (ms < toMs(priors.phases.first_report_ends)) return 0;
    if (ms < toMs(priors.phases.election_night_ends)) return 1;
    return 2;
  }

  // Expected share of the final count finished by time ms (piecewise linear between releases).
  function scheduleAt(ms, priors) {
    var pts = priors.schedule;
    if (ms <= toMs(pts[0].at)) return { mean: pts[0].counted_share, sd: pts[0].sd };
    for (var i = 1; i < pts.length; i++) {
      var t1 = toMs(pts[i].at);
      if (ms <= t1) {
        var t0 = toMs(pts[i - 1].at), f = (ms - t0) / (t1 - t0);
        return {
          mean: pts[i - 1].counted_share + f * (pts[i].counted_share - pts[i - 1].counted_share),
          sd: pts[i - 1].sd + f * (pts[i].sd - pts[i - 1].sd)
        };
      }
    }
    var lastPt = pts[pts.length - 1];
    return { mean: lastPt.counted_share, sd: lastPt.sd };
  }

  // ---------- how many votes there will be (exact Gaussian conditioning in log space) ----------

  // Unknowns: x = log(total votes on Y) and, for each report we condition on, w = log(share of the
  // final count done by then). Releases share one "counting speed" factor, so their w's are
  // correlated (rho). Each report says log(votes counted) = w + x, give or take a little.
  // Conditioning on those turns the turnout prior and the schedule prior into a posterior for x.
  function totalsPosterior(reports, priors) {
    var T = priors.turnout, rho = priors.schedule_correlation, tau = priors.noise.count_log_sd;
    var k = reports.length, n = k + 1;
    var mu = [Math.log(T.y_votes_median)], S = [];
    reports.forEach(function (rep) { mu.push(Math.log(rep.sched.mean)); });
    for (var i = 0; i < n; i++) { S.push([]); for (var j = 0; j < n; j++) S[i].push(0); }
    S[0][0] = T.y_votes_log_sd * T.y_votes_log_sd;
    for (i = 1; i < n; i++) for (j = 1; j < n; j++) {
      var si = reports[i - 1].sched.sd / reports[i - 1].sched.mean, sj = reports[j - 1].sched.sd / reports[j - 1].sched.mean;
      S[i][j] = (i === j ? 1 : rho) * si * sj;
    }
    reports.forEach(function (rep, idx) {
      var h = []; for (var c = 0; c < n; c++) h.push(c === 0 || c === idx + 1 ? 1 : 0);
      var Sh = S.map(function (row) { var acc = 0; for (var c2 = 0; c2 < n; c2++) acc += row[c2] * h[c2]; return acc; });
      var hSh = tau * tau, pred = 0;
      for (var c3 = 0; c3 < n; c3++) { hSh += h[c3] * Sh[c3]; pred += h[c3] * mu[c3]; }
      var resid = Math.log(rep.n) - pred;
      for (var r = 0; r < n; r++) {
        mu[r] += Sh[r] / hSh * resid;
        for (var c4 = 0; c4 < n; c4++) S[r][c4] -= Sh[r] * Sh[c4] / hSh;
      }
    });
    return { meanLog: mu[0], sdLog: Math.sqrt(Math.max(S[0][0], 1e-12)) };
  }

  // ---------- learning how the later batches lean (two-dimensional Gaussian update) ----------

  // Leans are (YES share of a batch) minus (YES share of the baseline early mail).
  // The lean can drift a little from one update to the next within a group (the mix of ballots
  // changes), so each further update in a group adds lean_drift_sd of fresh uncertainty first.
  function leanPosterior(base, increments, priors) {
    var L = priors.leans, q = priors.noise.lean_drift_sd || 0, seen = [false, false];
    var m = [L.election_night.mean, L.post_election.mean];
    var s1 = L.election_night.sd, s2 = L.post_election.sd, rho = L.correlation;
    var S = [[s1 * s1, rho * s1 * s2], [rho * s1 * s2, s2 * s2]];
    increments.forEach(function (inc) {
      if (inc.n < priors.noise.min_increment_votes || inc.phase === 0) return;
      var j = inc.phase === 1 ? 0 : 1;
      if (seen[j]) S[j][j] += q * q;
      seen[j] = true;
      var p = inc.yes / inc.n;
      var r = priors.noise.increment_sd * priors.noise.increment_sd + p * (1 - p) / inc.n;
      var k0 = S[0][j] / (S[j][j] + r), k1 = S[1][j] / (S[j][j] + r);
      var resid = (p - base) - m[j];
      m = [m[0] + k0 * resid, m[1] + k1 * resid];
      var Sj0 = S[j][0], Sj1 = S[j][1];
      S = [[S[0][0] - k0 * Sj0, S[0][1] - k0 * Sj1], [S[1][0] - k1 * Sj0, S[1][1] - k1 * Sj1]];
    });
    return { mean: m, cov: S };
  }

  // A draw of both leans. With tailDf set, the draw is Student-t with the same scale as the
  // normal: the middle looks the same, but big surprises are much likelier.
  function drawPair(rng, post, tailDf) {
    var a = Math.sqrt(Math.max(post.cov[0][0], 1e-12));
    var b = post.cov[1][0] / a;
    var c = Math.sqrt(Math.max(post.cov[1][1] - b * b, 1e-12));
    var z1 = rng.normal(), z2 = rng.normal(), k = 1;
    if (tailDf && tailDf > 2) {
      var chi = 0;
      for (var i = 0; i < tailDf; i++) { var z = rng.normal(); chi += z * z; }
      k = Math.sqrt(tailDf / Math.max(chi, 1e-9));
    }
    return [post.mean[0] + k * a * z1, post.mean[1] + k * (b * z1 + c * z2)];
  }

  function tailWidth(priors) {
    var df = priors.leans.tail_df;
    return df && df > 2 ? Math.sqrt(df / (df - 2)) : 1;
  }

  // ---------- weighted summaries ----------

  function weightedQuantiles(values, weights, qs) {
    var idx = values.map(function (_, i) { return i; }).sort(function (a, b) { return values[a] - values[b]; });
    var total = 0;
    idx.forEach(function (i) { total += weights[i]; });
    var out = {}, acc = 0, qi = 0, sorted = qs.slice().sort(function (a, b) { return a - b; });
    for (var k = 0; k < idx.length && qi < sorted.length; k++) {
      acc += weights[idx[k]];
      while (qi < sorted.length && acc >= sorted[qi] * total) { out[sorted[qi]] = values[idx[k]]; qi++; }
    }
    while (qi < sorted.length) { out[sorted[qi]] = values[idx[idx.length - 1]]; qi++; }
    return out;
  }

  var QS = [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95];

  // ---------- the forecast ----------

  /**
   * forecast(data, priors, opts) -> result
   *   data: the results.json object (uses history, status, threshold)
   *   priors: the data/peanut-priors.json object
   *   opts.sims: number of simulations (default priors.sims)
   *   opts.upTo: only use reports up to and including this index (for the "how it moved" chart)
   *   opts.remapTimes: place report i at the i-th scheduled release (demo/rehearsal data)
   *   opts.keepDraws: also return the simulated final YES shares (for back-testing)
   */
  function forecast(data, priors, opts) {
    opts = opts || {};
    var hist = cleanHistory(data, priors, opts.remapTimes);
    if (typeof opts.upTo === 'number') hist = hist.slice(0, opts.upTo + 1);
    if (!hist.length) return { status: 'waiting' };

    var last = hist[hist.length - 1];
    var threshold = (data && data.threshold) || 0.5;

    if (data && data.status === 'final' && typeof opts.upTo !== 'number') {
      return certain(last, last.yes / last.n > threshold, 'final');
    }

    hist.forEach(function (h) { h.phase = phaseOf(h.t, priors); });
    // A first report stamped during the night (late, or the 8:10 PM one was missed) still stands in
    // for the early mail; only a first report from after election night leaves the baseline mixed.
    var lateFirst = hist[0].phase === 1;
    if (lateFirst) hist[0].phase = 0;
    var nowPhase = last.phase;

    // Baseline: the YES share at the end of the first batch (normally the 8:10 PM early mail).
    var baseIdx = 0;
    while (baseIdx + 1 < hist.length && hist[0].phase === 0 && hist[baseIdx + 1].phase === 0) baseIdx++;
    var base = hist[baseIdx];
    var baseShare = base.yes / base.n;
    var baseMixed = lateFirst || hist[0].phase !== 0;

    var increments = [];
    for (var k = baseIdx + 1; k < hist.length; k++) {
      var dy = hist[k].yes - hist[k - 1].yes, dn = hist[k].n - hist[k - 1].n;
      if (dn > 0 && dy >= 0 && dy <= dn) increments.push({ yes: dy, n: dn, phase: hist[k].phase });
    }
    var post = leanPosterior(baseShare, increments, priors);

    // Election-night reports add vote-center ballots only, so the total is learned from the
    // first report and from post-election reports.
    var anchors = [{ n: base.n, sched: scheduleAt(base.t, priors) }];
    if (nowPhase === 2 && last !== base) anchors.push({ n: last.n, sched: scheduleAt(last.t, priors) });
    var tot = totalsPosterior(anchors, priors);
    var countedNight = nowPhase === 1 ? last.n - base.n : 0;
    var VC = priors.phases.vote_center_share;

    var sims = opts.sims || priors.sims || 40000;
    var seed = hashString(JSON.stringify([hist.map(function (h) { return [h.yes, h.no, h.t]; }), priors.version]));
    var rng = makeRng(seed);
    var Nz = priors.noise;
    var futureSd = Math.sqrt(Nz.remaining_batch_sd * Nz.remaining_batch_sd + Math.pow(Nz.lean_drift_sd || 0, 2));
    var finals = new Array(sims), remaining = new Array(sims), totals = new Array(sims), pass = 0;

    for (var s = 0; s < sims; s++) {
      var V = Math.exp(tot.meanLog + tot.sdLog * rng.normal());
      var R = Math.max(0, V - last.n);
      // Vote-center ballots still to come on election night (before the night ends); the rest come after.
      var vc = clamp(VC.mean + VC.sd * rng.normal(), 0.01, 0.3);
      var R1 = nowPhase <= 1 ? clamp(vc * V - countedNight, 0, R) : 0;
      var R2 = R - R1;

      var lean = drawPair(rng, post, priors.leans.tail_df);
      // Ballots not yet counted can drift from the ones counted so far, plus ordinary sampling noise.
      var p1 = clamp(baseShare + lean[0] + futureSd * rng.normal(), 0.01, 0.99);
      var p2 = clamp(baseShare + lean[1] + futureSd * rng.normal(), 0.01, 0.99);
      if (R1 > 0) p1 = clamp(p1 + Math.sqrt(p1 * (1 - p1) / R1) * rng.normal(), 0.01, 0.99);
      if (R2 > 0) p2 = clamp(p2 + Math.sqrt(p2 * (1 - p2) / R2) * rng.normal(), 0.01, 0.99);
      var total = last.n + R;
      var shareFinal = (last.yes + R1 * p1 + R2 * p2) / total + Nz.model_sd * (R / total) * rng.normal();

      finals[s] = shareFinal; remaining[s] = R; totals[s] = total;
      if (shareFinal > threshold) pass++;
    }

    var ones = finals.map(function () { return 1; });
    var shareQ = weightedQuantiles(finals, ones, QS);
    var remQ = weightedQuantiles(remaining, ones, [0.1, 0.5, 0.9]);
    var totQ = weightedQuantiles(totals, ones, [0.1, 0.5, 0.9]);
    var margin = last.yes - last.no, remMid = remQ[0.5];

    return {
      status: 'live',
      phase: nowPhase,
      baseMixed: baseMixed,
      counted: { yes: last.yes, no: last.no, n: last.n, share: last.yes / last.n, when: last.when },
      pYes: pass / sims,
      finalShare: shareQ,
      remaining: remQ,
      totalVotes: totQ,
      countedFraction: last.n / totQ[0.5],
      baseShare: baseShare,
      // Spread of each lean as drawn (Student-t draws are wider than their scale).
      leans: {
        electionNight: { mean: post.mean[0], sd: Math.sqrt(post.cov[0][0]) * tailWidth(priors) },
        postElection: { mean: post.mean[1], sd: Math.sqrt(post.cov[1][1]) * tailWidth(priors) }
      },
      // The share of the remaining votes the trailing side would need to come back (median remaining).
      trailingNeeds: remMid > 0 && margin !== 0 ? {
        side: margin > 0 ? 'NO' : 'YES',
        share: (remMid + Math.abs(margin)) / (2 * remMid)
      } : null,
      sims: sims,
      draws: opts.keepDraws ? finals : undefined
    };
  }

  function certain(last, passed, status) {
    var sh = last.yes / last.n, q = {};
    QS.forEach(function (k) { q[k] = sh; });
    return {
      status: status, phase: 2, pYes: passed ? 1 : 0, finalShare: q,
      remaining: { 0.1: 0, 0.5: 0, 0.9: 0 }, totalVotes: { 0.1: last.n, 0.5: last.n, 0.9: last.n },
      countedFraction: 1, counted: { yes: last.yes, no: last.no, n: last.n, share: sh, when: last.when },
      trailingNeeds: null
    };
  }

  // The wobble: the middle half of the chances Peanut gets when its assumptions (lean means,
  // counting pace and turnout) are each redrawn from their own uncertainty, 24 times.
  function wobble(data, priors, opts) {
    opts = opts || {};
    var J = opts.sets || 24, K = opts.scale || 1, L = priors.leans, ps = [];
    var rng = makeRng(hashString('wobble' + JSON.stringify((data && data.history) || [])));
    for (var j = 0; j < J; j++) {
      var p2 = JSON.parse(JSON.stringify(priors));
      var z1 = rng.normal(), z2 = L.correlation * z1 + Math.sqrt(1 - L.correlation * L.correlation) * rng.normal(), z3 = rng.normal(), z4 = rng.normal();
      p2.leans.election_night.mean += K * z1 * L.election_night.sd;
      p2.leans.post_election.mean += K * z2 * L.post_election.sd;
      p2.schedule.forEach(function (pt) { pt.counted_share = Math.min(1, pt.counted_share * Math.exp(K * z3 * pt.sd / pt.counted_share)); });
      p2.turnout.y_votes_median *= Math.exp(K * z4 * priors.turnout.y_votes_log_sd);
      ps.push(forecast(data, p2, { sims: opts.sims || 2500, remapTimes: opts.remapTimes }).pYes);
    }
    ps.sort(function (a, b) { return a - b; });
    function q(p) { var i = p * (ps.length - 1), lo = Math.floor(i), hi = Math.ceil(i); return ps[lo] + (ps[hi] - ps[lo]) * (i - lo); }
    return { lo: q(0.25), hi: q(0.75) };
  }

  // The forecast after each report, for the "how the needle moved" chart.
  function trajectory(data, priors, opts) {
    opts = opts || {};
    var hist = cleanHistory(data, priors, opts.remapTimes);
    return hist.map(function (h, i) {
      var r = forecast(data, priors, { upTo: i, sims: opts.sims || 12000, remapTimes: opts.remapTimes });
      return { when: h.when, t: h.t, pYes: r.pYes, median: r.finalShare[0.5], low: r.finalShare[0.1], high: r.finalShare[0.9], counted: h.yes / h.n };
    });
  }

  return {
    forecast: forecast, trajectory: trajectory, wobble: wobble, cleanHistory: cleanHistory, phaseOf: phaseOf,
    scheduleAt: scheduleAt, leanPosterior: leanPosterior, makeRng: makeRng, hashString: hashString
  };
});
