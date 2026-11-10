"""Relay 022: "Find my location", from Windows' own location service on this PC.

Asks Windows' location API (Windows.Devices.Geolocation.Geolocator, through Windows PowerShell, which reaches it with
nothing installed). Windows works the position out itself; Marsad sends nothing anywhere and never uses an IP lookup.
Nothing is written to disk and nothing is logged: the caller gets the figures in memory, to fill two fields for the
owner to confirm.

If Windows' location is switched off (for the PC, for this account, or for desktop apps), find() says which, and how
to turn it on in Windows' Privacy settings. It never changes a Windows setting.

    find(timeout=25) -> {"ok": True, "lat", "lon", "accuracy_m", "source"}
                      | {"ok": False, "reason": "off" | "denied" | "no fix" | "timeout" | "unavailable", "text": words}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROUND_DP = 2                    # "Round to about 1 km": two decimal places (0.01 deg of latitude is about 1.1 km)

SETTINGS_HOW = ("Turn it on in Windows: Settings > Privacy & security > Location. Switch on \"Location services\", "
                "\"Let apps access your location\" and \"Let desktop apps access your location\". Marsad does not change "
                "Windows settings itself. Typing the two values by hand always works.")

_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location"

# The Windows PowerShell side: ask for access, then one position. Prints one line of JSON, nothing else.
_PS = r"""
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $m = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
  function Wait-Op($op, $type, $ms) {
    $t = $m.MakeGenericMethod($type).Invoke($null, @($op))
    if (-not $t.Wait($ms)) { throw 'timeout' }
    return $t.Result
  }
  $null = [Windows.Devices.Geolocation.Geolocator, Windows.Devices.Geolocation, ContentType = WindowsRuntime]
  $acc = Wait-Op ([Windows.Devices.Geolocation.Geolocator]::RequestAccessAsync()) ([Windows.Devices.Geolocation.GeolocationAccessStatus]) 8000
  if ("$acc" -ne 'Allowed') { @{ status = 'denied'; access = "$acc" } | ConvertTo-Json -Compress; exit 0 }
  $g = New-Object Windows.Devices.Geolocation.Geolocator
  $g.DesiredAccuracyInMeters = 50
  $pos = Wait-Op ($g.GetGeopositionAsync()) ([Windows.Devices.Geolocation.Geoposition]) 20000
  $c = $pos.Coordinate
  @{ status = 'ok'; lat = $c.Point.Position.Latitude; lon = $c.Point.Position.Longitude; accuracy = $c.Accuracy;
     source = "$($c.PositionSource)" } | ConvertTo-Json -Compress
} catch {
  $w = "$($_.Exception.Message)"
  if ($w -match 'timeout') { @{ status = 'timeout' } | ConvertTo-Json -Compress }
  elseif ($w -match '0x80070005|denied|Access') { @{ status = 'denied'; access = 'error' } | ConvertTo-Json -Compress }
  else { @{ status = 'error'; error = $w.Substring(0, [Math]::Min(200, $w.Length)) } | ConvertTo-Json -Compress }
}
"""


def consent() -> dict:
    """Windows' three location switches, read (never written) from the registry: "Allow", "Deny" or None (not set)."""
    out = {"pc": None, "account": None, "desktop_apps": None}
    if sys.platform != "win32":
        return out
    import winreg
    for key, root, sub in (("pc", winreg.HKEY_LOCAL_MACHINE, _KEY), ("account", winreg.HKEY_CURRENT_USER, _KEY),
                           ("desktop_apps", winreg.HKEY_CURRENT_USER, _KEY + r"\NonPackaged")):
        try:
            with winreg.OpenKey(root, sub) as k:
                out[key] = str(winreg.QueryValueEx(k, "Value")[0])
        except OSError:
            pass
    return out


def off_reason(c: dict) -> str | None:
    """Which Windows switch keeps location off, in words, or None when none does."""
    if (c.get("pc") or "").lower() == "deny":
        return "Windows' location services are switched off for this PC."
    if (c.get("account") or "").lower() == "deny":
        return "Location access is switched off for your Windows account."
    if (c.get("desktop_apps") or "").lower() == "deny":
        return "\"Let desktop apps access your location\" is switched off in Windows."
    return None


def _powershell() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return exe if os.path.exists(exe) else "powershell.exe"


def _ask_windows(timeout: float) -> dict:
    """One answer from Windows' location API, as the PowerShell side printed it. Replaced in tests."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    got = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS],
                         capture_output=True, text=True, timeout=timeout, creationflags=flags)
    line = next((ln for ln in reversed((got.stdout or "").splitlines()) if ln.strip().startswith("{")), "")
    return json.loads(line) if line else {"status": "error", "error": (got.stderr or "no answer").strip()[:200]}


def find(timeout: float = 25.0) -> dict:
    """Ask Windows where this PC is. Never raises; the answer says why when there is no position."""
    if sys.platform != "win32":
        return {"ok": False, "reason": "unavailable", "text": "Finding the location needs Windows' location service."}
    why = off_reason(consent())
    if why:
        return {"ok": False, "reason": "off", "text": f"{why} {SETTINGS_HOW}"}
    try:
        d = _ask_windows(timeout)
    except subprocess.TimeoutExpired:
        d = {"status": "timeout"}
    except (OSError, ValueError) as exc:
        d = {"status": "error", "error": str(exc)[:200]}
    st = d.get("status")
    if st == "ok":
        try:
            lat, lon = float(d["lat"]), float(d["lon"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "reason": "no fix", "text": "Windows answered without a position. Try again, or type "
                                                             "the values by hand."}
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return {"ok": False, "reason": "no fix", "text": "Windows gave a position that cannot be right. Type the "
                                                             "values by hand."}
        acc = d.get("accuracy")
        return {"ok": True, "lat": lat, "lon": lon, "accuracy_m": float(acc) if acc is not None else None,
                "source": str(d.get("source") or "")}
    if st == "denied":
        return {"ok": False, "reason": "denied", "text": "Windows did not let Marsad read the location (access "
                                                         f"{d.get('access') or 'denied'}). {SETTINGS_HOW}"}
    if st == "timeout":
        return {"ok": False, "reason": "timeout", "text": "Windows did not find a position in time. It can take "
                                                          "longer the first time; try again, or type the values by "
                                                          "hand."}
    return {"ok": False, "reason": "unavailable", "text": "Windows' location service did not answer"
                                                          + (f" ({d.get('error')})" if d.get("error") else "")
                                                          + ". Type the values by hand."}


def rounded(v: float, on: bool) -> float:
    """Two decimal places (about 1 km) when the privacy option is on, else as given."""
    return round(float(v), ROUND_DP) if on else float(v)


def accuracy_words(m: float | None) -> str:
    if m is None:
        return "accuracy not reported"
    return f"\u00b1 {m / 1000:.1f} km" if m >= 1000 else f"\u00b1 {m:.0f} m"
