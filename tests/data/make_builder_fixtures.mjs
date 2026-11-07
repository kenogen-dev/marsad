// Runs the old Scan Builder's own functions (marsad/web/builder.html) on made-up settings and
// saves what they return, so the window's scan setup (relay 010, marsad/live.py scan_command,
// scan_warnings, conditions_text) can be tested against the page, case by case.
//
//   node tests/data/make_builder_fixtures.mjs     -> tests/data/builder_fixtures.json
//
// The functions are cut out of builder.html as text, not copied, and run in a sandbox with the
// globals boot() would set from /config (tests/data/builder_config.json, made from the made-up
// station tests/data/builder_station.json). Every input is synthetic.
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(here, '..', '..', 'marsad', 'web', 'builder.html'), 'utf8');
const cfg = JSON.parse(fs.readFileSync(path.join(here, 'builder_config.json'), 'utf8'));

function block(from) {
  let i = html.indexOf(from);
  if (i < 0) throw new Error('not found in the page: ' + from);
  let j = html.indexOf('{', i), depth = 0;
  for (; j < html.length; j++) {
    if (html[j] === '{') depth++;
    else if (html[j] === '}' && --depth === 0) return html.slice(i, j + 1);
  }
  throw new Error('unbalanced: ' + from);
}
function line(from) {
  const i = html.indexOf(from);
  if (i < 0) throw new Error('not found in the page: ' + from);
  return html.slice(i, html.indexOf('\n', i));
}

const src = [
  'var OUT = {};',
  'const $ = id => ({ set innerHTML(v) { OUT[id] = v; }, get value() { return OUT["v_" + id] || ""; } });',
  'let RTL_MAX = 2.8e6, RETUNE = 0.047, CEIL = 2147.483648, CMD = "marsad run";',
  'let FFTW = {};',
  'let GAINS = {}, LIMITS = {}, DEVICES = {}, GAPS = {}, RXLABEL = {}, TUNERS = {}, NOTES = [];',
  'let PRESETS = {}, chain = [], KNOWN = [];',
  'let ENCLOSURES = ["open bench", "in shielded box", "in shielded box, seam taped"];',
  'let SRC_LOAD = "50 ohm load";',
  line('const isSdr = m =>'),
  line('const sdrMod = () =>'),
  line('const isLoad = () =>'),
  line('const num = (v, d = 0) =>'),
  line('const fmtMhz = v =>'),
  block('function secs('),
  block('function chainText('), block('function conditionsText('),
  block('function stationNotes('),
  block('function geometry('), block('function planHops('), block('function geometryFftw('),
  block('function timeSplit('),
  block('function flags('), block('function command('), block('function drawFacts('),
  // boot()'s assignments from /config, as the page makes them
  `function boot(cfg) {
     RTL_MAX = cfg.rtl_max_sample_hz; RETUNE = cfg.retune_ms / 1000; CEIL = cfg.ceiling_mhz; CMD = cfg.cmd_prefix;
     FFTW = { ...cfg.fftw };
     for (const [rx, r] of Object.entries(cfg.receivers)) {
       GAINS[rx] = r.gains; LIMITS[rx] = r.range; DEVICES[rx] = r.device; GAPS[rx] = r.gaps; RXLABEL[rx] = r.label; TUNERS[rx] = r.tuner; }
     NOTES = cfg.notes || []; KNOWN = cfg.known || []; ENCLOSURES = cfg.enclosures || ENCLOSURES;
     const DUR_DEFAULT = { LS: [16, "h"], deep: [2, "h"], custom: [2, "h"] };
     for (const name of cfg.preset_order) { const p = cfg.presets[name]; PRESETS[name] = { ...p, dur: p.dur || DUR_DEFAULT[name] || [1, "h"] }; }
   }`,
  // update()'s geometry choice, then flags() and command()
  `function run(s, mods) {
     chain = mods.map(m => ({ ...m }));
     OUT = { v_engine: s.engine };
     const fftw = s.engine === "rtl_power_fftw";
     if (fftw) { s.hi = s.hiReq; s.clamped = false; } else { s.hi = Math.min(s.hiReq, CEIL); s.clamped = s.hiReq > CEIL; }
     const p = PRESETS[s.preset] || {};
     const centre = fftw && p.engine === "rtl_power_fftw" && p.centre != null && s.lo === p.lo && s.hiReq === p.hi ? p.centre : null;
     const g = fftw
       ? geometryFftw(s.lo, s.hiReq, s.frate, s.fbins, s.ftime, s.fover ?? 100 * s.crop, s.crop, s.durS, centre)
       : geometry(s.lo, s.hi, s.bin, s.integ, s.durS, s.crop);
     flags(g, s);
     drawFacts(s);
     const facts = [...(OUT.facts || "").matchAll(/<div class="fact (\\w*)"><b>(.*?)<\\/b><span>(.*?)<\\/span><\\/div>/gs)].map(m => [m[1], m[2], m[3]]);
     const fl = [...(OUT.flags || "").matchAll(/<div class="flag (\\w*)"><b>(.*?)<\\/b><span>(.*?)<\\/span><\\/div>/gs)].map(m => [m[1], m[2], m[3]]);
     return { command: command(g, s), flags: fl, facts, conditions: conditionsText(s), chain: chainText(),
              geometry: { hops: g.hops, bins: g.bins, sweeps: g.sweeps, listening: g.listening, binKhz: g.binKhz } };
   }`,
].join('\n');
const ctx = vm.createContext({ Math, JSON, Array, Object, Number, String, RegExp, isFinite, NaN, Infinity, Date });
vm.runInContext(src, ctx);
ctx.boot(cfg);

