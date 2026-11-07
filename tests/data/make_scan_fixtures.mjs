// Runs the web page's own Scan-tab functions on made-up inputs and saves what they return,
// so the desktop window's port (marsad_desktop/scan_calc.py) can be tested against the page.
//
//   node tests/data/make_scan_fixtures.mjs        -> tests/data/scan_page_fixtures.json
//
// The functions are cut out of marsad/web/index.html as text, not copied, and run in a
// sandbox with the few globals they read. Every input is synthetic. Rerun after changing
// the page's layer split, spur mask, family grid, readout or detection line.
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(here, '..', '..', 'marsad', 'web', 'index.html'), 'utf8');

function block(from) {                     // the text from `from` to its matching close brace
  let i = html.indexOf(from);
  if (i < 0) throw new Error('not found in the page: ' + from);
  let j = html.indexOf('{', i), depth = 0;
  for (; j < html.length; j++) {
    if (html[j] === '{') depth++;
    else if (html[j] === '}' && --depth === 0) return html.slice(i, j + 1);
  }
  throw new Error('unbalanced: ' + from);
}
function between(a, b) {                   // the text from `a` up to and including `b`
  const i = html.indexOf(a), j = html.indexOf(b, i);
  if (i < 0 || j < 0) throw new Error('not found in the page: ' + a);
  return html.slice(i, j + b.length);
}

const src = [
  'const COLS=1200;',
  'var lo=null,hi=null,stepK=null,hopC=[],intData=null,spurMask=null,peaks=[],rows=[],mh=[],marks={};',
  'var FAMILIES=[14.4,28.8,24,25,100],THR=10,hoverPct=null,hoverRow=null,lastFloor=null;',
  'var view={z:1,c:0.5,top:0,fit:true,ry:1};',
  block('function pct('),
  block('function vlo('), block('function vhi('), block('function fLo('), block('function fHi('),
  block('function fx('), block('function fracAt('),
  between('const MARK_COL=', ';'), between('const MARK_KEY=', ';'), between('const GRID_COL=', ';'),
  block('function drawMarks('),
  between('const layers=', ';'), 'var sigThr=6, layerVer=0;', 'const layerCache=new WeakMap();',
  block('function buildMask('), block('function sweepFloor('), block('function layerRow('),
  // the readout's family lookup and the detection line, cut from inside larger functions
  'function nearFamily(f){' + between('const near=FAMILIES', '[0];') + 'return near||null;}',
  'function detectionLine(d){let out="";const document={getElementById:()=>({set textContent(v){out=v;}})};'
    + block('if(d.peaks){peaks=d.peaks;') + 'return out;}',
].join('\n');
const ctx = vm.createContext({ Math, JSON, Array, Float32Array, Uint8Array, Number, String, isFinite, NaN, Infinity, WeakMap });
vm.runInContext(src, ctx);

// a small seeded generator, so the fixtures are the same every time
let seed = 20261107;
const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
const gauss = () => Math.sqrt(-2 * Math.log(rnd() + 1e-12)) * Math.cos(2 * Math.PI * rnd());

function makeRow(lo, hi, base, lines, holes) {
  const r = new Array(1200);
  for (let c = 0; c < 1200; c++) {
    const f = lo + c * (hi - lo) / 1199;
    let v = base + 0.6 * gauss() + 1.5 * Math.sin(c / 90);
    for (const [lf, lv, lw] of lines) v = Math.max(v, lv - Math.abs(f - lf) / lw);
    r[c] = Math.round(v * 100) / 100;
  }
  for (const h of holes) r[h] = null;
  return r;
}

const scenarios = [
  { name: 'narrow, hop centres fine enough to mask', lo: 400, hi: 440, stepK: 26.0417,
    hopC: Array.from({ length: 16 }, (_, k) => 401.25 + 2.5 * k),
    lines: [[403.2, -18, 0.02], [416.4, -26, 0.01], [432.0, -12, 0.2]], holes: [5, 6, 7, 600, 1199] },
  { name: 'wide, hop centres too coarse to mask', lo: 52, hi: 1660, stepK: 100,
    hopC: Array.from({ length: 766 }, (_, k) => 53.05 + 2.1 * k),
    lines: [[98.7, -6, 0.3], [1090.0, -20, 0.05], [1440.0, -24, 0.02]], holes: [0, 300] },
];
const combos = [[1, 1, 0], [1, 0, 1], [0, 1, 1], [1, 0, 0], [0, 1, 0], [0, 0, 1]];
const num = v => (v === null || v === undefined || v !== v) ? null : Math.round(v * 1e6) / 1e6;
const out = { generated_by: 'tests/data/make_scan_fixtures.mjs', page: 'marsad/web/index.html', scenarios: [] };

