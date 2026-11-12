"""Relay 027: who holds a radio, in plain words, and Marsad's own leftover setup processes.

When Detect, the tuner sweep or another setup measurement does not finish, the window says why: which program has a
radio open, or, when none does, that the radio did not answer (unplug it, wait, plug it back in). Rule 7 names what
holds a radio; rule 1 never stops a scan or a recorder. So the only process this module ever offers to stop is one
that Marsad itself started for setup on this station and that has outlived its job: a leftover.

How a leftover is recognised, never by its name alone:
  - every radio-opening process the setup code starts is written to the station's ledger (logs/setup_processes.json):
    its pid, its creation time, what it was for, its setup job (when it has one) and the latest it should still be
    running ("until");
  - a radio program running now is the setup's when its own pid, or one of its ancestors' (followed through the
    parent ids Windows keeps, even of a parent that has ended), is in the ledger with matching times;
  - it is a leftover when its job has ended, or (no job) its "until" has passed.
Everything else is named and never touched: this station's measurement still running (Cancel it on its step), a
Marsad scan or recorder (here or on another station), SDR++, or another program.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import host
from .config import Station
from .rtlpower import SDR_PROGRAMS

LEDGER = "setup_processes.json"
KEEP = 300                                  # ledger entries kept (oldest dropped)


# ---------------------------------------------------------------- processes

def _created_win(pid: int) -> float | None:
    """A process's creation time (epoch seconds) from GetProcessTimes, or None."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
        if not k.GetProcessTimes(wintypes.HANDLE(h), ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt),
                                 ctypes.byref(ut)):
            return None
        ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
        return ticks / 1e7 - 11644473600.0
    finally:
        k.CloseHandle(wintypes.HANDLE(h))


