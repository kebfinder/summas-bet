from __future__ import annotations

import math

from sqlalchemy.orm import Session

from config import LEAGUE_AVG_GOALS
from database.models import HistoricalMatch, Team
from utils.competitions import competition_profile_for_name
from utils.football import clamp, weighted_average


DEFAULT_MARKET_ODDS = {
    ("1X2", "Home Win"): 2.45,
    ("1X2", "Draw"): 3.35,
    ("1X2", "Away Win"): 2.85,
    ("DoubleChance", "1X"): 1.39,
    ("DoubleChance", "X2"): 1.52,
    ("DoubleChance", "12"): 1.34,
    ("DrawNoBet", "Home"): 1.72,
    ("DrawNoBet", "Away"): 1.92,
    ("BTTS", "Yes"): 1.91,
    ("BTTS", "No"): 1.91,
    ("Over1.5", "Over"): 1.34,
    ("Under1.5", "Under"): 3.25,
    ("Over2.5", "Over"): 1.95,
    ("Under2.5", "Under"): 1.91,
    ("Over3.5", "Over"): 3.05,
    ("Under3.5", "Under"): 1.36,
    ("HomeGoals0.5", "Over"): 1.28,
    ("HomeGoals0.5", "Under"): 4.1,
    ("HomeGoals1.5", "Over"): 1.86,
    ("HomeGoals1.5", "Under"): 1.97,
    ("AwayGoals0.5", "Over"): 1.36,
    ("AwayGoals0.5", "Under"): 3.6,
    ("AwayGoals1.5", "Over"): 2.20,
    ("AwayGoals1.5", "Under"): 1.64,
}


MARKET_GROUPS = {
    "1X2": (("1X2", "Home Win"), ("1X2", "Draw"), ("1X2", "Away Win")),
    "DrawNoBet": (("DrawNoBet", "Home"), ("DrawNoBet", "Away")),
    "BTTS": (("BTTS", "Yes"), ("BTTS", "No")),
    "Goals1.5": (("Over1.5", "Over"), ("Under1.5", "Under")),
    "Goals2.5": (("Over2.5", "Over"), ("Under2.5", "Under")),
    "Goals3.5": (("Over3.5", "Over"), ("Under3.5", "Under")),
    "HomeGoals0.5": (("HomeGoals0.5", "Over"), ("HomeGoals0.5", "Under")),
    "HomeGoals1.5": (("HomeGoals1.5", "Over"), ("HomeGoals1.5", "Under")),
    "AwayGoals0.5": (("AwayGoals0.5", "Over"), ("AwayGoals0.5", "Under")),
    "AwayGoals1.5": (("AwayGoals1.5", "Over"), ("AwayGoals1.5", "Under")),
}


MARKET_META = {
    "1X2": {"threshold": 0.04, "stability": 0.62, "volatility": "medium"},
    "DoubleChance": {"threshold": 0.025, "stability": 0.84, "volatility": "low"},
    "DrawNoBet": {"threshold": 0.04, "stability": 0.72, "volatility": "medium"},
    "BTTS": {"threshold": 0.04, "stability": 0.58, "volatility": "medium"},
    "Goals1.5": {"threshold": 0.025, "stability": 0.86, "volatility": "low"},
    "Goals2.5": {"threshold": 0.04, "stability": 0.64, "volatility": "medium"},
    "Goals3.5": {"threshold": 0.06, "stability": 0.46, "volatility": "high"},
    "HomeGoals0.5": {"threshold": 0.025, "stability": 0.79, "volatility": "low"},
    "HomeGoals1.5": {"threshold": 0.04, "stability": 0.61, "volatility": "medium"},
    "AwayGoals0.5": {"threshold": 0.025, "stability": 0.75, "volatility": "low"},
    "AwayGoals1.5": {"threshold": 0.06, "stability": 0.47, "volatility": "high"},
}


