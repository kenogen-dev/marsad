"""Marsad's desktop window (native Qt). A viewer and editor only: everything it
reads and saves goes through the background service (`marsad live`), so closing
the window can never affect a scan.

`rig.py` is pure Python and holds the chain model; the Qt screens import it.
"""