for (const s of scenarios) {
  ctx.lo = s.lo; ctx.hi = s.hi; ctx.stepK = s.stepK; ctx.hopC = s.hopC;
  const rows = [makeRow(s.lo, s.hi, -36, s.lines, s.holes), makeRow(s.lo, s.hi, -35.5, s.lines, s.holes)];
  const term = makeRow(s.lo, s.hi, -38, [], [10, 11]);
  const flat = [].concat(...rows.slice(-40));
  const sc = { name: s.name, lo: s.lo, hi: s.hi, step_k: s.stepK, hop_centres: s.hopC, rows, term,
               display_floor: [ctx.pct(flat, .2), ctx.pct(flat, .999)], layers: [] };
  for (const withTerm of [false, true]) {
    ctx.intData = withTerm ? { found: true, floor: term, families_mhz: [14.4] } : { found: false, families_mhz: [14.4] };
    ctx.spurMask = null; ctx.layerVer++;
    ctx.buildMask();
    if (!withTerm) sc.mask = Array.from(ctx.spurMask);
    sc.floor = Array.from(ctx.sweepFloor(rows[1])).map(num);
    for (const thr of [6, 12]) for (const [n, g, i] of combos) {
      vm.runInContext(`layers.noise=${!!n};layers.signal=${!!g};layers.internal=${!!i};`, ctx); ctx.sigThr = thr; ctx.layerVer++;
      sc.layers.push({ term: withTerm, thr, noise: !!n, signal: !!g, internal: !!i,
                       row: Array.from(ctx.layerRow(rows[1])).map(num) });
    }
  }
  out.scenarios.push(sc);
}

// the family grid: what drawMarks strokes and labels, read off a fake canvas
const grid = [];
for (const [lo, hi, W, z, c] of [[400, 440, 1000, 1, 0.5], [52, 1660, 1100, 1, 0.5], [52, 1660, 1100, 16, 0.6],
                                [1241, 2148, 1400, 1, 0.5], [1241, 2148, 1400, 40, 0.25], [225, 400, 900, 3, 0.4]]) {
  ctx.lo = lo; ctx.hi = hi; ctx.view.z = z; ctx.view.c = c; ctx.peaks = [];
  const ticks = [], labels = [];
  let style = null;
  const fake = { save() {}, restore() {}, beginPath() {}, stroke() {}, fill() {}, lineTo() {},
    set strokeStyle(v) { style = v; }, get strokeStyle() { return style; }, fillStyle: '', lineWidth: 1, font: '',
    textAlign: '', globalAlpha: 1,
    moveTo(x, y) { if (y === 0) ticks.push([style, x]); }, fillText(t, x) { labels.push([t, x]); } };
  ctx.marks = { sig: false, spur: false, art: false, grid: true, hop: false };
  ctx.drawMarks(fake, W, 200, v => v);
  grid.push({ lo, hi, W, z, c, f_lo: ctx.fLo(), f_hi: ctx.fHi(), ticks, labels });
}
out.grid = grid;

// the readout's nearest family
const fams = [14.4, 28.8, 24, 25, 100];
ctx.FAMILIES = fams;
out.nearest = [];
for (const f of [446.4, 446.52, 446.53, 1199.95, 1200.0044, 0.05, 14.39, 720.0, 1022.4, 1022.519, 1022.53, 299.999,
                 1265.44, 2131.2, 50.0, 24.06, 37.5]) {
  const n = ctx.nearFamily(f);
  out.nearest.push({ f, families: fams, near: n ? [n.k, n.n] : null });
}

// the detection count line
out.detections = [];
for (const [peaks, thr] of [[[], 10], [[{ kind: 'signal' }], 10], [[{ kind: 'spur' }, { kind: 'spur' }, { kind: 'artefact' }], 12],
                            [[{ kind: 'signal' }, { kind: 'signal' }, { kind: 'spur' }, { kind: 'artefact' }, { kind: 'artefact' }], 10.5]]) {
  ctx.THR = thr;
  out.detections.push({ peaks, thr, text: ctx.detectionLine({ peaks }) });
}

fs.writeFileSync(path.join(here, 'scan_page_fixtures.json'), JSON.stringify(out));
console.log('wrote', path.join('tests', 'data', 'scan_page_fixtures.json'),
            out.scenarios.length, 'scenarios,', grid.length, 'grid views,', out.nearest.length, 'readouts');
