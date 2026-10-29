/* PEANUT (Peninsula Election Analysis & Needle, Unofficial Tally): the forecast engine behind
 * the forecast tab (forecast.html).
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

  // Inverse of the standard normal CDF (Acklam's approximation, error below 1.2e-9).
  function normInv(p) {
    var a = [-39.69683028665376, 220.9460984245205, -275.9285104469687, 138.357751867269, -30.66479806614716, 2.506628277459239];
    var b = [-54.47609879822406, 161.5858368580409, -155.6989798598866, 66.80131188771972, -13.28068155288572];
    var c = [-0.007784894002430293, -0.3223964580411365, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
    var d = [0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416];
    var q, r;
    if (p < 0.02425 || p > 0.97575) {
      q = Math.sqrt(-2 * Math.log(p < 0.5 ? p : 1 - p));
      var x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
      return p < 0.5 ? x : -x;
    }
    q = p - 0.5; r = q * q;
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
  }

  function clamp(x, lo, hi) { return x < lo ? lo : x > hi ? hi : x; }
  function toMs(iso) { var t = Date.parse(iso); return isNaN(t) ? null : t; }

  // ---------- reading the county history ----------

  // Reports with votes, oldest first, one per distinct count. With remapTimes, report i is
  // treated as if it arrived at the i-th scheduled county release (used for demo and rehearsal data).
  function count(v) {
    if (typeof v === 'number') return v;
    return typeof v === 'string' && /^\s*\d+\s*$/.test(v) ? Number(v) : NaN;
  }

  function cleanHistory(data, priors, remapTimes) {
    var rows = (data && Array.isArray(data.history) ? data.history : []).map(function (h) {
      return h ? { yes: count(h.yes), no: count(h.no), t: toMs(h.county_updated), when: h.county_updated } : null;
    }).filter(function (h) {
      return h && Number.isInteger(h.yes) && Number.isInteger(h.no) && h.yes >= 0 && h.no >= 0 && h.yes + h.no > 0 && h.t !== null;
    }).map(function (h) { h.n = h.yes + h.no; return h; }).sort(function (a, b) { return a.t - b.t; });
    var out = [];
    rows.forEach(function (r) {
      // A correction replaces every earlier report it undercuts, so the counts only ever grow.
      while (out.length && (r.yes < out[out.length - 1].yes || r.no < out[out.length - 1].no)) out.pop();
      var prev = out[out.length - 1];
      if (prev && prev.yes === r.yes && prev.no === r.no) return; // a re-stamp, not new votes
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
    if (tailDf > 2) {
      var chi = 0;
      for (var i = 0; i < tailDf; i++) { var z = rng.normal(); chi += z * z; }
      k = Math.sqrt(tailDf / Math.max(chi, 1e-9));
    }
    return [post.mean[0] + k * a * z1, post.mean[1] + k * (b * z1 + c * z2)];
  }

  // Degrees of freedom for the fat tails: a whole number above 2, or 0 for plain normal draws.
  function tailDf(priors) {
    var df = Math.round(Number(priors.leans.tail_df) || 0);
    return df > 2 ? df : 0;
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
  var BASE_MERGE_MS = 20 * 60000;
  // The wobble: 96 redraws of the assumptions with 800 simulations each (checked against a 600-redraw reference).
  var WOBBLE_SETS = 96, WOBBLE_SIMS = 800;

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

    // Baseline: the YES share at the end of the first batch (normally the 8:10 PM early mail). A
    // re-posted first report within 20 minutes joins it; anything later that night is in-person ballots.
    var baseIdx = 0;
    while (baseIdx + 1 < hist.length && hist[baseIdx + 1].phase === 0 && hist[baseIdx + 1].t - hist[0].t <= BASE_MERGE_MS) baseIdx++;
    for (var b = baseIdx + 1; b < hist.length; b++) if (hist[b].phase === 0) hist[b].phase = 1;
    var base = hist[baseIdx];
    var baseShare = base.yes / base.n;
    var baseMixed = lateFirst || hist[0].phase !== 0;
    var nowPhase = last.phase;

    var increments = [], observed = { night: { yes: 0, n: 0 }, post: { yes: 0, n: 0 } };
    for (var k = baseIdx + 1; k < hist.length; k++) {
      var dy = hist[k].yes - hist[k - 1].yes, dn = hist[k].n - hist[k - 1].n;
      if (dn > 0 && dy >= 0 && dy <= dn) {
        increments.push({ yes: dy, n: dn, phase: hist[k].phase });
        var grp = hist[k].phase === 2 ? observed.post : observed.night;
        grp.yes += dy; grp.n += dn;
      }
    }
    var post = leanPosterior(baseShare, increments, priors);

    // Election-night reports add vote-center ballots only, so the total is learned from the
    // first report and from post-election reports.
    var anchors = [{ n: base.n, sched: scheduleAt(base.t, priors) }];
    if (nowPhase === 2 && last !== base) anchors.push({ n: last.n, sched: scheduleAt(last.t, priors) });
    var tot = totalsPosterior(anchors, priors);
    var countedNight = nowPhase === 1 ? last.n - base.n : 0;
    var VC = priors.phases.vote_center_share;
    // A first report from later in the night already holds some vote-center ballots.
    var vcInBase = nowPhase <= 1 && lateFirst ? Math.max(0, scheduleAt(base.t, priors).mean - priors.schedule[0].counted_share) : 0;
    // Past the last scheduled release (certification day) nothing is left to count.
    var complete = scheduleAt(last.t, priors).mean >= 0.9995;

    var sims = opts.sims || priors.sims || 20000;
    // The same random numbers for every forecast: everyone sees the same chance for the same
    // count, and the needle only moves when the count does.
    var rng = makeRng(hashString('peanut|' + priors.version));
    var Nz = priors.noise, df = tailDf(priors);
    var futureSd = Math.sqrt(Nz.remaining_batch_sd * Nz.remaining_batch_sd + Math.pow(Nz.lean_drift_sd || 0, 2));
    var finals = new Array(sims), remaining = new Array(sims), totals = new Array(sims), pass = 0;

    for (var s = 0; s < sims; s++) {
      // Every simulation draws the same set of random numbers, whatever the count.
      var zV = rng.normal(), zVc = rng.normal(), lean = drawPair(rng, post, df);
      var z1 = rng.normal(), z2 = rng.normal(), e1 = rng.normal(), e2 = rng.normal(), zM = rng.normal();

      var V = complete ? last.n : Math.exp(tot.meanLog + tot.sdLog * zV);
      var R = Math.max(0, V - last.n);
      // Vote-center ballots still to come on election night (before the night ends); the rest come after.
      var vc = clamp(VC.mean + VC.sd * zVc, 0.01, 0.3);
      var R1 = nowPhase <= 1 ? clamp((vc - vcInBase) * V - countedNight, 0, R) : 0;
      var R2 = R - R1;

      // Ballots not yet counted can drift from the ones counted so far, plus ordinary sampling noise.
      var p1 = clamp(baseShare + lean[0] + futureSd * z1, 0.01, 0.99);
      var p2 = clamp(baseShare + lean[1] + futureSd * z2, 0.01, 0.99);
      if (R1 > 0) p1 = clamp(p1 + Math.sqrt(p1 * (1 - p1) / R1) * e1, 0.01, 0.99);
      if (R2 > 0) p2 = clamp(p2 + Math.sqrt(p2 * (1 - p2) / R2) * e2, 0.01, 0.99);
      var total = last.n + R;
      var shareFinal = clamp((last.yes + R1 * p1 + R2 * p2) / total + Nz.model_sd * (R / total) * zM, 0, 1);

      finals[s] = shareFinal; remaining[s] = R; totals[s] = total;
      if (shareFinal > threshold) pass++;
    }

    var ones = finals.map(function () { return 1; });
    var shareQ = weightedQuantiles(finals, ones, QS);
    var remQ = weightedQuantiles(remaining, ones, [0.1, 0.5, 0.9]);
    var totQ = weightedQuantiles(totals, ones, [0.1, 0.5, 0.9]);
    var remMid = remQ[0.5];
    // The YES share of the remaining votes that would put YES exactly on the pass line.
    var yesNeeds = remMid > 0 ? (threshold * (last.n + remMid) - last.yes) / remMid : null;

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
      // Votes counted so far in each later group (in-person on election night, late ballots after).
      observed: observed,
      // Each lean's typical spread, on the same scale as the priors file (the fat tails come on top).
      leans: {
        electionNight: { mean: post.mean[0], sd: Math.sqrt(post.cov[0][0]) },
        postElection: { mean: post.mean[1], sd: Math.sqrt(post.cov[1][1]) }
      },
      // The share of the remaining votes the trailing side would need to come back (median remaining).
      // YES trails at a tie: it needs 50% plus one vote.
      trailingNeeds: yesNeeds === null ? null : last.yes / last.n <= threshold
        ? { side: 'YES', share: yesNeeds } : { side: 'NO', share: 1 - yesNeeds },
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
  // counting pace and turnout) are redrawn from their own uncertainty. The redraws are spread evenly
  // over each assumption's range (a Latin hypercube with a fixed shuffle) and share the forecast's
  // random numbers, so the band only changes when the count does.
  function wobbleSets(priors, opts) {
    opts = opts || {};
    var J = opts.sets || WOBBLE_SETS, K = opts.scale || 1, L = priors.leans, rng = makeRng(hashString('wobble|' + priors.version));
    var strata = [0, 1, 2, 3].map(function () {
      var perm = [], i;
      for (i = 0; i < J; i++) perm.push(i);
      for (i = J - 1; i > 0; i--) { var m = Math.floor(rng.uniform() * (i + 1)), t = perm[i]; perm[i] = perm[m]; perm[m] = t; }
      return perm.map(function (k) { return normInv((k + 0.5) / J); });
    });
    var sets = [];
    for (var j = 0; j < J; j++) {
      var p2 = JSON.parse(JSON.stringify(priors));
      var z1 = strata[0][j], z2 = L.correlation * z1 + Math.sqrt(1 - L.correlation * L.correlation) * strata[1][j];
      var z3 = strata[2][j], z4 = strata[3][j];
      p2.leans.election_night.mean += K * z1 * L.election_night.sd;
      p2.leans.post_election.mean += K * z2 * L.post_election.sd;
      p2.schedule.forEach(function (pt) { pt.counted_share = Math.min(1, pt.counted_share * Math.exp(K * z3 * pt.sd / pt.counted_share)); });
      p2.turnout.y_votes_median *= Math.exp(K * z4 * priors.turnout.y_votes_log_sd);
      sets.push(p2);
    }
    return sets;
  }

  // shift (optional): the needle minus the chance under the shipped assumptions on the wobble's short
  // random stream. All redraws share that stream, so they share its sampling error; subtracting it puts
  // the band where much longer runs would put it, around the needle.
  function wobbleBand(ps, shift) {
    ps = ps.slice().sort(function (a, b) { return a - b; });
    function q(p) { var i = p * (ps.length - 1), lo = Math.floor(i), hi = Math.ceil(i); return ps[lo] + (ps[hi] - ps[lo]) * (i - lo); }
    var s = shift || 0;
    return { lo: clamp(q(0.25) + s, 0, 1), hi: clamp(q(0.75) + s, 0, 1) };
  }

  function wobbleShift(data, priors, needle, remap) {
    return needle - forecast(data, priors, { sims: WOBBLE_SIMS, remapTimes: remap }).pYes;
  }



  function wobble(data, priors, opts) {
    opts = opts || {};
    var ps = wobbleSets(priors, opts).map(function (p2) {
      return forecast(data, p2, { sims: opts.sims || WOBBLE_SIMS, remapTimes: opts.remapTimes }).pYes;
    });
    var shift = opts.sims ? 0 : wobbleShift(data, priors, forecast(data, priors, { remapTimes: opts.remapTimes }).pYes, opts.remapTimes);
    return wobbleBand(ps, shift);
  }

  // The forecast after each report, for the "how the needle moved" chart. With the default number
  // of simulations each point is exactly what the needle showed after that report.
  function trajectory(data, priors, opts) {
    opts = opts || {};
    var hist = cleanHistory(data, priors, opts.remapTimes);
    return hist.map(function (h, i) {
      var r = forecast(data, priors, { upTo: i < hist.length - 1 ? i : undefined, sims: opts.sims, remapTimes: opts.remapTimes });
      return { when: h.when, t: h.t, pYes: r.pYes, median: r.finalShare[0.5], low: r.finalShare[0.1], high: r.finalShare[0.9], counted: h.yes / h.n };
    });
  }

  // The page's work for one set of results, in small steps and in the order the page wants it: the
  // needle, then the wobble band, then the chart. Each step returns a message for the page, or null.
  // cache keeps chart points between calls: a point never changes once its report is in.
  function plan(data, priors, opts, cache) {
    opts = opts || {}; cache = cache || {};
    var remap = opts.remapTimes, r = null, steps = [], sets = null, ps = [], points = [], shift = null;
    steps.push(function () { r = forecast(data, priors, { remapTimes: remap }); return { type: 'needle', r: r }; });
    for (var j = 0; j < WOBBLE_SETS; j += 8) steps.push(wobbleStep(j));
    function wobbleStep(from) {
      return function () {
        if (r.status !== 'live') return null;
        sets = sets || wobbleSets(priors);
        if (shift === null) shift = wobbleShift(data, priors, r.pYes, remap);
        sets.slice(from, from + 8).forEach(function (p2) { ps.push(forecast(data, p2, { sims: WOBBLE_SIMS, remapTimes: remap }).pYes); });
        return ps.length === sets.length ? { type: 'band', band: wobbleBand(ps, shift) } : null;
      };
    }
    var hist = cleanHistory(data, priors, remap);
    hist.forEach(function (h, i) {
      steps.push(function () {
        var last = i === hist.length - 1, key = priors.version + JSON.stringify(hist.slice(0, i + 1).map(function (x) { return [x.yes, x.no, x.t]; }));
        var p = last ? r.pYes : key in cache ? cache[key] : (cache[key] = forecast(data, priors, { upTo: i, remapTimes: remap }).pYes);
        points.push({ t: h.t, pYes: p });
        return last ? { type: 'points', points: points, final: r.status === 'final' } : null;
      });
    });
    if (!hist.length) steps.push(function () { return { type: 'points', points: [], final: false }; });
    return steps;
  }

  return {
    forecast: forecast, trajectory: trajectory, wobble: wobble, wobbleSets: wobbleSets, wobbleBand: wobbleBand,
    wobbleShift: wobbleShift, plan: plan, cleanHistory: cleanHistory, phaseOf: phaseOf, scheduleAt: scheduleAt,
    leanPosterior: leanPosterior, makeRng: makeRng, hashString: hashString, normInv: normInv
  };
});
