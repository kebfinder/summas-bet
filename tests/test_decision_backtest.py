from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.decision_backtest import resolve_market_outcome, _summarize
from services.preview_calibration import CalibrationConfig


def test_resolve_market_outcome_supported_markets():
    assert resolve_market_outcome("Over 1.5", 1, 1) == "hit"
    assert resolve_market_outcome("Over 2.5", 1, 1) == "miss"
    assert resolve_market_outcome("BTTS", 2, 0) == "miss"
    assert resolve_market_outcome("Under 3.5", 2, 1) == "hit"
    assert resolve_market_outcome("Home or Draw (1X)", 0, 0) == "hit"
    assert resolve_market_outcome("Away or Draw (X2)", 2, 0) == "miss"


def test_summarize_returns_hit_rates():
    rows = [
        {
            "selected_market": "Over 1.5",
            "confidence_band": "high",
            "enrichment_completeness": "full",
            "actual_outcome": "hit",
            "confidence_score": 0.82,
        },
        {
            "selected_market": "Over 1.5",
            "confidence_band": "medium",
            "enrichment_completeness": "partial",
            "actual_outcome": "miss",
            "confidence_score": 0.61,
        },
    ]

    summary = _summarize(rows, total_fixtures_evaluated=3, skipped_fixtures=1)

    assert summary["total_fixtures_evaluated"] == 3
    assert summary["total_predictions_generated"] == 2
    assert summary["fixtures_skipped"] == 1
    assert summary["hit_rate_overall"] == 0.5
    assert summary["hit_rate_by_market"]["Over 1.5"]["hit_rate"] == 0.5


def test_summarize_empty_rows_safe():
    summary = _summarize([], total_fixtures_evaluated=0, skipped_fixtures=0)
    assert summary["total_predictions_generated"] == 0
    assert summary["hit_rate_overall"] == 0.0


def test_calibration_config_defaults_available():
    config = CalibrationConfig(enabled=True)
    assert config.enabled is True
    assert config.min_market_samples > 0
