"""`marsad watch`: the triggered recorder (RECORDER_DESIGN.md phases 2 and 3).

    marsad watch fixed --freq 1265.44M --receiver v5 --duration 1h
        Park one radio on one frequency. Every time the band rises 12 dB over its
        floor an event is catalogued with a fingerprint; an open band also gets IQ
        (SigMF, cut down around the signal), at most one capture per 30 min per
        frequency. Each capture is characterised and its recipe (rate, tuning,
        duration, gain) is used for the next one, so the second capture of
        anything is better than the first.

    marsad watch survey milair 2h --chain "..." --conditions "..."
        The XTR surveys with the normal `marsad run` (started unchanged, so its
        fingerprint and files are exactly what a plain run makes); the v5 records
        what the survey finds. Options this command does not know are passed to
        `marsad run`.

    marsad watch list [--last 30]          the captures catalogue, newest first
    marsad watch ... --dry-run             show the plan, open no radio

Trigger rules (each a station.json `recorder` setting): SNR >= 12 dB over the pass's
local floor on 2 consecutive passes; never a clock-harmonic family (14.4, 24, 25,
28.8, 100, 480 MHz), a hop-centre DC spike or a hop-wide impulse - except the
steady reference carriers (385.03, 299.999 MHz), which get one capture a day; a
30 min cooldown per frequency; capture 30 s, at most 120 s. The AMBUSH: one pass
at >= 20 dB parks the recorder on that frequency for 10 min, recording if it comes
back; a two-pass trigger elsewhere pre-empts it.

Receivers are found by SERIAL with rtl_eeprom (the USB index moves when a dongle
is unplugged), and the recorder's rtl_sdr is given the serial, never an index.
Private bands never have raw IQ written: see capture.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import statistics as st
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import capture as cap
from . import characterise, host, probe
from .config import Station
from .rtlpower import find_tool, parse_duration

FAMILY_14_4 = (14.4, "14.4 MHz SDR clock harmonic")
STATUS_EVERY_S = 2.0       # how often a running watch rewrites its status file
RECENT_EVENTS = 12         # events the status file lists, for the Scan tab's markers


# ---------------------------------------------------------------- small parsers

def parse_freq_hz(text: str) -> float:
    """1265.44M, 341.31e6, 385030k, or a bare number: MHz below 100000, Hz above."""
    t = str(text).strip().lower().replace("hz", "")
    m = re.fullmatch(r"([0-9.]+(?:e[0-9+-]+)?)\s*([kmg]?)", t)
    if not m:
        raise SystemExit(f"cannot read a frequency from {text!r} (try 1265.44M)")
    v = float(m.group(1))
    mult = {"k": 1e3, "m": 1e6, "g": 1e9}.get(m.group(2))
    if mult:
        return v * mult
    return v * 1e6 if v < 1e5 else v


# ---------------------------------------------------------------- which radio

def resolve_receiver(station: Station, role: str, cfg: dict, detect=None) -> dict:
    """Find a receiver by its serial with rtl_eeprom. Refuses rather than guess.

    Returns {"role", "serial", "device" (today's index), "tuner"}.
    """
    serial = cap.serial_of(cfg, role)
    if not serial:
        raise SystemExit(f"  station.json has no serial for the {role!r} receiver "
                         "(recorder.serials); the recorder never picks a radio by index.")
    got = (detect or probe.detect_receivers)(station)
    for d in got.get("found", []):
        if (d.get("serial") or "").strip() == serial:
            return {"role": role, "serial": serial, "device": d["device"],
                    "tuner": station.tuner_of(role) or d.get("tuner", "")}
    seen = ", ".join(f"{d.get('product') or '?'} SN {d.get('serial') or '?'} at {d['device']}"
                     for d in got.get("found", [])) or (got.get("error") or "nothing")
    raise SystemExit(f"\n  The {role} (serial {serial}) was not found. Detected: {seen}.\n"
                     "  Plug it in, or close the program holding it (SDR++, rtl_tcp...).\n"
                     "  Nothing was recorded.")


def in_range(station: Station, role: str, freq_hz: float) -> bool:
    r = station.receivers.get(role) or {}
    lo, hi = r.get("range_mhz") or (0, 1e9)
    if not (lo <= freq_hz / 1e6 <= hi):
        return False
    return not any(a <= freq_hz / 1e6 <= b for a, b in r.get("gaps_mhz") or [])


# ---------------------------------------------------------------- the recorder

def launch_rtl_sdr(cmd: list, log_path: Path):
    """Start rtl_sdr writing IQ to its stdout pipe. Returns (stream, stop)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, bufsize=0,
                            creationflags=host.popen_group_flags())

    def stop():
        if proc.poll() is None:
            try:
                host.ask_to_stop(proc)
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait()
        try:
            proc.stdout.close()
        except OSError:
            pass
        log.close()
    return proc.stdout, stop


class Recorder:
    """One radio, its catalogue rows, its captures and the characteriser loop."""

    def __init__(self, station: Station, cfg: dict, rx: dict, exe: list, chain: str,
                 gain_db: float | None = None, launch=None, quiet: bool = False,
                 inline_characterise: bool = False):
        self.station, self.cfg, self.rx = station, cfg, rx
        self.exe, self.chain = list(exe), chain
        self.gain_db = float(gain_db if gain_db is not None else (cfg.get("gain_db") or {}).get(rx["role"], 29.7))
        self.ppm = cap.ppm_of(cfg, rx["serial"])
        self.launch = launch or launch_rtl_sdr
        self.con = cap.connect(station)
        self.budget = cap.Budget(station, self.con, cfg)
        self.pool = self.budget.pool
        self.quiet = quiet
        self.clock = time.time()          # end of the stream so far (sample-counted)
        self.last_iq: dict = {}
        self.recipe_ready = threading.Event()
        self.lock = threading.Lock()
        self.threads: list = []
        self.cooldown_s = float(cfg.get("cooldown_min", 30)) * 60
        # A replayed file arrives faster than a radio does; the tests characterise in step
        # so the retune lands where it would in real time.
        self.inline = inline_characterise
        self.st = None                    # the live status, once start_status has run
        self._hb = threading.Event()
        self.sess = None                  # the session listening now: its live spectrum goes in the status

    def say(self, text: str) -> None:
        if not self.quiet:
            print(text, flush=True)

    # -- the live status the web app shows (Captures strip, and a line on every tab)
    def status_path(self) -> Path:
        return self.pool / f"recorder_status_{self.rx['serial']}.json"

    def start_status(self, mode: str, ends_at: float | None = None, **fields) -> None:
        """Begin reporting. A thread rewrites the status file every STATUS_EVERY_S seconds
        (and every change writes it at once), so the page can tell a running watch from a
        dead one by the `updated` stamp. It never touches the radio or the stream."""
        with self.lock:
            self.st = {"state": "running", "mode": mode, "receiver": self.rx["role"],
                       "serial": self.rx["serial"], "pid": os.getpid(), "started": time.time(),
                       "ends_at": ends_at, "job": None, "events": 0, "last_event": None,
                       "capture": None, "captures": 0, "cooldowns": {}, "recent_events": [], **fields}
        self._hb = threading.Event()
        threading.Thread(target=self._beat, daemon=True).start()
        self.write_status()

    def _beat(self) -> None:
        while not self._hb.wait(STATUS_EVERY_S):
            self.write_status()

    def note_status(self, **fields) -> None:
        if self.st is None:
            return
        with self.lock:
            self.st.update(fields)
        self.write_status()

    def stop_status(self) -> None:
        if self.st is None:
            return
        self._hb.set()
        with self.lock:
            self.st.update(state="stopped", job=None, capture=None, stopped=time.time())
        self.write_status()

    def snapshot(self) -> dict:
        with self.lock:
            d = json.loads(json.dumps(self.st))
        now = time.time()
        cool = [{"mhz": round(float(k) / 1e6, 4), "until": t} for k, t in d.pop("cooldowns").items() if t > now]
        d["cooldowns"] = sorted(cool, key=lambda c: c["until"])
        job = d.get("job")
        if job:
            t = job["target_mhz"] * 1e6
            nxt = [c["until"] for c in cool if abs(c["mhz"] * 1e6 - t) <= 25e3]
            job["next_capture_at"] = max(nxt) if nxt else None       # None: the next event may record
        d["ambush"] = ({"mhz": job["target_mhz"], "until": job["until"]}
                       if job and job.get("kind") == "ambush" else None)
        # The session replaces its `live` dict whole, so this reads it without a lock and
        # never touches the stream: power per column only, the same for every tier.
        sess = self.sess
        live = getattr(sess, "live", None) if sess is not None else None
        d["spectrum"] = dict(live) if (live and d.get("job")) else None
        d["updated"] = now
        return d

    def write_status(self) -> None:
        """Best effort, atomic: a failed write never stops a capture."""
        if self.st is None:
            return
        try:
            self.pool.mkdir(parents=True, exist_ok=True)
            path = self.status_path()
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self.snapshot()), encoding="utf-8")
            os.replace(tmp, path)
        except (OSError, ValueError):
            pass

    def cool(self, freq_hz: float, until: float) -> None:
        if self.st is not None:
            with self.lock:
                self.st["cooldowns"][str(round(freq_hz))] = until

    def day_dir(self, epoch: float) -> Path:
        return self.pool / dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")

    # -- one session on one tuning
    def session(self, plan: dict, band: dict, mode: str, duration_s: float, capture_s: float,
                trigger: str, survey: str | None = None, stop=None, restart=None,
                stop_after_capture: bool = False, extra: dict | None = None) -> dict:
        tier = band["tier"]
        cmd = cap.rtl_sdr_command(self.exe[0], self.rx["serial"], plan, self.ppm)
        cmd = self.exe + cmd[1:]
        state = {"prev_start": None, "rows": []}
        t0 = max(self.clock, time.time())

        def on_event(ev, sess):
            self._event_row(ev, sess, plan, band, mode, trigger, survey, state, extra or {})

        def allow_iq(t_s):
            now = sess.t0 + t_s
            target = plan.get("target_hz", plan["signal_hz"])
            key = round(target / 10e3)
            last = self.last_iq.get(key)
            if last is None:
                rows = cap.near(self.con, target, max(10e3, plan["width_hz"] / 2),
                                "iq_status = 'written'")
                last = rows[0]["started_epoch"] if rows else None
            if mode == "watch" and last is not None and now - last < self.cooldown_s:
                return "cooldown"
            expect = capture_s * plan["store_rate_hz"] * 4
            r = self.budget.iq_refusal(expect)
            if r:
                self.budget.say_once(r)
            return r

        def open_writer(sess_, epoch):
            target = plan.get("target_hz", plan["signal_hz"])
            self.last_iq[round(target / 10e3)] = epoch
            if mode == "watch":
                self.cool(target, epoch + self.cooldown_s)
            stem = cap.stem_for(target, epoch, self.rx["role"])
            meta = {"description": f"{trigger}; {band.get('band') or 'open band'}",
                    "hw": f"{self.station.receivers.get(self.rx['role'], {}).get('label', self.rx['role'])}, "
                          f"SN {self.rx['serial']}",
                    "receiver": self.rx["role"], "serial": self.rx["serial"], "ppm": self.ppm,
                    "chain": self.chain, "signal_hz": plan["signal_hz"], "trigger": trigger,
                    "survey": survey}
            self.say(f"    recording IQ: {stem} ({plan['store_rate_hz'] / 1e3:g} kS/s, {capture_s:g} s)")
            self.note_status(capture={"file": stem + ".sigmf-data", "started": epoch, "ends_at": epoch + capture_s,
                                      "target_mhz": target / 1e6})
            return cap.IQWriter(self.day_dir(epoch), stem, plan, meta, epoch)

        def on_closed(c, sess_):
            if self.st is not None:
                self.note_status(capture=None, captures=self.st["captures"] + 1)
            self.budget.add(c["bytes"])
            c["gain_db"] = plan.get("gain_db")
            self._characterise_later(c)

        kind = "ambush" if stop_after_capture else {"now": "capture", "watch": "fixed watch"}[mode]
        self.note_status(job={"kind": kind, "target_mhz": plan.get("target_hz", plan["signal_hz"]) / 1e6,
                              "tuned_mhz": plan["tune_hz"] / 1e6, "rate": plan["rtl_rate_hz"],
                              "gain_db": plan.get("gain_db"), "tier": tier, "band": band.get("band") or None,
                              "trigger": trigger, "since": time.time(), "until": time.time() + duration_s})
        stream, stop_proc = self.launch(cmd, self.pool / "rtl_sdr.log")
        sess = cap.Session(plan, tier, mode=mode, duration_s=duration_s, capture_s=capture_s,
                           threshold_db=float(self.cfg.get("threshold_db", 12)),
                           hold_s=float(self.cfg.get("hold_s", 1.0)),
                           event_max_s=float(self.cfg.get("capture_max_s", 120)), t0_epoch=t0,
                           on_event=on_event, allow_iq=allow_iq,
                           open_writer=None if tier == "private" else open_writer,
                           on_capture_closed=on_closed, stop=stop, restart=restart,
                           stop_after_capture=stop_after_capture)
        reader = cap.StreamReader(stream, sess.block * 2)
        self.sess = sess
        try:
            out = sess.run(reader)
        except KeyboardInterrupt:
            sess.finish()              # close the event and the capture, so its .sigmf-meta exists
            raise
        finally:
            stop_proc()
            self.sess = None
            self.note_status(job=None, capture=None)
        self.clock = t0 + out["seconds"]
        out["eof"] = out["ended"] == "eof"
        out["rows"] = state["rows"]
        return out

    def _event_row(self, ev, sess, plan, band, mode, trigger, survey, state, extra):
        start = sess.t0 + ev["start_s"]
        private = band["tier"] == "private"
        flags = []
        if private:
            flags.append(cap.PRIVATE)
            why = cap.anomaly(ev, band, self.cfg)
            if why:
                flags.append(cap.ANOMALY)
                ev["anomaly"] = why
        stem = cap.stem_for(plan.get("target_hz", plan["signal_hz"]), start, self.rx["role"])
        fp = cap.save_fingerprint(self.day_dir(start) / "fingerprints", stem, ev, start, plan["signal_hz"])
        fp_bytes = fp.stat().st_size
        self.budget.add(fp_bytes)
        c = ev.get("capture_started")
        if private:
            iq = "private: not kept"
        elif c is not None:
            iq = "written"
        elif ev.get("in_capture"):
            iq = "inside an earlier capture"
        else:
            iq = ev.get("iq_refused") or "not recorded"
        row = {"started": dt.datetime.fromtimestamp(start).isoformat(timespec="milliseconds"),
               "started_epoch": start, "duration_s": ev["duration_s"],
               "freq_hz": plan["signal_hz"] + ev.get("centre_rel_hz", 0.0),
               "target_hz": plan.get("target_hz", plan["signal_hz"]),
               "tuned_hz": plan["tune_hz"], "receiver": self.rx["role"], "serial": self.rx["serial"],
               "mode": mode, "trigger": trigger, "survey": survey, "tier": band["tier"],
               "band": band.get("band") or None, "flags": "; ".join(flags) or None,
               "snr_peak_db": round(ev["snr_peak_db"], 2), "snr_median_db": round(ev["snr_median_db"], 2),
               "bandwidth_hz": ev.get("bandwidth_hz"), "peak_hz": plan["signal_hz"] + ev["peak_rel_hz"],
               "duty": round(ev["duty"], 4),
               "repeat_s": (start - state["prev_start"]) if state["prev_start"] else None,
               "captured": 1 if (mode != "watch" or c is not None) else 0,
               "iq_status": iq, "fingerprint": str(fp), "fp_bytes": fp_bytes,
               "rtl_rate_hz": plan["rtl_rate_hz"], "store_rate_hz": None if private else plan["store_rate_hz"],
               "gain_db": plan.get("gain_db"), "ppm": self.ppm, "chain": self.chain,
               "notes": "; ".join(x for x in [ev.get("anomaly"), extra.get("notes")] if x) or None}
        state["prev_start"] = start
        rid = cap.add_row(self.con, row)
        if self.st is not None:
            last = {"at": start, "mhz": row["freq_hz"] / 1e6, "snr_db": row["snr_peak_db"],
                    "iq": iq, "private": private}
            self.note_status(events=self.st["events"] + 1, last_event=last,
                             recent_events=(self.st.get("recent_events") or [])[-(RECENT_EVENTS - 1):] + [last])
        state["rows"].append(rid)
        self.say(f"    event {row['started'][11:19]}  {row['freq_hz'] / 1e6:.4f} MHz  "
                 f"peak {row['snr_peak_db']:.1f} dB  {row['duration_s']:.2f} s  {iq}"
                 + (f"  [{row['flags']}]" if row["flags"] else ""))
        if c is not None:
            with self.lock:
                c["row_id"] = rid
            self._link(c)

    def _link(self, c: dict) -> None:
        """Write what is known about a capture into its row, whichever half finished last."""
        with self.lock:
            rid = c.get("row_id")
            if rid is None or c.get("path") is None:
                return
            fields = {"iq_path": c["path"].replace(".sigmf-meta", ".sigmf-data"), "iq_bytes": c["bytes"]}
            if not c.get("linked"):
                c["linked"] = True
            else:
                fields = {}
            res = c.get("result")
            if res is not None and not c.get("result_linked"):
                c["result_linked"] = True
                fields.update(res)
        if fields:
            con = cap.connect(self.station)
            try:
                cap.update_row(con, rid, **fields)
            finally:
                con.close()

    def _characterise_later(self, c: dict) -> None:
        def work():
            meta = Path(c["path"])
            try:
                res = characterise.characterise(meta)
                stem = characterise.out_stem(meta)
                md = meta.parent / f"{stem}.characterise.md"
                md.write_text(characterise.to_markdown(res), encoding="utf-8")
                (meta.parent / f"{stem}.characterise.json").write_text(
                    json.dumps(characterise._clean(res), indent=2), encoding="utf-8")
                recipe = res.get("recipe")
                if recipe:
                    recipe = characterise._clean(recipe)
                    recipe["gain_db_used"] = c.get("gain_db")
                    recipe["occupied_hz"] = (res.get("occupied") or {}).get("bandwidth_hz")
                out = {"family": res.get("family"), "characterised": str(md),
                       "recipe": recipe}
                self.say(f"    characterised {meta.name}: {res.get('family')}")
            except BaseException as exc:      # a bad capture must not stop the watch
                out = {"notes": f"characterise failed: {exc}"}
                recipe = None
            with self.lock:
                c["result"] = out
            self._link(c)
            if recipe:
                self.recipe_ready.set()
        if self.inline:
            work()
            return
        t = threading.Thread(target=work, daemon=True)
        self.threads.append(t)
        t.start()

    def wait_characterised(self, timeout: float = 600) -> None:
        for t in self.threads:
            t.join(timeout)


