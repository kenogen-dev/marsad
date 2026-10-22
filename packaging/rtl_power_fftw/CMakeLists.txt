# Windows (MSVC) build of rtl_power_fftw for the marsad station.
#
# Upstream's CMakeLists.txt uses pkg-config and GCC flags, neither of which
# exists on a plain Visual Studio Build Tools install, so this file replaces it.
# The source is unchanged. See BUILD.md beside this file for the full recipe.
#
# Configure with:
#   cmake -S <this dir> -B <build dir> -G Ninja -DCMAKE_BUILD_TYPE=Release
#         -DRPF_SOURCE=<rtl-power-fftw checkout>
#         -DTCLAP_INCLUDE=<tclap>/include
#         -DRTLSDR_INCLUDE=<rtl-sdr-blog>/include
#         -DRTLSDR_IMPLIB=<path to rtlsdr.lib made from rtlsdr.dll>
#         -DFFTW3F_ROOT=<fftw install prefix with include/ and lib/>

cmake_minimum_required(VERSION 3.20)
project(rtl_power_fftw_win CXX)

set(RPF_SOURCE     "" CACHE PATH   "rtl-power-fftw source checkout")
set(TCLAP_INCLUDE  "" CACHE PATH   "TCLAP include dir (header-only)")
set(RTLSDR_INCLUDE "" CACHE PATH   "librtlsdr include dir (rtl-sdr.h)")
set(RTLSDR_IMPLIB  "" CACHE FILEPATH "import library for rtlsdr.dll")
set(FFTW3F_ROOT    "" CACHE PATH   "FFTW single-precision install prefix")

foreach(v RPF_SOURCE TCLAP_INCLUDE RTLSDR_INCLUDE RTLSDR_IMPLIB FFTW3F_ROOT)
  if(NOT ${v})
    message(FATAL_ERROR "${v} must be set (see BUILD.md)")
  endif()
endforeach()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(rtl_power_fftw
  ${RPF_SOURCE}/src/acquisition.cxx
  ${RPF_SOURCE}/src/datastore.cxx
  ${RPF_SOURCE}/src/device.cxx
  ${RPF_SOURCE}/src/interrupts.cxx
  ${RPF_SOURCE}/src/params.cxx
  ${RPF_SOURCE}/src/rtl_power_fftw.cxx
)

target_include_directories(rtl_power_fftw PRIVATE
  ${TCLAP_INCLUDE}
  ${RTLSDR_INCLUDE}
  ${FFTW3F_ROOT}/include
)

# TCLAP checks for HAVE_LONG_LONG (upstream CMake does the same probe).
# rtlsdr_static is NOT defined, so rtl-sdr_export.h declares dllimport.
target_compile_definitions(rtl_power_fftw PRIVATE
  HAVE_LONG_LONG
  _USE_MATH_DEFINES
  NOMINMAX
)
target_compile_options(rtl_power_fftw PRIVATE /W3 /O2 /EHsc)

target_link_libraries(rtl_power_fftw PRIVATE
  ${FFTW3F_ROOT}/lib/fftw3f.lib
  ${RTLSDR_IMPLIB}
)

# Statically link the MSVC runtime so the exe needs no extra DLLs beside rtlsdr.dll.
set_property(TARGET rtl_power_fftw PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded")
