from pathlib import Path
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.fixture_enrichment import TeamRecentMetrics, enrich_fixture, summarize_enrichment


def test_summarize_enrichment_counts_and_missing_fields():
    enriched = {
        1: {"enrichment_completeness": "full", "missing_fields": []},
        2: {"enrichment_completeness": "partial", "missing_fields": ["over_2_5_rate"]},
        3: {
            "enrichment_completeness": "missing",
            "missing_fields": ["home_avg_goals_scored_recent", "over_2_5_rate"],
        },
    }

    summary = summarize_enrichment(enriched)

    assert summary["total_fixtures_inspected"] == 3
    assert summary["fully_enriched_count"] == 1
    assert summary["partially_enriched_count"] == 1
    assert summary["missing_enrichment_count"] == 1
    first_missing = summary["common_missing_fields"][0]
    assert first_missing["field"] == "over_2_5_rate"
    assert first_missing["count"] == 2


def test_enrich_fixture_marks_no_history_as_missing(monkeypatch):
    fixture = SimpleNamespace(
        id=44,
        home_id=1,
        away_id=2,
        date=datetime(2026, 4, 9, 18, 0),
        league="Test League",
        home=SimpleNamespace(name="Home"),
        away=SimpleNamespace(name="Away"),
    )

    monkeypatch.setattr(
        "services.fixture_enrichment._team_metrics",
        lambda db, team_id, before_dt, sample_size: TeamRecentMetrics(
            sample_size=0,
            form_sequence="",
            points_per_match=None,
            avg_scored=None,
            avg_conceded=None,
            over_1_5_rate=None,
            over_2_5_rate=None,
            btts_rate=None,
        ),
    )
    monkeypatch.setattr("services.fixture_enrichment._h2h_summary", lambda *args, **kwargs: None)

    enriched = enrich_fixture(db=object(), fixture=fixture, sample_size=5)
    assert enriched["enrichment_completeness"] == "missing"
    assert "home_recent_form" in enriched["missing_fields"]
    assert "away_recent_form" in enriched["missing_fields"]
