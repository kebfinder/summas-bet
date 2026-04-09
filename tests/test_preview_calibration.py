from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.preview_calibration import CalibrationConfig, calibrate_candidate, derive_market_evidence


def test_derive_market_evidence_counts_hits():
    evidence = derive_market_evidence(
        [
            {"selected_market": "Over 1.5", "actual_outcome": "hit"},
            {"selected_market": "Over 1.5", "actual_outcome": "miss"},
            {"selected_market": "BTTS", "actual_outcome": "hit"},
        ]
    )
    assert evidence["Over 1.5"]["samples"] == 2
    assert evidence["Over 1.5"]["hit_rate"] == 0.5
    assert evidence["BTTS"]["hit_rate"] == 1.0


def test_calibrate_candidate_applies_penalties_and_updates_band():
    config = CalibrationConfig(
        enabled=True,
        min_market_samples=10,
        min_market_hit_rate=0.55,
        suppress_underperforming_markets=False,
        underperformance_penalty_scale=0.2,
        low_evidence_penalty=0.0,
        min_confidence_after_calibration=0.0,
    )
    candidate = {"candidate_market": "Over 2.5", "confidence_score": 0.72, "confidence_band": "high"}
    market_evidence = {"Over 2.5": {"samples": 20, "hit_rate": 0.45}}

    adjusted = calibrate_candidate(candidate, "partial", config, market_evidence)

    assert adjusted is not None
    assert adjusted["confidence_score"] < 0.72
    assert adjusted["confidence_band"] in {"medium", "low", "very_low"}
    assert "calibration" in adjusted


def test_calibrate_candidate_can_suppress_underperforming_market():
    config = CalibrationConfig(
        enabled=True,
        min_market_samples=10,
        min_market_hit_rate=0.55,
        suppress_underperforming_markets=True,
    )
    candidate = {"candidate_market": "Over 2.5", "confidence_score": 0.72}
    market_evidence = {"Over 2.5": {"samples": 25, "hit_rate": 0.40}}

    adjusted = calibrate_candidate(candidate, "full", config, market_evidence)
    assert adjusted is None