MARKET_DISPLAY_NAMES = {
    "1X2": "Match Result",
    "DoubleChance": "Double Chance",
    "DrawNoBet": "Draw No Bet",
    "BTTS": "BTTS",
    "Over1.5": "Goals 1.5",
    "Under1.5": "Goals 1.5",
    "Over2.5": "Goals 2.5",
    "Under2.5": "Goals 2.5",
    "Over3.5": "Goals 3.5",
    "Under3.5": "Goals 3.5",
    "HomeGoals0.5": "Home Team Goals 0.5",
    "HomeGoals1.5": "Home Team Goals 1.5",
    "AwayGoals0.5": "Away Team Goals 0.5",
    "AwayGoals1.5": "Away Team Goals 1.5",
}


def _points(goals_for: int, goals_against: int) -> int:
    if goals_for > goals_against:
        return 3
    if goals_for == goals_against:
        return 1
    return 0


def _team_match_rows(db: Session, team_id: int, venue: str, limit: int):
    query = db.query(HistoricalMatch)
    if venue == "home":
        query = query.filter(HistoricalMatch.home_id == team_id)
    else:
        query = query.filter(HistoricalMatch.away_id == team_id)
    return query.order_by(HistoricalMatch.date.desc()).limit(limit).all()


def _team_all_match_rows(db: Session, team_id: int, limit: int):
    home_matches = _team_match_rows(db, team_id, "home", limit)
    away_matches = _team_match_rows(db, team_id, "away", limit)
    rows = []
    for match in home_matches:
        rows.append(
            {
                "date": match.date,
                "venue": "home",
                "goals_for": match.home_goals or 0,
                "goals_against": match.away_goals or 0,
                "points": _points(match.home_goals or 0, match.away_goals or 0) / 3.0,
                "btts": 1.0 if match.btts else 0.0,
                "over15": 1.0 if match.over_1_5 else 0.0,
                "over25": 1.0 if match.over_2_5 else 0.0,
            }
        )
    for match in away_matches:
        rows.append(
            {
                "date": match.date,
                "venue": "away",
                "goals_for": match.away_goals or 0,
                "goals_against": match.home_goals or 0,
                "points": _points(match.away_goals or 0, match.home_goals or 0) / 3.0,
                "btts": 1.0 if match.btts else 0.0,
                "over15": 1.0 if match.over_1_5 else 0.0,
                "over25": 1.0 if match.over_2_5 else 0.0,
            }
        )
    rows.sort(key=lambda item: item["date"], reverse=True)
    return rows


def _subset_metric(rows: list[dict], key: str, fallback: float, limit: int, decay: float = 0.92) -> float:
    subset = rows[:limit]
    return weighted_average([row[key] for row in subset], fallback=fallback, decay=decay)


def team_profile(db: Session, team: Team, venue_limit: int = 8, recent_limit: int = 8, short_limit: int = 3) -> dict:
    home_matches = _team_match_rows(db, team.id, "home", venue_limit)
    away_matches = _team_match_rows(db, team.id, "away", venue_limit)
    all_rows = _team_all_match_rows(db, team.id, max(recent_limit + 4, 12))

    venue_rows_home = [row for row in all_rows if row["venue"] == "home"][:venue_limit]
    venue_rows_away = [row for row in all_rows if row["venue"] == "away"][:venue_limit]

    fallback_scored = team.avg_scored or 1.25
    fallback_conceded = team.avg_conceded or 1.25

    return {
        "home_scored": weighted_average([match.home_goals or 0 for match in home_matches], fallback=fallback_scored, decay=0.92),
        "home_conceded": weighted_average([match.away_goals or 0 for match in home_matches], fallback=fallback_conceded, decay=0.92),
        "away_scored": weighted_average([match.away_goals or 0 for match in away_matches], fallback=fallback_scored, decay=0.92),
        "away_conceded": weighted_average([match.home_goals or 0 for match in away_matches], fallback=fallback_conceded, decay=0.92),
        "recent_scored": _subset_metric(all_rows, "goals_for", fallback_scored, recent_limit),
        "recent_conceded": _subset_metric(all_rows, "goals_against", fallback_conceded, recent_limit),
        "recent_form": _subset_metric(all_rows, "points", team.form or 0.5, recent_limit),
        "short_scored": _subset_metric(all_rows, "goals_for", fallback_scored, short_limit, decay=0.96),
        "short_conceded": _subset_metric(all_rows, "goals_against", fallback_conceded, short_limit, decay=0.96),
        "short_form": _subset_metric(all_rows, "points", team.form or 0.5, short_limit, decay=0.96),
        "home_form": _subset_metric(venue_rows_home, "points", team.form or 0.5, short_limit, decay=0.96),
        "away_form": _subset_metric(venue_rows_away, "points", team.form or 0.5, short_limit, decay=0.96),
        "home_support": len(home_matches),
        "away_support": len(away_matches),
        "recent_support": len(all_rows[:recent_limit]),
    }


