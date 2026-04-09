#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database.database import SessionLocal
from main import _collect_fixture_bundle, _resolve_requested_date
from services.decision_preview import build_decision_preview, build_ranked_daily_preview


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only decision preview using enrichment data only. "
            "This does not touch production scoring/ranking/prediction outputs."
        )
    )
    parser.add_argument("--date", required=True, help="Date in YYYY-MM-DD format")
    parser.add_argument("--source", default="hybrid", help="Fixture source: hybrid, openligadb, footballdata")
    parser.add_argument("--sample", type=int, default=15, help="How many ranked rows to print")
    return parser.parse_args()


def _write_preview_artifact(report: dict, requested_date: str) -> str:
    output_dir = Path("artifacts") / "decision_preview"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"decision_preview_{requested_date}_{timestamp}.json"
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path.as_posix()


def main() -> int:
    args = _parse_args()
    db = SessionLocal()
    try:
        requested_date = _resolve_requested_date(args.date)
        bundle = _collect_fixture_bundle(args.source, db, match_date=requested_date)
        enriched = bundle.get("fixture_enrichment", {})
        preview = build_decision_preview(enriched)
        ranked = build_ranked_daily_preview(enriched)

        report = {
            "mode": "decision_preview_read_only",
            "requested_date": requested_date.isoformat(),
            "source": args.source,
            "collection": {
                "raw_fixtures": int(bundle.get("raw_fixtures", 0)),
                "unique_fixtures": int(bundle.get("unique_fixtures", 0)),
                "duplicates_removed": int(bundle.get("duplicates_removed", 0)),
                "source_breakdown": dict(bundle.get("source_breakdown", {})),
            },
            "preview_summary": preview.get("summary", {}),
            "ranked_summary": ranked.get("summary", {}),
            "fixture_previews": preview.get("fixtures", []),
            "ranked_fixtures": ranked.get("ranked_fixtures", []),
            "note": "Read-only enrichment decision preview. Production prediction/scoring flow is unchanged.",
        }
        artifact_path = _write_preview_artifact(report, requested_date.isoformat())

        print("=== Decision Preview (Read-Only) ===")
        print(f"Date: {requested_date.isoformat()} | Source: {args.source}")
        print("Preview summary:")
        print(json.dumps(report["preview_summary"], indent=2))
        print("Ranked summary:")
        print(json.dumps(report["ranked_summary"], indent=2))
        print("Sample ranked candidates (unlimited ranking; this is a console sample only):")

        printed = 0
        for row in report["ranked_fixtures"]:
            identity = row.get("fixture_identity", {})
            print(
                "- rank={rank} | {competition} | {home} vs {away} | {market} | score={score} ({band})".format(
                    rank=row.get("rank"),
                    competition=identity.get("competition") or "Unknown",
                    home=identity.get("home_team") or "Unknown",
                    away=identity.get("away_team") or "Unknown",
                    market=row.get("selected_market"),
                    score=row.get("confidence_score"),
                    band=row.get("confidence_band"),
                )
            )
            printed += 1
            if printed >= max(args.sample, 0):
                break

        print(f"Artifact saved: {artifact_path}")
        return 0
    except Exception as exc:
        print(f"ERROR: decision preview failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
