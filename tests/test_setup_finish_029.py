"""Relay 029: every setup step says what finishes it, the same way on the line, the buttons and a disabled Next."""
import pytest



from marsad_desktop import setup_words as S  # noqa: E402

MEASURE = ("range", "warmup", "retune", "floor")
PATHS = ("adsb", "baseline")


def test_every_page_has_a_finish_line():
    for _, key in S.PAGES:
        w = S.finish_words(key, "not started")
        assert w["finish"].startswith("To finish this step: "), key
        assert w["needed"].startswith("Still needed: "), key


@pytest.mark.parametrize("key", MEASURE + PATHS)
def test_short_test_never_finishes(key):
    w = S.finish_words(key, "not started")
    assert "The short test is only a quick check and doesn't finish the step." in w["finish"]
    assert "doesn't finish the step" in w["needed"]


def test_only_range_offers_the_estimate():
    assert "or keep the estimate" in S.finish_words("range", "not started")["finish"]
    for key in ("warmup", "retune", "floor") + PATHS:
        assert "estimate" not in S.finish_words(key, "not started")["finish"]


def test_exact_range_wording_from_the_relay():
    assert S.finish_words("range", "not started")["finish"] == (
        "To finish this step: run the full measurement, or keep the estimate. The short test is only a quick check "
        "and doesn't finish the step.")


def test_after_a_short_test_passes():
    w = S.finish_words("warmup", "not started", {"short": True, "outcome": "done"})
    assert w["needed"] == "Short test OK. Now run the full measurement to finish this step."
    w = S.finish_words("adsb", "not started", {"short": True, "outcome": "done"})
    assert w["needed"] == "Short test OK. Now run the full check to finish this step."


def test_after_a_full_run_needs_attention():
    w = S.finish_words("range", "needs attention", {"short": False, "outcome": "attention"})
    assert w["needed"] == "This step isn't finished. Run it again, or keep the estimate to move on."
    w = S.finish_words("floor", "needs attention", {"short": False, "outcome": "failed"})
    assert w["needed"].startswith("This step isn't finished.") and "estimate" not in w["needed"]


def test_button_labels_say_what_they_do():
    assert S.BTN_SHORT == "Run short test (check only)"
    assert S.BTN_FULL["measure"] == "Run full measurement (finishes the step)"
    assert S.BTN_ESTIMATE == "Keep the estimate (finishes the step)"
    assert S.BTN_FULL["paths"].endswith("(finishes the step)")
