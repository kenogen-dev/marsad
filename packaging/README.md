# packaging

- `marsad.spec`, `launcher.py` - the PyInstaller recipe. Build with
  `.venv\Scripts\pyinstaller.exe --noconfirm packaging\marsad.spec`; the result is `dist\marsad\`.
- `rtl_power_gainfix/`, `rtl_power_fftw/`: patches and build scripts for the two engines, each with a `BUILD.md`.