# ---------------------------------------------------------------- phase 2: fixed watch

def capture_len(cfg: dict, recipe: dict | None, override: float | None = None) -> float:
    s = override or (recipe or {}).get("duration_s") or cfg.get("capture_s", 30)
    return float(min(float(s), float(cfg.get("capture_max_s", 120))))


def fixed_watch(rec: Recorder, freq_hz: float, duration_s: float, width_hz: float | None = None,
                capture_s: float | None = None, stop=None) -> dict:
    """Park on freq_hz for duration_s. Restarts the radio when a new recipe arrives."""
    band = cap.band_tier(freq_hz, rec.cfg)
    end = rec.clock + duration_s
    stop = stop or threading.Event()
    total = {"events": 0, "captures": 0, "sessions": 0, "plan": None}
    own = rec.st is None
    if own:
        rec.start_status("fixed watch", ends_at=time.time() + duration_s, watch_mhz=freq_hz / 1e6)
    try:
        _fixed_loop(rec, freq_hz, band, end, width_hz, capture_s, stop, total)
    finally:
        rec.wait_characterised()
        if own:
            rec.stop_status()
    return total


def _fixed_loop(rec, freq_hz, band, end, width_hz, capture_s, stop, total) -> None:
    while rec.clock < end - 0.5 and not stop.is_set():
        recipe = cap.recipe_for(rec.con, freq_hz)
        plan = cap.plan_capture(freq_hz, None if recipe else width_hz, rec.rx["tuner"], recipe, rec.gain_db)
        clen = capture_len(rec.cfg, recipe, capture_s)
        total["plan"] = plan
        rec.recipe_ready.clear()
        rec.say(f"  listening on {freq_hz / 1e6:.4f} MHz: tuned {plan['tune_hz'] / 1e6:.4f}, "
                f"{plan['rtl_rate_hz'] / 1e6:g} MS/s, cut {plan['store_rate_hz'] / 1e3:g} kS/s, "
                f"watching {plan['width_hz'] / 1e3:.0f} kHz, gain {plan['gain_db']:g} ({plan['source']})")
        out = rec.session(plan, band, "watch", end - rec.clock, clen, "fixed watch",
                          stop=stop, restart=rec.recipe_ready)
        total["events"] += out["events"]
        total["captures"] += out["captures"]
        total["sessions"] += 1
        if out["eof"]:
            rec.say("  the radio stopped sending samples; the watch ends here.")
            break
        if rec.recipe_ready.is_set():
            rec.say("  new recipe from the characteriser: retuning")


