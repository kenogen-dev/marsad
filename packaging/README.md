# packaging

- `marsad.spec`, `launcher.py` - the PyInstaller recipe. Build with
  `.venv\Scripts\pyinstaller.exe --noconfirm packaging\marsad.spec`; the result is `dist\marsad\`.
- `port_builder.py`, `port_index.py` - the scripts that generate
  `marsad/web/builder.html` and `marsad/web/index.html`
  from `tools/scan_builder.html` and `tools/live_view.py`. Each one is a
  list of exact string replacements that assert they matched, so if a source file is
  edited later they can be re-run (`py packaging\port_index.py C:\SDR`) and will fail
  loudly rather than produce a half-converted file.
- `port_analyze.py` was the one-shot that produced `marsad/analyze.py` from the original
  `tools/survey.py` with the function bodies byte for byte. `tools/survey.py` is a wrapper now,
  so the script no longer has a source to read; it is kept as the record of how the port was done.
