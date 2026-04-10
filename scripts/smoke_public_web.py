from __future__ import annotations

import json
from datetime import date as calendar_date
from datetime import timedelta

from database.database import SessionLocal
import main


def run_smoke(reference_date: calendar_date, days: int = 3) -> list[dict]:
    rows: list[dict] = []
    for offset in range(days):
        target = reference_date + timedelta(days=offset)
        db = SessionLocal()
        try:
            bundle = main._collect_public_web_bundle(db, match_date=target)
            status = bundle.get("source_status", {}).get("public_web", {})
            rows.append(
                {
                    "date": target.isoformat(),
                    "ran": status.get("ran"),
                    "raw_fixtures": status.get("raw_fixtures"),
                    "persisted_fixtures": status.get("survived_unique_fixtures"),
                    "analyzable_candidates_estimate": status.get("analyzable_candidates_estimate"),
                    "failure_reason": status.get("failure_reason"),
                    "adapter_candidate_counts": status.get("adapter_candidate_counts"),
                    "adapter_stage_counts": status.get("adapter_stage_counts"),
                    "adapter_provider_usage": status.get("adapter_provider_usage"),
                }
            )
        finally:
            db.close()
    return rows


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Smoke test public_web adapter contribution diagnostics.")
    parser.add_argument("--date", default=calendar_date.today().isoformat(), help="Reference date in YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=3, help="Number of days to run including the reference date")
    args = parser.parse_args()

    reference = calendar_date.fromisoformat(args.date)
    report = run_smoke(reference, days=max(int(args.days), 1))
    print(json.dumps({"reference_date": reference.isoformat(), "days": int(args.days), "runs": report}, indent=2))