# ---------------------------------------------------------------- phase 3: what a pass shows

def pass_detections(latest: dict, step_hz: float, hop_centres_hz, threshold_db: float,
                    floor_window_mhz: float, families: list) -> list:
    """Detections in ONE pass, against that pass's own local floor.

    The live view's method (marsad/live.py Tailer.peaks) on the latest pass rather
    than the max-hold: block medians a quarter of the floor window wide, the median
    of the five blocks around each point as the floor, contiguous bins over the
    threshold grouped, width 6 dB down. Tags: clock families, hop centres, hop-wide.
    """
    if not latest or not step_hz:
        return []
    keys = sorted(latest)
    lo_k = keys[0]
    bw = max(1, int(round(floor_window_mhz * 1e3 / 4)))
    blocks: dict = {}
    for k in keys:
        v = latest[k]
        if v == v:
            blocks.setdefault(int((k - lo_k) // bw), []).append(v)
    bmed = {b: st.median(vs) for b, vs in blocks.items()}
    floor = {b: st.median([bmed[j] for j in range(b - 2, b + 3) if j in bmed]) for b in bmed}
    step_k = step_hz / 1e3
    tol_k = max(step_hz * 1.5, 10e3) / 1e3
    hc_k = sorted(h / 1e3 for h in hop_centres_hz)
    hop_k = st.median([b - a for a, b in zip(hc_k, hc_k[1:])]) if len(hc_k) > 1 else 0.0
    snr = []
    for k in keys:
        f = floor.get(int((k - lo_k) // bw))
        v = latest[k]
        snr.append(None if (f is None or v != v) else v - f)
    out, i, n = [], 0, len(keys)
    while i < n:
        if snr[i] is None or snr[i] < threshold_db:
            i += 1
            continue
        j = i
        while j + 1 < n and snr[j + 1] is not None and snr[j + 1] >= threshold_db and keys[j + 1] - keys[j] <= 2 * step_k:
            j += 1
        p = max(range(i, j + 1), key=lambda t: snr[t])
        thr = latest[keys[p]] - 6
        a = p
        while a > 0 and (latest.get(keys[a - 1], -1e9) >= thr) and keys[a] - keys[a - 1] <= 2 * step_k:
            a -= 1
        b = p
        while b < n - 1 and (latest.get(keys[b + 1], -1e9) >= thr) and keys[b + 1] - keys[b] <= 2 * step_k:
            b += 1
        width_k = (b - a + 1) * step_k
        f_mhz = keys[p] / 1e3
        tags = []
        for base, label in families:
            m = round(f_mhz / base)
            if m >= 1 and abs(f_mhz - m * base) * 1e3 <= tol_k:
                tags.append(f"{label} (x{m})")
        if hc_k and min(abs(h - keys[p]) for h in hc_k) <= tol_k:
            tags.append("hop centre: zero-IF DC artefact")
        if hop_k and width_k >= 0.8 * hop_k:
            tags.append(f"hop-wide ({width_k / hop_k:.1f} hops): impulse")
        out.append({"f_mhz": f_mhz, "snr_db": float(snr[p]), "width_khz": width_k,
                    "tags": tags, "tol_khz": tol_k})
        i = j + 1
    return out


def trigger_families(station: Station) -> list:
    """The clock families that never trigger. The sub-MHz 600 kHz comb is left out:
    ~7 % of arbitrary lines land on it by chance (CLAUDE.md), so it is no evidence."""
    fams = [(f, lab) for f, lab in station.spur_families if f >= 1.0]
    if not any(abs(f - 14.4) < 1e-6 for f, _ in fams):
        fams.append(FAMILY_14_4)
    return fams


def same_signal(f1_mhz: float, w1_khz: float, f2_mhz: float, w2_khz: float, tol_khz: float = 25.0) -> bool:
    """Two detections are one signal when their occupied bands overlap (at least within tol)."""
    return abs(f1_mhz - f2_mhz) * 1e3 <= max(tol_khz, ((w1_khz or 0) + (w2_khz or 0)) / 2)


class Triggers:
    """The trigger rules, as a state machine fed one pass at a time. No radio, no clock.

    `last_handled(freq_hz, tol_hz, width_hz)` says when the recorder last spent a capture
    on that signal (the catalogue, so a restart does not re-capture). Two detections are
    the same signal when their occupied bands overlap (`same_signal`), so a broad hump whose
    peak bin wanders pass to pass cools down once, not once per bin. Returns actions:
    {"kind": "capture" | "ambush", "f_mhz", "snr_db", "width_khz", "why"}.
    """

    def __init__(self, cfg: dict, last_handled=None):
        self.threshold = float(cfg.get("threshold_db", 12))
        self.passes = int(cfg.get("passes", 2))
        self.ambush_db = float(cfg.get("ambush_db", 20))
        self.cooldown_s = float(cfg.get("cooldown_min", 30)) * 60
        self.refs = [float(x) for x in cfg.get("reference_carriers_mhz", [])]
        self.last_handled = last_handled or (lambda f, tol, w=0.0: None)
        self.fired: dict = {}             # freq MHz -> (epoch, width kHz), until the catalogue has the row
        self.tracks: list = []            # {"f", "w", "count", "pass"}
        self.n = 0

    def reference(self, f_mhz: float, tol_khz: float) -> bool:
        return any(abs(f_mhz - r) * 1e3 <= max(tol_khz, 15.0) for r in self.refs)

    def cooling(self, f_mhz: float, now: float, tol_khz: float, width_khz: float = 0.0) -> bool:
        tol = max(tol_khz, 25.0)
        times = [t for k, (t, w) in self.fired.items() if same_signal(k, w, f_mhz, width_khz, tol)]
        last = self.last_handled(f_mhz * 1e6, tol * 1e3, width_khz * 1e3)
        if last is not None:
            times.append(last)
        if not times:
            return False
        t = max(times)
        if self.reference(f_mhz, tol_khz):
            return t >= cap.local_midnight(now)          # one reference capture a day
        return now - t < self.cooldown_s

    def feed(self, dets: list, now: float) -> list:
        self.n += 1
        tracks, actions = [], []
        for d in sorted(dets, key=lambda x: -x["snr_db"]):
            if d["snr_db"] < self.threshold:
                continue
            ref = self.reference(d["f_mhz"], d["tol_khz"])
            if d["tags"] and not ref:
                continue                                  # a known artefact never triggers
            tol = max(2 * d["tol_khz"] / 1.5, 25.0)
            prev = next((t for t in self.tracks if t["pass"] == self.n - 1
                         and same_signal(t["f"], t["w"], d["f_mhz"], d["width_khz"], tol)), None)
            count = prev["count"] + 1 if prev else 1
            tracks.append({"f": d["f_mhz"], "w": d["width_khz"], "count": count, "pass": self.n})
            if self.cooling(d["f_mhz"], now, d["tol_khz"], d["width_khz"]):
                continue
            act = {"f_mhz": d["f_mhz"], "snr_db": d["snr_db"], "width_khz": d["width_khz"],
                   "reference": ref}
            if count >= self.passes:
                act.update(kind="capture", why=f"{count} passes, {d['snr_db']:.1f} dB"
                           + (" (reference carrier)" if ref else ""))
            elif count == 1 and d["snr_db"] >= self.ambush_db:
                act.update(kind="ambush", why=f"one pass at {d['snr_db']:.1f} dB")
            else:
                continue
            self.fired[d["f_mhz"]] = (now, d["width_khz"])
            actions.append(act)
        self.tracks = tracks
        return actions


class Dispatcher:
    """One recorder, one job at a time. A capture outranks an ambush and pre-empts it."""

    PRIORITY = {"capture": 2, "ambush": 1}

    def __init__(self, run_job):
        self.run_job = run_job
        self.queue: list = []
        self.current = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.closing = False
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def submit(self, act: dict) -> None:
        with self.lock:
            cur = self.current
            def same(q):
                return same_signal(q["f_mhz"], q.get("width_khz", 0), act["f_mhz"], act.get("width_khz", 0))
            if cur and same(cur) and cur["kind"] == "ambush":
                return                              # already parked there; it records when it shows
            self.queue = [q for q in self.queue if not same(q)]
            self.queue.append(act)
            self.queue.sort(key=lambda q: (-self.PRIORITY[q["kind"]], -q["snr_db"]))
            if cur and self.PRIORITY[act["kind"]] > self.PRIORITY[cur["kind"]]:
                cur["stop"].set()
        self.wake.set()

    def _loop(self):
        while True:
            self.wake.wait(1.0)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.queue:
                        self.current = None
                        break
                    job = self.queue.pop(0)
                    job["stop"] = threading.Event()
                    self.current = job
                self.run_job(job)
            if self.closing:
                return

    def close(self, wait_s: float = 150) -> None:
        with self.lock:
            self.queue = [q for q in self.queue if q["kind"] == "capture"]
            if self.current and self.current["kind"] == "ambush":
                self.current["stop"].set()
        self.closing = True
        self.wake.set()
        self.t.join(wait_s)


def survey_job(rec: Recorder, station: Station, survey_name: str, xtr_ppm: float):
    """What the dispatcher runs for one action: a capture or an ambush on the v5."""
    cfg = rec.cfg

    def run(job):
        f_true = job["f_mhz"] * 1e6 / (1.0 + xtr_ppm * 1e-6)      # the survey radio's own error out
        band = cap.band_tier(f_true, cfg)
        trigger = f"survey {job['why']}"
        if not in_range(station, rec.rx["role"], f_true):
            cap.add_row(rec.con, {"started": dt.datetime.now().isoformat(timespec="seconds"),
                                  "started_epoch": time.time(), "freq_hz": f_true, "target_hz": f_true,
                                  "receiver": rec.rx["role"], "mode": job["kind"], "trigger": trigger,
                                  "survey": survey_name, "tier": band["tier"], "band": band["band"] or None,
                                  "flags": cap.PRIVATE if band["tier"] == "private" else None,
                                  "snr_peak_db": job["snr_db"], "bandwidth_hz": job["width_khz"] * 1e3,
                                  "captured": 1, "iq_status": "outside the recorder's range"})
            return
        recipe = cap.recipe_for(rec.con, f_true)
        plan = cap.plan_capture(f_true, None if recipe else max(job["width_khz"] * 1e3, 5e3),
                                rec.rx["tuner"], recipe, rec.gain_db)
        clen = capture_len(cfg, recipe)
        rec.cool(f_true, (cap.local_midnight(time.time()) + 86400) if job.get("reference")
                 else time.time() + rec.cooldown_s)
        rec.say(f"  -> {job['kind']} {f_true / 1e6:.4f} MHz ({job['why']}"
                + (f"; {band['band']}, PRIVATE: fingerprint only" if band["tier"] == "private" else "") + ")")
        if job["kind"] == "capture":
            rec.session(plan, band, "now", clen, clen, trigger, survey_name, stop=job["stop"],
                        extra={"notes": f"survey SNR {job['snr_db']:.1f} dB, width {job['width_khz']:.1f} kHz"})
        else:
            # the survey pass itself is the record of a one-off that never returns
            cap.add_row(rec.con, {"started": dt.datetime.now().isoformat(timespec="seconds"),
                                  "started_epoch": time.time(), "freq_hz": f_true, "target_hz": f_true, "receiver": "survey",
                                  "mode": "survey pass", "trigger": trigger, "survey": survey_name,
                                  "tier": band["tier"], "band": band["band"] or None,
                                  "flags": cap.PRIVATE if band["tier"] == "private" else None,
                                  "snr_peak_db": job["snr_db"], "bandwidth_hz": job["width_khz"] * 1e3,
                                  "captured": 1, "iq_status": "ambush: recorder parked 10 min"})
            rec.session(plan, band, "watch", float(cfg.get("ambush_min", 10)) * 60, clen,
                        f"ambush after {trigger}", survey_name, stop=job["stop"], stop_after_capture=True)
    return run


def find_survey_file(station: Station, since: float, timeout_s: float = 120, proc=None) -> Path | None:
    """The newest survey CSV or .dat (with its sidecar) written since `since`."""
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        cands = [p for pat in ("*.csv", "*.dat") for p in station.surveys.glob(pat)
                 if p.stat().st_mtime >= since - 2 and p.with_suffix(".json").exists()]
        if cands:
            return max(cands, key=lambda p: p.stat().st_mtime)
        if proc is not None and proc.poll() is not None:
            return None
        time.sleep(1.0)
    return None


def watch_tailer(path: Path):
    """The live viewer's tailer for this survey, keeping EVERY completed pass at full
    resolution until the watcher takes it (the viewer itself only keeps the newest:
    the first poll of a running survey can bring several passes at once)."""
    from . import live
    base = live.DatTailer if path.suffix == ".dat" else live.Tailer

    class PassTailer(base):
        def _finish(self, stamp):
            n = len(self.sweeps)
            super()._finish(stamp)
            if len(self.sweeps) != n or self.dropped:
                self.done.append((self.latest, self.step_hz, sorted(self.hop_centres)))

    t = PassTailer.__new__(PassTailer)
    t.done = []
    t.__init__(path, 1000, 4)
    return t


def follow_survey(tailer, triggers: Triggers, dispatcher, families, floor_window_mhz: float,
                  alive, say=print, poll_s: float = 1.0, on_pass=None) -> int:
    """Feed each completed pass of a growing survey to the trigger rules. Returns passes seen."""
    seen = 0
    while True:
        running = alive()
        tailer.poll()
        with tailer.lock:
            todo, tailer.done = tailer.done, []
        for latest, step, hc in todo:
            seen += 1
            if on_pass:
                on_pass(seen)
            dets = pass_detections(latest, step, hc, triggers.threshold, floor_window_mhz, families)
            for act in triggers.feed(dets, time.time()):
                say(f"  pass {seen}: {act['kind']} {act['f_mhz']:.4f} MHz ({act['why']})")
                dispatcher.submit(act)
        if not running:
            return seen
        time.sleep(poll_s)


# ---------------------------------------------------------------- the command

def build_parser(prog: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description="The triggered recorder (RECORDER_DESIGN.md)")
    sub = ap.add_subparsers(dest="what", required=True)
    f = sub.add_parser("fixed", help="park one radio on one frequency and record whenever it is active")
    f.add_argument("--freq", required=True, help="signal frequency, e.g. 1265.44M")
    f.add_argument("--receiver", default="v5", help="receiver role (station.json receivers), default v5")
    f.add_argument("--duration", required=True, help="how long to watch, e.g. 1h, 30m")
    f.add_argument("--width", help="watched bandwidth before any recipe exists, e.g. 50k (default: the whole band)")
    f.add_argument("--gain", type=float, help="tuner gain (default: recorder.gain_db for this receiver)")
    f.add_argument("--capture", type=float, help="seconds per IQ capture (default: the recipe, else 30)")
    f.add_argument("--chain", help="the physical chain of this radio (default: station.json's default chain)")
    f.add_argument("--dry-run", action="store_true", help="show the plan, open no radio")
    s = sub.add_parser("survey", help="XTR surveys with `marsad run`, the v5 records what it finds")
    s.add_argument("preset")
    s.add_argument("duration")
    s.add_argument("--recorder", default="v5", help="recorder role, default v5")
    s.add_argument("--surveyor", default="xtr", help="survey role, default xtr")
    s.add_argument("--recorder-gain", type=float)
    s.add_argument("--recorder-chain", help="the recorder's physical chain (the survey's is --chain)")
    s.add_argument("--dry-run", action="store_true")
    li = sub.add_parser("list", help="the captures catalogue, newest first")
    li.add_argument("--last", type=int, default=30)
    return ap


def show_list(station: Station, last: int) -> int:
    con = cap.connect(station)
    rows = con.execute("SELECT * FROM captures ORDER BY started_epoch DESC LIMIT ?", (last,)).fetchall()
    if not rows:
        print("  The captures catalogue is empty.")
        return 0
    print(f"  {'started':19}  {'MHz':>10}  {'mode':11} {'SNR':>5}  {'bw kHz':>7}  {'dur s':>6}  iq / flags")
    for r in rows:
        bw = f"{r['bandwidth_hz'] / 1e3:7.1f}" if r["bandwidth_hz"] else "      -"
        print(f"  {(r['started'] or '')[:19]:19}  {r['freq_hz'] / 1e6:10.4f}  {(r['mode'] or '')[:11]:11} "
              f"{(r['snr_peak_db'] or 0):5.1f}  {bw}  {(r['duration_s'] or 0):6.2f}  {r['iq_status'] or ''}"
              + (f"  [{r['flags']}]" if r["flags"] else "") + (f"  {r['family']}" if r["family"] else ""))
    return 0


def rtl_sdr_exe(station: Station) -> list:
    exe = find_tool("rtl_sdr", station.tool_dirs())
    if exe is None:
        raise SystemExit("  rtl_sdr not found in rtl-sdr/ or on PATH.")
    return [str(exe)]


def main(argv=None, station: Station | None = None, prog: str = "marsad watch", detect=None,
         launch=None, exe: list | None = None, spawn=None, poll_s: float = 1.0) -> int:
    """detect / launch / exe / spawn replace rtl_eeprom, rtl_sdr and the `marsad run` child
    in the tests, so the whole recorder runs with no dongle."""
    station = station or Station.load()
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser(prog)
    args, rest = ap.parse_known_args(argv)
    if args.what == "list":
        return show_list(station, args.last)
    cfg = cap.settings(station)
    if args.what == "fixed":
        if rest:
            ap.error(f"unrecognised arguments: {' '.join(rest)}")
        return main_fixed(station, cfg, args, detect, launch, exe)
    return main_survey(station, cfg, args, rest, detect, launch, exe, spawn or subprocess.Popen, poll_s)


def main_fixed(station, cfg, args, detect, launch, exe) -> int:
    freq = parse_freq_hz(args.freq)
    _, dur = parse_duration(args.duration)
    role = args.receiver
    if role not in station.receivers:
        raise SystemExit(f"  unknown receiver {role!r}; station.json has {', '.join(station.receivers)}")
    if not in_range(station, role, freq):
        raise SystemExit(f"  {freq / 1e6:.4f} MHz is outside what the {role} can tune.")
    band = cap.band_tier(freq, cfg)
    chain = args.chain or station.defaults.get("chain_order", "")
    width = parse_freq_hz(args.width) if args.width else None
    tuner = station.tuner_of(role)
    gain = args.gain if args.gain is not None else (cfg.get("gain_db") or {}).get(role, 29.7)
    print(f"  marsad watch fixed: {freq / 1e6:.4f} MHz on the {role} for {args.duration}")
    print(f"  band: {band['band'] or 'not in the tier table'} -> {band['tier'].upper()}"
          + ("  (fingerprint only: no IQ is ever written for this band)" if band["tier"] == "private" else ""))
    print(f"  chain: {chain or '(none given)'}")
    print(f"  pool: {cap.pool_dir(station, cfg)}")
    if args.dry_run:
        con = cap.connect(station)
        recipe = cap.recipe_for(con, freq)
        plan = cap.plan_capture(freq, None if recipe else width, tuner, recipe, gain)
        ser = cap.serial_of(cfg, role) or "?"
        print(f"  plan: {json.dumps({k: plan[k] for k in ('tune_hz', 'rtl_rate_hz', 'store_rate_hz', 'width_hz', 'gain_db', 'source')})}")
        print("  would run: " + " ".join(cap.rtl_sdr_command("rtl_sdr", ser, plan, cap.ppm_of(cfg, ser))))
        print("  (dry run: no radio opened)")
        return 0
    rx = resolve_receiver(station, role, cfg, detect)
    rec = Recorder(station, cfg, rx, exe or rtl_sdr_exe(station), chain, gain, launch)
    stop = threading.Event()
    try:
        tot = fixed_watch(rec, freq, dur, width, args.capture, stop)
    except KeyboardInterrupt:
        stop.set()
        print("\n  stopped.")
        rec.wait_characterised()
        return 1
    print(f"\n  done: {tot['events']} events, {tot['captures']} IQ captures. `marsad watch list` shows them.")
    return 0


def main_survey(station, cfg, args, rest, detect, launch, exe, spawn, poll_s) -> int:
    pre = station.presets.get(args.preset)
    if pre is None:
        raise SystemExit(f"  unknown preset {args.preset!r}")
    _, dur = parse_duration(args.duration)
    gain = args.recorder_gain if args.recorder_gain is not None else (cfg.get("gain_db") or {}).get(args.recorder, 29.7)
    rchain = args.recorder_chain or "(recorder chain not given)"
    if "--device" in rest or any(a.startswith("--device=") for a in rest):
        raise SystemExit("  --device is chosen by serial here; leave it out.")
    if args.dry_run:
        print(f"  would detect {args.surveyor} (SN {cap.serial_of(cfg, args.surveyor)}) and "
              f"{args.recorder} (SN {cap.serial_of(cfg, args.recorder)}) with rtl_eeprom,")
        print(f"  run: marsad run {args.preset} {args.duration} --device <{args.surveyor} index> "
              f"--non-interactive --stop-file <...> {' '.join(rest)}")
        print(f"  and record on the {args.recorder} at gain {gain:g}: threshold {cfg.get('threshold_db')} dB, "
              f"{cfg.get('passes')} passes, ambush {cfg.get('ambush_db')} dB, cooldown {cfg.get('cooldown_min')} min.")
        print("  (dry run: no radio opened)")
        return 0
    surveyor = resolve_receiver(station, args.surveyor, cfg, detect)
    rx = resolve_receiver(station, args.recorder, cfg, detect)
    rec = Recorder(station, cfg, rx, exe or rtl_sdr_exe(station), rchain, gain, launch)
    stopfile = station.reports / f"watch_{cap.stamp(time.time())}.stop"
    cmd = host.self_command("run", args.preset, args.duration, "--device", str(surveyor["device"]),
                            "--non-interactive", "--stop-file", str(stopfile), *rest,
                            prefer_venv=station.venv_python)
    print(f"  survey on the {args.surveyor} (index {surveyor['device']}), recorder on the {args.recorder} "
          f"(SN {rx['serial']})\n  starting: {' '.join(cmd)}")
    t_start = time.time()
    proc = spawn(cmd, cwd=str(station.root))
    path = find_survey_file(station, t_start, proc=proc)
    if path is None:
        proc.wait()
        raise SystemExit("  the survey did not start (its own message is above). Nothing was recorded.")
    print(f"  following {path.name}")
    fw = float(pre.get("floor_window") or 2.0)
    survey_name = path.stem
    trig = Triggers(cfg, lambda f, tol, w=0.0: cap.last_captured(rec.con, f, tol, w))
    xtr_ppm = cap.ppm_of(cfg, surveyor["serial"])
    disp = Dispatcher(survey_job(rec, station, survey_name, xtr_ppm))
    rec.start_status("survey + recorder", ends_at=t_start + dur, survey=survey_name,
                     surveyor=args.surveyor, surveyor_serial=surveyor["serial"], passes=0)
    try:
        follow_survey(watch_tailer(path), trig, disp, trigger_families(station), fw,
                      alive=lambda: proc.poll() is None, say=rec.say, poll_s=poll_s,
                      on_pass=lambda n: rec.note_status(passes=n))
    except KeyboardInterrupt:
        stopfile.parent.mkdir(parents=True, exist_ok=True)
        stopfile.write_text("stop")
        print("\n  stopping: the survey finishes and analyses what it has; the recorder ends its capture.")
        proc.wait()
    finally:
        disp.close()
        rec.wait_characterised()
        rec.stop_status()
    print(f"\n  survey and recorder done. `marsad watch list` shows the catalogue.")
    return proc.returncode or 0
