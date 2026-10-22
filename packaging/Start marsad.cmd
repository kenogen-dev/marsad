@echo off
rem Starts the marsad web app and keeps this window open if anything goes wrong.
rem Double-clicking marsad.exe does the same thing; this file exists so an error
rem message stays on screen instead of vanishing with the console.
cd /d "%~dp0"
"%~dp0marsad.exe" %*
if errorlevel 1 (
  echo.
  echo marsad stopped with an error. Read the lines above, then press any key.
  pause >nul
)
