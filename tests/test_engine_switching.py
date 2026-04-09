from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class _DummyDB:
    pass


def test_get_top_predictions_legacy_default(monkeypatch):
    monkeypatch.setattr(main, "ENABLE_CALIBRATED_DECISION_ENGINE", False)
    monkeypatch.setattr(main, "CALIBRATED_DECISION_ENGINE_MODE", "legacy")
    monkeypatch.setattr(main, "_top_predictions", lambda db, limit, min_edge, match_date: [("p", {"rank_score": 88})])
    monkeypatch.setattr(main, "_serialize_top_prediction", lambda prediction, details: {"fixture_id": 1, "confidence_score": 88})
    monkeypatch.setattr(main, "_available_date_labels", lambda db, predicted_only=False: [])

    response = main.get_top_predictions(limit=20, min_edge=0.025, match_date=None, db=_DummyDB())

    assert response["count"] == 1
    assert response["picks"][0]["fixture_id"] == 1
    assert "engine_mode=legacy" in (response.get("note") or "")


def test_get_top_predictions_calibrated_enabled(monkeypatch):
    monkeypatch.setattr(main, "ENABLE_CALIBRATED_DECISION_ENGINE", True)
    monkeypatch.setattr(main, "CALIBRATED_DECISION_ENGINE_MODE", "legacy")
    monkeypatch.setattr(
        main,
        "_calibrated_engine_picks",
        lambda db, match_date, limit=20: {
            "picks": [{"fixture_id": 7, "confidence_score": 79.0}],
            "engine_path": "calibrated_engine",
        },
    )
    monkeypatch.setattr(main, "_available_date_labels", lambda db, predicted_only=False: [])

    response = main.get_top_predictions(limit=20, min_edge=0.025, match_date=None, db=_DummyDB())

    assert response["count"] == 1
    assert response["picks"][0]["fixture_id"] == 7
    assert "engine_mode=active" in (response.get("note") or "")
    assert "engine_path=calibrated_engine" in (response.get("note") or "")


def test_get_top_predictions_calibrated_fallback(monkeypatch):
    monkeypatch.setattr(main, "ENABLE_CALIBRATED_DECISION_ENGINE", True)
    monkeypatch.setattr(main, "CALIBRATED_DECISION_ENGINE_MODE", "legacy")
    monkeypatch.setattr(
        main,
        "_calibrated_engine_picks",
        lambda db, match_date, limit=20: {
            "picks": [],
            "engine_path": "calibrated_engine_fallback_to_legacy",
            "fallback_reason": "insufficient_calibrated_candidates",
        },
    )
    monkeypatch.setattr(main, "_top_predictions", lambda db, limit, min_edge, match_date: [("p", {"rank_score": 77})])
    monkeypatch.setattr(main, "_serialize_top_prediction", lambda prediction, details: {"fixture_id": 3, "confidence_score": 77})
    monkeypatch.setattr(main, "_available_date_labels", lambda db, predicted_only=False: [])

    response = main.get_top_predictions(limit=20, min_edge=0.025, match_date=None, db=_DummyDB())

    assert response["count"] == 1
    assert response["picks"][0]["fixture_id"] == 3
    assert "engine_mode=active" in (response.get("note") or "")
    assert "fallback_reason=insufficient_calibrated_candidates" in (response.get("note") or "")


def test_get_top_predictions_shadow_mode(monkeypatch):
    monkeypatch.setattr(main, "ENABLE_CALIBRATED_DECISION_ENGINE", False)
    monkeypatch.setattr(main, "CALIBRATED_DECISION_ENGINE_MODE", "shadow")
    monkeypatch.setattr(main, "_top_predictions", lambda db, limit, min_edge, match_date: [("p", {"rank_score": 82})])
    monkeypatch.setattr(main, "_serialize_top_prediction", lambda prediction, details: {"fixture_id": 11, "confidence_score": 82})
    monkeypatch.setattr(
        main,
        "_calibrated_engine_picks",
        lambda db, match_date, limit=20: {"picks": [{"fixture_id": 7}], "engine_path": "calibrated_engine"},
    )
    monkeypatch.setattr(main, "_available_date_labels", lambda db, predicted_only=False: [])
    monkeypatch.setattr(main, "ENABLE_ENGINE_SHADOW_COMPARE_ARTIFACT", False)

    response = main.get_top_predictions(limit=20, min_edge=0.025, match_date=None, db=_DummyDB())

    assert response["count"] == 1
    assert response["picks"][0]["fixture_id"] == 11
    assert "engine_mode=shadow" in (response.get("note") or "")
    assert "calibrated_count=1" in (response.get("note") or "")
