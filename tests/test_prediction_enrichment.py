import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from services.prediction_enrichment import probability_pct, confidence_stars, value_status, risk_tier


def test_probability_pct():
    assert probability_pct(0.65) == 65.0


def test_confidence_stars():
    assert confidence_stars(0.8) == 5
    assert confidence_stars(0.46) == 2


def test_value_status():
    assert value_status(0.5, None)[0] == "Odds unavailable"
    assert value_status(0.5, 2.2)[0] in {"Value", "Fair", "Poor value"}


def test_risk_tier():
    assert risk_tier(0.7, "1X2") == "Safe"
    assert risk_tier(0.45, "1X2") == "Bold"