def league_profile(db: Session, league: str | None, limit: int = 320) -> dict:
    competition = competition_profile_for_name(league)
    baseline = LEAGUE_AVG_GOALS if LEAGUE_AVG_GOALS > 0 else 1.25
    fallback_home = competition["home_goals"] if competition["home_goals"] > 0 else baseline * 1.08
    fallback_away = competition["away_goals"] if competition["away_goals"] > 0 else baseline * 0.92

    default_profile = {
        "home_goals": fallback_home,
        "away_goals": fallback_away,
        "btts": competition["btts"],
        "over15": competition["over15"],
        "over25": competition["over25"],
        "over35": competition["over35"],
        "home_scoring": clamp(fallback_home / max(fallback_home + 0.45, 0.1), 0.45, 0.90),
        "away_scoring": clamp(fallback_away / max(fallback_away + 0.52, 0.1), 0.35, 0.82),
        "volatility": 0.56 if competition["competition_class"] != "experimental" else 0.66,
        "matches": 0,
        "competition": competition,
    }

    if not league:
        return default_profile

    matches = (
        db.query(HistoricalMatch)
        .join(Team, HistoricalMatch.home_id == Team.id)
        .filter(Team.league == league)
        .order_by(HistoricalMatch.date.desc())
        .limit(limit)
        .all()
    )
    if not matches:
        return default_profile

    totals = [(match.home_goals or 0) + (match.away_goals or 0) for match in matches]
    mean_total = weighted_average(totals, fallback=fallback_home + fallback_away, decay=0.97)
    variance = weighted_average(
        [((total - mean_total) ** 2) for total in totals],
        fallback=1.4,
        decay=0.97,
    )

    return {
        "home_goals": weighted_average([match.home_goals or 0 for match in matches], fallback=fallback_home, decay=0.97),
        "away_goals": weighted_average([match.away_goals or 0 for match in matches], fallback=fallback_away, decay=0.97),
        "btts": weighted_average([1.0 if match.btts else 0.0 for match in matches], fallback=0.52, decay=0.97),
        "over15": weighted_average([1.0 if match.over_1_5 else 0.0 for match in matches], fallback=0.74, decay=0.97),
        "over25": weighted_average([1.0 if match.over_2_5 else 0.0 for match in matches], fallback=0.51, decay=0.97),
        "over35": weighted_average([1.0 if total >= 4 else 0.0 for total in totals], fallback=0.28, decay=0.97),
        "home_scoring": weighted_average([1.0 if (match.home_goals or 0) > 0 else 0.0 for match in matches], fallback=0.78, decay=0.97),
        "away_scoring": weighted_average([1.0 if (match.away_goals or 0) > 0 else 0.0 for match in matches], fallback=0.66, decay=0.97),
        "volatility": clamp(math.sqrt(max(variance, 0.0)) / 2.4, 0.25, 1.0),
        "matches": len(matches),
        "competition": competition,
    }


def _is_cup_like(league_name: str | None) -> bool:
    text = str(league_name or "").lower()
    return any(token in text for token in ("cup", "pokal", "knockout", "playoff", "play-off"))


def _is_final_like(league_name: str | None) -> bool:
    return "final" in str(league_name or "").lower()


