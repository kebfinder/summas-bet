#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as calendar_date, datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database.database import SessionLocal
from services.decision_backtest import run_decision_preview_backtest


def _parse_date(value: str) -> calendar_date:
    return calendar_date.fromisoformat(str(value).strip())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only historical backtest for enrichment-based decision preview. "
            "Production scoring/ranking/prediction flows are not modified."
        )
    )
    parser.add_argument("--start-date", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--sample", type=int, default=15, help="How many evaluation rows to print")
    return parser.parse_args()


def _write_artifact(report: dict, start_date: str, end_date: str) -> str:
    output_dir = Path("artifacts") / "decision_backtest"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"decision_backtest_{start_date}_to_{end_date}_{timestamp}.json"
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path.as_posix()


def main() -> int:
    args = _parse_args()
    db = SessionLocal()

    try:
        start_date = _parse_date(args.start_date)
        end_date = _parse_date(args.end_date)
        if end_date < start_date:
            print("ERROR: end-date must be on or after start-date", file=sys.stderr)
            return 1

        backtest = run_decision_preview_backtest(db, start_date, end_date)
        report = {
            "mode": "read_only_decision_preview_backtest",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "summary": backtest.get("summary", {}),
            "evaluations": backtest.get("evaluations", []),
            "note": backtest.get("note"),
        }
        artifact_path = _write_artifact(report, start_date.isoformat(), end_date.isoformat())

        print("=== Decision Preview Backtest (Read-Only) ===")
        print(f"Range: {start_date.isoformat()} -> {end_date.isoformat()}")
        print(json.dumps(report["summary"], indent=2))
        if report.get("note"):
            print(f"Note: {report['note']}")

        print("Sample evaluations:")
        for row in report["evaluations"][: max(args.sample, 0)]:
            identity = row.get("fixture_identity", {})
            print(
                "- {competition} | {home} vs {away} | market={market} | conf={conf} ({band}) | outcome={outcome}".format(
                    competition=identity.get("competition") or "Unknown",
                    home=identity.get("home_team") or "Unknown",
                    away=identity.get("away_team") or "Unknown",
                    market=row.get("selected_market"),
                    conf=row.get("confidence_score"),
                    band=row.get("confidence_band"),
                    outcome=row.get("actual_outcome"),
                )
            )

        print(f"Artifact saved: {artifact_path}")
        return 0
    except Exception as exc:
        print(f"ERROR: backtest failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
