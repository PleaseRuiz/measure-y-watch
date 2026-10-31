// Confetti for Measure Y Watch. When Measure Y has something to celebrate (see index.html and
// forecast.html for when), confetti pops once per visit, then a few pieces keep drifting down,
// slowly, for as long as the celebration lasts. Decoration only: taps go through it, screen readers
// skip it, it rests while the tab is hidden, and nothing moves for readers who asked their device for
// less motion (even if they ask while it falls). The "Confetti ON/OFF" chip (button#confettiBtn with
// a .state span) stops it, and the choice is remembered. A page without this file just doesn't celebrate.
//
// window.MYWParty
//   celebrate(on)  The page says whether there's something to celebrate right now (safe to call on every
//                  update). On: the pop (once per visit) and the trickle, unless the reader turned confetti
//                  off, and the chip shows. Off: the trickle fades out and the chip hides.
//   burst()        One short burst, for a tap. It works with the chip OFF too: the reader asked for it.
//   start(opts)    The pop (not with {pop: false}), then the trickle until stop(). False if nothing moved.
//   stop()         The trickle ends: whatever is falling fades out in about half a second.
//   isOn()         Whether the trickle is on (it rests while the tab is hidden or with less motion).
//   lessMotion()   Whether the reader asked for less motion.
(function () {
  'use strict';
  var COLORS = ['#0B7A61', '#1FA37F', '#4FD6B0', '#F2B33D', '#E8B98C', '#7A4CC2', '#3B82F6', '#EC4899'];
  var DURATION = 4500, FADE = 900, QUICK = 500;
  var KEY = 'myw-confetti'; // '0' = the reader turned confetti off
  var motionQuery = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  var on = false;   // the trickle is on: start() until stop()
  var pop = null;   // the burst that's falling, if any

  function lessMotion() { return !!(motionQuery && motionQuery.matches); }

  // ---------- the pop ----------

  // Returns true if confetti started (false with reduced motion, or while a burst is still falling).
  function burst() {
    if (lessMotion() || pop || !document.body || !window.requestAnimationFrame) return false;
    var canvas = document.createElement('canvas');
    var ctx = canvas.getContext && canvas.getContext('2d');
    if (!ctx) return false;
    canvas.className = 'party-confetti';
    canvas.setAttribute('aria-hidden', 'true');
    canvas.style.cssText = 'position:fixed;left:0;top:0;width:100vw;height:100vh;pointer-events:none;z-index:1000';
    document.body.appendChild(canvas);
    var w, h;
    function fit() {
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth; h = window.innerHeight;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    fit();
    var n = Math.max(60, Math.min(180, Math.round(w / 4))), pieces = [];
    for (var i = 0; i < n; i++) {
      pieces.push({
        x: Math.random() * w, y: -20 - Math.random() * h * 0.6,
        vx: (Math.random() - 0.5) * 2.4, vy: 1.6 + Math.random() * 2.6,
        a: Math.random() * Math.PI * 2, va: (Math.random() - 0.5) * 0.24,
        sw: Math.random() * Math.PI * 2, size: 6 + Math.random() * 6, color: COLORS[i % COLORS.length]
      });
    }
    var start = null, quickAt = null;
    var me = pop = {
      fit: fit,
      quick: function () { if (quickAt === null) quickAt = -1; }, // fade out now (stop)
      kill: function () { pop = null; canvas.remove(); }          // gone at once (less motion)
    };
    function frame(t) {
      if (pop !== me) return;
      if (start === null) start = t;
      var age = t - start;
      if (quickAt === -1) quickAt = age;
      var alpha = age > DURATION - FADE ? (DURATION - age) / FADE : 1;
      if (quickAt !== null) alpha = Math.min(alpha, 1 - (age - quickAt) / QUICK);
      if (alpha <= 0) { me.kill(); return; }
      ctx.clearRect(0, 0, w, h);
      ctx.globalAlpha = alpha;
      for (var j = 0; j < pieces.length; j++) {
        var p = pieces[j];
        p.vy = Math.min(p.vy + 0.035, 5.5);
        p.sw += 0.05;
        p.x += p.vx + Math.sin(p.sw) * 0.6; p.y += p.vy; p.a += p.va;
        ctx.save();
        ctx.translate(p.x, p.y); ctx.rotate(p.a);
        ctx.fillStyle = p.color;
        ctx.fillRect(-p.size / 2, -p.size / 4, p.size, (p.size / 2) * Math.abs(Math.cos(p.a)) + 1);
        ctx.restore();
      }
      window.requestAnimationFrame(frame);
    }
    window.requestAnimationFrame(frame);
    return true;
  }

  // ---------- the trickle ----------
  // A few small pieces of paper. Each is a span that the browser's compositor moves (Web Animations,
  // transform and opacity only), so the page itself does no work while they fall. Each piece loops
  // through six different falls (a new spot, swing, spin and speed each time, about 70 seconds in all),
  // going back to the top around the outside of the screen between them. The same spans are used the
  // whole time, and they leave the page when the trickle ends.

  var MAX = 14, FALLS = 6;
  var CAN_TRICKLE = !!(window.Element && Element.prototype.animate && window.KeyframeEffect && KeyframeEffect.prototype.setKeyframes);
  var layer = null, pieces = [], fadeIn = null, fadeOut = null, vw = 0, vh = 0, resizeTimer = 0;

  // A handful: 8 on a small phone, up to 14 on a wide screen.
  function howMany(w) { return Math.max(8, Math.min(MAX, Math.round(w / 40))); }
  // The size the pieces fall through. Returns true if it changed.
  function measure() {
    var w = layer.clientWidth || window.innerWidth, h = layer.clientHeight || window.innerHeight;
    if (w === vw && h === vh) return false;
    vw = w; vh = h;
    return true;
  }

  function makeLayer() {
    layer = document.createElement('div');
    layer.className = 'party-trickle';
    layer.setAttribute('aria-hidden', 'true');
    // As tall as the screen with the browser's address bar hidden (100lvh), so the bar sliding in and out
    // as the reader scrolls changes nothing here.
    layer.style.cssText = 'position:fixed;left:0;top:0;width:100%;height:100%;height:100lvh;overflow:hidden;pointer-events:none;z-index:1000;contain:strict';
    for (var i = 0; i < MAX; i++) {
      var el = document.createElement('span');
      // Parked off screen until it falls. 10 x 5 px, scaled per fall (7 to 11.5 px long, like the pop's).
      el.style.cssText = 'position:absolute;left:0;top:0;width:10px;height:5px;border-radius:1px;opacity:.9;will-change:transform;' +
        'transform:translate(-40px,-40px);background:' + COLORS[(i * 3) % COLORS.length];
      layer.appendChild(el);
      pieces.push({ el: el, anim: null });
    }
  }

  // Six falls for a piece. Each: a random spot across the screen, swinging side to side as it drifts from
  // just above the screen to just below it in 9 to 14 seconds, turning and flipping like paper.
  var GAP = 3; // ms between two falls
  function plan(p) {
    p.falls = []; p.total = GAP * (FALLS - 1); p.rot = Math.random() * 360;
    for (var j = 0; j < FALLS; j++) {
      var f = { x: Math.random(), dur: 9000 + Math.random() * 5000, legs: 4 + Math.floor(Math.random() * 4),
        amp: 10 + Math.random() * 16, side: Math.random() < 0.5 ? -1 : 1, spin: (Math.random() - 0.5) * 300,
        flip: (2 + Math.random() * 4) * 360 * (Math.random() < 0.5 ? -1 : 1), scale: 0.7 + Math.random() * 0.45 };
      p.falls.push(f); p.total += f.dur;
    }
  }
  // The falls as keyframes in pixels for this screen size. Each leg eases in and out like a swing, so the
  // piece slows at the ends of its swing and drifts faster through the middle, like falling paper. Between
  // two falls the piece goes back to the top around the outside of the screen, in a few milliseconds.
  function path(p) {
    var out = [], at = 0, top = -24, bottom = vh + 24, rot = p.rot, flip = 0;
    function key(ms, x, y, scale) {
      out.push({ offset: Math.min(1, ms / p.total), easing: 'ease-in-out', transform: 'translate(' + x.toFixed(1) + 'px,' + y.toFixed(1) +
        'px) rotate(' + rot.toFixed(1) + 'deg) rotateX(' + flip.toFixed(1) + 'deg) scale(' + scale.toFixed(2) + ')' });
    }
    for (var j = 0; j < p.falls.length; j++) {
      var f = p.falls[j], x = f.x * vw, rot0 = rot, flip0 = flip;
      for (var k = 0; k <= f.legs; k++) {
        var t = k / f.legs;
        rot = rot0 + f.spin * t; flip = flip0 + f.flip * t;
        key(at + f.dur * t, x + f.amp * f.side * (k % 2 ? 1 : -1), top + (bottom - top) * t, f.scale);
      }
      at += f.dur;
      if (j < p.falls.length - 1) { key(at + 1, -40, bottom, f.scale); key(at + 2, -40, top, f.scale); at += GAP; }
    }
    out[out.length - 1].offset = 1;
    return out;
  }
  // Start a piece on new falls, part way through them (at: 0 to 1).
  function launch(p, at) {
    plan(p);
    if (!p.anim) p.anim = p.el.animate(path(p), { duration: p.total, iterations: Infinity });
    else { p.anim.effect.setKeyframes(path(p)); p.anim.effect.updateTiming({ duration: p.total }); }
    p.anim.currentTime = at * p.total;
    if (document.hidden) p.anim.pause(); else p.anim.play();
  }

  // Show the trickle, fading in after `delay` ms.
  function showTrickle(delay) {
    if (!CAN_TRICKLE || !document.body) return;
    try {
      if (!layer) makeLayer();
      var showing = !!layer.parentNode, from = showing ? getComputedStyle(layer).opacity : 0;
      if (fadeOut) { fadeOut.onfinish = null; fadeOut.cancel(); fadeOut = null; }
      if (fadeIn) { fadeIn.cancel(); fadeIn = null; }
      if (!showing) document.body.appendChild(layer);
      var resized = measure(), n = howMany(vw);
      for (var i = 0; i < pieces.length; i++) {
        var p = pieces[i], idle = !p.anim || p.anim.playState === 'idle' || p.anim.playState === 'finished';
        if (i >= n) { if (p.anim) p.anim.cancel(); }
        else if (!showing || idle) launch(p, Math.random());
        else if (resized) p.anim.effect.setKeyframes(path(p)); // resized while fading out
      }
      var a = fadeIn = layer.animate([{ opacity: from }, { opacity: 1 }], { duration: FADE, delay: delay, fill: 'backwards' });
      a.onfinish = function () { if (fadeIn === a) fadeIn = null; };
      if (document.hidden) a.pause();
    } catch (e) { hideTrickle(true); } // decoration only
  }

  // Fade the trickle out (or take it away at once) and let the browser forget it.
  function hideTrickle(now) {
    if (!layer || !layer.parentNode) return;
    if (now || document.hidden || !CAN_TRICKLE) { removeLayer(); return; }
    if (fadeOut) return;
    var from = getComputedStyle(layer).opacity;
    if (fadeIn) { fadeIn.cancel(); fadeIn = null; }
    fadeOut = layer.animate([{ opacity: from }, { opacity: 0 }], { duration: QUICK, fill: 'forwards' });
    fadeOut.onfinish = removeLayer;
  }
  function removeLayer() {
    if (fadeIn) { fadeIn.cancel(); fadeIn = null; }
    if (fadeOut) { fadeOut.onfinish = null; fadeOut.cancel(); fadeOut = null; }
    for (var i = 0; i < pieces.length; i++) if (pieces[i].anim) pieces[i].anim.cancel();
    if (layer && layer.parentNode) layer.parentNode.removeChild(layer);
  }

  // Hidden tab: everything holds still where it is, and carries on when the reader comes back.
  function eachAnim(fn) {
    if (!layer || !layer.parentNode) return;
    for (var i = 0; i < pieces.length; i++) if (pieces[i].anim && pieces[i].anim.playState !== 'idle') fn(pieces[i].anim);
    if (fadeIn) fn(fadeIn);
    if (fadeOut) fn(fadeOut);
  }
  function onVisibility() {
    if (document.hidden) { eachAnim(function (a) { a.pause(); }); return; }
    eachAnim(function (a) { if (a.playState === 'paused') a.play(); });
  }

  // New size or a turned phone: the same pieces, on paths that fit the new screen.
  function onResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (pop) pop.fit();
      if (!layer || !layer.parentNode || fadeOut || !measure()) return;
      try {
        var n = howMany(vw);
        for (var i = 0; i < pieces.length; i++) {
          var p = pieces[i], idle = !p.anim || p.anim.playState === 'idle';
          if (i >= n) { if (p.anim) p.anim.cancel(); } else if (idle) launch(p, Math.random()); else p.anim.effect.setKeyframes(path(p));
        }
      } catch (e) { hideTrickle(true); }
    }, 150);
  }

  // ---------- on and off ----------

  function start(opts) {
    if (on) return false;
    on = true;
    if (lessMotion()) return false; // it starts if the reader allows motion again
    var didPop = !(opts && opts.pop === false) && burst();
    showTrickle(didPop ? DURATION - FADE : 0); // the trickle takes over as the pop fades
    return true;
  }
  function stop() {
    if (!on) return;
    on = false;
    if (pop) pop.quick();
    hideTrickle(false);
  }
  function isOn() { return on; }

  // Less motion, asked for while confetti falls: everything stops at once.
  function onMotionChange() {
    if (lessMotion()) { if (pop) pop.kill(); hideTrickle(true); }
    else if (on) showTrickle(0);
    paintChip();
  }

  // ---------- the page's side: when to celebrate, and the chip ----------

  var party = false, popped = false, chip = null, bound = false; // popped: the pop once per visit
  function recall() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function save(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  var wanted = recall() !== '0';

  function celebrate(yes) {
    party = !!yes;
    if (!bound) bindChip();
    sync();
  }
  function sync() {
    if (party && wanted) { if (!on) { start({ pop: !popped }); popped = true; } }
    else stop();
    paintChip();
  }
  function bindChip() {
    bound = true;
    chip = document.getElementById('confettiBtn');
    if (!chip) return;
    chip.addEventListener('click', function () {
      wanted = !wanted;
      save(wanted ? '1' : '0');
      if (wanted && party) { start(); popped = true; } else stop(); // turning it on pops again
      paintChip();
    });
  }
  // The chip shows only while confetti can fall. If it hides while it has keyboard focus (the celebration ended
  // on a correction, a pause or an error), focus moves to the Wobble chip when that shows, else to the zone
  // label (#zone, tabindex -1), instead of being dropped to the page.
  function paintChip() {
    if (!chip) return;
    var hide = !(party && CAN_TRICKLE && !lessMotion());
    if (hide && !chip.hidden && document.activeElement === chip) {
      var w = document.getElementById('wobbleBtn'), to = w && !w.hidden ? w : document.getElementById('zone');
      if (to && to.focus) { try { to.focus(); } catch (e) {} }
    }
    chip.hidden = hide;
    chip.setAttribute('aria-pressed', wanted ? 'true' : 'false');
    var s = chip.querySelector('.state');
    if (s) s.textContent = wanted ? 'ON' : 'OFF';
  }

  if (motionQuery) {
    if (motionQuery.addEventListener) motionQuery.addEventListener('change', onMotionChange);
    else if (motionQuery.addListener) motionQuery.addListener(onMotionChange);
  }
  document.addEventListener('visibilitychange', onVisibility);
  window.addEventListener('resize', onResize);
  // The same choice in another tab of this site.
  window.addEventListener('storage', function (e) {
    if (e.key !== KEY && e.key !== null) return;
    wanted = recall() !== '0';
    sync();
  });

  window.MYWParty = { celebrate: celebrate, burst: burst, start: start, stop: stop, isOn: isOn, lessMotion: lessMotion };
})();
