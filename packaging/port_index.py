"""Generate marsad/web/index.html from the PAGE string in tools/live_view.py.

live_view.py is still being edited by hand (zoom, pan, history scroll, the
finished marker), so the package page is derived from it rather than copied
once: re-run this and the latest Scan tab comes across. What is added:
  - a Builder tab holding the scan builder page in a frame
  - occupancy.db tables on the Archive tab
  - the hover readout's harmonic families from /config instead of a literal
  - the last-used tab remembered in the browser
Every replacement asserts it matched exactly once.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / "tools"))
import live_view  # noqa: E402

src = live_view.PAGE


def rep(old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, f"expected {count} match(es), found {n}: {old[:70]!r}"
    src = src.replace(old, new)


rep('<title>live scan</title>', '<title>SDR station</title>')
rep("'live scan'", "'SDR station'")
rep('<h1><span class="dot" id="dot"></span>Live scan</h1>', '<h1><span class="dot" id="dot"></span>SDR station</h1>')

# CSS additions
rep('.stale{background:#e8b14f}\n',
    '.stale{background:#e8b14f}\n'
    '#builder{width:100%;height:calc(100vh - 84px);border:0;background:var(--bg);display:block}\n'
    '.note{font-size:12px;color:var(--dim);margin:0 0 8px}\n'
    '.term{color:#e8b14f}\n'
    '.lv{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#4fcf96}\n')
rep('tbody tr{cursor:pointer}\ntbody tr:hover{background:var(--panel)}',
    'tbody tr.go{cursor:pointer}\ntbody tr.go:hover{background:var(--bg)}')

# tabs and panes
rep('  <div class="tab on" data-pane="scan">Scan</div>\n  <div class="tab" data-pane="archive">Archive</div>',
    '  <div class="tab on" data-pane="scan">Scan</div>\n  <div class="tab" data-pane="builder">Builder</div>\n'
    '  <div class="tab" data-pane="archive">Archive</div>')
rep('<div class="pane" id="pane-archive">\n  <main>\n    <div class="wrap">\n'
    '      <div class="lab">Every survey in surveys/ — click a row to open it on the Scan tab</div>\n'
    '      <div style="overflow-x:auto"><table id="arch"><tbody></tbody></table></div>\n    </div>\n  </main>\n</div>',
    '<div class="pane" id="pane-builder">\n  <iframe id="builder" title="Scan builder"></iframe>\n</div>\n\n'
    '<div class="pane" id="pane-archive">\n  <main>\n    <div class="wrap">\n'
    '      <div class="lab">Every survey in surveys/ — click a row to open it on the Scan tab</div>\n'
    '      <div style="overflow-x:auto"><table id="arch"><tbody></tbody></table></div>\n    </div>\n'
    '    <div class="wrap">\n      <div class="lab">occupancy.db — archived surveys with their comparability fingerprint</div>\n'
    '      <p class="note" id="dbnote">loading…</p>\n'
    '      <div style="overflow-x:auto"><table id="dbsurveys"><tbody></tbody></table></div>\n    </div>\n'
    '    <div class="wrap">\n      <div class="lab">Frequencies seen in at least two antenna surveys — the persistent set</div>\n'
    '      <div style="overflow-x:auto"><table id="dbseen"><tbody></tbody></table></div>\n    </div>\n  </main>\n</div>')

# JS: families from config, escaping, archive loader, pane memory, 404 tolerance
rep('let liveFile=null, curFile=null, surveys=[];\n',
    'let liveFile=null, curFile=null, surveys=[];\n'
    'let FAMILIES=[14.4,28.8,24,25,100];\n'
    "fetch('/config').then(r=>r.json()).then(c=>{if(c.readout_families)FAMILIES=c.readout_families;}).catch(()=>{});\n"
    "const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));\n")
rep('const near=[14.4,28.8,24,25,100].map(', 'const near=FAMILIES.map(')
rep('+surveys.map(s=>`<tr data-f="${s.name}"><td>${s.name}',
    '+surveys.map(s=>`<tr class="go" data-f="${esc(s.name)}"><td>${esc(s.name)}')
rep("""function showPane(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.pane===name));
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='pane-'+name));
  if(name==='scan')setTimeout(draw,0);        // canvases size to zero while hidden
}""",
    """async function loadArchive(){
  try{
    const d=await (await fetch('/archive')).json();
    const note=document.getElementById('dbnote');
    if(!d.db){note.textContent='No occupancy.db yet. It is created by `marsad archive add --all` or automatically after each `marsad run`.';}
    else note.textContent=d.surveys.length+' archived survey(s) in '+d.db+(d.error?'  ('+d.error+')':'');
    document.getElementById('dbsurveys').innerHTML=
      '<thead><tr><th>Survey</th><th>Started</th><th>Sweeps</th><th>Signals</th><th>Floor dB</th><th>Fingerprint</th></tr></thead><tbody>'
      +d.surveys.map(s=>`<tr><td>${esc(s.name)}${s.terminated?' <span class="term">terminated</span>':''}</td>`
        +`<td>${esc((s.started||'').slice(0,16))}</td><td>${s.sweeps??'—'}</td><td>${s.n??'—'}</td>`
        +`<td>${s.median_floor_db??'—'}</td><td style="text-align:left">${esc(s.fingerprint)}</td></tr>`).join('')
      +'</tbody>';
    document.getElementById('dbseen').innerHTML=
      '<thead><tr><th>MHz</th><th>Runs</th><th>Avg SNR</th><th>Avg occ %</th><th>Tags</th></tr></thead><tbody>'
      +d.seen.map(r=>`<tr><td>${Number(r.f).toFixed(2)}</td><td>${r.n}</td><td>${r.snr}</td><td>${r.occ}</td>`
        +`<td style="text-align:left;white-space:normal">${esc(r.tags||'')}</td></tr>`).join('')
      +'</tbody>';
  }catch(e){}
}

function showPane(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.pane===name));
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='pane-'+name));
  if(name==='scan')setTimeout(draw,0);        // canvases size to zero while hidden
  if(name==='builder'){const f=document.getElementById('builder');if(!f.src)f.src='/builder';}
  if(name==='archive')loadArchive();
  try{localStorage.setItem('sdr-pane',name);}catch(e){}
}""")
rep("""    const r=await fetch('/data?since='+have+(curFile?'&file='+encodeURIComponent(curFile):''));
    const d=await r.json();""",
    """    const r=await fetch('/data?since='+have+(curFile?'&file='+encodeURIComponent(curFile):''));
    if(r.status===404){setTimeout(tick,5000);return;}   // no survey yet
    const d=await r.json();""")
rep("""setInterval(loadSurveys,30000);
tick();
</script></body></html>""",
    """setInterval(loadSurveys,30000);
tick();
let startPane='scan';
try{startPane=localStorage.getItem('sdr-pane')||'scan';}catch(e){}
if(startPane!=='scan')showPane(startPane);
</script></body></html>""")

out = root / "marsad" / "web" / "index.html"
out.write_text(src + "\n", encoding="utf-8")
print(len(src.splitlines()), "lines ->", out)
