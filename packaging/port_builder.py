"""Turn tools/scan_builder.html into sdrstation/web/builder.html.

The page keeps all of its layout and logic. What changes:
  - the station constants (gains, limits, gaps, devices, presets, known
    facts, chain modules, retune time, ceiling) are loaded from /config
    instead of being written into the page;
  - the command it emits starts with `sdr run` (the packaged command);
  - the Google Fonts link goes, so the page works offline;
  - the page is forced to the dark theme to match the app it sits inside;
  - "Reset all" referenced a CHAIN_DEFAULT that never existed; it now
    rebuilds the chain for the current receiver.
Every replacement asserts it matched exactly once, so a drift in the source
file fails loudly instead of producing a half-converted page.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
src = (root / "tools" / "scan_builder.html").read_text(encoding="utf-8")


def rep(old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, f"expected {count} match(es), found {n}: {old[:60]!r}"
    src = src.replace(old, new)


# ---- head: offline, dark, proper document
rep('<title>rtl_power Scan Builder</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;500&display=swap">\n',
    '<!doctype html><html data-theme="dark"><head><meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<title>Scan builder</title>\n')
rep('</style>\n\n<div class="wrap">', '</style></head><body>\n<div class="wrap">')
rep('</script>\n', '</script></body></html>\n')
rep('padding-block: 26px 56px;', 'padding-block: 18px 40px;')

# ---- constants become variables filled from /config
rep('const RTL_MAX = 2.8e6, RETUNE = 0.047, CEIL = 2147.483648;\n'
    '// Different tuners, different gain ladders. The E4000\'s 29.0 is not an R820T step, and\n'
    '// librtlsdr silently snaps to the nearest — so the dropdown only ever offers real ones.\n'
    'const GAINS = {\n'
    '  xtr: [-1, 1.5, 4, 6.5, 9, 11.5, 14, 16.5, 19, 21.5, 24, 29, 34, 42],\n'
    '  v5: [0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4,\n'
    '       28, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48, 49.6]\n'
    '};\n',
    '// Everything station-specific arrives from /config (built from station.json) and is\n'
    '// assigned in boot(). Different tuners have different gain ladders, and librtlsdr\n'
    '// silently snaps to the nearest step, so the dropdown only ever offers real ones.\n'
    'let RTL_MAX = 2.8e6, RETUNE = 0.047, CEIL = 2147.483648, CMD = "sdr run";\n'
    'let GAINS = {}, LIMITS = {}, DEVICES = {}, GAPS = {}, RXLABEL = {};\n'
    'let PRESETS = {}, PRESET_ORDER = [], KNOWN = [], CHAIN_BASE = [], SDR_BASE = {}, CHAIN_OFF = {}, ENC_DEFAULT = {};\n'
    'const DUR_DEFAULT = { LS: [16, "h"], deep: [2, "h"], custom: [2, "h"] };\n')
rep('const LIMITS = { xtr: [52, 2187], v5: [24, 1766] };\n'
    '// Enumeration order as measured 2026-10-16. librtlsdr indexes by USB order, which is NOT\n'
    '// guaranteed across replugs - the Device flag warns whenever this is departed from.\n'
    'const DEVICES = { v5: 0, xtr: 1 };\n'
    'const GAPS = { xtr: [[1095, 1240]], v5: [] };\n', '')

# presets block -> removed (filled from config)
start = src.index('const PRESETS = {\n  LS:')
end = src.index('};\n', start) + 3
src = src[:start] + src[end:]
# known block -> removed
start = src.index('/* What this station already knows is in the band, from its own measurements. */\nconst KNOWN = [')
end = src.index('];\n', start) + 3
src = src[:start] + src[end:]
# chain base -> removed
start = src.index('// ONE physical rig. Everything from the antenna down')
end = src.index('const CANON = Object.fromEntries')
src = src[:start] + src[end:]
rep('const CANON = Object.fromEntries(CHAIN_BASE.map(m => [m.id, m.name]));',
    'let CANON = {};')
rep('const SRC_ANT = "Antenna (90° L, 60 mm)", SRC_LOAD = "50 ohm load";',
    'let SRC_ANT = "Antenna", SRC_LOAD = "50 ohm load";')
rep('let curRx = "xtr";\nlet chains = { xtr: freshChain("xtr"), v5: freshChain("v5") };\n'
    'let chain = chains[curRx];   // alias, so edits land in chains[curRx] directly\n',
    'let curRx = "xtr";\nlet chains = {};\nlet chain = [];   // alias, so edits land in chains[curRx] directly\n')
rep('const c = { ...m, on: CHAIN_OFF[rx].includes(m.id) ? false : m.on };',
    'const c = { ...m, on: (CHAIN_OFF[rx] || []).includes(m.id) ? false : m.on };')

# receiver dropdown is built from config, not hard-coded
rep('<select id="receiver"><option value="xtr">XTR (E4000)</option><option value="v5">SMArt v5 (R860)</option></select>',
    '<select id="receiver"></select>')
rep('function fillGains(rx, want) {\n  const list = GAINS[rx] || GAINS.xtr;',
    'function fillGains(rx, want) {\n  const list = GAINS[rx] || Object.values(GAINS)[0] || [0];')
# text that assumed two particular receivers
rep("""  out.push(["", `${tmin}–${tmax} MHz`, s.rx === "xtr"
    ? "Elonics E4000, zero-IF: every hop centre carries a DC spike."
    : "R860, low-IF: no tuning gap, cleaner above ~700 MHz, better for 1090 MHz."]);""",
    """  out.push(["", `${tmin}–${tmax} MHz`, RXLABEL[s.rx] || s.rx]);""")
rep("""    out.push(["warn", "Device", `--device ${s.device}, but the ${s.rx.toUpperCase()} enumerated as index ${DEVICES[s.rx]} on 2026-10-16. Fine if USB order has since changed — but confirm with rtl_test first, because a wrong index silently records one dongle's data under the other's name.`]);""",
    """    out.push(["warn", "Device", `--device ${s.device}, but station.json says the ${s.rx.toUpperCase()} enumerates as index ${DEVICES[s.rx]}. Fine if USB order has since changed — but confirm with rtl_test first, because a wrong index silently records one dongle's data under the other's name.`]);""")
rep("""<code>rtl-sdr/rtl_test.exe</code> and read its device list before a long run.""",
    """<code>rtl_test</code> and read its device list before a long run.""")
rep("""    <p class="hint">Device follows the receiver — v5 is index 0 and the XTR index 1, as they
    enumerated on 2026-10-16. That order is""",
    """    <p class="hint">Device follows the receiver, using the index recorded in station.json. That order is""")

# ---- the command it emits
rep('const parts = ["py tools/run_survey.py", cliPreset, dur];',
    'const parts = [CMD, cliPreset, dur];')
rep('after.push("py tools/adsb_check.py --seconds 60 --gain 40.2 --device " + (s.device || 0) + " --label " + q(s.tag || "run"));',
    'after.push("sdr adsb-check --seconds 600 --gain 40.2 --device " + (s.device || 0) + " --label " + q(s.tag || "run"));')
rep('after.push("# compare: py tools/spur_stats.py reports/<before>/bins.csv reports/<after>/bins.csv");',
    'after.push("# compare: sdr compare-runs <before-tag> <after-tag>");')
rep('<p class="hint">Run from <code>C:\\SDR</code>. Each run writes',
    '<p class="hint">Run from the station folder (the one holding station.json). Each run writes')
rep("and <code>spur_stats.py</code> will pair them.", "and <code>sdr compare-runs</code> will pair them.")
rep("<code>survey.py compare</code> refuses to treat them as equivalent.",
    "<code>sdr compare</code> refuses to treat them as equivalent.")
rep("Pair it with tools/adsb_check.py for the sensitivity half",
    "Pair it with sdr adsb-check for the sensitivity half")
rep("and run_survey.py falls back to whatever station.json last remembered.",
    "and sdr run falls back to whatever station.json last remembered.")
rep("Minutes carry\n// decimals because run_survey.py's duration parser accepts them",
    "Minutes carry\n// decimals because the duration parser accepts them")
rep("Retuning costs 47 ms per hop whatever the integration",
    "Retuning costs <span id=\"retunems\">47</span> ms per hop whatever the integration")

# ---- Reset all bug
rep('$("reset").onclick = () => { load(DEFAULTS); chain = CHAIN_DEFAULT.map(m => ({ ...m })); update(); };',
    '$("reset").onclick = () => { load(DEFAULTS); chain = chains[curRx] = freshChain(curRx); update(); };')

# ---- start-up: everything from `$("preset").innerHTML =` onward runs after config arrives
boot_start = src.index('$("preset").innerHTML = Object.entries(PRESETS)')
boot_end = src.index('update();\n</script>')
boot_body = src[boot_start:boot_end]
boot_body = boot_body.replace(
    'fillGains("xtr", 29);\n',
    'fillGains(curRx, 29);\n')
boot_body = boot_body.replace(
    'const DEFAULTS = {\n  preset: "LS", lo: 1241, hiReq: 2148, bin: 50, gain: 29, integ: 120, crop: 0.25, fir: 9,\n'
    '  durVal: 16, unit: "h", rx: "xtr", device: "0", tag: "overnight", target: "",',
    'const first = PRESETS[PRESET_ORDER[0]] || { lo: 100, hi: 200, bin: 50, gain: 29, integ: 120 };\n'
    'const DEFAULTS = {\n  preset: PRESET_ORDER[0] || "custom", lo: first.lo, hiReq: first.hi, bin: first.bin, gain: first.gain, integ: first.integ, crop: 0.25, fir: 9,\n'
    '  durVal: 1, unit: "h", rx: curRx, device: String(DEVICES[curRx] ?? 0), tag: "", target: "",')
assert 'const DEFAULTS = {\n  preset: PRESET_ORDER[0]' in boot_body
boot_body = boot_body.replace('$("preset").innerHTML = Object.entries(PRESETS)\n  .map(([k, p]) =>',
                              '$("preset").innerHTML = PRESET_ORDER.map(k => [k, PRESETS[k]])\n  .map(([k, p]) =>')
assert 'PRESET_ORDER.map(k => [k, PRESETS[k]])' in boot_body
boot_body = boot_body.replace('try { saved = JSON.parse(localStorage.getItem("scanbuild2") || "null"); } catch (e) {}',
                              'try { saved = JSON.parse(localStorage.getItem("scanbuild3") || "null"); } catch (e) {}')
assert 'scanbuild3' in boot_body
boot_body = boot_body.replace('  for (const rx of ["xtr", "v5"])\n    if (Array.isArray(saved.chains[rx])',
                              '  for (const rx of Object.keys(RXLABEL))\n    if (Array.isArray(saved.chains[rx])')
assert 'Object.keys(RXLABEL)' in boot_body
boot_body = boot_body.replace('const oldRx = saved.s && saved.s.rx === "v5" ? "v5" : "xtr";',
                              'const oldRx = saved.s && RXLABEL[saved.s.rx] ? saved.s.rx : curRx;')
boot_body = boot_body.replace('document.querySelectorAll("input, select, textarea").forEach(el => {',
                              'if (!saved || !saved.s || !saved.s.lo) applyPreset($("preset").value);\n'
                              'document.querySelectorAll("input, select, textarea").forEach(el => {')
assert 'applyPreset($("preset").value);\ndocument' in boot_body

boot = '''async function boot() {
  let cfg = null;
  try { cfg = await (await fetch("/config")).json(); } catch (e) {}
  if (!cfg) { document.body.insertAdjacentHTML("afterbegin", '<p style="color:var(--bad);padding:12px">Could not load /config from the station server.</p>'); return; }
  RTL_MAX = cfg.rtl_max_sample_hz; RETUNE = cfg.retune_ms / 1000; CEIL = cfg.ceiling_mhz; CMD = cfg.cmd_prefix || "sdr run";
  $("retunems").textContent = cfg.retune_ms;
  for (const [rx, r] of Object.entries(cfg.receivers)) {
    GAINS[rx] = r.gains; LIMITS[rx] = r.range; DEVICES[rx] = r.device; GAPS[rx] = r.gaps; RXLABEL[rx] = r.label;
    SDR_BASE[rx] = rx.toUpperCase(); CHAIN_OFF[rx] = cfg.chain_off[rx] || []; ENC_DEFAULT[rx] = cfg.enc_default[rx] || "open bench";
  }
  $("receiver").innerHTML = Object.entries(RXLABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  // JavaScript reorders number-like keys ("900") to the front of any object, so the
  // dropdown and the default preset follow the explicit order the server sends.
  PRESET_ORDER = cfg.preset_order || Object.keys(cfg.presets);
  for (const name of PRESET_ORDER) { const p = cfg.presets[name]; PRESETS[name] = { ...p, dur: p.dur || DUR_DEFAULT[name] || [1, "h"] }; }
  KNOWN = cfg.known || [];
  CHAIN_BASE = cfg.chain_modules || [];
  CANON = Object.fromEntries(CHAIN_BASE.map(m => [m.id, m.name]));
  SRC_ANT = (CHAIN_BASE[0] && CHAIN_BASE[0].name) || "Antenna";
  curRx = Object.keys(RXLABEL)[0] || "xtr";
  for (const rx of Object.keys(RXLABEL)) chains[rx] = freshChain(rx);
  chain = chains[curRx];
  $("receiver").value = curRx;

'''
src = src[:boot_start] + boot + boot_body + 'update();\n}\nboot();\n</script>' + src[boot_end + len('update();\n</script>'):]

out = root / "sdrstation" / "web" / "builder.html"
out.write_text(src, encoding="utf-8")
print(len(src.splitlines()), "lines ->", out)
