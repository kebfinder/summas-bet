from __future__ import annotations

from typing import Iterable


def _normalize_market(market: str, selection: str) -> tuple[str, str]:
    return (str(market or "").strip().lower(), str(selection or "").strip().lower())


def evaluate_prediction(market: str, selection: str, home_goals: int, away_goals: int) -> str:
    total_goals = home_goals + away_goals
    market_key, selection_key = _normalize_market(market, selection)

    if market_key in {"1x2"}:
        if selection_key == "home win":
            return "won" if home_goals > away_goals else "lost"
        if selection_key == "draw":
            return "won" if home_goals == away_goals else "lost"
        if selection_key == "away win":
            return "won" if away_goals > home_goals else "lost"

    if market_key == "btts":
        if selection_key == "yes":
            return "won" if home_goals > 0 and away_goals > 0 else "lost"
        if selection_key == "no":
            return "won" if home_goals == 0 or away_goals == 0 else "lost"

    if market_key in {"over1.5", "under1.5", "over2.5", "under2.5", "over3.5", "under3.5"}:
        thresholds = {"over1.5": 1.5, "under1.5": 1.5, "over2.5": 2.5, "under2.5": 2.5, "over3.5": 3.5, "under3.5": 3.5}
        threshold = thresholds[market_key]
        if market_key.startswith("over"):
            return "won" if total_goals > threshold else "lost"
        return "won" if total_goals < threshold else "lost"

    if market_key == "doublechance":
        if selection_key == "1x":
            return "won" if home_goals >= away_goals else "lost"
        if selection_key == "x2":
            return "won" if away_goals >= home_goals else "lost"
        if selection_key == "12":
            return "won" if home_goals != away_goals else "lost"

    if market_key == "drawnobet":
        if home_goals == away_goals:
            return "void"
        if selection_key == "home":
            return "won" if home_goals > away_goals else "lost"
        if selection_key == "away":
            return "won" if away_goals > home_goals else "lost"

    if market_key == "homegoals0.5" and selection_key == "over":
        return "won" if home_goals >= 1 else "lost"
    if market_key == "homegoals1.5" and selection_key == "over":
        return "won" if home_goals >= 2 else "lost"
    if market_key == "awaygoals0.5" and selection_key == "over":
        return "won" if away_goals >= 1 else "lost"
    if market_key == "awaygoals1.5" and selection_key == "over":
        return "won" if away_goals >= 2 else "lost"

    return "pending"


def compute_performance(predictions: Iterable[dict]) -> dict:
    prediction_list = list(predictions)
    total = len(prediction_list)
    wins = sum(1 for item in prediction_list if item.get("status") == "won")
    losses = sum(1 for item in prediction_list if item.get("status") == "lost")
    void_count = sum(1 for item in prediction_list if item.get("status") == "void")
    pending_count = sum(1 for item in prediction_list if item.get("status") == "pending")
    settled_count = wins + losses

    if total == 0:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "void_count": 0,
            "pending_count": 0,
            "settled_count": 0,
            "win_rate": 0,
            "win_rate_settled": 0,
        }

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "void_count": void_count,
        "pending_count": pending_count,
        "settled_count": settled_count,
        "win_rate": round((wins / total) * 100, 2),
        "win_rate_settled": round((wins / settled_count) * 100, 2) if settled_count else 0,
    }
