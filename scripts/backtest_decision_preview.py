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
from services.decision_backtest import run_backtest_with_calibration_comparison
from services.preview_calibration import CalibrationConfig


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
    parser.add_argument("--enable-calibration", action="store_true", help="Enable read-only calibration adjustments")
    parser.add_argument("--min-market-samples", type=int, default=30, help="Minimum backtest samples before market hit-rate penalties/suppression")
    parser.add_argument("--min-market-hit-rate", type=float, default=0.5, help="Target minimum hit rate used for underperformance penalties")
    parser.add_argument("--suppress-underperforming-markets", action="store_true", help="Suppress markets that underperform once sample threshold is met")
    parser.add_argument("--underperformance-penalty-scale", type=float, default=0.2, help="Penalty multiplier for market underperformance gap")
    parser.add_argument("--low-evidence-penalty", type=float, default=0.03, help="Penalty applied when market sample size is below threshold")
    parser.add_argument("--min-confidence-after-calibration", type=float, default=None, help="Optional minimum confidence after calibration")
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

        calibration_config = CalibrationConfig(
            enabled=bool(args.enable_calibration),
            min_market_samples=max(int(args.min_market_samples), 0),
            min_market_hit_rate=max(min(float(args.min_market_hit_rate), 1.0), 0.0),
            suppress_underperforming_markets=bool(args.suppress_underperforming_markets),
            underperformance_penalty_scale=max(float(args.underperformance_penalty_scale), 0.0),
            low_evidence_penalty=max(float(args.low_evidence_penalty), 0.0),
            min_confidence_after_calibration=(
                max(min(float(args.min_confidence_after_calibration), 1.0), 0.0)
                if args.min_confidence_after_calibration is not None
                else None
            ),
        )
        results = run_backtest_with_calibration_comparison(
            db,
            start_date,
            end_date,
            calibration_config=calibration_config,
        )
        backtest = results.get("calibrated", {})
        baseline = results.get("baseline", {})
        comparison = results.get("comparison", {})
        report = {
            "mode": "read_only_decision_preview_backtest",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "calibration_enabled": calibration_config.enabled,
            "baseline_summary": baseline.get("summary", {}),
            "calibrated_summary": backtest.get("summary", {}),
            "comparison": comparison,
            "market_evidence": results.get("market_evidence", {}),
            "summary": backtest.get("summary", {}),
            "evaluations": backtest.get("evaluations", []),
            "note": backtest.get("note"),
        }
        artifact_path = _write_artifact(report, start_date.isoformat(), end_date.isoformat())

        print("=== Decision Preview Backtest (Read-Only) ===")
        print(f"Range: {start_date.isoformat()} -> {end_date.isoformat()}")
        print(f"Calibration enabled: {calibration_config.enabled}")
        print("Baseline summary:")
        print(json.dumps(report["baseline_summary"], indent=2))
        print("Calibrated summary:")
        print(json.dumps(report["summary"], indent=2))
        print("Before-vs-after comparison:")
        print(json.dumps(report["comparison"], indent=2))
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
