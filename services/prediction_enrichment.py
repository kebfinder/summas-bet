from __future__ import annotations
from typing import Optional


def probability_pct(probability: Optional[float]) -> float:
    """Conservative display probability percentage from 0-1 score.
    This is a UI-facing confidence proxy, not guaranteed true probability."""
    p = max(0.0, min(1.0, float(probability or 0.0)))
    return round(p * 100.0, 1)


def confidence_stars(probability: Optional[float]) -> int:
    p = probability_pct(probability)
    if p >= 75:
        return 5
    if p >= 65:
        return 4
    if p >= 55:
        return 3
    if p >= 45:
        return 2
    return 1


def confidence_band(probability: Optional[float]) -> str:
    stars = confidence_stars(probability)
    return {1: "Low", 2: "Medium", 3: "Balanced", 4: "High", 5: "Very High"}[stars]


def risk_tier(probability: Optional[float], market: str = "") -> str:
    p = probability_pct(probability)
    high_var_markets = {"CorrectScore", "Over3.5", "Under1.5"}
    if market in high_var_markets or p < 52:
        return "Bold"
    if p >= 66:
        return "Safe"
    return "Balanced"


def fair_odds_from_probability(probability: Optional[float]) -> Optional[float]:
    p = probability_pct(probability)
    if p <= 0:
        return None
    return round(100.0 / p, 2)


def value_status(probability: Optional[float], bookmaker_odds: Optional[float]) -> tuple[str, Optional[float], Optional[float]]:
    fair = fair_odds_from_probability(probability)
    if not bookmaker_odds or bookmaker_odds <= 0 or fair is None:
        return "Odds unavailable", fair, None
    delta = round(bookmaker_odds - fair, 3)
    if delta >= 0.12:
        return "Value", fair, delta
    if delta <= -0.12:
        return "Poor value", fair, delta
    return "Fair", fair, delta


def form_to_points(form: str) -> int:
    return sum(3 if c == "W" else 1 if c == "D" else 0 for c in form)
