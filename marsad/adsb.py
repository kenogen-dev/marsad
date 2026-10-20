"""ADS-B receive check at 1090 MHz: a positive control for the antenna/chain.

rtl_adsb is fixed at 1090 MHz, so this is a pass/fail proof that the receive
path works, plus a message rate that can be compared across antennas.

Comparisons are only valid at identical gain, quality and duration, so those
are recorded in the sidecar and should be held constant across a sweep.

    marsad adsb-check --seconds 600 --gain 40.2 --label "v5 46mm"
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from collections import Counter
from datetime import datetime

from . import host
from .config import Station
from .rtlpower import find_tool

# rtl_adsb emits one frame per line as *<hex>; - 14 hex = 56-bit short,
# 28 hex = 112-bit long (DF17/18 are the ADS-B extended squitters).
FRAME_RE = re.compile(r"^\*([0-9A-Fa-f]+);")


def parse_frames(text):
    """Pull frames out of rtl_adsb output and pick out ICAO addresses.

    Only DF11 (all-call reply) and DF17/DF18 (ADS-B squitter) carry the ICAO
    address in the clear. In DF4/5/20/21 it is XORed with the parity field, so
    it cannot be read without a known-aircraft list - those are counted but
    contribute no ICAO.
    """
    short_n = long_n = 0
    icaos = Counter()
    df_counts = Counter()
    for line in text.splitlines():
        m = FRAME_RE.match(line.strip())
        if not m:
            continue
        hexs = m.group(1).upper()
        if len(hexs) == 28:
            long_n += 1
        elif len(hexs) == 14:
            short_n += 1
        else:
            continue
        df = int(hexs[0:2], 16) >> 3
        df_counts[df] += 1
        if (df in (17, 18) and len(hexs) == 28) or (df == 11 and len(hexs) == 14):
            icaos[hexs[2:8]] += 1
    return {
        "frames_total": short_n + long_n,
        "frames_short": short_n,
        "frames_long": long_n,
        "icao_distinct": len(icaos),
        "icao_max_repeat": max(icaos.values()) if icaos else 0,
        "icao_list": sorted(icaos),
        "df_counts": dict(sorted(df_counts.items())),
    }


def verdict(stats, seconds):
    """Turn the counts into a pass/fail call on the receive path.

    ICAO addresses are the gate, not raw frame count. With -e 5 allowed bit
    errors, noise occasionally decodes into a syntactically valid frame, and
    56-bit short frames are far easier to fake than 112-bit long ones. A real
    aircraft emits many frames carrying the SAME address, so a repeated ICAO
    is very hard to counterfeit. Rate is deliberately NOT part of the gate.
    """
    n_icao = stats["icao_distinct"]
    repeat = stats["icao_max_repeat"]
    long_n = stats["frames_long"]
    rate = stats["frames_per_min"]
    if n_icao >= 2 and long_n >= 10:
        return "pass", (f"{n_icao} distinct aircraft, {long_n} long frames, "
                        f"{rate}/min - receive path confirmed")
    if n_icao == 1 and repeat >= 5:
        return "pass", (f"1 aircraft seen {repeat} times, {long_n} long frames - "
                        f"receive path confirmed, traffic thin")
    if n_icao >= 1:
        return "weak", (f"{n_icao} ICAO but only {repeat} repeat(s), {long_n} long "
                        f"frames - path probably works; re-run longer or in daylight")
    if stats["frames_total"] > 0:
        return "fail", (f"{stats['frames_total']} frames but zero valid ICAO - "
                        f"consistent with noise decoding, not aircraft")
    return "fail", "no frames at all - check device index, gain, and that the antenna is connected"


def main(argv=None, station: Station | None = None, prog: str = "marsad adsb-check") -> int:
    station = station or Station.load()
    ap = argparse.ArgumentParser(prog=prog, description="ADS-B receive check at 1090 MHz")
    ap.add_argument("--seconds", type=int, default=600, help="capture duration (default 600)")
    ap.add_argument("--gain", default="40.2", help="fixed tuner gain, a v5 ladder step")
    ap.add_argument("--device", default="0", help="rtl device index (0 = v5)")
    ap.add_argument("--quality", default="1")
    ap.add_argument("--errors", default="5")
    ap.add_argument("--label", default="", help="what is on the antenna end, e.g. 'v5 46mm'")
    args = ap.parse_args(argv)

    rtl_adsb = find_tool("rtl_adsb", station.tool_dirs())
    if rtl_adsb is None:
        raise SystemExit("rtl_adsb not found: put the RTL-SDR Blog release in rtl-sdr/ or on PATH")
    outdir = station.recordings
    outdir.mkdir(exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    # The filename is a convenience; the sidecar's `label` is the record. Windows
    # still enforces MAX_PATH=260 for rtl_adsb, so cap the slug.
    SLUG_MAX = 48
    slug = re.sub(r"[^A-Za-z0-9]+", "-", args.label).strip("-") or "adsb"
    if len(slug) > SLUG_MAX:
        slug = slug[:SLUG_MAX].rstrip("-")
        print(f"  (filename shortened to '{slug}'; the full label is in the sidecar)")
    raw = outdir / f"adsb_{slug}_{stamp}.txt"
    if len(str(raw)) > 255:
        raise SystemExit(f"output path is {len(str(raw))} chars, over the Windows limit: {raw}")

    cmd = [str(rtl_adsb), "-d", args.device, "-g", str(args.gain),
           "-Q", str(args.quality), "-e", str(args.errors), str(raw)]
    print("  " + " ".join(cmd))
    print(f"  capturing {args.seconds}s to {raw.name} ...")

    started = datetime.now()
    t0 = time.time()
    # rtl_adsb block-buffers its output file, so a hard kill discards anything
    # still in the buffer. A console break makes it flush and exit cleanly,
    # which needs its own process group on Windows.
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=host.popen_group_flags())
    exited_early = False
    try:
        proc.wait(timeout=args.seconds)
        exited_early = True
        print("  rtl_adsb exited early - check the SDR is free and the device index is right")
    except subprocess.TimeoutExpired:
        host.ask_to_stop(proc)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  WARNING: rtl_adsb did not exit cleanly; buffered frames may be lost")
    elapsed = time.time() - t0
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""

    text = raw.read_text(errors="replace") if raw.exists() else ""
    stats = parse_frames(text)
    stats["seconds"] = round(elapsed, 1)
    stats["frames_per_min"] = round(stats["frames_total"] / (elapsed / 60.0), 1) if elapsed else 0.0

    call, reason = verdict(stats, elapsed)

    # "the capture never ran" and "the capture ran and heard nothing" are
    # completely different outcomes. A run that did not happen is not evidence
    # about the sky.
    if exited_early:
        call = "error"
        reason = ("rtl_adsb exited before the capture window - this is NOT a reception "
                  "result and says nothing about the chain")
        if err.strip():
            print("\n  rtl_adsb said:")
            for line in err.strip().splitlines()[-8:]:
                print(f"    {line}")
        if not raw.exists():
            print(f"\n  no output file was created: {raw}")
            print(f"  (path is {len(str(raw))} chars; Windows refuses at 260)")

    sidecar = raw.with_suffix(".json")
    sidecar.write_text(json.dumps({
        "schema": 1, "raw": raw.name, "label": args.label,
        "started_local": started.isoformat(timespec="seconds"),
        "command_line": " ".join(cmd),
        "settings": {"frequency_mhz": 1090.0, "device": args.device, "gain_db": args.gain,
                     "quality": args.quality, "allowed_errors": args.errors,
                     "seconds_requested": args.seconds},
        "stats": stats, "verdict": call, "reason": reason,
        "stderr_tail": err[-500:],
    }, indent=2))

    print(f"\n  frames        {stats['frames_total']}  "
          f"({stats['frames_long']} long / {stats['frames_short']} short)")
    print(f"  rate          {stats['frames_per_min']} frames/min")
    print(f"  aircraft      {stats['icao_distinct']} distinct ICAO")
    print(f"  downlink fmt  {stats['df_counts']}")
    print(f"\n  VERDICT: {call.upper()} - {reason}")
    print(f"  sidecar: {sidecar}")
    return 0