def build_match_context(db: Session, fixture) -> dict:
    home = fixture.home
    away = fixture.away
    league = league_profile(db, fixture.league)
    competition = league["competition"]
    home_profile = team_profile(db, home)
    away_profile = team_profile(db, away)

    venue_support = clamp((home_profile["home_support"] + away_profile["away_support"]) / 16.0, 0.25, 1.0)
    sample_support = clamp(min(home.matches_played or 0, away.matches_played or 0) / 18.0, 0.25, 1.0)
    league_support = clamp(league["matches"] / max(competition["league_sample_target"], 60), 0.25, 1.0)
    data_quality_base = clamp((0.45 * venue_support) + (0.30 * sample_support) + (0.25 * league_support), 0.25, 1.0)
    source_quality = 0.72 + (0.28 * competition["source_confidence"])
    data_quality = round(
        clamp(data_quality_base * competition["quality_multiplier"] * source_quality, 0.22, 1.0),
        3,
    )

    home_attack_signal = (0.70 * home_profile["home_scored"]) + (0.30 * home_profile["recent_scored"])
    away_attack_signal = (0.70 * away_profile["away_scored"]) + (0.30 * away_profile["recent_scored"])
    home_defense_signal = (0.70 * home_profile["home_conceded"]) + (0.30 * home_profile["recent_conceded"])
    away_defense_signal = (0.70 * away_profile["away_conceded"]) + (0.30 * away_profile["recent_conceded"])

    home_attack = (
        0.50 * (home.avg_scored or league["home_goals"])
        + 0.30 * home_attack_signal
        + 0.20 * home_profile["short_scored"]
    )
    away_attack = (
        0.50 * (away.avg_scored or league["away_goals"])
        + 0.30 * away_attack_signal
        + 0.20 * away_profile["short_scored"]
    )
    home_defense = (
        0.50 * (home.avg_conceded or league["away_goals"])
        + 0.30 * home_defense_signal
        + 0.20 * home_profile["short_conceded"]
    )
    away_defense = (
        0.50 * (away.avg_conceded or league["home_goals"])
        + 0.30 * away_defense_signal
        + 0.20 * away_profile["short_conceded"]
    )

    home_xg_raw = (0.44 * home_attack) + (0.31 * away_defense) + (0.25 * league["home_goals"])
    away_xg_raw = (0.44 * away_attack) + (0.31 * home_defense) + (0.25 * league["away_goals"])

    form_gap = (
        (0.65 * home_profile["home_form"]) + (0.35 * home_profile["recent_form"])
        - (0.65 * away_profile["away_form"]) - (0.35 * away_profile["recent_form"])
    )
    home_xg_raw *= clamp(1 + (form_gap * 0.10), 0.88, 1.14)
    away_xg_raw *= clamp(1 - (form_gap * 0.08), 0.88, 1.12)

    competition_penalty = 0.0
    if _is_cup_like(fixture.league):
        competition_penalty += 0.03
    if _is_final_like(fixture.league):
        competition_penalty += 0.02
    if competition["experimental"]:
        competition_penalty += 0.03

    home_xg = clamp((data_quality * home_xg_raw) + ((1 - data_quality) * league["home_goals"]), 0.45, 3.2)
    away_xg = clamp((data_quality * away_xg_raw) + ((1 - data_quality) * league["away_goals"]), 0.35, 3.0)
    home_xg = clamp(home_xg * (1 - competition_penalty), 0.45, 3.2)
    away_xg = clamp(away_xg * (1 - competition_penalty), 0.35, 3.0)

    blend_weights = market_blend_weights(data_quality, competition)

    return {
        "home_xg": round(home_xg, 3),
        "away_xg": round(away_xg, 3),
        "data_quality": data_quality,
        "venue_support": round(venue_support, 3),
        "sample_support": round(sample_support, 3),
        "league_support": round(league_support, 3),
        "blend_weights": blend_weights,
        "league": league,
        "competition": competition,
        "home_profile": home_profile,
        "away_profile": away_profile,
        "form_gap": round(form_gap, 3),
        "competition_penalty": round(competition_penalty, 3),
        "empirical": {
            "btts": clamp((0.55 * ((home.btts_rate + away.btts_rate) / 2.0)) + (0.45 * league["btts"]), 0.08, 0.92),
            "over15": clamp((0.55 * ((home.over15_rate + away.over15_rate) / 2.0)) + (0.45 * league["over15"]), 0.18, 0.97),
            "over25": clamp((0.55 * ((home.over25_rate + away.over25_rate) / 2.0)) + (0.45 * league["over25"]), 0.08, 0.90),
            "over35": clamp((0.45 * ((home.over25_rate + away.over25_rate) / 2.0)) + (0.55 * league["over35"]), 0.04, 0.72),
            "home_scoring": clamp((0.60 * home.attack_strength / max(home.defense_strength or 1.0, 0.4)) / 2.0 + (0.40 * league["home_scoring"]), 0.25, 0.96),
            "away_scoring": clamp((0.60 * away.attack_strength / max(away.defense_strength or 1.0, 0.4)) / 2.0 + (0.40 * league["away_scoring"]), 0.18, 0.90),
        },
    }


