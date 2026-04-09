from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.decision_preview import (
    build_decision_preview,
    build_ranked_daily_preview,
    preview_for_enriched_fixture,
    select_best_candidate_for_fixture,
)


def test_preview_for_enriched_fixture_generates_candidates():
    enriched = {
        "fixture_identity": {
            "competition": "EPL",
            "home_team": "Alpha FC",
            "away_team": "Beta FC",
            "kickoff": "2026-04-10T19:00:00",
        },
        "enrichment_completeness": "full",
        "missing_fields": [],
        "home_recent_form": {"points_per_match": 2.1},
        "away_recent_form": {"points_per_match": 1.0},
        "home_avg_goals_scored_recent": 1.8,
        "home_avg_goals_conceded_recent": 0.9,
        "away_avg_goals_scored_recent": 1.2,
        "away_avg_goals_conceded_recent": 1.6,
        "over_1_5_rate": 0.8,
        "over_2_5_rate": 0.64,
        "btts_rate": 0.58,
        "h2h_summary": {"avg_total_goals": 2.9},
    }

    preview = preview_for_enriched_fixture(enriched)

    assert preview["status"] == "ok"
    assert preview["candidate_markets"]
    markets = {item["candidate_market"] for item in preview["candidate_markets"]}
    assert "Over 1.5" in markets


def test_build_decision_preview_summary_counts():
    preview = build_decision_preview(
        {
            1: {
                "fixture_identity": {"competition": "EPL", "home_team": "A", "away_team": "B", "kickoff": "k"},
                "enrichment_completeness": "missing",
                "missing_fields": ["over_1_5_rate"],
            }
        }
    )

    assert preview["summary"]["fixtures_previewed"] == 1
    assert preview["summary"]["fixtures_skipped_insufficient_enrichment"] == 1


def test_ranked_daily_preview_builds_unlimited_ranked_rows():
    enriched = {
        1: {
            "fixture_identity": {"competition": "EPL", "home_team": "A", "away_team": "B", "kickoff": "k1"},
            "enrichment_completeness": "full",
            "missing_fields": [],
            "home_recent_form": {"points_per_match": 2.0},
            "away_recent_form": {"points_per_match": 1.0},
            "home_avg_goals_scored_recent": 1.7,
            "home_avg_goals_conceded_recent": 1.0,
            "away_avg_goals_scored_recent": 1.2,
            "away_avg_goals_conceded_recent": 1.5,
            "over_1_5_rate": 0.8,
            "over_2_5_rate": 0.62,
            "btts_rate": 0.55,
        },
        2: {
            "fixture_identity": {"competition": "EPL", "home_team": "C", "away_team": "D", "kickoff": "k2"},
            "enrichment_completeness": "partial",
            "missing_fields": ["over_2_5_rate"],
            "home_recent_form": {"points_per_match": 1.6},
            "away_recent_form": {"points_per_match": 1.2},
            "home_avg_goals_scored_recent": 1.3,
            "home_avg_goals_conceded_recent": 1.1,
            "away_avg_goals_scored_recent": 1.2,
            "away_avg_goals_conceded_recent": 1.3,
            "over_1_5_rate": 0.68,
            "over_2_5_rate": 0.45,
            "btts_rate": 0.52,
        },
    }

    ranked = build_ranked_daily_preview(enriched)

    assert ranked["summary"]["total_fixtures_evaluated"] == 2
    assert ranked["summary"]["fixtures_with_ranked_candidates"] == 2
    assert len(ranked["ranked_fixtures"]) == 2
    assert ranked["ranked_fixtures"][0]["rank"] == 1
    assert ranked["ranked_fixtures"][1]["rank"] == 2


def test_select_best_candidate_returns_highest_confidence():
    fixture_preview = {
        "candidate_markets": [
            {"candidate_market": "BTTS", "confidence_score": 0.51},
            {"candidate_market": "Over 1.5", "confidence_score": 0.71},
            {"candidate_market": "Under 3.5", "confidence_score": 0.64},
        ]
    }
    best = select_best_candidate_for_fixture(fixture_preview)
    assert best["candidate_market"] == "Over 1.5"
