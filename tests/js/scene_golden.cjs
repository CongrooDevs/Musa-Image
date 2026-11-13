// Carga musa-scene.js en un vm sin DOM, evalúa los casos de oro con MusaScene._math
// e imprime {"results": [...]} (mismo formato que "expect" del fixture).
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(root, "src/musa/web/musa-scene.js"), "utf8");
const ctx = vm.createContext({});
vm.runInContext(src, ctx, { filename: "musa-scene.js" });
const M = ctx.MusaScene._math;

const fixture = JSON.parse(fs.readFileSync(path.join(root, "tests/fixtures/scene_golden.json"), "utf8"));

function evaluate(c) {
  const p = c.params, t = c.t;
  switch (c.type) {
    case "bob": return { dy: M.bob(p.amplitude, p.period, p.phase, t) };
    case "sway": return { theta: M.sway(p.amplitude, p.period, p.phase, t) };
    case "pulse": return { s: M.pulse(p.amplitude, p.period, p.phase, t) };
    case "fall": return M.fall(p.amplitude, p.period, p.phase, p.copies, t);
    case "frames": return { index: M.framesIndex(p.n, p.fps, p.loop, t) };
    case "states": return { index: M.statesIndex(p.m, p.hold, p.loop, t) };
    case "frames_placement":
      return { box: M.framesPlacement(p.box, p.frame_w, p.frame_h, p.frame_boxes) };
    case "states_placement":
      return { box: M.statesPlacement(p.bbox_k, p.offset_layer, p.offset_k) };
    case "pivot": return { pivot: M.pivot(p.preset, p.box) };
    case "fall_auto": return { amplitude: M.fallAutoAmplitude(p.height) };
    default: throw new Error("tipo desconocido: " + c.type);
  }
}

process.stdout.write(JSON.stringify({ results: fixture.cases.map(evaluate) }));
