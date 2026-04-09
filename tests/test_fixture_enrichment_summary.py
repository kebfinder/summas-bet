from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.fixture_enrichment import summarize_enrichment


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
