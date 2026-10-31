// Confetti for Measure Y Watch: one short burst over the page when Measure Y has something to
// celebrate (see index.html and forecast.html for when). Decoration only: clicks go through it,
// screen readers skip it, it's gone after a few seconds, and it doesn't run at all for readers who
// asked their device for less motion. A page without this file just doesn't celebrate.
(function () {
  'use strict';
  var COLORS = ['#0B7A61', '#1FA37F', '#4FD6B0', '#F2B33D', '#E8B98C', '#7A4CC2', '#3B82F6', '#EC4899'];
  var DURATION = 4500, FADE = 900;
  var running = null;

  function lessMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  // Returns true if confetti started (false with reduced motion, or while a burst is still falling).
  function burst() {
    if (lessMotion() || running || !document.body || !window.requestAnimationFrame) return false;
    var canvas = document.createElement('canvas');
    var ctx = canvas.getContext && canvas.getContext('2d');
    if (!ctx) return false;
    canvas.className = 'party-confetti';
    canvas.setAttribute('aria-hidden', 'true');
    canvas.style.cssText = 'position:fixed;left:0;top:0;width:100vw;height:100vh;pointer-events:none;z-index:1000';
    document.body.appendChild(canvas);
    var w = window.innerWidth, h = window.innerHeight, dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    ctx.scale(dpr, dpr);
    var n = Math.max(60, Math.min(180, Math.round(w / 4))), pieces = [];
    for (var i = 0; i < n; i++) {
      pieces.push({
        x: Math.random() * w, y: -20 - Math.random() * h * 0.6,
        vx: (Math.random() - 0.5) * 2.4, vy: 1.6 + Math.random() * 2.6,
        a: Math.random() * Math.PI * 2, va: (Math.random() - 0.5) * 0.24,
        sw: Math.random() * Math.PI * 2, size: 6 + Math.random() * 6, color: COLORS[i % COLORS.length]
      });
    }
    var start = null;
    running = canvas;
    function frame(t) {
      if (start === null) start = t;
      var age = t - start;
      ctx.clearRect(0, 0, w, h);
      ctx.globalAlpha = age > DURATION - FADE ? Math.max(0, (DURATION - age) / FADE) : 1;
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
      if (age < DURATION) window.requestAnimationFrame(frame);
      else { canvas.remove(); running = null; }
    }
    window.requestAnimationFrame(frame);
    return true;
  }

  window.MYWParty = { burst: burst, lessMotion: lessMotion };
})();
