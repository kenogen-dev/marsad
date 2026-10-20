# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the single-folder Windows build.

    .venv\\Scripts\\pyinstaller.exe --noconfirm packaging\\sdr.spec

Produces dist\\sdr\\sdr.exe plus its support files. One folder, not one file:
a one-file build unpacks 300 MB to a temp folder on every launch, which is slow
and sets off some antivirus products; a folder starts instantly.

scipy is included on purpose: survey.py's peak finder uses scipy.signal when it
is present and a slower pure-Python fallback when it is not, and the two do not
find exactly the same peaks. The frozen build must give the same peaks.csv as
the venv, so it ships the same libraries.
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
PKG = ROOT / "sdrstation"

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(PKG / "web" / "index.html"), "sdrstation/web"),
        (str(PKG / "web" / "builder.html"), "sdrstation/web"),
        (str(PKG / "web" / "ROADMAP.md"), "sdrstation/web"),
        (str(PKG / "station.default.json"), "sdrstation"),
        (str(ROOT / "README.md"), "."),
    ],
    hiddenimports=[
        "matplotlib.backends.backend_agg",
        "scipy.signal",
        "pandas._libs.tslibs.base",
        "sqlite3",
        "winsound",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # GUI toolkits and notebooks that matplotlib/pandas can pull in but the
        # analyser never uses (it draws to PNG with the Agg backend only).
        "tkinter", "_tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
        "IPython", "jupyter", "notebook", "pytest", "PyInstaller",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sdr",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="sdr",
)