def poisson_probability(lambda_val: float, goals: int) -> float:
    return (lambda_val ** goals * math.exp(-lambda_val)) / math.factorial(goals)


def dixon_coles_tau(home_goals: int, away_goals: int, home_xg: float, away_xg: float, rho: float = -0.08) -> float:
    if home_goals == 0 and away_goals == 0:
        return 1 - (home_xg * away_xg * rho)
    if home_goals == 0 and away_goals == 1:
        return 1 + (home_xg * rho)
    if home_goals == 1 and away_goals == 0:
        return 1 + (away_xg * rho)
    if home_goals == 1 and away_goals == 1:
        return 1 - rho
    return 1.0


def build_score_matrix(home_xg: float, away_xg: float, max_goals: int = 7, rho: float = -0.08) -> list[list[float]]:
    matrix = []
    total = 0.0
    for home_goals in range(max_goals + 1):
        row = []
        for away_goals in range(max_goals + 1):
            prob = poisson_probability(home_xg, home_goals) * poisson_probability(away_xg, away_goals)
            prob *= max(dixon_coles_tau(home_goals, away_goals, home_xg, away_xg, rho), 0.01)
            row.append(prob)
            total += prob
        matrix.append(row)

    if total <= 0:
        return matrix

    for home_goals in range(len(matrix)):
        for away_goals in range(len(matrix[home_goals])):
            matrix[home_goals][away_goals] /= total
    return matrix


def market_group_key(market: str) -> str:
    if market in {"Over1.5", "Under1.5"}:
        return "Goals1.5"
    if market in {"Over2.5", "Under2.5"}:
        return "Goals2.5"
    if market in {"Over3.5", "Under3.5"}:
        return "Goals3.5"
    return market


def market_threshold(market: str) -> float:
    group = market_group_key(market)
    return MARKET_META.get(group, {"threshold": 0.04})["threshold"]


def market_stability(market: str) -> float:
    group = market_group_key(market)
    return MARKET_META.get(group, {"stability": 0.55})["stability"]


def market_volatility(market: str) -> str:
    group = market_group_key(market)
    return MARKET_META.get(group, {"volatility": "medium"})["volatility"]


def market_display_name(market: str) -> str:
    return MARKET_DISPLAY_NAMES.get(market, market)


def _devig_from_group(group_entries: tuple[tuple[str, str], ...]) -> dict[tuple[str, str], float]:
    implied = {}
    total = 0.0
    for market, selection in group_entries:
        odds = DEFAULT_MARKET_ODDS.get((market, selection))
        if odds and odds > 1:
            value = 1.0 / odds
            implied[(market, selection)] = value
            total += value

    if total <= 0:
        return {}

    return {key: value / total for key, value in implied.items()}


def market_implied_probability(market: str, selection: str) -> tuple[float | None, float | None]:
    odds = DEFAULT_MARKET_ODDS.get((market, selection))
    if market == "DoubleChance":
        one_x_two = _devig_from_group(MARKET_GROUPS["1X2"])
        home_prob = one_x_two.get(("1X2", "Home Win"), 0.0)
        draw_prob = one_x_two.get(("1X2", "Draw"), 0.0)
        away_prob = one_x_two.get(("1X2", "Away Win"), 0.0)
        if selection == "1X":
            return home_prob + draw_prob, odds
        if selection == "X2":
            return away_prob + draw_prob, odds
        if selection == "12":
            return home_prob + away_prob, odds
        return None, odds

    group = MARKET_GROUPS.get(market_group_key(market), ())
    if not group:
        return None, odds

    if len(group) == 1:
        if odds is None or odds <= 1:
            return None, odds
        raw = clamp(1.0 / odds, 0.02, 0.96)
        return raw, odds

    devigged = _devig_from_group(group)
    return devigged.get((market, selection)), odds


