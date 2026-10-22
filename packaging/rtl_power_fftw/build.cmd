@echo off
rem Build rtl_power_fftw for Windows with the Visual Studio 2022 Build Tools.
rem
rem   build.cmd [WORKDIR]      default WORKDIR is %TEMP%\rpf-build
rem
rem Needs: VS Build Tools 2022 with "Desktop development with C++" (cl, link,
rem lib, dumpbin, CMake and Ninja all come with it), git, curl and tar (both in
rem Windows 10+). Nothing is installed; everything lands under WORKDIR.
rem The import library is made from rtl-sdr\rtlsdr.dll in this repo, so the
rem result uses exactly the driver rtl_power uses. See BUILD.md.
setlocal
set WORK=%~1
if "%WORK%"=="" set WORK=%TEMP%\rpf-build
set HERE=%~dp0
set DLL=%HERE%..\..\rtl-sdr\rtlsdr.dll
if not exist "%DLL%" (echo rtlsdr.dll not found at %DLL% & exit /b 1)
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
mkdir "%WORK%" 2>nul
cd /d "%WORK%" || exit /b 1

rem --- sources, pinned ------------------------------------------------------
if not exist rtl-power-fftw git clone -q https://github.com/AD-Vega/rtl-power-fftw.git || exit /b 1
git -C rtl-power-fftw checkout -q cee9a22207ea995bd12adbc6bcfbec92521548b1 || exit /b 1
git -C rtl-power-fftw apply --check "%HERE%windows-ctrl-break.patch" >nul 2>&1 && git -C rtl-power-fftw apply "%HERE%windows-ctrl-break.patch"
if not exist tclap git clone -q https://git.code.sf.net/p/tclap/code tclap || exit /b 1
git -C tclap checkout -q 61b0ad5d8fa0449eb06682436dac93fe0385af10 || exit /b 1
if not exist rtl-sdr-blog git clone -q https://github.com/rtlsdrblog/rtl-sdr-blog.git || exit /b 1
git -C rtl-sdr-blog checkout -q V1.4.0 || exit /b 1
if not exist fftw-3.3.10.tar.gz curl -sSL -o fftw-3.3.10.tar.gz https://www.fftw.org/fftw-3.3.10.tar.gz || exit /b 1
certutil -hashfile fftw-3.3.10.tar.gz SHA256 | findstr /i 56c932549852cddcfafdab3820b0200c7742675be92179e59e6215b340e26467 >nul || (echo FFTW tarball checksum mismatch & exit /b 1)
if not exist fftw-3.3.10 tar xzf fftw-3.3.10.tar.gz || exit /b 1

rem --- FFTW, single precision, static, static CRT ---------------------------
cmake -S fftw-3.3.10 -B build-fftwf -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DENABLE_FLOAT=ON -DENABLE_SSE2=ON -DENABLE_AVX2=ON -DBUILD_TESTS=OFF -DCMAKE_POLICY_DEFAULT_CMP0091=NEW -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded -DCMAKE_INSTALL_PREFIX=%WORK%\fftw-install || exit /b 1
cmake --build build-fftwf --parallel || exit /b 1
cmake --install build-fftwf || exit /b 1

rem --- import library for the rtlsdr.dll rtl_power already uses -------------
mkdir rtlsdr-lib 2>nul
dumpbin /exports "%DLL%" > rtlsdr-lib\exports.txt || exit /b 1
powershell -NoProfile -Command "$n = Get-Content '%WORK%\rtlsdr-lib\exports.txt' | Where-Object { $_ -match '^\s+\d+\s+[0-9A-F]+\s+[0-9A-F]{8}\s+(\S+)' } | ForEach-Object { $Matches[1] }; @('LIBRARY rtlsdr.dll','EXPORTS') + $n | Set-Content -Encoding ascii '%WORK%\rtlsdr-lib\rtlsdr.def'" || exit /b 1
lib /nologo /def:rtlsdr-lib\rtlsdr.def /machine:x64 /out:rtlsdr-lib\rtlsdr.lib || exit /b 1

rem --- rtl_power_fftw itself ------------------------------------------------
cmake -S "%HERE%." -B build-rpf -G Ninja -DCMAKE_BUILD_TYPE=Release -DRPF_SOURCE=%WORK%\rtl-power-fftw -DTCLAP_INCLUDE=%WORK%\tclap\include -DRTLSDR_INCLUDE=%WORK%\rtl-sdr-blog\include -DRTLSDR_IMPLIB=%WORK%\rtlsdr-lib\rtlsdr.lib -DFFTW3F_ROOT=%WORK%\fftw-install || exit /b 1
cmake --build build-rpf --parallel || exit /b 1
echo.
echo Built %WORK%\build-rpf\rtl_power_fftw.exe
echo Copy it to rtl-sdr\ beside rtl_power.exe (it needs only rtlsdr.dll from there).
