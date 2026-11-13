/* Musa · reproductor de escenas animadas (spec 3 §4).
 * Script clásico, sin dependencias, sin fetch ni canvas.
 * window.MusaScene = { mountScene(container, scene, {autoplay, baseUrl}), _math }
 * La parte matemática (_math) es idéntica a scene_math.py y funciona sin DOM. */
(function (root) {
  "use strict";

  var TWO_PI = 2 * Math.PI;
  var FALL_FADE = 0.15;

  /* ---------- matemática pura (espejo de scene_math.py) ---------- */
  function frac(x) { return x - Math.floor(x); }
  function roundHalfUp(x) { return Math.floor(x + 0.5); }
  function phaseAt(t, period, phase) { return frac(t / period + phase); }

  function bob(amplitude, period, phase, t) {
    return -amplitude * Math.sin(TWO_PI * phaseAt(t, period, phase));
  }
  function sway(amplitude, period, phase, t) {
    return amplitude * Math.sin(TWO_PI * phaseAt(t, period, phase));
  }
  function pulse(amplitude, period, phase, t) {
    return 1 + amplitude * Math.sin(TWO_PI * phaseAt(t, period, phase));
  }
  function fall(amplitude, period, phase, copies, t) {
    var p = phaseAt(t, period, phase), dy = [], opacity = [];
    for (var c = 0; c < copies; c++) {
      var pc = frac(p + c / copies);
      dy.push(-amplitude * (1 - pc));
      opacity.push(Math.min(1, pc / FALL_FADE, (1 - pc) / FALL_FADE));
    }
    return { dy: dy, opacity: opacity };
  }
  function fallAutoAmplitude(height) { return roundHalfUp(1.2 * height); }

  function pivot(preset, box) {
    var x0 = box[0], y0 = box[1], x1 = box[2], y1 = box[3];
    if (preset === "sway") return [(x0 + x1) / 2, y1];
    if (preset === "pulse") return [(x0 + x1) / 2, (y0 + y1) / 2];
    return [0, 0];
  }

  function seqLength(n, loop) {
    if (loop === "pingpong" && n > 1) return 2 * n - 2;
    return Math.max(n, 1);
  }
  function seqToIndex(s, n, loop) {
    if (loop === "pingpong" && n > 1) return s < n ? s : 2 * n - 2 - s;
    return s;
  }
  function mod(a, n) { return ((a % n) + n) % n; }

  function framesIndex(n, fps, loop, t) {
    var k = Math.floor(t * fps);
    return seqToIndex(mod(k, seqLength(n, loop)), n, loop);
  }
  function statesIndex(m, hold, loop, t) {
    var k = Math.floor(t / hold);
    if (loop === "once") return Math.min(k, m - 1);
    return seqToIndex(mod(k, seqLength(m, loop)), m, loop);
  }

  function framesPlacement(box, frameW, frameH, frameBoxes) {
    var f = null;
    for (var i = 0; i < frameBoxes.length; i++) {
      if (frameBoxes[i] != null) { f = frameBoxes[i]; break; }
    }
    if (f === null) throw new Error("frame_boxes sin ningún frame");
    var s = (box[3] - box[1]) / (f[3] - f[1]);
    return [box[0] - f[0] * s, box[1] - f[1] * s, frameW * s, frameH * s];
  }
  function statesPlacement(bboxK, offsetLayer, offsetK) {
    return [
      bboxK[0] + offsetLayer[0] + offsetK[0],
      bboxK[1] + offsetLayer[1] + offsetK[1],
      bboxK[2] - bboxK[0],
      bboxK[3] - bboxK[1]
    ];
  }

  var _math = {
    frac: frac, roundHalfUp: roundHalfUp, phaseAt: phaseAt,
    bob: bob, sway: sway, pulse: pulse, fall: fall,
    fallAutoAmplitude: fallAutoAmplitude, pivot: pivot,
    seqLength: seqLength, seqToIndex: seqToIndex,
    framesIndex: framesIndex, statesIndex: statesIndex,
    framesPlacement: framesPlacement, statesPlacement: statesPlacement
  };

  /* ---------- reproductor (DOM) ---------- */
  function pct(v, total) { return (v / total * 100) + "%"; }

  function placeBox(el, box, W, H) {
    var s = el.style;
    s.position = "absolute";
    s.left = pct(box.x, W);
    s.top = pct(box.y, H);
    s.width = pct(box.w, W);
    s.height = pct(box.h, H);
  }

  function makeImg(url, box, W, H) {
    var img = document.createElement("img");
    img.src = url;
    img.alt = "";
    img.draggable = false;
    img.style.display = "block";
    img.style.maxWidth = "none";
    img.style.pointerEvents = "none";
    placeBox(img, box, W, H);
    return img;
  }

  function setOrigin(el, piv, box) {
    el.style.transformOrigin =
      ((piv[0] - box.x) / box.w * 100) + "% " + ((piv[1] - box.y) / box.h * 100) + "%";
  }

  function mountScene(container, scene, opts) {
    opts = opts || {};
    var autoplay = opts.autoplay !== false;
    var baseUrl = opts.baseUrl || "";
    var W = scene.width, H = scene.height;

    container.style.position = "relative";
    container.style.overflow = "hidden";
    container.style.aspectRatio = W + " / " + H;
    container.style.width = "100%";

    var k = 1;
    var updaters = [];  // cada uno: function (t)
    var els = [];

    function measure() {
      var cw = container.clientWidth;
      k = cw > 0 ? cw / W : 1;
    }

    (scene.layers || []).forEach(function (layer) {
      if (layer.visible === false) return;
      var anim = layer.animation || { type: "static" };
      var base = { x: layer.x, y: layer.y, w: layer.w, h: layer.h };
      var el, i;

      if (anim.type === "motion") {
        var preset = anim.preset, period = anim.period, phase = anim.phase || 0;
        var piv = anim.pivot || [0, 0];
        if (preset === "fall") {
          var copies = anim.copies || 3;
          var amp = anim.amplitude;
          var imgs = [];
          for (i = 0; i < copies; i++) {
            el = makeImg(baseUrl + layer.file, base, W, H);
            container.appendChild(el); els.push(el); imgs.push(el);
          }
          updaters.push(function (t) {
            var r = fall(amp, period, phase, copies, t);
            for (var c = 0; c < copies; c++) {
              imgs[c].style.transform = "translate(0px, " + (r.dy[c] * k) + "px)";
              imgs[c].style.opacity = String(r.opacity[c]);
            }
          });
        } else {
          el = makeImg(baseUrl + layer.file, base, W, H);
          container.appendChild(el); els.push(el);
          setOrigin(el, piv, base);
          var amp2 = anim.amplitude;
          updaters.push(function (t) {
            if (preset === "bob") {
              el.style.transform = "translate(0px, " + (bob(amp2, period, phase, t) * k) + "px)";
            } else if (preset === "sway") {
              el.style.transform = "rotate(" + sway(amp2, period, phase, t) + "deg)";
            } else if (preset === "pulse") {
              el.style.transform = "scale(" + pulse(amp2, period, phase, t) + ")";
            }
          });
        }
      } else if (anim.type === "frames") {
        var n = anim.frames, fps = anim.fps, loop = anim.loop || "loop";
        var fbox = { x: anim.x, y: anim.y, w: anim.w, h: anim.h };
        el = document.createElement("div");
        el.style.backgroundImage = "url(\"" + String(baseUrl + anim.file).replace(/"/g, "%22") + "\")";
        el.style.backgroundRepeat = "no-repeat";
        el.style.backgroundSize = (n * 100) + "% 100%";
        el.style.pointerEvents = "none";
        placeBox(el, fbox, W, H);
        container.appendChild(el); els.push(el);
        updaters.push(function (t) {
          var idx = framesIndex(n, fps, loop, t);
          el.style.backgroundPosition = (n > 1 ? idx / (n - 1) * 100 : 0) + "% 0%";
        });
      } else if (anim.type === "states") {
        var hold = anim.hold, sloop = anim.loop || "loop";
        var list = [{ file: layer.file, x: layer.x, y: layer.y, w: layer.w, h: layer.h }]
          .concat(anim.states || []);
        var simgs = list.map(function (st) {
          var im = makeImg(baseUrl + st.file, st, W, H);
          container.appendChild(im); els.push(im);
          return im;
        });
        updaters.push(function (t) {
          var idx = statesIndex(list.length, hold, sloop, t);
          for (var s = 0; s < simgs.length; s++) {
            simgs[s].style.display = s === idx ? "block" : "none";
          }
        });
      } else {
        el = makeImg(baseUrl + layer.file, base, W, H);
        container.appendChild(el); els.push(el);
      }
    });

    function draw(t) {
      for (var i = 0; i < updaters.length; i++) updaters[i](t);
    }

    var ro = null;
    if (typeof root.ResizeObserver === "function") {
      ro = new root.ResizeObserver(function () { measure(); draw(current); });
      ro.observe(container);
    }

    var current = 0;      // t en segundos
    var raf = 0;
    var startedAt = 0;    // timestamp (ms) en que t = current
    var state = { playing: false };

    function frame(now) {
      if (!state.playing) return;
      current = (now - startedAt) / 1000;
      draw(current);
      raf = root.requestAnimationFrame(frame);
    }
    function play() {
      if (state.playing) return;
      state.playing = true;
      startedAt = performance.now() - current * 1000;
      raf = root.requestAnimationFrame(frame);
    }
    function pause() {
      state.playing = false;
      if (raf) root.cancelAnimationFrame(raf);
      raf = 0;
    }
    function seek(t) {
      current = t;
      if (state.playing) startedAt = performance.now() - t * 1000;
      draw(t);
    }
    function destroy() {
      pause();
      if (ro) ro.disconnect();
      for (var i = 0; i < els.length; i++) {
        if (els[i].parentNode) els[i].parentNode.removeChild(els[i]);
      }
      els = []; updaters = [];
    }

    measure();
    draw(0);
    var reduce = typeof root.matchMedia === "function" &&
      root.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (autoplay && !reduce) play();

    return {
      play: play, pause: pause, seek: seek, destroy: destroy,
      get playing() { return state.playing; }
    };
  }

  root.MusaScene = { mountScene: mountScene, _math: _math };
})(typeof window !== "undefined" ? window : globalThis);
