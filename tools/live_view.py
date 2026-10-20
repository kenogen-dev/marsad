"""Watch an rtl_power CSV develop while it is still being written.

Read-only: it tails the file and never touches the SDR, so it is safe to run
alongside a recording. rtl_power block-buffers its output, so rows appear in
bursts of roughly a dozen rather than one at a time - the view lags the radio
by a few seconds, never more.

    py tools/live_view.py                 # follows the newest CSV in surveys/
    py tools/live_view.py --csv surveys/survey_x.csv --pin   # stay on one file

One viewer on port 8731 is enough: it switches to a newer CSV as soon as one
appears, so a tab left open from the last run shows the next run too.

Then open http://127.0.0.1:8731/
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
SURVEYS = ROOT / "surveys"


class Tailer:
    """Accumulates completed sweeps from a growing rtl_power CSV.

    A sweep is detected by the start frequency wrapping back down: rtl_power
    walks hops upward through the span, so the first row whose Hz_low is not
    above the previous one begins a new sweep.
    """

    def __init__(self, path: Path, cols: int, keep: int):
        self.path = path
        self.cols = cols
        self.keep = keep
        self.offset = 0
        self.partial = ""
        self.cur: list[tuple[float, float]] = []   # (freq_mhz, dB) this sweep
        self.last_lo: float | None = None
        self.lo = self.hi = None
        self.sweeps: list[list[float | None]] = []
        self.stamps: list[str] = []
        self.medians: list[float] = []     # whole-sweep median: the cage statistic, live
        self.t0: float | None = None
        self.last_growth = time.time()    # for --exit-after-idle
        self.hops: int | None = None      # rows per sweep; the sidecar fills this in
        self.cur_rows = 0
        self.sidecar = self._sidecar()    # must come AFTER hops, which it sets
        self.last_finish: float | None = None
        self.dropped = 0        # sweeps rolled out of the ring buffer
        self.rows = 0
        self.lock = threading.Lock()

    def _sidecar(self) -> dict:
        """The .json written beside the CSV carries the range and the target duration,
        which the CSV itself never states. Absent for a hand-run rtl_power."""
        try:
            d = json.loads(self.path.with_suffix(".json").read_text())
        except (OSError, ValueError):
            return {}
        rp = d.get("rtl_power", {})
        # Hop count from the sidecar, so the FIRST sweep can close itself. Learned from
        # a completed sweep otherwise, which means sweep 1 has to wait for sweep 2 to
        # start - a whole extra interval before anything is drawn.
        g = d.get("geometry") or {}
        if g.get("hops"):
            self.hops = int(g["hops"])
        if rp.get("freq_low_mhz") is not None:
            self.lo = float(rp["freq_low_mhz"])
            self.hi = float(rp.get("freq_high_mhz_effective") or rp.get("freq_high_mhz"))
        return {"duration_s": d.get("duration_requested_s"),
                "integration_s": rp.get("integration_s"),
                "gain_db": rp.get("gain_db"),
                "receiver": d.get("receiver"),
                "tag": (d.get("csv") or "").split("_")[3] if d.get("csv") else None}

    def _downsample(self, pairs) -> list:
        span = max(1e-9, self.hi - self.lo)
        row = [None] * self.cols
        for f, v in pairs:
            c = int((f - self.lo) / span * (self.cols - 1))
            if 0 <= c < self.cols and (row[c] is None or v > row[c]):
                row[c] = v
        return row

    # -- parsing ---------------------------------------------------------
    def _row(self, line: str) -> None:
        parts = line.split(",")
        if len(parts) < 7:
            return
        try:
            lo = float(parts[2]); step = float(parts[4])
            vals = [float(v) for v in parts[6:] if v.strip()]
        except ValueError:
            return
        self.rows += 1
        if self.last_lo is not None and lo <= self.last_lo:
            self._finish(parts[0].strip() + " " + parts[1].strip())
        self.last_lo = lo
        self.cur_rows += 1
        base = lo / 1e6
        st = step / 1e6
        for i, v in enumerate(vals):
            self.cur.append((base + i * st, v))
        # Close the sweep the moment it is full rather than waiting for the next one to
        # start. rtl_power writes a whole interval's rows at once, so waiting left a
        # COMPLETED sweep sitting in the partial buffer for up to a full interval - which
        # both delayed the sweep count and pinned the cursor to the top of the band.
        if self.hops and self.cur_rows >= self.hops:
            self._finish(parts[0].strip() + " " + parts[1].strip())

    def _finish(self, stamp: str) -> None:
        if not self.cur:
            return
        if self.lo is None:
            self.lo = min(f for f, _ in self.cur)
            self.hi = max(f for f, _ in self.cur)
        # Max within each display column, not mean: a single narrow spur must
        # survive 22,896 bins being squeezed into ~1,200 pixels.
        self.sweeps.append(self._downsample(self.cur))
        self.stamps.append(stamp)
        # Median over the FULL resolution sweep, not the downsampled row - the
        # downsample takes maxima, which would bias a median upward.
        self.medians.append(round(st.median([v for _, v in self.cur]), 3))
        if self.t0 is None:
            self.t0 = time.time()
        if self.hops is None and self.cur_rows:
            self.hops = self.cur_rows
        self.cur = []
        self.cur_rows = 0
        self.last_finish = time.time()
        while len(self.sweeps) > self.keep:
            self.sweeps.pop(0); self.stamps.pop(0); self.medians.pop(0); self.dropped += 1

    # -- polling ---------------------------------------------------------
    def poll(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.offset:        # file replaced under us
            self.offset = 0; self.partial = ""
        if size == self.offset:
            return
        self.last_growth = time.time()
        with open(self.path, "r", errors="replace") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        with self.lock:
            data = self.partial + chunk
            lines = data.split("\n")
            self.partial = lines.pop()     # last piece may be mid-line
            for ln in lines:
                if ln.strip():
                    self._row(ln)

    def payload(self, since: int) -> dict:
        with self.lock:
            first = self.dropped
            start = max(0, since - first)
            rows = self.sweeps[start:]
            done = self.dropped + len(self.sweeps)
            # Where rtl_power is right now: the top of the sweep still being written.
            # rtl_power dwells on each hop for interval/hops and dumps the whole sweep
            # at the end, so the file cannot say where the tuner is right now. Estimate it
            # from the clock instead: it walks the span linearly across one interval.
            pct = None
            integ = self.sidecar.get("integration_s")
            if integ and self.last_finish:
                pct = max(0.0, min(1.0, (time.time() - self.last_finish) / float(integ)))
            elif self.cur and self.lo is not None and self.hi:
                pct = max(0.0, min(1.0, (max(f for f, _ in self.cur) - self.lo)
                                   / max(1e-9, self.hi - self.lo)))
            cur_hi = None
            if pct is not None and self.lo is not None and self.hi:
                cur_hi = self.lo + pct * (self.hi - self.lo)
            eta = None
            dur = self.sidecar.get("duration_s")
            integ = self.sidecar.get("integration_s")
            if dur and integ and done:
                eta = max(0, int(dur - (done + (pct or 0)) * integ))
            return {
                "file": self.path.name,
                "lo": self.lo, "hi": self.hi, "cols": self.cols,
                "total": done,
                "from": first + start,
                "rows": rows,
                "stamps": self.stamps[start:],
                "medians": self.medians[start:],
                "partial": self._downsample(self.cur) if (self.cur and self.lo is not None) else None,
                "cursor": cur_hi, "cursor_pct": pct, "cursor_est": True,
                "hops": self.hops,
                "eta_s": eta,
                "expected": int(dur / integ) if (dur and integ) else None,
                "meta": self.sidecar,
                "parsed_rows": self.rows,
                "bytes": self.offset,
            }


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>live scan</title>
<style>
:root{--bg:#0a0e13;--panel:#121a22;--line:#253442;--ink:#dde6ee;--dim:#8599a8;--accent:#4fd3e8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px ui-sans-serif,system-ui,sans-serif}
header{display:flex;gap:18px;align-items:baseline;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid var(--line)}
h1{font-size:14px;margin:0;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);font-weight:700}
.s{font-family:ui-monospace,Consolas,monospace;font-variant-numeric:tabular-nums}
.s b{color:var(--accent);font-weight:600}
main{padding:12px 16px}
.wrap{border:1px solid var(--line);border-radius:6px;background:var(--panel);padding:10px;margin-bottom:12px}
.lab{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--dim);margin-bottom:6px}
.key{display:flex;flex-wrap:wrap;gap:6px 15px;margin:-2px 0 8px;font-size:11px;color:var(--dim)}
.key span{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.key i{display:inline-block;width:16px;height:2px;flex:none;border-radius:1px}
.key i.dash{background:repeating-linear-gradient(90deg,rgba(133,153,168,.65) 0 3px,transparent 3px 7px)}
.key i.vert{width:2px;height:11px}
canvas{display:block;width:100%;image-rendering:pixelated}
#spec{height:190px}
#fall{height:380px}
#trend{height:110px}
.ax{display:flex;justify-content:space-between;font-family:ui-monospace,monospace;font-size:11px;color:var(--dim);margin-top:4px}
.xs{position:relative;height:14px;margin-top:3px;font-family:ui-monospace,monospace;font-size:10px;color:var(--dim)}
.ro{font-family:ui-monospace,monospace;font-size:12px;color:var(--dim);margin-top:6px;min-height:16px}
.ro b{color:var(--accent);font-weight:600}
canvas{cursor:crosshair}
.tabs{display:flex;gap:2px;padding:0 16px;border-bottom:1px solid var(--line);background:var(--panel)}
.tab{padding:9px 16px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);
     cursor:pointer;border:1px solid transparent;border-bottom:none;border-radius:5px 5px 0 0;user-select:none}
.tab:hover{color:var(--ink)}
.tab.on{color:var(--accent);background:var(--bg);border-color:var(--line);margin-bottom:-1px}
.pane{display:none}
.pane.on{display:block}
.pick{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--line)}
.pick select{width:auto;min-width:340px;max-width:100%}
.pick .lv{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--ok,#4fcf96)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:right;padding:6px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal}
th{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
td{font-family:ui-monospace,Consolas,monospace;font-variant-numeric:tabular-nums}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--panel)}
pre.md{white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;
       line-height:1.6;color:var(--ink);padding:16px;margin:0}
.dot{width:8px;height:8px;border-radius:50%;background:var(--accent);display:inline-block;margin-right:6px}
.stale{background:#e8b14f}
</style></head><body>
<header>
  <h1><span class="dot" id="dot"></span>Live scan</h1>
  <div class="s" id="file">—</div>
  <div class="s">sweeps <b id="n">0</b></div>
  <div class="s">last <b id="last">—</b></div>
  <div class="s">floor <b id="floor">—</b> dB</div>
  <div class="s">peak <b id="peak">—</b> dB</div>
  <div class="s" title="estimated from the clock: rtl_power walks the span once per interval">tuned <b id="cur">—</b></div>
  <div class="s">left <b id="eta">—</b></div>
  <div class="s" id="meta">—</div>
</header>
<div class="tabs">
  <div class="tab on" data-pane="scan">Scan</div>
  <div class="tab" data-pane="archive">Archive</div>
  <div class="tab" data-pane="roadmap">Roadmap</div>
</div>

<div class="pane on" id="pane-scan">
<div class="pick">
  <label for="survey" style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)">Survey</label>
  <select id="survey"></select>
  <span class="lv" id="islive"></span>
</div>
<main>
  <div class="wrap"><div class="lab">Spectrum — latest sweep, with max-hold</div>
    <div class="key">
      <span><i style="background:#4fd3e8;height:3px"></i>latest complete sweep</span>
      <span><i style="background:rgba(79,211,232,.45)"></i>sweep in progress</span>
      <span><i style="background:rgba(232,177,79,.55)"></i>max-hold — highest seen at each frequency</span>
      <span><i class="dash"></i>noise floor</span>
      <span><i class="vert" style="background:#e8b14f"></i>where the tuner is now</span>
    </div>
    <canvas id="spec"></canvas><div class="xs" id="xs"></div>
    <div class="ro" id="readout">&nbsp;</div></div>
  <div class="wrap"><div class="lab">Waterfall — newest at the top, # = sweep number</div>
    <canvas id="fall"></canvas><div class="xs" id="xf"></div></div>
  <div class="wrap"><div class="lab">Whole-sweep median — the cage statistic, per sweep</div>
    <canvas id="trend"></canvas>
    <div class="ax"><span id="t0">—</span><span id="t1">—</span></div></div>
</main>
</div>

<div class="pane" id="pane-archive">
  <main>
    <div class="wrap">
      <div class="lab">Every survey in surveys/ — click a row to open it on the Scan tab</div>
      <div style="overflow-x:auto"><table id="arch"><tbody></tbody></table></div>
    </div>
  </main>
</div>

<div class="pane" id="pane-roadmap">
  <main><div class="wrap"><pre class="md" id="road">loading…</pre></div></main>
</div>

<script>
const COLS=1200;
let rows=[], meds=[], lo=null, hi=null, total=0, have=0, lastBytes=0, stale=0;
let partial=null, cursorPct=null, hoverPct=null;
let liveFile=null, curFile=null, surveys=[];

// magma-ish ramp: dark blue -> magenta -> amber -> white
const STOPS=[[0,[8,6,22]],[.25,[70,20,90]],[.5,[150,40,100]],[.72,[225,95,70]],[.88,[250,175,90]],[1,[255,245,220]]];
function cmap(t){t=Math.max(0,Math.min(1,t));
  for(let i=1;i<STOPS.length;i++){const [b,cb]=STOPS[i];const [a,ca]=STOPS[i-1];
    if(t<=b){const u=(t-a)/(b-a);return [0,1,2].map(k=>Math.round(ca[k]+(cb[k]-ca[k])*u));}}
  return STOPS[STOPS.length-1][1];}

function pct(arr,p){const v=arr.filter(x=>x!==null).sort((a,b)=>a-b);
  return v.length?v[Math.min(v.length-1,Math.floor(p*v.length))]:null;}

function fit(c){const r=c.getBoundingClientRect();
  if(c.width!==Math.round(r.width)||c.height!==Math.round(r.height)){
    c.width=Math.round(r.width);c.height=Math.round(r.height);}return c.getContext('2d');}

function ticks(lo,hi){
  const span=hi-lo;
  for(const step of [1,2,5,10,20,25,50,100,200,250,500,1000]){
    if(span/step<=12){const out=[];
      for(let v=Math.ceil(lo/step)*step;v<=hi;v+=step)out.push(v);
      return {step,vals:out};}}
  return {step:span,vals:[lo,hi]};
}

function grid(x,W,H,tk){
  x.save();x.strokeStyle='rgba(133,153,168,.14)';x.lineWidth=1;
  x.fillStyle='rgba(133,153,168,.75)';x.font='10px ui-monospace,Consolas,monospace';
  for(const v of tk.vals){
    const px=Math.round((v-lo)/(hi-lo)*W)+.5;
    x.beginPath();x.moveTo(px,0);x.lineTo(px,H);x.stroke();
  }
  x.restore();
}

function labels(el,tk){
  el.innerHTML=tk.vals.map(v=>{
    const pct=(v-lo)/(hi-lo)*100;
    return `<span style="position:absolute;left:${pct}%;transform:translateX(-50%)">${v}</span>`;
  }).join('');
}

function drawSpec(floor,span){
  const c=document.getElementById('spec'),x=fit(c),W=c.width,H=c.height;
  x.clearRect(0,0,W,H);
  const tk=ticks(lo,hi);
  grid(x,W,H,tk);
  const y=v=>H-4-((v-floor)/span)*(H-10);
  // dB gridlines every 10 dB above the floor
  x.save();x.strokeStyle='rgba(133,153,168,.10)';
  for(let d=0;d<=span;d+=10){x.beginPath();x.moveTo(0,y(floor+d));x.lineTo(W,y(floor+d));x.stroke();
    x.fillStyle='rgba(133,153,168,.6)';x.font='10px ui-monospace,monospace';
    x.fillText((floor+d).toFixed(0),3,y(floor+d)-2);}
  x.restore();
  for(const [arr,col,w] of [[mh,'rgba(232,177,79,.55)',1],[rows[rows.length-1],'#4fd3e8',1.3]]){
    if(!arr)continue;
    x.beginPath();x.lineWidth=w;x.strokeStyle=col;let on=false;
    for(let i=0;i<COLS;i++){const v=arr[i];if(v===null||v===undefined){on=false;continue;}
      const px=i/(COLS-1)*W,py=y(v);if(!on){x.moveTo(px,py);on=true;}else x.lineTo(px,py);}
    x.stroke();}
  if(partial){x.beginPath();x.lineWidth=1;x.strokeStyle='rgba(79,211,232,.45)';let on=false;
    for(let i=0;i<COLS;i++){const v=partial[i];if(v===null){on=false;continue;}
      const px=i/(COLS-1)*W,py=y(v);if(!on){x.moveTo(px,py);on=true;}else x.lineTo(px,py);}
    x.stroke();}
  x.strokeStyle='rgba(133,153,168,.35)';x.setLineDash([3,4]);x.beginPath();
  x.moveTo(0,y(floor));x.lineTo(W,y(floor));x.stroke();x.setLineDash([]);
  if(cursorPct!==null){const cx=cursorPct*W;
    x.strokeStyle='#e8b14f';x.lineWidth=1.5;x.beginPath();x.moveTo(cx,0);x.lineTo(cx,H);x.stroke();
    x.fillStyle='#e8b14f';x.beginPath();x.moveTo(cx,0);x.lineTo(cx-5,8);x.lineTo(cx+5,8);x.fill();}
  if(hoverPct!==null){const hx=hoverPct*W;
    x.strokeStyle='rgba(221,230,238,.6)';x.lineWidth=1;x.beginPath();x.moveTo(hx,0);x.lineTo(hx,H);x.stroke();}
  labels(document.getElementById('xs'),tk);
}

let fallImg=null;
function drawFall(floor,span){
  const f=document.getElementById('fall'),g=fit(f),FW=f.width,FH=f.height;
  const img=g.createImageData(FW,FH);
  const n=Math.min(rows.length,FH);
  for(let ry=0;ry<FH;ry++){
    const src=rows[rows.length-1-Math.floor(ry/FH*n)];
    for(let px=0;px<FW;px++){
      const v=src?src[Math.floor(px/FW*COLS)]:null;
      const o=(ry*FW+px)*4;
      const rgb=(v===null||v===undefined)?[18,26,34]:cmap((v-floor)/span);
      img.data[o]=rgb[0];img.data[o+1]=rgb[1];img.data[o+2]=rgb[2];img.data[o+3]=255;}}
  fallImg=img;
  overlayFall();
}

function overlayFall(){
  const f=document.getElementById('fall'),g=f.getContext('2d'),FW=f.width,FH=f.height;
  if(!fallImg)return;
  g.putImageData(fallImg,0,0);
  const tk=ticks(lo,hi);
  g.save();g.strokeStyle='rgba(221,230,238,.13)';g.lineWidth=1;
  for(const v of tk.vals){const px=Math.round((v-lo)/(hi-lo)*FW)+.5;
    g.beginPath();g.moveTo(px,0);g.lineTo(px,FH);g.stroke();}
  g.restore();
  const n=Math.min(rows.length,FH);
  // Sweep boundaries. One line per sweep is readable at 15 sweeps and a solid block at
  // 180, so thin them to at most ~16 lines and label a subset of those.
  if(n>1){
    const step=Math.max(1,Math.ceil(n/16));
    const labelEvery=step*(n/step>9?2:1);
    g.save();
    g.font='9px ui-monospace,Consolas,monospace';
    g.textBaseline='top';
    for(let k=0;k<=n;k+=step){
      const y=Math.round(k/n*FH)+.5;
      g.strokeStyle='rgba(221,230,238,.18)';g.lineWidth=1;
      g.beginPath();g.moveTo(0,y);g.lineTo(FW,y);g.stroke();
      const sweep=total-k;
      if(sweep<1||k%labelEvery)continue;
      const txt='#'+sweep;
      const w=g.measureText(txt).width;
      g.fillStyle='rgba(10,14,19,.72)';
      g.fillRect(2,y+1,w+6,11);
      g.fillStyle='rgba(221,230,238,.78)';
      g.fillText(txt,5,y+2);
    }
    g.restore();
  }
  if(cursorPct!==null){const cx=cursorPct*FW;
    g.strokeStyle='rgba(232,177,79,.85)';g.lineWidth=1.5;
    g.beginPath();g.moveTo(cx,0);g.lineTo(cx,Math.min(FH,FH/Math.max(1,n)));g.stroke();}
  if(hoverPct!==null){const hx=hoverPct*FW;
    g.strokeStyle='rgba(221,230,238,.55)';g.lineWidth=1;
    g.beginPath();g.moveTo(hx,0);g.lineTo(hx,FH);g.stroke();}
  labels(document.getElementById('xf'),ticks(lo,hi));
}

let mh=[];
function draw(){
  if(!rows.length||lo===null)return;
  const flat=[].concat(...rows.slice(-40));
  const floor=pct(flat,.2), top=pct(flat,.999);
  if(floor===null)return;
  const span=Math.max(6,top-floor);
  document.getElementById('floor').textContent=floor.toFixed(2);
  document.getElementById('peak').textContent=top.toFixed(2);
  mh=new Array(COLS).fill(null);
  for(const r of rows)for(let i=0;i<COLS;i++)if(r[i]!==null&&(mh[i]===null||r[i]>mh[i]))mh[i]=r[i];
  lastFloor=floor;lastSpan=span;
  drawSpec(floor,span);drawFall(floor,span);trend();
  readout();
}

let lastFloor=null,lastSpan=null;
function redrawLight(){
  if(lastFloor===null)return;
  drawSpec(lastFloor,lastSpan);overlayFall();readout();
}

function readout(){
  const el=document.getElementById('readout');
  if(hoverPct===null||!rows.length){el.innerHTML='&nbsp;';return;}
  const f=lo+hoverPct*(hi-lo);
  const i=Math.round(hoverPct*(COLS-1));
  const cur=rows[rows.length-1][i], hold=mh[i];
  const near=[14.4,28.8,24,25,100].map(k=>({k,n:Math.round(f/k),d:Math.abs(f-Math.round(f/k)*k)}))
    .filter(o=>o.d<0.12&&o.n>0).sort((a,b)=>a.d-b.d)[0];
  el.innerHTML=`<b>${f.toFixed(3)} MHz</b>`
    +(cur!==null?`  latest <b>${cur.toFixed(1)}</b> dB`:'')
    +(hold!==null?`  max-hold <b>${hold.toFixed(1)}</b> dB`:'')
    +(cur!==null&&lastFloor!==null?`  over floor <b>${(cur-lastFloor).toFixed(1)}</b> dB`:'')
    +(near?`  <span style="color:#e8b14f">≈ ${near.k} MHz × ${near.n}</span>`:'');
}

function hook(id){
  const c=document.getElementById(id);
  c.addEventListener('mousemove',e=>{const r=c.getBoundingClientRect();
    hoverPct=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));redrawLight();});
  c.addEventListener('mouseleave',()=>{hoverPct=null;redrawLight();});
}

function trend(){
  if(meds.length<2)return;
  const c=document.getElementById('trend'),x=fit(c),W=c.width,H=c.height;
  x.clearRect(0,0,W,H);
  const lo=Math.min(...meds),hi=Math.max(...meds);
  // Always show at least a 0.5 dB window, or run-to-run noise looks like a trend.
  const mid=(lo+hi)/2,half=Math.max(0.25,(hi-lo)/2*1.25);
  const y=v=>H-6-((v-(mid-half))/(2*half))*(H-12);
  x.strokeStyle='rgba(133,153,168,.2)';x.lineWidth=1;
  for(const f of [0,.5,1]){const py=6+f*(H-12);x.beginPath();x.moveTo(0,py);x.lineTo(W,py);x.stroke();}
  x.beginPath();x.lineWidth=1.6;x.strokeStyle='#4fd3e8';
  meds.forEach((v,i)=>{const px=meds.length<2?0:i/(meds.length-1)*W;
    i?x.lineTo(px,y(v)):x.moveTo(px,y(v));});
  x.stroke();
  const last=meds[meds.length-1];
  x.fillStyle='#4fd3e8';x.beginPath();x.arc(W-2,y(last),3,0,7);x.fill();
  document.getElementById('t0').textContent=(mid-half).toFixed(2)+' dB';
  document.getElementById('t1').textContent='now '+last.toFixed(3)+' dB  ·  spread '+(hi-lo).toFixed(3)+' dB';
}

function fmt(s){if(s===null||s===undefined)return '—';
  const m=Math.floor(s/60);return m>=60?Math.floor(m/60)+'h'+String(m%60).padStart(2,'0'):m+'m';}

function resetView(){
  rows=[];meds=[];have=0;lo=null;hi=null;total=0;partial=null;cursorPct=null;
  fallImg=null;lastFloor=null;mh=[];
  for(const id of ['n','last','floor','peak','cur','eta'])document.getElementById(id).textContent='—';
}

async function loadSurveys(){
  try{
    const d=await (await fetch('/surveys')).json();
    liveFile=d.live;surveys=d.surveys;
    if(!curFile)curFile=liveFile;
    const sel=document.getElementById('survey');
    sel.innerHTML=surveys.map(s=>{
      const bits=[s.receiver?s.receiver.toUpperCase():null,s.gain!=null?('g'+s.gain):null,
                  s.integ!=null?('i'+s.integ):null,s.mb+' MB'].filter(Boolean).join(' · ');
      return `<option value="${s.name}"${s.name===curFile?' selected':''}>${s.name} — ${bits}</option>`;
    }).join('');
    document.getElementById('arch').innerHTML=
      '<thead><tr><th>Survey</th><th>Rx</th><th>Gain</th><th>Integ</th><th>Sweeps</th><th>Range MHz</th><th>MB</th></tr></thead><tbody>'
      +surveys.map(s=>`<tr data-f="${s.name}"><td>${s.name}${s.name===liveFile?' <span class="lv">live</span>':''}</td>`
        +`<td>${s.receiver?s.receiver.toUpperCase():'—'}</td><td>${s.gain??'—'}</td><td>${s.integ??'—'}</td>`
        +`<td>${s.sweeps??'—'}</td><td>${s.lo!=null?s.lo+'–'+s.hi:'—'}</td><td>${s.mb}</td></tr>`).join('')
      +'</tbody>';
    document.querySelectorAll('#arch tr[data-f]').forEach(tr=>tr.onclick=()=>{
      curFile=tr.dataset.f;document.getElementById('survey').value=curFile;
      resetView();showPane('scan');});
  }catch(e){}
}

function showPane(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.pane===name));
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='pane-'+name));
  if(name==='scan')setTimeout(draw,0);        // canvases size to zero while hidden
}

async function tick(){
  try{
    const r=await fetch('/data?since='+have+(curFile?'&file='+encodeURIComponent(curFile):''));
    const d=await r.json();
    if(d.live&&d.live!==liveFile){
      // The server moved to a newer CSV. Follow it if we were watching live;
      // leave a deliberately chosen archive file alone.
      const wasLive=(!curFile||curFile===liveFile);
      liveFile=d.live;
      if(wasLive){curFile=liveFile;resetView();loadSurveys();setTimeout(tick,500);return;}
    }
    if(curFile&&d.file!==curFile){setTimeout(tick,500);return;}   // response for a file we left
    if(d.lo!==null){lo=d.lo;hi=d.hi;}
    document.getElementById('file').textContent=d.file;
    if(d.rows.length){rows=rows.concat(d.rows);meds=meds.concat(d.medians||[]);
      have=d.from+d.rows.length;
      if(rows.length>400){rows=rows.slice(-400);meds=meds.slice(-400);}}
    partial=d.partial||null;
    cursorPct=(d.cursor_pct===null||d.cursor_pct===undefined)?null:d.cursor_pct;
    document.getElementById('cur').textContent=d.cursor?('~'+d.cursor.toFixed(0)+' MHz'):'—';
    document.getElementById('eta').textContent=fmt(d.eta_s);
    const m=d.meta||{};
    document.getElementById('islive').textContent=(curFile===liveFile)?'live':'';
    document.getElementById('meta').textContent=
      [m.receiver?m.receiver.toUpperCase():null,
       m.gain_db!=null?('g'+m.gain_db):null,
       m.integration_s!=null?('i'+m.integration_s):null,
       d.expected?('of '+d.expected+' sweeps'):null].filter(Boolean).join('  ');
    total=d.total;
    document.getElementById('n').textContent=total;
    // Only overwrite when a sweep actually completed - otherwise every poll between
    // sweeps blanks it, since the payload is incremental and returns no stamps.
    if(d.stamps.length)document.getElementById('last').textContent=d.stamps[d.stamps.length-1].slice(11);
    stale=(d.bytes===lastBytes)?stale+1:0;lastBytes=d.bytes;
    document.getElementById('dot').className='dot'+(stale>6?' stale':'');
    draw();
  }catch(e){}
  setTimeout(tick,2500);
}
addEventListener('resize',draw);
hook('spec');hook('fall');
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>showPane(t.dataset.pane));
document.getElementById('survey').onchange=e=>{curFile=e.target.value;resetView();};
fetch('/roadmap').then(r=>r.text()).then(t=>document.getElementById('road').textContent=t)
  .catch(()=>document.getElementById('road').textContent='TOOL_ROADMAP.md not found');
loadSurveys();
setInterval(loadSurveys,30000);
tick();
</script></body></html>"""


