#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database.database import SessionLocal
from main import _collect_fixture_bundle, _resolve_requested_date, _write_enrichment_debug_report
from services.fixture_enrichment import summarize_enrichment


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only fixture enrichment inspection. "
            "Collects fixtures for a supplied date and prints enrichment completeness/missing-field patterns."
        )
    )
    parser.add_argument("--date", required=True, help="Date in YYYY-MM-DD format")
    parser.add_argument("--source", default="hybrid", help="Fixture source: hybrid, openligadb, footballdata")
    parser.add_argument("--sample", type=int, default=10, help="How many fixture rows to print in terminal output")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db = SessionLocal()
    try:
        requested_date = _resolve_requested_date(args.date)
        bundle = _collect_fixture_bundle(args.source, db, match_date=requested_date)
        enriched_map = bundle.get("fixture_enrichment", {})
        fixtures = list(enriched_map.values())
        summary = summarize_enrichment(enriched_map)

        common_missing = Counter()
        for item in fixtures:
            for field in item.get("missing_fields", []):
                common_missing[field] += 1

        report = {
            "mode": "cli_read_only_debug",
            "requested_date": requested_date.isoformat(),
            "source": args.source,
            "collection": {
                "raw_fixtures": int(bundle.get("raw_fixtures", 0)),
                "unique_fixtures": int(bundle.get("unique_fixtures", 0)),
                "duplicates_removed": int(bundle.get("duplicates_removed", 0)),
                "source_breakdown": dict(bundle.get("source_breakdown", {})),
            },
            "summary": summary,
            "fixtures": fixtures,
        }
        artifact_path = _write_enrichment_debug_report(report, requested_date)

        print("=== Fixture Enrichment Debug ===")
        print(f"Date: {requested_date.isoformat()} | Source: {args.source}")
        print(
            "Totals: raw={raw} unique={unique} duplicates_removed={dupes}".format(
                raw=report["collection"]["raw_fixtures"],
                unique=report["collection"]["unique_fixtures"],
                dupes=report["collection"]["duplicates_removed"],
            )
        )
        print("Summary:")
        print(json.dumps(summary, indent=2))

        print("Sample fixtures:")
        for row in fixtures[: max(args.sample, 0)]:
            identity = row.get("fixture_identity", {})
            print(
                "- {competition} | {home} vs {away} | {kickoff} | completeness={status}".format(
                    competition=identity.get("competition") or "Unknown",
                    home=identity.get("home_team") or "Unknown",
                    away=identity.get("away_team") or "Unknown",
                    kickoff=identity.get("kickoff") or "Unknown",
                    status=row.get("enrichment_completeness") or "missing",
                )
            )

        print("Top missing fields:")
        for field, count in common_missing.most_common(10):
            print(f"- {field}: {count}")

        print(f"Artifact saved: {artifact_path}")
        return 0
    except Exception as exc:  # CLI diagnostic path
        print(f"ERROR: enrichment debug failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