def created(pid: int) -> float | None:
    try:
        if host.IS_WINDOWS:
            return _created_win(pid)
        out = subprocess.run(["ps", "-o", "etimes=", "-p", str(int(pid))], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        return time.time() - float(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None


def snapshot() -> list[dict]:
    """Every process: {"pid", "ppid", "name" (lower case), "created"}. Module-level, so tests replace it."""
    procs = []
    if host.IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        if snap in (None, wintypes.HANDLE(-1).value):
            return []
        try:
            e = PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            ok = k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                procs.append({"pid": int(e.th32ProcessID), "ppid": int(e.th32ParentProcessID),
                              "name": e.szExeFile.lower()})
                ok = k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
    else:
        try:
            out = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        for line in out.splitlines():
            bits = line.split(None, 2)
            if len(bits) == 3 and bits[0].isdigit() and bits[1].isdigit():
                procs.append({"pid": int(bits[0]), "ppid": int(bits[1]), "name": Path(bits[2]).name.lower()})
    return procs


def command_lines(pids) -> dict:
    """{pid: command line} for a few processes (only to tell a Marsad scan from other programs). Module-level, so
    tests replace it. Empty when the system will not say."""
    pids = sorted({int(p) for p in pids if p})
    if not pids:
        return {}
    try:
        if host.IS_WINDOWS:
            flt = " or ".join(f"ProcessId={p}" for p in pids)
            ps = ("Get-CimInstance Win32_Process -Filter '" + flt + "' | ForEach-Object { '{0}|{1}' -f "
                  "$_.ProcessId,($_.CommandLine -replace '[\\r\\n]+',' ') }")
            out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                                 capture_output=True, text=True, timeout=25, stdin=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        else:
            out = "\n".join(ln.strip().replace(" ", "|", 1) for ln in subprocess.run(
                ["ps", "-o", "pid=,args=", "-p", ",".join(map(str, pids))], capture_output=True, text=True,
                timeout=20).stdout.splitlines())
    except (OSError, subprocess.SubprocessError):
        return {}
    res = {}
    for line in out.splitlines():
        a, _, b = line.strip().partition("|")
        if a.strip().isdigit():
            res[int(a)] = b.strip()
    return res


def _sdr_names() -> set:
    """Programs that hold a radio open. rtl_eeprom too: Detect's own question, and a stuck one holds the radio, but the
    engine's SDR_PROGRAMS leaves it out (it would refuse runs during every detection)."""
    names = [*SDR_PROGRAMS, "rtl_eeprom"]
    return {p.lower() + ".exe" for p in names} | {p.lower() for p in names}


# ---------------------------------------------------------------- the ledger

def _ledger_path(station: Station) -> Path:
    return station.root / "logs" / LEDGER


def ledger(station: Station) -> list[dict]:
    try:
        d = json.loads(_ledger_path(station).read_text(encoding="utf-8"))
        return [e for e in d.get("processes") or [] if isinstance(e, dict) and e.get("pid")]
    except (OSError, ValueError, AttributeError):
        return []


def note_started(station: Station, pid: int, purpose: str, until_s: float, job: str | None = None,
                 radio: str | None = None, step: str | None = None) -> dict:
    """Write one radio-opening process Marsad started for setup into the ledger. Never raises: a ledger that cannot
    be written only means that process is named instead of being offered for stopping."""
    t = created(pid) or time.time()
    e = {"pid": int(pid), "created": t, "purpose": purpose, "until": t + float(until_s), "job": job,
         "radio": radio, "step": step, "at": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        p = _ledger_path(station)
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = (ledger(station) + [e])[-KEEP:]
        tmp = p.with_suffix(".json.part")
        tmp.write_text(json.dumps({"schema": 1, "processes": rows}, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass
    return e


# ---------------------------------------------------------------- who holds a radio

def _running_jobs(station: Station) -> list:
    from .service_info import setup_jobs_running
    return setup_jobs_running(station)


def _entry_running(e: dict, jobs: list, now: float, table: dict, holder_pid: int) -> bool:
    """Is the work this ledger entry was started for still going?
      - a setup job's entry: that job has not ended;
      - an entry with no job (the tuner sweep's own process, which also runs by hand): its process is still alive, or
        a job of the same radio and step is running;
      - Detect's rtl_eeprom (the entry is the radio program itself): its time limit has not passed."""
    if e.get("job"):
        return any(str(j.get("id")) == str(e["job"]) for j in jobs)
    me = table.get(int(e["pid"]))
    if me is not None and "created" not in me:
        me["created"] = created(int(e["pid"]))
    if int(e["pid"]) != int(holder_pid) and me is not None and \
            (me.get("created") is None or abs(float(me["created"]) - float(e.get("created") or 0)) <= 2):
        return True
    if e.get("radio") and e.get("step") and any(j.get("radio") == e["radio"] and j.get("step") == e["step"]
                                                 for j in jobs):
        return True
    return int(e["pid"]) == int(holder_pid) and now <= float(e.get("until") or 0)


def _ancestors(p: dict, table: dict, depth: int = 12) -> list[int]:
    """The parent ids above a process, nearest first, including the first one that has ended (Windows keeps the
    number). Stops where a pid was reused by a younger process."""
    out, cur = [], p
    for _ in range(depth):
        pp = int(cur.get("ppid") or 0)
        if pp <= 0 or pp in out:
            break
        out.append(pp)
        parent = table.get(pp)
        if parent is None:
            break
        if "created" not in parent:
            parent["created"] = created(pp)
        pc, cc = parent.get("created"), cur.get("created")
        if pc is not None and cc is not None and pc > cc + 1:
            break                                           # the number now belongs to a younger process
        cur = parent
    return out


def _ledger_match(p: dict, chain: list[int], table: dict, entries: list[dict]) -> dict | None:
    pc = p.get("created")
    for pid in [p["pid"], *chain]:
        for e in reversed(entries):
            if int(e["pid"]) != pid:
                continue
            ec = float(e.get("created") or 0)
            alive = table.get(pid)
            if alive is not None and alive.get("created") is not None and abs(alive["created"] - ec) > 2 \
                    and alive["created"] < (pc or 0) + 1:
                continue                                    # that number is a different process now
            if pc is not None and pc < ec - 2:
                continue                                    # older than the entry: not its child
            return e
    return None


def _station_of(cmd: str) -> str | None:
    m = re.search(r"--station\s+(\"[^\"]+\"|\S+)", cmd or "")
    return m.group(1).strip('"') if m else None


def holders(station: Station, procs: list | None = None, cmdlines=None, now: float | None = None) -> list[dict]:
    """Every radio program running now, each {"pid", "program", "kind", "text", "stoppable", "created"}.

    kind: "setup-leftover" (Marsad's own setup process that outlived its job: the only stoppable kind),
          "setup-running" (this station's measurement, still running: Cancel it on its step),
          "marsad-run" (a Marsad scan or recorder, never touched), "sdrpp" (SDR++), "other"."""
    now = time.time() if now is None else now
    procs = snapshot() if procs is None else procs
    table = {int(p["pid"]): dict(p) for p in procs}
    names = _sdr_names()
    radio = [p for p in table.values() if p.get("name") in names]
    if not radio:
        return []
    for p in radio:
        if "created" not in p:
            p["created"] = created(p["pid"])
    entries = ledger(station)
    running = _running_jobs(station)
    out, ask = [], set()
    chains = {}
    for p in radio:
        chains[p["pid"]] = _ancestors(p, table)
        ask.update(chains[p["pid"]][:6])
    cl = None
    for p in sorted(radio, key=lambda x: x["pid"]):
        prog = p["name"][:-4] if p["name"].endswith(".exe") else p["name"]
        e = _ledger_match(p, chains[p["pid"]], table, entries)
        base = {"pid": p["pid"], "program": prog, "created": p.get("created"), "stoppable": False}
        if e is not None:
            what = e.get("purpose") or "setup measurement"
            base.update(job=e.get("job"), radio=e.get("radio"), step=e.get("step"))
            if _entry_running(e, running, now, table, p["pid"]):
                out.append(dict(base, kind="setup-running",
                                text=f"{prog}: this station's {what}, still running (Cancel it on its step)"))
            else:
                age = (now - float(p.get("created") or e.get("created") or now)) / 60
                out.append(dict(base, kind="setup-leftover", stoppable=True, purpose=what,
                                text=f"{prog}: left over from Marsad's {what}, which has ended "
                                     f"({age:.0f} min old). Nothing is recording through it."))
            continue
        if prog.startswith(("sdrpp", "sdrplusplus")):
            out.append(dict(base, kind="sdrpp", text="SDR++"))
            continue
        if cl is None:
            cl = (cmdlines if cmdlines is not None else command_lines)
            cl = cl(ask) if callable(cl) else cl
        marsad_cmd = next((cl.get(a) for a in chains[p["pid"]] if "marsad" in (cl.get(a) or "").lower()), None)
        if marsad_cmd is not None:
            where = _station_of(marsad_cmd)
            here = where and Path(where).resolve() == Path(station.root).resolve()
            kind = "a recorder (marsad watch)" if " watch" in marsad_cmd else "a scan"
            out.append(dict(base, kind="marsad-run",
                            text=f"{prog}: {kind} started by Marsad " + ("on this station" if here else
                                                                         f"on the station at {where}" if where else
                                                                         "on your main station")
                                 + ". Marsad never stops a scan or recorder for you: wait for it to end, or stop it "
                                   "where it was started."))
            continue
        out.append(dict(base, kind="other", text=f"{prog} (another program)"))
    return out


def timeout_words(what: str, secs: float, hs: list[dict]) -> str:
    """Why `what` did not finish within `secs`, and what to do, in plain words."""
    if not hs:
        return (f"{what} didn't finish within {secs:g} s, and no other program has a radio open: the radio didn't "
                "answer. Unplug it, wait 5 seconds, plug it back in, then try again.")
    lines = [f"{what} didn't finish within {secs:g} s. A radio is held by:"] + [f"  - {h['text']}" for h in hs]
    todo = []
    if any(h["kind"] == "setup-leftover" for h in hs):
        todo.append("stop the leftover measurement (the button below asks first)")
    if any(h["kind"] == "sdrpp" for h in hs):
        todo.append("close SDR++")
    if any(h["kind"] == "other" for h in hs):
        todo.append("close " + ", ".join(sorted({h["program"] for h in hs if h["kind"] == "other"})))
    if any(h["kind"] in ("marsad-run", "setup-running") for h in hs):
        todo.append("wait for the run to finish")
    return "\n".join(lines) + ("\nThen try again: " + "; ".join(todo) + "." if todo else "")


def busy_words(what: str, hs: list[dict]) -> str:
    """Why `what` cannot start now: the programs holding radios, named."""
    if not hs:
        return f"{what} waits until no radio program is running."
    return (f"{what} opens each radio for a moment, so it waits until nothing is using one. Running now:\n"
            + "\n".join(f"  - {h['text']}" for h in hs))


def stop_leftover(station: Station, pid: int) -> dict:
    """Stop one leftover setup process (and anything under it), after the owner confirmed in the window. Checked again
    here: refused for anything that is not this station's own leftover (rule 1: never a scan or a recorder)."""
    hs = holders(station)
    h = next((x for x in hs if int(x["pid"]) == int(pid)), None)
    if h is None:
        return {"stopped": False, "text": "That process has already ended.", "holders": hs}
    if not h.get("stoppable"):
        raise ValueError("Marsad only stops its own leftover setup measurement, and this one is not that: "
                         + h["text"])
    try:
        if host.IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=20,
                           stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            host.kill_pid(int(pid))
    except (OSError, subprocess.SubprocessError):
        pass
    for _ in range(20):
        if not any(int(p["pid"]) == int(pid) for p in snapshot()):
            break
        time.sleep(0.25)
    left = holders(station)
    gone = not any(int(x["pid"]) == int(pid) for x in left)
    return {"stopped": gone, "holders": left,
            "text": (f"Stopped the leftover {h['program']} ({h.get('purpose') or 'setup measurement'})."
                     if gone else f"{h['program']} did not stop. Unplug the radio, wait 5 seconds and plug it back in.")}