TAILERS: dict = {}
TAIL_LOCK = threading.Lock()
LAST_REQUEST = [time.time()]      # any HTTP hit defers the idle shutdown


def get_tailer(name: str, cols: int, keep: int):
    """One Tailer per CSV, built the first time it is asked for.

    Browsing an old survey should not disturb the live one, and re-reading a
    90 MB file on every poll would be wasteful - each Tailer keeps its own
    byte offset and only reads what is new.
    """
    with TAIL_LOCK:
        t = TAILERS.get(name)
        if t is None:
            path = SURVEYS / name
            if not path.exists():
                return None
            t = Tailer(path, cols, keep)
            TAILERS[name] = t
        return t


def survey_list() -> list:
    """Every CSV in surveys/, newest first, with whatever the sidecar knows."""
    out = []
    for f in sorted(SURVEYS.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = {}
        try:
            d = json.loads(f.with_suffix(".json").read_text())
            rp = d.get("rtl_power", {})
            g = d.get("geometry", {})
            meta = {"receiver": d.get("receiver"), "gain": rp.get("gain_db"),
                    "integ": rp.get("integration_s"), "sweeps": g.get("sweeps"),
                    "lo": rp.get("freq_low_mhz"), "hi": rp.get("freq_high_mhz_effective"),
                    "started": d.get("started_local"), "tag": d.get("preset")}
        except (OSError, ValueError):
            pass
        out.append({"name": f.name, "mb": round(f.stat().st_size / 1e6, 1),
                    "mtime": f.stat().st_mtime, **meta})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Live view of a growing rtl_power CSV")
    ap.add_argument("--csv", help="path to the CSV (default: newest in surveys/)")
    ap.add_argument("--port", type=int, default=8731)
    ap.add_argument("--cols", type=int, default=1200, help="display columns")
    ap.add_argument("--keep", type=int, default=400, help="sweeps held in memory")
    ap.add_argument("--pin", action="store_true",
                    help="stay on the starting CSV instead of switching to a newer "
                         "one when it appears in surveys/")
    ap.add_argument("--exit-after-idle", type=float, default=0, metavar="MIN",
                    help="shut down this many minutes after the CSV stops growing "
                         "(0 = run until Ctrl-C). Lets run_survey.py start a viewer "
                         "that cleans itself up once the scan is over.")
    args = ap.parse_args()

    def newest_csv() -> Path | None:
        files = sorted(SURVEYS.glob("*.csv"), key=lambda p: p.stat().st_mtime)
        return files[-1] if files else None

    if args.csv:
        path = Path(args.csv)
        if not path.is_absolute():
            path = ROOT / args.csv
    else:
        path = newest_csv()
        if path is None:
            raise SystemExit("no CSVs in surveys/")
    follow = not args.pin
    print(f"  watching {path.name}" + ("" if args.pin else "  (will follow a newer CSV)"))

    # The live file can change under a running server, so the handler and the
    # polling loop share this holder instead of closing over fixed names.
    tail = Tailer(path, args.cols, args.keep)
    with TAIL_LOCK:
        TAILERS[path.name] = tail
    live = {"path": path, "tail": tail}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):          # keep the console for the scan
            pass

        def do_GET(self):
            u = urlparse(self.path)
            LAST_REQUEST[0] = time.time()
            q = parse_qs(u.query)
            if u.path == "/data":
                since = int(q.get("since", ["0"])[0])
                name = (q.get("file") or [None])[0]
                cur = live["tail"]
                t = cur if not name or name == live["path"].name else get_tailer(name, args.cols, args.keep)
                if t is None:
                    self.send_error(404); return
                if t is not cur:
                    t.poll()        # archived files are not polled in the background
                # "live" rides on every poll so the page notices a switch within one
                # tick instead of waiting for its 30 s survey-list refresh.
                body = json.dumps({**t.payload(since), "live": live["path"].name}).encode()
                ctype = "application/json"
            elif u.path == "/surveys":
                body = json.dumps({"live": live["path"].name, "follow": follow,
                                   "surveys": survey_list()}).encode()
                ctype = "application/json"
            elif u.path == "/roadmap":
                md = ROOT / "TOOL_ROADMAP.md"
                body = (md.read_text(encoding="utf-8") if md.exists()
                        else "TOOL_ROADMAP.md not found").encode()
                ctype = "text/plain; charset=utf-8"
            elif u.path in ("/", "/index.html"):
                body = PAGE.encode()
                ctype = "text/html; charset=utf-8"
            else:
                self.send_error(404); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)

    def loop():
        idle = args.exit_after_idle * 60
        while True:
            if follow:
                n = newest_csv()
                if n is not None and n.name != live["path"].name:
                    t = get_tailer(n.name, args.cols, args.keep)
                    if t is not None:
                        live["path"], live["tail"] = n, t
                        print(f"  now watching {n.name}")
            tail = live["tail"]
            tail.poll()
            # Only arm the idle timer once the file has actually produced something,
            # so a viewer started a moment before rtl_power does not kill itself
            # during the first integration.
            quiet = min(time.time() - tail.last_growth, time.time() - LAST_REQUEST[0])
            if idle and tail.rows and quiet > idle:
                print(f"  no new data for {args.exit_after_idle:g} min - shutting down")
                srv.shutdown()
                return
            time.sleep(2.0)

    threading.Thread(target=loop, daemon=True).start()

    note = f", exits {args.exit_after_idle:g} min after the scan ends" if args.exit_after_idle else ""
    print(f"  http://127.0.0.1:{args.port}/   (Ctrl-C to stop{note})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
