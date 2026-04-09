from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _norm_expected_total(expected_total: Optional[float]) -> float:
    if expected_total is None:
        return 0.0
    return _clamp((expected_total - 1.2) / 2.2)


def _confidence_band(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.55:
        return "medium"
    if score >= 0.4:
        return "low"
    return "very_low"


def _append_candidate(candidates: List[dict], fixture_identity: dict, market: str, score: float, why: str, signals: Dict[str, float]):
    confidence = round(_clamp(score), 3)
    band = _confidence_band(confidence)
    candidates.append(
        {
            "fixture_identity": fixture_identity,
            "candidate_market": market,
            "explanation": why,
            "confidence_score": confidence,
            "confidence_band": band,
            "contributing_signals": signals,
        }
    )


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def preview_for_enriched_fixture(enriched: dict) -> dict:
    fixture_identity = dict(enriched.get("fixture_identity") or {})
    completeness = enriched.get("enrichment_completeness", "missing")
    missing_fields = list(enriched.get("missing_fields") or [])

    home_avg_scored = _as_float(enriched.get("home_avg_goals_scored_recent"))
    home_avg_conceded = _as_float(enriched.get("home_avg_goals_conceded_recent"))
    away_avg_scored = _as_float(enriched.get("away_avg_goals_scored_recent"))
    away_avg_conceded = _as_float(enriched.get("away_avg_goals_conceded_recent"))

    over15 = _as_float(enriched.get("over_1_5_rate"))
    over25 = _as_float(enriched.get("over_2_5_rate"))
    btts = _as_float(enriched.get("btts_rate"))

    home_ppm = _as_float((enriched.get("home_recent_form") or {}).get("points_per_match"))
    away_ppm = _as_float((enriched.get("away_recent_form") or {}).get("points_per_match"))

    h2h_avg_total = _as_float((enriched.get("h2h_summary") or {}).get("avg_total_goals"))

    required_metrics = [home_avg_scored, home_avg_conceded, away_avg_scored, away_avg_conceded, over15, over25, btts]
    available_metrics = [item for item in required_metrics if item is not None]

    if not available_metrics:
        return {
            "fixture_identity": fixture_identity,
            "enrichment_completeness": completeness,
            "status": "insufficient_data",
            "missing_fields": missing_fields,
            "candidate_markets": [],
            "note": "No usable enrichment metrics available for preview.",
        }

    expected_total = None
    if None not in (home_avg_scored, away_avg_conceded, away_avg_scored, home_avg_conceded):
        expected_total = (home_avg_scored + away_avg_conceded + away_avg_scored + home_avg_conceded) / 2.0

    total_signal = _norm_expected_total(expected_total)
    h2h_signal = _norm_expected_total(h2h_avg_total)
    combined_over15 = _clamp(((over15 or 0.0) * 0.65) + (total_signal * 0.25) + (h2h_signal * 0.10))
    combined_over25 = _clamp(((over25 or 0.0) * 0.60) + (total_signal * 0.30) + (h2h_signal * 0.10))
    combined_btts = _clamp(((btts or 0.0) * 0.65) + (total_signal * 0.20) + (h2h_signal * 0.15))

    home_strength = None
    away_strength = None
    if home_avg_scored is not None and away_avg_conceded is not None:
        home_strength = (home_avg_scored + away_avg_conceded) / 2.0
    if away_avg_scored is not None and home_avg_conceded is not None:
        away_strength = (away_avg_scored + home_avg_conceded) / 2.0

    home_form = home_ppm if home_ppm is not None else 1.0
    away_form = away_ppm if away_ppm is not None else 1.0

    side_delta = 0.0
    if home_strength is not None and away_strength is not None:
        side_delta += (home_strength - away_strength) * 0.35
    side_delta += (home_form - away_form) * 0.22

    one_x_score = _clamp(0.5 + side_delta)
    x_two_score = _clamp(0.5 - side_delta)

    under35_score = _clamp(1.0 - ((combined_over25 * 0.60) + (total_signal * 0.30) + (h2h_signal * 0.10)))

    candidates: List[dict] = []

    if combined_over15 >= 0.45:
        _append_candidate(
            candidates,
            fixture_identity,
            "Over 1.5",
            combined_over15,
            "Recent totals and over 1.5 hit rates indicate a moderate-to-strong chance of 2+ goals.",
            {"over_1_5_rate": round(over15 or 0.0, 3), "expected_total_signal": round(total_signal, 3), "h2h_goal_signal": round(h2h_signal, 3)},
        )

    if combined_over25 >= 0.50:
        _append_candidate(
            candidates,
            fixture_identity,
            "Over 2.5",
            combined_over25,
            "Over 2.5 rate and expected total goals are supportive.",
            {"over_2_5_rate": round(over25 or 0.0, 3), "expected_total_signal": round(total_signal, 3), "h2h_goal_signal": round(h2h_signal, 3)},
        )

    if combined_btts >= 0.50:
        _append_candidate(
            candidates,
            fixture_identity,
            "BTTS",
            combined_btts,
            "Both-teams-to-score rate and team scoring/conceding patterns suggest mutual goal potential.",
            {"btts_rate": round(btts or 0.0, 3), "expected_total_signal": round(total_signal, 3), "h2h_goal_signal": round(h2h_signal, 3)},
        )

    if under35_score >= 0.50:
        _append_candidate(
            candidates,
            fixture_identity,
            "Under 3.5",
            under35_score,
            "Goal environment appears controlled enough for an under 3.5 lean.",
            {"inverse_over_2_5": round(1.0 - (over25 or 0.0), 3), "expected_total_signal": round(total_signal, 3), "h2h_goal_signal": round(h2h_signal, 3)},
        )

    if one_x_score >= 0.56:
        _append_candidate(
            candidates,
            fixture_identity,
            "Home or Draw (1X)",
            one_x_score,
            "Home side profile and form trend provide a double-chance edge.",
            {
                "home_form_ppm": round(home_form, 3),
                "away_form_ppm": round(away_form, 3),
                "home_strength": round(home_strength or 0.0, 3),
                "away_strength": round(away_strength or 0.0, 3),
            },
        )

    if x_two_score >= 0.56:
        _append_candidate(
            candidates,
            fixture_identity,
            "Away or Draw (X2)",
            x_two_score,
            "Away side profile and form trend provide a double-chance edge.",
            {
                "home_form_ppm": round(home_form, 3),
                "away_form_ppm": round(away_form, 3),
                "home_strength": round(home_strength or 0.0, 3),
                "away_strength": round(away_strength or 0.0, 3),
            },
        )

    if not candidates:
        return {
            "fixture_identity": fixture_identity,
            "enrichment_completeness": completeness,
            "status": "insufficient_data",
            "missing_fields": missing_fields,
            "candidate_markets": [],
            "note": "Available enrichment did not cross preview thresholds.",
        }

    low_confidence_floor = completeness in {"partial", "missing"}
    if low_confidence_floor:
        for candidate in candidates:
            candidate["confidence_band"] = "low" if candidate["confidence_band"] in {"high", "medium"} else candidate["confidence_band"]

    return {
        "fixture_identity": fixture_identity,
        "enrichment_completeness": completeness,
        "status": "ok" if completeness == "full" else "partial",
        "missing_fields": missing_fields,
        "candidate_markets": candidates,
        "note": "Preview generated from enrichment-only signals; not tied to production scoring pipeline.",
    }


def build_decision_preview(enriched_by_fixture_id: Dict[int, dict]) -> dict:
    previews = []
    market_counter = Counter()
    fixtures_with_candidates = 0
    insufficient_count = 0

    for fixture_id, enriched in sorted((enriched_by_fixture_id or {}).items()):
        fixture_preview = preview_for_enriched_fixture(enriched)
        fixture_preview["fixture_id"] = fixture_id
        previews.append(fixture_preview)

        candidates = fixture_preview.get("candidate_markets") or []
        if candidates:
            fixtures_with_candidates += 1
            for candidate in candidates:
                market_counter[candidate.get("candidate_market", "unknown")] += 1
        else:
            insufficient_count += 1

    summary = {
        "fixtures_previewed": len(previews),
        "fixtures_with_candidates": fixtures_with_candidates,
        "fixtures_skipped_insufficient_enrichment": insufficient_count,
        "market_frequency_counts": dict(sorted(market_counter.items(), key=lambda item: item[0])),
    }

    return {
        "summary": summary,
        "fixtures": previews,
    }


def _completeness_priority(completeness: str) -> int:
    order = {"full": 2, "partial": 1, "missing": 0}
    return order.get(str(completeness or "").lower(), 0)


def select_best_candidate_for_fixture(fixture_preview: dict) -> Optional[dict]:
    candidates = list(fixture_preview.get("candidate_markets") or [])
    if not candidates:
        return None

    ranked_candidates = sorted(
        candidates,
        key=lambda item: (
            float(item.get("confidence_score", 0.0) or 0.0),
            item.get("candidate_market", ""),
        ),
        reverse=True,
    )
    return ranked_candidates[0]


def build_ranked_daily_preview(enriched_by_fixture_id: Dict[int, dict]) -> dict:
    ranked_rows: List[dict] = []
    market_counter = Counter()
    confidence_band_counter = Counter()
    confidence_values: List[float] = []
    skipped = 0

    for fixture_id, enriched in sorted((enriched_by_fixture_id or {}).items()):
        fixture_preview = preview_for_enriched_fixture(enriched)
        best_candidate = select_best_candidate_for_fixture(fixture_preview)
        if not best_candidate:
            skipped += 1
            continue

        confidence = float(best_candidate.get("confidence_score", 0.0) or 0.0)
        confidence_values.append(confidence)
        band = str(best_candidate.get("confidence_band") or "very_low")
        confidence_band_counter[band] += 1

        market = str(best_candidate.get("candidate_market") or "unknown")
        market_counter[market] += 1

        ranked_rows.append(
            {
                "fixture_id": fixture_id,
                "fixture_identity": fixture_preview.get("fixture_identity", {}),
                "selected_market": market,
                "confidence_score": round(confidence, 3),
                "confidence_band": band,
                "explanation": best_candidate.get("explanation"),
                "contributing_signals": best_candidate.get("contributing_signals", {}),
                "enrichment_completeness": fixture_preview.get("enrichment_completeness", "missing"),
            }
        )

    ranked_rows.sort(
        key=lambda row: (
            float(row.get("confidence_score", 0.0) or 0.0),
            _completeness_priority(row.get("enrichment_completeness", "missing")),
            row.get("selected_market", ""),
        ),
        reverse=True,
    )

    for index, row in enumerate(ranked_rows, start=1):
        row["rank"] = index

    avg_confidence = round(sum(confidence_values) / len(confidence_values), 3) if confidence_values else 0.0
    summary = {
        "total_fixtures_evaluated": len(enriched_by_fixture_id or {}),
        "fixtures_with_ranked_candidates": len(ranked_rows),
        "fixtures_skipped": skipped,
        "market_distribution": dict(sorted(market_counter.items(), key=lambda item: item[0])),
        "confidence_distribution": dict(sorted(confidence_band_counter.items(), key=lambda item: item[0])),
        "average_confidence_score": avg_confidence,
    }

    return {
        "summary": summary,
        "ranked_fixtures": ranked_rows,
    }
