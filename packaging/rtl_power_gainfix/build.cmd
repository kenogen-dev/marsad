@echo off
rem Rebuilds rtl-sdr\rtl_power_gainfix.exe from scratch (see BUILD.md).
rem   build.cmd [WORKDIR]      default WORKDIR is %TEMP%\rp-gainfix-build
rem Needs the Visual Studio 2022 Build Tools (cl, lib, dumpbin), git, and the
rem station's rtl-sdr\rtlsdr.dll. Nothing is installed.
setlocal
set HERE=%~dp0
set WORK=%~1
if "%WORK%"=="" set WORK=%TEMP%\rp-gainfix-build
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
mkdir "%WORK%\stub" 2>nul
cd /d "%WORK%" || exit /b 1

rem rtl_power.c includes these two but uses nothing from them
type nul > stub\pthread.h
type nul > stub\libusb.h

if not exist rtl-sdr-blog git clone -q https://github.com/rtlsdrblog/rtl-sdr-blog.git || exit /b 1
git -C rtl-sdr-blog checkout -q -f V1.4.0 || exit /b 1
git -C rtl-sdr-blog apply "%HERE%r82xx-gain-after-tune.patch" || exit /b 1
git -C rtl-sdr-blog apply "%HERE%win64-accumulators.patch" || exit /b 1

rem import library for the station's own rtlsdr.dll (not rebuilt)
dumpbin /nologo /exports "%HERE%..\..\rtl-sdr\rtlsdr.dll" > exports.txt || exit /b 1
powershell -NoProfile -Command "$n = Get-Content exports.txt | Where-Object { $_ -match '^\s+\d+\s+[0-9A-F]+\s+[0-9A-F]{8}\s+(\S+)' } | ForEach-Object { $Matches[1] }; @('LIBRARY rtlsdr.dll','EXPORTS') + $n | Set-Content -Encoding ascii rtlsdr.def" || exit /b 1
lib /nologo /def:rtlsdr.def /machine:x64 /out:rtlsdr.lib || exit /b 1

set SRC=rtl-sdr-blog\src
cl /nologo /O2 /W3 /MT /D_CRT_SECURE_NO_WARNINGS /I stub /I rtl-sdr-blog\include %SRC%\rtl_power.c %SRC%\convenience\convenience.c %SRC%\getopt\getopt.c rtlsdr.lib /Fe:rtl_power_gainfix.exe || exit /b 1
echo Built %WORK%\rtl_power_gainfix.exe