// the Builder's chain modules, as the page holds them (made-up names)
const M = (name, extra = {}) => ({ name, on: true, ...extra });
const antenna = [M("made-up antenna", { id: "src" }), M("Notch"), M("LNA"), M("Coax"),
  M("radio-a (in shielded box)", { id: "sdr", base: "radio-a", enc: "in shielded box" }), M("USB"), M("PC")];
const bench = [M("made-up antenna", { id: "src" }), M("Coax"),
  M("radio-a (open bench)", { id: "sdr", base: "radio-a", enc: "open bench" }), M("PC")];
const load = [M("50 ohm load", { id: "src" }), M("DC block"), M("Coax"),
  M("radio-a (in shielded box)", { id: "sdr", base: "radio-a", enc: "in shielded box" }), M("USB"), M("PC")];
const padded = [M("made-up antenna", { id: "src" }), M("10 dB attenuator"), M("Coax"),
  M("radio-b (open bench)", { id: "sdr", base: "radio-b", enc: "open bench" }), M("PC")];
const none = [M("made-up antenna", { id: "src", on: false }), M("PC", { on: false })];

const base = {
  preset: "band1", lo: 300, hiReq: 420, bin: 25, gain: 29, integ: 30, crop: 0.25, fir: 9,
  engine: "rtl_power", fbins: 128, ftime: 1, frate: 2400000, fover: null,
  durVal: 1, unit: "h", rx: "radio-a", device: "0", tag: "", target: null,
  analyze: true, useskip: true, skipmin: 16, thresh: 10, floorwin: null,
  dryfirst: false, nonint: true, temp: "", humid: "", where: "inside", sky: "", notes: ""
};
const fftw = { ...base, preset: "upper", engine: "rtl_power_fftw", lo: 2150, hiReq: 2190, bin: 18.75, gain: 29,
  integ: 1, fbins: 128, ftime: 1, frate: 2400000, fover: 25, rx: "radio-a" };
const S = (o) => { const s = { ...base, ...o }; s.durS = s.durVal * (s.unit === "h" ? 3600 : 60); return s; };
const F = (o) => { const s = { ...fftw, ...o }; s.durS = s.durVal * (s.unit === "h" ? 3600 : 60); return s; };

