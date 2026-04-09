from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _confidence_band(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.55:
        return "medium"
    if score >= 0.40:
        return "low"
    return "very_low"


@dataclass
class CalibrationConfig:
    enabled: bool = False
    min_market_samples: int = 30
    min_market_hit_rate: float = 0.5
    suppress_underperforming_markets: bool = False
    underperformance_penalty_scale: float = 0.2
    low_evidence_penalty: float = 0.03
    completeness_penalties: Dict[str, float] = field(
        default_factory=lambda: {"full": 0.0, "partial": 0.08, "missing": 0.18}
    )
    min_confidence_after_calibration: Optional[float] = None
    market_penalties: Dict[str, float] = field(default_factory=dict)


def derive_market_evidence(evaluations: list[dict]) -> Dict[str, dict]:
    market_totals: Dict[str, dict] = {}
    for row in evaluations or []:
        market = str(row.get("selected_market") or "unknown")
        stat = market_totals.setdefault(market, {"samples": 0, "hits": 0})
        stat["samples"] += 1
        if row.get("actual_outcome") == "hit":
            stat["hits"] += 1

    evidence = {}
    for market, stat in market_totals.items():
        samples = int(stat["samples"])
        hits = int(stat["hits"])
        evidence[market] = {
            "samples": samples,
            "hits": hits,
            "hit_rate": round((hits / samples), 4) if samples else 0.0,
        }
    return evidence


def calibrate_candidate(
    candidate: dict,
    enrichment_completeness: str,
    config: CalibrationConfig,
    market_evidence: Optional[Dict[str, dict]] = None,
) -> Optional[dict]:
    if not config.enabled:
        return dict(candidate)

    market = str(candidate.get("candidate_market") or "unknown")
    evidence = dict((market_evidence or {}).get(market, {}))
    samples = int(evidence.get("samples", 0) or 0)
    hit_rate = float(evidence.get("hit_rate", 0.0) or 0.0)

    if (
        config.suppress_underperforming_markets
        and samples >= config.min_market_samples
        and hit_rate < config.min_market_hit_rate
    ):
        return None

    penalty = 0.0
    base_penalty = float(config.market_penalties.get(market, 0.0) or 0.0)
    penalty += max(base_penalty, 0.0)

    if samples >= config.min_market_samples:
        gap = max(config.min_market_hit_rate - hit_rate, 0.0)
        penalty += gap * max(config.underperformance_penalty_scale, 0.0)
    else:
        penalty += max(config.low_evidence_penalty, 0.0)

    penalty += float(config.completeness_penalties.get(str(enrichment_completeness), 0.1) or 0.0)

    original_score = float(candidate.get("confidence_score", 0.0) or 0.0)
    adjusted_score = round(_clamp(original_score - penalty), 3)
    if config.min_confidence_after_calibration is not None and adjusted_score < config.min_confidence_after_calibration:
        return None

    calibrated = dict(candidate)
    calibrated["confidence_score"] = adjusted_score
    calibrated["confidence_band"] = _confidence_band(adjusted_score)
    calibrated["calibration"] = {
        "market": market,
        "market_samples": samples,
        "market_hit_rate": hit_rate,
        "penalty_total": round(penalty, 4),
        "penalty_components": {
            "market_base_penalty": round(base_penalty, 4),
            "underperformance_or_low_evidence": round(penalty - base_penalty - float(config.completeness_penalties.get(str(enrichment_completeness), 0.1) or 0.0), 4),
            "completeness_penalty": round(float(config.completeness_penalties.get(str(enrichment_completeness), 0.1) or 0.0), 4),
        },
    }
    return calibrated


def select_best_candidate_with_calibration(
    fixture_preview: dict,
    enrichment_completeness: str,
    config: CalibrationConfig,
    market_evidence: Optional[Dict[str, dict]] = None,
) -> Optional[dict]:
    candidates = list(fixture_preview.get("candidate_markets") or [])
    if not candidates:
        return None

    adjusted = []
    for candidate in candidates:
        calibrated = calibrate_candidate(
            candidate=candidate,
            enrichment_completeness=enrichment_completeness,
            config=config,
            market_evidence=market_evidence,
        )
        if calibrated:
            adjusted.append(calibrated)

    if not adjusted:
        return None

    adjusted.sort(
        key=lambda item: (
            float(item.get("confidence_score", 0.0) or 0.0),
            str(item.get("candidate_market") or ""),
        ),
        reverse=True,
    )
    return adjusted[0]
