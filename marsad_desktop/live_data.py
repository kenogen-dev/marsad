"""The Live Scan screen's data rules, apart from the drawing so they can be tested on their own
(relay 009). Pure Python: no Qt, no service.

- Recorder states, judged by freshness (rule 9): a recorder whose status file has not been
  rewritten for STALE_S seconds is "silent", whatever the file says. Freshness is timed on the
  window's own monotonic clock, from the moment the window last saw the status change, so a
  change of the PC's clock (summer time, a sync) can neither silence a live recorder nor wake a
  dead one.
- The survey's position: rtl_power writes a whole sweep at once, so all that can be shown is "sweep
  N in progress, S s left", from the clock; rtl_power_fftw writes hop by hop, so its real current
  hop can be shown.
- Refused hops, counted the way the web page counts them.

The page's own calculations (layer split, spur mask, family grid, readout) are in scan_calc.
"""
from __future__ import annotations

import math
import time

STALE_S = 15.0                      # rule 9: no rewrite for this long and a recorder is silent


# ---------------------------------------------------------------- recorders
class RecorderWatch:
    """Remembers, per recorder (by serial), when its status was last seen to change, on a clock
    that only moves forward. The states:

    - "running": its file says running and changed within STALE_S;
    - "starting": running and fresh, but no job yet (the radio is being set up);
    - "silent": its file says running but has not changed for STALE_S - closed, killed or hung;
    - "stopped": its file says stopped (a watch that ended normally);
    - "seen": silent, and the owner pressed "Mark as seen": shown quietly, like stopped.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.seen_at: dict = {}         # serial -> (the status's own "updated" value, clock when first seen)
        self.acknowledged: dict = {}    # serial -> the "updated" value the owner marked as seen

    def update(self, statuses: list) -> list:
        """Each status (as the service gives it) with "view": its state, and "silent_s"."""
        now = self.clock()
        out = []
        for st in statuses or []:
            key = str(st.get("serial") or st.get("receiver") or "?")
            stamp = st.get("updated")
            was = self.seen_at.get(key)
            if was is None or was[0] != stamp:
                self.seen_at[key] = (stamp, now)
                if self.acknowledged.get(key) != stamp:
                    self.acknowledged.pop(key, None)
            age = now - self.seen_at[key][1]
            if st.get("state") == "running":
                if age > STALE_S:
                    view = "seen" if self.acknowledged.get(key) == stamp else "silent"
                else:
                    view = "running" if st.get("job") else "starting"
            else:
                view = "stopped"
            out.append({**st, "view": view, "silent_s": age if view in ("silent", "seen") else 0.0})
        return out

    def mark_seen(self, serial) -> None:
        key = str(serial)
        if key in self.seen_at:
            self.acknowledged[key] = self.seen_at[key][0]


def silent_text(seconds: float) -> str:
    return f"silent {seconds:.0f} s" if seconds < 120 else f"silent {seconds / 60:.0f} min"


# ---------------------------------------------------------------- relay 018: what runs while Live Scan is blank
def scan_running(p: dict) -> bool:
    """Is the newest recording still being written? Its sidecar has no end, and a sweep finished recently
    (within three intervals, at least 90 s): a killed run that never wrote an end stops counting by the clock."""
    if not p or not p.get("file") or p.get("finished"):
        return False
    age = p.get("last_finish_age_s")
    integ = float(p.get("integration_s") or 0) or 0.0
    return age is None or float(age) <= max(90.0, 3 * integ)


def elapsed_text(started_local: str | None, now) -> str | None:
    """"1 h 12 min" since a sidecar's started_local, or None."""
    import datetime as _dt
    if not started_local:
        return None
    try:
        t0 = _dt.datetime.fromisoformat(started_local)
    except ValueError:
        return None
    s = max(0, int((now - t0).total_seconds()))
    return f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else f"{max(1, s // 60)} min"