const cases = [
  ["preset as saved", S({}), antenna],
  ["tag, conditions, analysis options", S({ tag: "made-up-tag", temp: "21.5", humid: "40", where: "outside", sky: "overcast",
    notes: "fan on, \"quoted\" word", thresh: 8, floorwin: 0.5, skipmin: 10 }), antenna],
  ["dry-run line first, interactive", S({ dryfirst: true, nonint: false, temp: "18", tag: "t1" }), antenna],
  ["locked settings changed (custom range)", S({ lo: 310.5, hiReq: 415.25, bin: 12.5, gain: 34, integ: 60 }), antenna],
  ["non-standard crop and FIR", S({ crop: 0.3, fir: 4 }), antenna],
  ["no analysis, no warm-up", S({ analyze: false, useskip: false }), antenna],
  ["other receiver, device mismatch", S({ rx: "radio-b", gain: 29.7, device: "3" }), padded],
  ["ceiling clamped", S({ preset: "wideish", lo: 1300, hiReq: 2200, bin: 50 }), antenna],
  ["outside the tuner", S({ lo: 30, hiReq: 120 }), antenna],
  ["stop below start", S({ lo: 420, hiReq: 300 }), antenna],
  ["PLL gap", S({ lo: 1050, hiReq: 1300, bin: 50, integ: 120 }), antenna],
  ["under the retune floor", S({ preset: "wideish", lo: 1300, hiReq: 2147, bin: 50, integ: 10 }), antenna],
  ["dwell under half", S({ preset: "wideish", lo: 1300, hiReq: 2147, bin: 50, integ: 26 }), antenna],
  ["dwell under three quarters", S({ preset: "wideish", lo: 1300, hiReq: 2147, bin: 50, integ: 50 }), antenna],
  ["tiny FFT", S({ bin: 200 }), antenna],
  ["thin sweeps", S({ integ: 600, durVal: 60, unit: "m" }), antenna],
  ["warm-up as long as the run", S({ durVal: 10, unit: "m", skipmin: 16 }), antenna],
  ["target on a hop centre", S({ target: 300 + 120 / 48 * 0.5 }), antenna],
  ["target clear of hop centres", S({ target: 301.0 }), antenna],
  ["narrow span, no floor window", S({ preset: "narrow", lo: 432, hiReq: 436, bin: 5, integ: 2, floorwin: null }), antenna],
  ["narrow span, floor window set", S({ preset: "narrow", lo: 432, hiReq: 436, bin: 5, integ: 2, floorwin: 0.3 }), antenna],
  ["terminated, tag silent", S({ tag: "box-test" }), load],
  ["terminated, tag says so", S({ tag: "term-box" }), load],
  ["antenna but tag says terminated", S({ tag: "term-oops" }), antenna],
  ["tag says enclosure off, chain enclosed", S({ tag: "cage-off-1" }), antenna],
  ["tag says enclosure on, chain open bench", S({ tag: "box-on-2" }), bench],
  ["nothing connected", S({}), none],
  ["station note on receiver and span", S({ rx: "radio-b", gain: 29.7, preset: "custom", lo: 300, hiReq: 420, bin: 50, integ: 120, device: "1" }), padded],
  ["station note on gain", S({ gain: 42 }), antenna],
  ["custom preset", S({ preset: "custom", lo: 500, hiReq: 600, bin: 50, gain: 29.7, integ: 120, rx: "radio-b", device: "1", durVal: 2 }), padded],
  ["fftw preset as saved", F({}), antenna],
  ["fftw settings changed", F({ fbins: 256, ftime: 0.5, frate: 2048000, fover: 30, gain: 34, tag: "f1" }), antenna],
  ["fftw odd bins", F({ fbins: 127 }), antenna],
  ["fftw overlap below crop", F({ fover: 10 }), antenna],
  ["fftw short dwell", F({ ftime: 0.05 }), antenna],
  ["fftw non-standard crop", F({ crop: 0.3, fover: 30 }), antenna],
  ["fftw from an rtl_power preset", F({ preset: "band1", lo: 300, hiReq: 420 }), antenna],
  ["fftw single hop as saved", F({ preset: "line", lo: 999.3, hiReq: 1001.7, gain: 40.2, fbins: 2048, ftime: 60, fover: 0, rx: "radio-b", device: "1" }), padded],
  ["fftw single hop moved", F({ preset: "line", lo: 999.0, hiReq: 1002.0, gain: 40.2, fbins: 2048, ftime: 60, fover: 0, rx: "radio-b", device: "1" }), padded],
];

const out = cases.map(([name, s, mods]) => ({ name, scan: s, modules: mods, ...ctx.run(JSON.parse(JSON.stringify(s)), mods) }));
fs.writeFileSync(path.join(here, 'builder_fixtures.json'), JSON.stringify(out, null, 1) + '\n');
console.log(`${out.length} cases -> tests/data/builder_fixtures.json`);
