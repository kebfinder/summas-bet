from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date as calendar_date, datetime, time
from typing import Dict, Iterable, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from database.models import Fixture
from services.decision_preview import select_best_candidate_for_fixture, preview_for_enriched_fixture
from services.fixture_enrichment import enrich_fixture
from services.preview_calibration import CalibrationConfig, calibrate_candidate, derive_market_evidence


def _safe_rate(hits: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(hits / total, 4)


def resolve_market_outcome(market: str, home_goals: int, away_goals: int) -> str:
    total_goals = int(home_goals) + int(away_goals)
    normalized = str(market or "").strip().lower()

    if normalized == "over 1.5":
        return "hit" if total_goals >= 2 else "miss"
    if normalized == "over 2.5":
        return "hit" if total_goals >= 3 else "miss"
    if normalized == "btts":
        return "hit" if home_goals > 0 and away_goals > 0 else "miss"
    if normalized == "under 3.5":
        return "hit" if total_goals <= 3 else "miss"
    if normalized in {"home or draw (1x)", "1x", "home or draw"}:
        return "hit" if home_goals >= away_goals else "miss"
    if normalized in {"away or draw (x2)", "x2", "away or draw"}:
        return "hit" if away_goals >= home_goals else "miss"

    return "push"


def _date_window(start_date: calendar_date, end_date: calendar_date):
    start_dt = datetime.combine(start_date, time.min)
    end_dt = datetime.combine(end_date, time.max)
    return start_dt, end_dt


def _completed_fixtures(db: Session, start_date: calendar_date, end_date: calendar_date) -> Iterable[Fixture]:
    start_dt, end_dt = _date_window(start_date, end_date)
    return (
        db.query(Fixture)
        .filter(Fixture.date >= start_dt, Fixture.date <= end_dt)
        .filter(Fixture.home_goals.isnot(None), Fixture.away_goals.isnot(None))
        .order_by(Fixture.date.asc())
        .all()
    )


def _row_from_fixture(fixture: Fixture, enrichment: dict, best_candidate: dict, outcome: str) -> dict:
    return {
        "fixture_id": fixture.id,
        "fixture_identity": {
            "competition": fixture.league,
            "home_team": fixture.home.name if fixture.home else None,
            "away_team": fixture.away.name if fixture.away else None,
            "kickoff": fixture.date.isoformat() if fixture.date else None,
        },
        "selected_market": best_candidate.get("candidate_market"),
        "confidence_score": float(best_candidate.get("confidence_score", 0.0) or 0.0),
        "confidence_band": best_candidate.get("confidence_band"),
        "actual_outcome": outcome,
        "result": {"home_goals": fixture.home_goals, "away_goals": fixture.away_goals},
        "explanation": best_candidate.get("explanation"),
        "contributing_signals": best_candidate.get("contributing_signals", {}),
        "calibration": best_candidate.get("calibration"),
        "enrichment_completeness": enrichment.get("enrichment_completeness", "missing"),
        "missing_fields": enrichment.get("missing_fields", []),
    }


def _summarize(rows: list[dict], total_fixtures_evaluated: int, skipped_fixtures: int) -> dict:
    total_predictions = len(rows)
    hits = sum(1 for row in rows if row.get("actual_outcome") == "hit")

    by_market = defaultdict(lambda: {"total": 0, "hits": 0})
    by_band = defaultdict(lambda: {"total": 0, "hits": 0})
    by_completeness = defaultdict(lambda: {"total": 0, "hits": 0})

    confidence_totals = Counter()
    confidence_hits = Counter()

    for row in rows:
        market = str(row.get("selected_market") or "unknown")
        band = str(row.get("confidence_band") or "unknown")
        completeness = str(row.get("enrichment_completeness") or "unknown")
        outcome = row.get("actual_outcome")
        score = float(row.get("confidence_score", 0.0) or 0.0)

        by_market[market]["total"] += 1
        by_band[band]["total"] += 1
        by_completeness[completeness]["total"] += 1

        confidence_bin = f"{int(score * 10) * 10}-{int(score * 10) * 10 + 9}%"
        confidence_totals[confidence_bin] += 1

        if outcome == "hit":
            by_market[market]["hits"] += 1
            by_band[band]["hits"] += 1
            by_completeness[completeness]["hits"] += 1
            confidence_hits[confidence_bin] += 1

    market_hit_rate = {
        market: {
            "total": stat["total"],
            "hits": stat["hits"],
            "hit_rate": _safe_rate(stat["hits"], stat["total"]),
        }
        for market, stat in sorted(by_market.items())
    }
    band_hit_rate = {
        band: {
            "total": stat["total"],
            "hits": stat["hits"],
            "hit_rate": _safe_rate(stat["hits"], stat["total"]),
        }
        for band, stat in sorted(by_band.items())
    }
    completeness_hit_rate = {
        key: {
            "total": stat["total"],
            "hits": stat["hits"],
            "hit_rate": _safe_rate(stat["hits"], stat["total"]),
        }
        for key, stat in sorted(by_completeness.items())
    }
    confidence_vs_hit_rate = {
        key: {
            "total": confidence_totals[key],
            "hits": confidence_hits[key],
            "hit_rate": _safe_rate(confidence_hits[key], confidence_totals[key]),
        }
        for key in sorted(confidence_totals.keys())
    }

    avg_conf = round(sum(float(row.get("confidence_score", 0.0) or 0.0) for row in rows) / total_predictions, 4) if total_predictions else 0.0

    return {
        "total_fixtures_evaluated": total_fixtures_evaluated,
        "total_predictions_generated": total_predictions,
        "fixtures_skipped": skipped_fixtures,
        "hit_rate_overall": _safe_rate(hits, total_predictions),
        "average_confidence_score": avg_conf,
        "hit_rate_by_market": market_hit_rate,
        "hit_rate_by_confidence_band": band_hit_rate,
        "hit_rate_by_enrichment_completeness": completeness_hit_rate,
        "average_confidence_vs_actual_hit_rate": confidence_vs_hit_rate,
    }


def _select_candidate_with_optional_calibration(
    fixture_preview: dict,
    enrichment: dict,
    calibration_config: Optional[CalibrationConfig],
    market_evidence: Optional[Dict[str, dict]],
) -> Optional[dict]:
    candidates = list(fixture_preview.get("candidate_markets") or [])
    if not candidates:
        return None

    if calibration_config and calibration_config.enabled:
        calibrated = []
        completeness = str(enrichment.get("enrichment_completeness", "missing"))
        for candidate in candidates:
            adjusted = calibrate_candidate(
                candidate=candidate,
                enrichment_completeness=completeness,
                config=calibration_config,
                market_evidence=market_evidence,
            )
            if adjusted:
                calibrated.append(adjusted)
        if not calibrated:
            return None
        fixture_preview = dict(fixture_preview)
        fixture_preview["candidate_markets"] = calibrated

    return select_best_candidate_for_fixture(fixture_preview)


def run_decision_preview_backtest(
    db: Session,
    start_date: calendar_date,
    end_date: calendar_date,
    calibration_config: Optional[CalibrationConfig] = None,
    market_evidence: Optional[Dict[str, dict]] = None,
) -> dict:
    try:
        fixtures = list(_completed_fixtures(db, start_date, end_date))
    except SQLAlchemyError as exc:
        return {
            "summary": {
                "total_fixtures_evaluated": 0,
                "total_predictions_generated": 0,
                "fixtures_skipped": 0,
                "hit_rate_overall": 0.0,
                "average_confidence_score": 0.0,
                "hit_rate_by_market": {},
                "hit_rate_by_confidence_band": {},
                "hit_rate_by_enrichment_completeness": {},
                "average_confidence_vs_actual_hit_rate": {},
            },
            "evaluations": [],
            "note": f"Historical data unavailable or incomplete for backtest query: {exc.__class__.__name__}.",
        }
    if not fixtures:
        return {
            "summary": {
                "total_fixtures_evaluated": 0,
                "total_predictions_generated": 0,
                "fixtures_skipped": 0,
                "hit_rate_overall": 0.0,
                "average_confidence_score": 0.0,
                "hit_rate_by_market": {},
                "hit_rate_by_confidence_band": {},
                "hit_rate_by_enrichment_completeness": {},
                "average_confidence_vs_actual_hit_rate": {},
            },
            "evaluations": [],
            "note": "No completed fixtures found for the supplied date range.",
        }

    evaluations = []
    skipped = 0

    for fixture in fixtures:
        try:
            enrichment = enrich_fixture(db, fixture)
            fixture_preview = preview_for_enriched_fixture(enrichment)
            best_candidate = _select_candidate_with_optional_calibration(
                fixture_preview=fixture_preview,
                enrichment=enrichment,
                calibration_config=calibration_config,
                market_evidence=market_evidence,
            )
            if not best_candidate:
                skipped += 1
                continue

            outcome = resolve_market_outcome(
                str(best_candidate.get("candidate_market") or ""),
                int(fixture.home_goals),
                int(fixture.away_goals),
            )
            evaluations.append(_row_from_fixture(fixture, enrichment, best_candidate, outcome))
        except Exception:
            skipped += 1

    summary = _summarize(evaluations, total_fixtures_evaluated=len(fixtures), skipped_fixtures=skipped)
    return {
        "summary": summary,
        "evaluations": evaluations,
    }


def run_backtest_with_calibration_comparison(
    db: Session,
    start_date: calendar_date,
    end_date: calendar_date,
    calibration_config: Optional[CalibrationConfig] = None,
) -> dict:
    baseline = run_decision_preview_backtest(db, start_date, end_date)
    config = calibration_config or CalibrationConfig(enabled=False)
    if not config.enabled:
        return {
            "baseline": baseline,
            "calibrated": baseline,
            "comparison": {
                "fixtures_with_candidates_before": int(baseline.get("summary", {}).get("total_predictions_generated", 0)),
                "fixtures_with_candidates_after": int(baseline.get("summary", {}).get("total_predictions_generated", 0)),
                "average_confidence_before": float(baseline.get("summary", {}).get("average_confidence_score", 0.0) or 0.0),
                "average_confidence_after": float(baseline.get("summary", {}).get("average_confidence_score", 0.0) or 0.0),
                "market_distribution_before": {
                    key: value.get("total", 0)
                    for key, value in (baseline.get("summary", {}).get("hit_rate_by_market", {}) or {}).items()
                },
                "market_distribution_after": {
                    key: value.get("total", 0)
                    for key, value in (baseline.get("summary", {}).get("hit_rate_by_market", {}) or {}).items()
                },
                "hit_rate_overall_before": float(baseline.get("summary", {}).get("hit_rate_overall", 0.0) or 0.0),
                "hit_rate_overall_after": float(baseline.get("summary", {}).get("hit_rate_overall", 0.0) or 0.0),
            },
            "market_evidence": derive_market_evidence(baseline.get("evaluations", [])),
        }

    evidence = derive_market_evidence(baseline.get("evaluations", []))
    calibrated = run_decision_preview_backtest(
        db,
        start_date,
        end_date,
        calibration_config=config,
        market_evidence=evidence,
    )
    comparison = {
        "fixtures_with_candidates_before": int(baseline.get("summary", {}).get("total_predictions_generated", 0)),
        "fixtures_with_candidates_after": int(calibrated.get("summary", {}).get("total_predictions_generated", 0)),
        "average_confidence_before": float(baseline.get("summary", {}).get("average_confidence_score", 0.0) or 0.0),
        "average_confidence_after": float(calibrated.get("summary", {}).get("average_confidence_score", 0.0) or 0.0),
        "market_distribution_before": {
            key: value.get("total", 0)
            for key, value in (baseline.get("summary", {}).get("hit_rate_by_market", {}) or {}).items()
        },
        "market_distribution_after": {
            key: value.get("total", 0)
            for key, value in (calibrated.get("summary", {}).get("hit_rate_by_market", {}) or {}).items()
        },
        "hit_rate_overall_before": float(baseline.get("summary", {}).get("hit_rate_overall", 0.0) or 0.0),
        "hit_rate_overall_after": float(calibrated.get("summary", {}).get("hit_rate_overall", 0.0) or 0.0),
    }
    return {
        "baseline": baseline,
        "calibrated": calibrated,
        "comparison": comparison,
        "market_evidence": evidence,
        "calibration_config": {
            "enabled": config.enabled,
            "min_market_samples": config.min_market_samples,
            "min_market_hit_rate": config.min_market_hit_rate,
            "suppress_underperforming_markets": config.suppress_underperforming_markets,
            "underperformance_penalty_scale": config.underperformance_penalty_scale,
            "low_evidence_penalty": config.low_evidence_penalty,
            "completeness_penalties": dict(config.completeness_penalties),
            "min_confidence_after_calibration": config.min_confidence_after_calibration,
            "market_penalties": dict(config.market_penalties),
        },
    }
