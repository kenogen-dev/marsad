"""marsad (المرصد, al-marsad: the observatory) - record, analyse and compare rtl_power spectrum surveys.

One command, `marsad` (alias `sdr`), wraps everything the station's tools do. Universal facts
about rtl_power live in `rtlpower.py`; everything particular to one station
(receivers, spur families, presets, measured retune time) lives in the
station.json that `config.py` loads.
"""
__version__ = "0.1.0"
