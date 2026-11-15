"""The setup guide's pages and the words that say what finishes each one (relay 029).

Kept apart from setup_screen.py so it needs no Qt: the public tests read it directly.
"""
from __future__ import annotations

PASSED = ("done", "estimate kept")      # relay 024: an estimate lets the guide go on; it never unlocks scanning

# The guide's pages, in order: (checklist step, page key). Measure and Path checks have one page per part.
PAGES = [("add", "add"), ("measure", "range"), ("measure", "warmup"), ("measure", "retune"), ("measure", "floor"),
         ("draw", "draw"), ("confirm", "confirm"), ("paths", "adsb"), ("paths", "baseline")]
TOUCHED = ("done", "needs re-checking", "in progress", "needs attention", "estimate kept", "to redo")

# Relay 029: what finishes each step, said the same way above the buttons, on the buttons, beside a disabled Next
# and in its tooltip. Measurement and path-check pages share one pattern; "range" alone can keep an estimate.
BTN_SHORT = "Run short test (check only)"
BTN_FULL = {"measure": "Run full measurement (finishes the step)", "paths": "Run full check (finishes the step)"}
BTN_ESTIMATE = "Keep the estimate (finishes the step)"
_FULL_WORD = {"measure": "the full measurement", "paths": "the full check"}


def page_group(key: str) -> str:
    return next((st for st, k in PAGES if k == key), key)


def finish_words(key: str, state: str, run: dict | None = None) -> dict:
    """The words that say what finishes a page (relay 029), as {"finish": line above the buttons, "needed": the
    "Still needed" line and disabled Next's tooltip}. Pure, so the tests read it without a window."""
    run = run or {}
    group = page_group(key)
    if key == "add":
        return {"finish": "To finish this step: add the radio (Marsad lists the RTL-SDRs Windows sees).",
                "needed": "Still needed: add the radio."}
    if key == "draw":
        return {"finish": "To finish this step: draw the rig on the Chain screen and press Use for runs. It finishes "
                          "when the rig used for runs has a path to this radio.",
                "needed": "Still needed: draw the rig on the Chain screen and press Use for runs."}
    if key == "confirm":
        return {"finish": "To finish this step: check each part and connection against the drawing, then press "
                          "It's built as drawn.",
                "needed": "Still needed: check the rig against the drawing, then press It's built as drawn."}
    full = _FULL_WORD.get(group, "the full measurement")
    estimate = key == "range"
    finish = (f"To finish this step: run {full}" + (", or keep the estimate" if estimate else "")
              + ". The short test is only a quick check and doesn't finish the step.")
    if state == "needs attention" and not run.get("short"):
        needed = ("This step isn't finished. Run it again, or keep the estimate to move on." if estimate else
                  f"This step isn't finished. Run {full} again to finish it.")
    elif run.get("short") and run.get("outcome") == "done" and state not in PASSED:
        needed = f"Short test OK. Now run {full} to finish this step."
    elif run.get("short") and run.get("outcome") in ("attention", "failed") and state not in PASSED:
        needed = (f"The short test did not pass: see what it says above. The step still needs {full}"
                  + (" (or keep the estimate)." if estimate else "."))
    else:
        needed = (f"Still needed: run {full}" + (", or keep the estimate" if estimate else "")
                  + ". The short test is only a quick check and doesn't finish the step.")
    return {"finish": finish, "needed": needed}
