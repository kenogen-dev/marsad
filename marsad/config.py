"""Station configuration: where the data lives and what the receiver is.

One file, station.json, in the station folder. It holds the facts that differ
from one station to the next (receivers, spur families, presets, the measured
retune time) and a `defaults` section that `marsad run` rewrites with the last
chain and antenna answers. Everything else in the file is preserved exactly.

The station folder is found in this order:
  1. --station PATH on the command line, or the SDR_STATION environment variable
  2. the current directory, if it holds a station.json
  3. the folder the executable lives in (frozen build)
  4. the source checkout (the folder above this package)
  5. the current directory, with built-in defaults

station.local.json beside station.json, if it exists, is laid over it (dicts merged
key by key, anything else replaced). It holds what must not go into git - today the
station's `location` - and .gitignore keeps it out. Nothing writes it: `marsad run`,
the Chain tab and calibrate-retune write station.json only, re-read from disk, so a
local key can never be copied into the shared file.

Built-in defaults come from station.default.json beside this module, which
describes the station the program was first written for. `marsad init` copies it
out so a new station has something to edit.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .rtlpower import frozen

STATION_FILE = "station.json"
LOCAL_FILE = "station.local.json"       # git-ignored overlay (the location)
DEFAULT_FILE = Path(__file__).resolve().parent / "station.default.json"

# The flat keys the pre-packaging station.json held: just the chain answers.
LEGACY_DEFAULT_KEYS = ("antenna_shape", "whip_length_mm", "lna", "filter", "chain_order")
# chain_used_at: when a run last recorded the default chain_order. A later run
# without --chain must confirm the rig is still built like that (record.py).
DEFAULT_KEYS = LEGACY_DEFAULT_KEYS + ("chain_used_at",)


def _strip_notes(d):
    """Drop every key starting with an underscore, recursively."""
    if isinstance(d, dict):
        return {k: _strip_notes(v) for k, v in d.items() if not str(k).startswith("_")}
    if isinstance(d, list):
        return [_strip_notes(v) for v in d]
    return d


def builtin_defaults() -> dict:
    return _strip_notes(json.loads(DEFAULT_FILE.read_text(encoding="utf-8")))


def source_root() -> Path | None:
    """The git checkout, when running from source."""
    if frozen():
        return None
    return Path(__file__).resolve().parent.parent


def find_root(explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("SDR_STATION")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd()
    if (cwd / STATION_FILE).exists():
        return cwd
    if frozen():
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / STATION_FILE).exists():
            return exe_dir
    src = source_root()
    if src and (src / STATION_FILE).exists():
        return src
    return cwd


class Station:
    """Everything the tools need to know about where they are running."""

    def __init__(self, root: Path, data: dict, file_found: bool):
        self.root = Path(root)
        self.data = data
        self.file_found = file_found

    # ---- loading -----------------------------------------------------
    @classmethod
    def load(cls, root: str | os.PathLike | None = None) -> "Station":
        root = find_root(root)
        base = builtin_defaults()
        path = root / STATION_FILE
        found = path.exists()
        if found:
            try:
                user = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"  (ignoring unreadable {path.name}: {exc})", file=sys.stderr)
                user = {}
            if not isinstance(user, dict):
                user = {}
            user = _strip_notes(user)
            if _is_legacy(user):
                # The old flat file held only the chain answers.
                user = {"defaults": user}
            for key, val in user.items():
                if key == "defaults" and isinstance(val, dict):
                    base["defaults"].update(val)
                else:
                    base[key] = val
        local = root / LOCAL_FILE
        if local.exists():
            try:
                over = json.loads(local.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"  (ignoring unreadable {local.name}: {exc})", file=sys.stderr)
                over = {}
            if isinstance(over, dict):
                _overlay(base, _strip_notes(over))
        return cls(root, base, found)

    @property
    def file(self) -> Path:
        return self.root / STATION_FILE

    # ---- folders -----------------------------------------------------
    @property
    def surveys(self) -> Path:
        return self.root / "surveys"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def recordings(self) -> Path:
        return self.root / "recordings"

    @property
    def db(self) -> Path:
        return self.root / "occupancy.db"

    @property
    def venv_python(self) -> Path:
        """The station's own venv, if it has one (source checkouts do)."""
        if os.name == "nt":
            return self.root / ".venv" / "Scripts" / "python.exe"
        return self.root / ".venv" / "bin" / "python"

    def tool_dirs(self) -> list[Path]:
        """Where to look for rtl_power & co. before falling back to PATH."""
        dirs = []
        cfg = self.data.get("rtl_sdr_dir")
        if cfg:
            p = Path(cfg)
            dirs.append(p if p.is_absolute() else self.root / p)
        if frozen():
            exe_dir = Path(sys.executable).resolve().parent
            dirs += [exe_dir / "rtl-sdr", exe_dir]
        src = source_root()
        if src:
            dirs.append(src / "rtl-sdr")
        seen, unique = set(), []
        for d in dirs:
            if d not in seen:
                seen.add(d)
                unique.append(d)
        return unique

    # ---- facts ---------------------------------------------------------
    @property
    def receivers(self) -> dict:
        return self.data.get("receivers", {})

    def receiver_gaps(self) -> dict:
        """{name: [(lo, hi), ...]} in MHz, the analyser's mask table."""
        return {name: [tuple(g) for g in r.get("gaps_mhz", [])]
                for name, r in self.receivers.items()}

    def rtl_power(self):
        """(path or None, warning or None): the rtl_power build to run.
        rtl_power_gainfix unless station.json's rtl_power_exe says otherwise."""
        from .rtlpower import choose_rtl_power
        return choose_rtl_power(self.data.get("rtl_power_exe"), self.tool_dirs(), self.root)

    def tuner_of(self, receiver: str) -> str | None:
        r = self.receivers.get(receiver)
        return (r or {}).get("tuner")

    @property
    def spur_families(self) -> list[tuple[float, str]]:
        return [(float(f), str(label)) for f, label in self.data.get("spur_families_mhz", [])]

    @property
    def presets(self) -> dict:
        return self.data.get("presets", {})

    @property
    def retune_ms_per_hop(self) -> float:
        return float(self.data.get("retune_ms_per_hop", 47.0))

    @property
    def detection_floors(self) -> dict:
        return {k: float(v) for k, v in self.data.get("detection_floor_db", {}).items()}

    @property
    def defaults(self) -> dict:
        return dict(self.data.get("defaults", {}))

    # ---- writing -------------------------------------------------------
    def save_defaults(self, answers: dict) -> None:
        """Update the `defaults` section in station.json and nothing else.

        Reads the file fresh so hand edits made since start-up survive, and
        writes the full structure back if the file was still the old flat form.
        """
        keep = {k: answers[k] for k in DEFAULT_KEYS if k in answers}
        try:
            if self.file.exists():
                on_disk = json.loads(self.file.read_text(encoding="utf-8"))
                if not isinstance(on_disk, dict):
                    on_disk = {}
            else:
                on_disk = {}
            if _is_legacy(on_disk):
                on_disk = {"defaults": on_disk}
            on_disk.setdefault("defaults", {}).update(keep)
            self.file.write_text(json.dumps(on_disk, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError) as exc:
            print(f"  (could not save defaults: {exc})")
        self.data.setdefault("defaults", {}).update(keep)

    def set_value(self, key: str, value) -> None:
        """Write one top-level key (used by calibrate-retune)."""
        on_disk = {}
        if self.file.exists():
            try:
                on_disk = json.loads(self.file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                on_disk = {}
        if _is_legacy(on_disk):
            on_disk = {"defaults": on_disk}
        on_disk[key] = value
        self.file.write_text(json.dumps(on_disk, indent=2) + "\n", encoding="utf-8")
        self.data[key] = value

    def write_full(self, overwrite: bool = False) -> Path:
        """`marsad init`: put a complete, commented station.json in the folder."""
        if self.file.exists() and not overwrite:
            raise SystemExit(f"{self.file} already exists (use --force to replace it)")
        self.file.write_text(DEFAULT_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        return self.file


def _overlay(base: dict, over: dict) -> None:
    """Lay `over` onto `base` in place: dicts merge key by key, anything else replaces."""
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _overlay(base[key], val)
        else:
            base[key] = val


def _is_legacy(d: dict) -> bool:
    """The pre-packaging station.json: only chain answers at the top level."""
    return bool(d) and all(k in LEGACY_DEFAULT_KEYS for k in d)