def running_banner(p: dict, rec_views: list, now) -> dict | None:
    """Relay 018: Live Scan opens blank, but a run in progress is never hidden. {"text", "kind": "scan" | "recorder"
    | "silent"} for the banner, or None when nothing runs. A recorder that has stopped reporting is said to be silent
    (rule 9), never running."""
    bits, kind = [], None
    if scan_running(p):
        f = p.get("facts") or {}
        tag = f.get("tag") or p.get("file")
        el = elapsed_text(f.get("started_local"), now)
        bits.append(f"A scan is running: {tag}" + (f", {el}" if el else "")
                    + (f" (radio {f['receiver']})" if f.get("receiver") else ""))
        kind = "scan"
    live = [v for v in rec_views or [] if v.get("view") in ("running", "starting")]
    silent = [v for v in rec_views or [] if v.get("view") == "silent"]
    for v in live:
        job = v.get("job") or {}
        where = f" on {job['target_mhz']:.4f} MHz" if isinstance(job.get("target_mhz"), (int, float)) else ""
        bits.append(f"A recorder is running: {v.get('receiver') or v.get('serial') or 'radio'}"
                    f" ({v.get('mode') or 'watch'}){where}")
        kind = kind or "recorder"
    for v in silent:
        bits.append(f"The recorder on {v.get('receiver') or v.get('serial') or 'a radio'} has stopped reporting "
                    f"({silent_text(v.get('silent_s') or 0)})")
        kind = kind or "silent"
    return {"text": ". ".join(bits) + ".", "kind": kind} if bits else None


# ---------------------------------------------------------------- the survey's position
def progress(p: dict, fetched_at: float, now: float) -> dict:
    """Where the survey is, for the thin bar along the spectrum's top (rtl_power) or the current-hop
    band (rtl_power_fftw). `p` is /live/progress's answer, fetched at `fetched_at` on the window's
    clock; between answers the rtl_power bar moves on with the clock, so it flows rather than jumps.

    Returns {"fraction": 0..1 or None, "text": ..., "kind": "sweep" | "hop" | "done" | "waiting"}."""
    if not p or not p.get("file"):
        return {"fraction": None, "text": "no recording yet", "kind": "waiting"}
    done = int(p.get("total") or 0)
    expected = p.get("expected")
    if p.get("finished"):
        return {"fraction": 1.0, "kind": "done",
                "text": f"finished · {done} sweep{'s' if done != 1 else ''}"
                        + (f" of {expected}" if expected else "")}
    if p.get("engine") == "rtl_power_fftw":
        pct = p.get("cursor_pct")
        planned, tuned = p.get("planned_hops"), p.get("tuned_hops")
        text = f"pass {done + 1} in progress"
        if planned:
            text += f" · {planned} hops planned"
        if p.get("eta_s") is not None:
            text += f" · {_clock(p['eta_s'])} left in the run"
        return {"fraction": pct, "kind": "hop", "text": text}
    integ = p.get("integration_s")
    pct = p.get("cursor_pct")
    if integ and pct is not None:
        pct = min(1.0, float(pct) + max(0.0, now - fetched_at) / float(integ))
        left = max(0.0, (1.0 - pct) * float(integ))
        text = f"sweep {done + 1} in progress · {left:.0f} s left"
        if expected:
            text = f"sweep {done + 1} of {expected} in progress · {left:.0f} s left"
        return {"fraction": pct, "kind": "sweep", "text": text}
    return {"fraction": None, "kind": "waiting", "text": f"sweep {done + 1} in progress"}


def current_hop(p: dict, hop_centres_mhz: list, half_mhz: float | None) -> tuple | None:
    """rtl_power_fftw's hop being measured now: the planned hop after the last block written
    (its top edge is the cursor). (lo, hi) in MHz, or None when it cannot be known."""
    cur = p.get("cursor") if p else None
    if cur is None or not hop_centres_mhz:
        return None
    ahead = [c for c in sorted(hop_centres_mhz) if c > cur]
    c = ahead[0] if ahead else sorted(hop_centres_mhz)[0]          # past the top: the next pass begins
    half = half_mhz if half_mhz else (_spacing(hop_centres_mhz) / 2 if len(hop_centres_mhz) > 1 else 1.0)
    return (c - half, c + half)


def _spacing(cs: list) -> float:
    cs = sorted(cs)
    d = [b - a for a, b in zip(cs, cs[1:])]
    return sorted(d)[len(d) // 2] if d else 1.0


def _clock(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 3600} h {s % 3600 // 60:02d} min" if s >= 3600 else (f"{s // 60} min {s % 60:02d} s" if s >= 60
                                                                        else f"{s} s")


# ---------------------------------------------------------------- refused hops
def hops_text(planned, tuned, refused) -> str | None:
    """"22 of 22 hops tuned, 8 refused", word for word as the web page says it: an rtl_power_fftw
    run's hops tuned against those planned, and ", K refused" only when the tuner refused some.
    None when the run says nothing about hops (rtl_power)."""
    if not planned:
        return None
    n_ref = len(refused) if isinstance(refused, (list, tuple, set)) else int(refused or 0)
    return f"{tuned or 0} of {planned} hops tuned" + (f", {n_ref} refused" if n_ref else "")


def median(values) -> float | None:
    v = sorted(x for x in values if x is not None and not (isinstance(x, float) and math.isnan(x)))
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2