def market_blend_weights(data_quality: float, competition: dict | None = None) -> dict:
    if data_quality >= 0.72:
        weights = {"model": 0.75, "market": 0.25}
    elif data_quality >= 0.55:
        weights = {"model": 0.60, "market": 0.40}
    else:
        weights = {"model": 0.45, "market": 0.55}

    if competition:
        shrink = competition.get("calibration_shrink", 0.0)
        weights["model"] = clamp(weights["model"] - shrink, 0.35, 0.80)
        weights["market"] = round(1 - weights["model"], 3)
    return weights


def blend_with_market_probability(market: str, selection: str, model_probability: float, data_quality: float, context: dict | None = None) -> float:
    market_probability, _ = market_implied_probability(market, selection)
    if market_probability is None:
        return round(clamp(model_probability, 0.01, 0.99), 4)

    competition = context.get("competition") if context else None
    weights = market_blend_weights(data_quality, competition)
    blended = (weights["model"] * model_probability) + (weights["market"] * market_probability)
    return round(clamp(blended, 0.01, 0.99), 4)


def calibrate_probability(market: str, selection: str, poisson_probability: float, context: dict) -> float:
    prior = None
    if market == "BTTS":
        prior = context["empirical"]["btts"] if selection == "Yes" else 1 - context["empirical"]["btts"]
    elif market in {"Over1.5", "Under1.5"}:
        prior = context["empirical"]["over15"] if selection == "Over" else 1 - context["empirical"]["over15"]
    elif market in {"Over2.5", "Under2.5"}:
        prior = context["empirical"]["over25"] if selection == "Over" else 1 - context["empirical"]["over25"]
    elif market in {"Over3.5", "Under3.5"}:
        prior = context["empirical"]["over35"] if selection == "Over" else 1 - context["empirical"]["over35"]
    elif market == "HomeGoals0.5":
        prior = context["empirical"]["home_scoring"] if selection == "Over" else 1 - context["empirical"]["home_scoring"]
    elif market == "AwayGoals0.5":
        prior = context["empirical"]["away_scoring"] if selection == "Over" else 1 - context["empirical"]["away_scoring"]

    if prior is None:
        return round(clamp(poisson_probability, 0.01, 0.99), 4)

    data_quality = context["data_quality"]
    class_shrink = context.get("competition", {}).get("calibration_shrink", 0.0)
    poisson_weight = clamp(0.74 - ((1 - data_quality) * 0.12) - class_shrink, 0.48, 0.78)
    blended = (poisson_weight * poisson_probability) + ((1 - poisson_weight) * prior)
    return round(clamp(blended, 0.01, 0.99), 4)


def market_edge(market: str, selection: str, probability: float, data_quality: float) -> tuple[float, float | None]:
    market_probability, odds = market_implied_probability(market, selection)
    if odds is None or market_probability is None:
        return 0.0, odds

    raw_edge = probability - market_probability
    quality_adjusted = raw_edge * (0.65 + (0.35 * data_quality))
    return round(quality_adjusted, 3), odds


def market_is_qualified(market: str, edge: float, data_quality: float) -> bool:
    return edge >= market_threshold(market) and data_quality >= 0.42


def market_stake(market: str, edge: float, data_quality: float) -> float:
    threshold = market_threshold(market)
    if edge < threshold or data_quality < 0.42:
        return 0.0

    surplus = edge - threshold
    stability = market_stability(market)
    raw_stake = (0.9 + (surplus * 16.0)) * data_quality * (0.8 + (0.2 * stability))
    return round(clamp(raw_stake, 0.0, 3.0), 2)


def market_confidence(probability: float, edge: float, data_quality: float, market: str) -> str:
    stability = market_stability(market)
    threshold = market_threshold(market)
    score = (
        0.42 * probability
        + 0.24 * data_quality
        + 0.18 * stability
        + 0.16 * clamp(edge / max(threshold * 2.2, 0.05), 0.0, 1.0)
    )
    if score >= 0.67:
        return "high"
    if score >= 0.52:
        return "medium"
    return "low"
