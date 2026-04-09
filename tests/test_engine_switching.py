from pathlib import Path
from datetime import datetime
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
        lambda db, match_date, limit=20, min_edge=0.025: {
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
        lambda db, match_date, limit=20, min_edge=0.025: {
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
        lambda db, match_date, limit=20, min_edge=0.025: {"picks": [{"fixture_id": 7}], "engine_path": "calibrated_engine"},
    )
    monkeypatch.setattr(main, "_available_date_labels", lambda db, predicted_only=False: [])
    monkeypatch.setattr(main, "ENABLE_ENGINE_SHADOW_COMPARE_ARTIFACT", False)

    response = main.get_top_predictions(limit=20, min_edge=0.025, match_date=None, db=_DummyDB())

    assert response["count"] == 1
    assert response["picks"][0]["fixture_id"] == 11
    assert "engine_mode=shadow" in (response.get("note") or "")
    assert "calibrated_count=1" in (response.get("note") or "")


def test_get_top_predictions_forwards_min_edge_to_calibrated(monkeypatch):
    monkeypatch.setattr(main, "ENABLE_CALIBRATED_DECISION_ENGINE", True)
    monkeypatch.setattr(main, "CALIBRATED_DECISION_ENGINE_MODE", "legacy")
    captured = {}

    def _fake_calibrated(db, match_date, limit=20, min_edge=0.0):
        captured["min_edge"] = min_edge
        return {"picks": [{"fixture_id": 5, "confidence_score": 75.0}], "engine_path": "calibrated_engine"}

    monkeypatch.setattr(main, "_calibrated_engine_picks", _fake_calibrated)
    monkeypatch.setattr(main, "_available_date_labels", lambda db, predicted_only=False: [])

    response = main.get_top_predictions(limit=20, min_edge=0.07, match_date=None, db=_DummyDB())

    assert response["count"] == 1
    assert captured["min_edge"] == 0.07


def test_calibrated_engine_picks_does_not_trigger_backtest(monkeypatch):
    class _Query:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return self._rows

    class _DB:
        def __init__(self, rows):
            self._rows = rows

        def query(self, model):
            return _Query(self._rows)

    class _Team:
        def __init__(self, name):
            self.name = name

    class _Fixture:
        def __init__(self):
            self.id = 9
            self.home_id = 1
            self.away_id = 2
            self.home = _Team("A")
            self.away = _Team("B")
            self.date = datetime(2026, 4, 9, 18, 0)
            self.status = "scheduled"
            self.league = "Test"

    monkeypatch.setattr(
        main,
        "run_backtest_with_calibration_comparison",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("backtest should not run here")),
    )
    monkeypatch.setattr(main, "_cached_market_evidence", lambda db, resolved_date: {"Over 2.5": {"samples": 12, "hits": 6, "hit_rate": 0.5}})
    monkeypatch.setattr(main, "enrich_fixtures", lambda db, fixtures: {9: {"enrichment_completeness": "full"}})
    monkeypatch.setattr(main, "preview_for_enriched_fixture", lambda enrichment: {"candidate_markets": [{"candidate_market": "Over 2.5", "confidence_score": 0.66, "confidence_band": "medium", "explanation": "x"}]})
    monkeypatch.setattr(
        main,
        "select_best_candidate_with_calibration",
        lambda fixture_preview, enrichment_completeness, config, market_evidence: fixture_preview["candidate_markets"][0],
    )

    out = main._calibrated_engine_picks(_DB([_Fixture()]), match_date=datetime(2026, 4, 9).date(), limit=20, min_edge=0.025)
    assert out["picks"]


def test_calibrated_engine_picks_applies_min_edge(monkeypatch):
    class _Query:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return self._rows

    class _DB:
        def __init__(self, rows):
            self._rows = rows

        def query(self, model):
            return _Query(self._rows)

    class _Team:
        def __init__(self, name):
            self.name = name

    class _Fixture:
        def __init__(self):
            self.id = 10
            self.home_id = 1
            self.away_id = 2
            self.home = _Team("A")
            self.away = _Team("B")
            self.date = datetime(2026, 4, 9, 18, 0)
            self.status = "scheduled"
            self.league = "Test"

    monkeypatch.setattr(main, "_cached_market_evidence", lambda db, resolved_date: {})
    monkeypatch.setattr(main, "enrich_fixtures", lambda db, fixtures: {10: {"enrichment_completeness": "full"}})
    monkeypatch.setattr(main, "preview_for_enriched_fixture", lambda enrichment: {"candidate_markets": [{"candidate_market": "Over 2.5", "confidence_score": 0.51, "confidence_band": "low", "explanation": "thin"}]})
    monkeypatch.setattr(
        main,
        "select_best_candidate_with_calibration",
        lambda fixture_preview, enrichment_completeness, config, market_evidence: fixture_preview["candidate_markets"][0],
    )

    out = main._calibrated_engine_picks(_DB([_Fixture()]), match_date=datetime(2026, 4, 9).date(), limit=20, min_edge=0.02)
    assert out["picks"] == []
    assert out["fallback_reason"] == "insufficient_calibrated_candidates_or_edge"
