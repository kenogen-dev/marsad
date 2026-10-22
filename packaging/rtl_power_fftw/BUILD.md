# rtl_power_fftw for Windows: how `rtl-sdr\rtl_power_fftw.exe` was built

Built 2026-10-21 on the station PC. There is no official Windows binary of
rtl_power_fftw, so this is a native MSVC build from source. **Nothing was
installed**: the Visual Studio 2022 Build Tools already on the PC supplied the
compiler, CMake and Ninja; the three libraries were fetched as source and built
into a work folder; the only file that left that folder is the executable.

`build.cmd` in this folder repeats every step below from scratch (about two
minutes, needs the internet for the four downloads). `CMakeLists.txt` here
replaces upstream's, which relies on pkg-config and GCC flags that a Build
Tools install does not have; the source itself is unchanged apart from the
patch below.

## What went in

| Part | Version | Where from | Pinned to |
|---|---|---|---|
| rtl-power-fftw | 1.0-beta2 (`--version`) | github.com/AD-Vega/rtl-power-fftw | commit `cee9a22207ea995bd12adbc6bcfbec92521548b1` (2020-06-01, the tip) |
| FFTW | 3.3.10 | www.fftw.org/fftw-3.3.10.tar.gz | sha256 `56c932549852cddcfafdab3820b0200c7742675be92179e59e6215b340e26467` |
| TCLAP (header-only) | 1.4.0-rc2+73 | git.code.sf.net/p/tclap/code | commit `61b0ad5d8fa0449eb06682436dac93fe0385af10` |
| rtl-sdr header | RTL-SDR Blog V1.4.0 | github.com/rtlsdrblog/rtl-sdr-blog | tag `V1.4.0` = commit `aed0ea19` |
| librtlsdr itself | the station's existing `rtl-sdr\rtlsdr.dll`, version string `v0.6.0-131-gaed0` | RTL-SDR Blog release already in use by rtl_power | (not rebuilt) |

Toolchain: Visual Studio Build Tools 2022 17.14.13, MSVC 14.44.35207 (x64),
CMake 3.31.6-msvc6, Ninja 1.12.1, Windows SDK 10.0.26100.

## How

1. **FFTW** was built with its own CMake file as a static single-precision
   library (`ENABLE_FLOAT`, SSE2 and AVX2 codelets, static MSVC runtime). The
   first attempt used the dynamic runtime and the final link failed on
   `__imp_clock` / `_aligned_malloc`; the static runtime fixed that and means
   the exe needs no VC redistributable.
2. **librtlsdr** was not built. `dumpbin /exports` on the DLL rtl_power already
   uses gave its 38 exported functions, those went into a `.def` file, and
   `lib /def` made an import library from it. The header comes from the
   RTL-SDR Blog source at the tag matching the DLL's version string. The
   program only calls thirteen plain functions (open, close, set/get sample
   rate, centre frequency, gains, ppm, read_sync, reset_buffer), all of which
   have been in librtlsdr since 2013, so the header version is not critical,
   but it is pinned anyway. Using the same DLL as rtl_power means both engines
   drive the dongle through the same driver code.
3. **TCLAP** is header-only; nothing to build.
4. **rtl_power_fftw**: the six `.cxx` files compiled with `/O2 /EHsc /W3`,
   C++17, `HAVE_LONG_LONG` defined (upstream's CMake probes for it; TCLAP wants
   it), `_USE_MATH_DEFINES` and `NOMINMAX`, linked against `fftw3f.lib` and the
   import library, static runtime. Warnings are all C4244 narrowing
   conversions in upstream code; none were silenced or fixed.

Result: `rtl_power_fftw.exe`, 3,125,760 bytes, depends on `rtlsdr.dll` and
`KERNEL32.dll` only. `--version` prints `rtl_power_fftw  version: 1.0-beta2`.
sha256 `1207476e0b48ed878fd202ab999957859c4bbc571a044d9f29a8ce2d74a418b5`.

## The one patch: `windows-ctrl-break.patch`

Upstream already has a `#ifdef _WIN32` branch in `interrupts.cxx` that uses
`signal(SIGINT, ...)` instead of `sigaction`. It only catches Ctrl+C. marsad
stops a recording it supervises by sending **Ctrl+Break** to the child's
process group (that is the only console event Windows lets a parent aim at
one child; a child started in its own process group does not receive Ctrl+C
at all), which the MSVC runtime delivers as `SIGBREAK`. Unpatched, the program
would simply die on it and lose the pass in progress. The patch installs the
same handler for `SIGBREAK`, so marsad's Stop button and Ctrl-C path get the
same graceful finish (finish the pass, write it, exit) that Ctrl+C gives. It
also honours the `install` flag on Windows, which upstream ignored there.
Four lines; `git apply` it onto the pinned commit.

## Known limits of the program, from its source (not from this build)

- No crop, no FIR, no offset tuning (`rtlsdr_set_offset_tuning` is never
  called), no direct sampling, no bias-tee control. The XTR's bias-tee is
  always on in hardware, so that last one does not matter here.
- Frequencies are 64-bit, so rtl_power's 2^31 Hz ceiling does not exist.
- The window function is not built in: `-w FILE` takes N values you supply.
- `-B FILE` subtracts N values **by bin index**, the same N values for every
  hop, so baseline subtraction is only meaningful for a single-hop run.
- The centre bin of every hop is replaced by the mean of its two neighbours.
