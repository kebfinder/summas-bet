import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.result_evaluator import compute_performance, evaluate_prediction


def test_evaluate_prediction_market_coverage():
    assert evaluate_prediction("1X2", "Home Win", 2, 1) == "won"
    assert evaluate_prediction("BTTS", "No", 2, 0) == "won"
    assert evaluate_prediction("Over2.5", "Over", 2, 1) == "won"
    assert evaluate_prediction("Under2.5", "Under", 1, 1) == "won"
    assert evaluate_prediction("DoubleChance", "12", 1, 1) == "lost"
    assert evaluate_prediction("DrawNoBet", "Away", 1, 1) == "void"
    assert evaluate_prediction("HomeGoals1.5", "Over", 2, 0) == "won"


def test_compute_performance_settled_metrics():
    summary = compute_performance([
        {"status": "won"},
        {"status": "lost"},
        {"status": "void"},
        {"status": "pending"},
    ])
    assert summary["total"] == 4
    assert summary["settled_count"] == 2
    assert summary["win_rate"] == 25.0
    assert summary["win_rate_settled"] == 50.0
    assert summary["void_count"] == 1
    assert summary["pending_count"] == 1
