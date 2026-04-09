from __future__ import annotations

from datetime import date as calendar_date, datetime, timedelta
import re
import unicodedata


def normalize_team_name(name: str | None) -> str:
    if not name:
        return ""
    return " ".join(str(name).split())


_TEAM_STOPWORDS = {
    "fc",
    "cf",
    "sc",
    "ac",
    "afc",
    "club",
    "de",
    "the",
}


def normalize_team_identity(name: str | None) -> str:
    normalized = normalize_team_name(name).lower()
    if not normalized:
        return ""
    folded = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode("ascii")
    folded = re.sub(r"\(.*?\)", " ", folded)
    folded = re.sub(r"[^a-z0-9\s]", " ", folded)
    tokens = [token for token in folded.split() if token and token not in _TEAM_STOPWORDS]
    return " ".join(tokens)


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def weighted_average(values: list[float], fallback: float = 0.0, decay: float = 0.9) -> float:
    if not values:
        return fallback

    weighted_sum = 0.0
    total_weight = 0.0
    weight = 1.0

    for value in values:
        weighted_sum += float(value) * weight
        total_weight += weight
        weight *= decay

    return weighted_sum / total_weight if total_weight else fallback


def parse_match_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None

    match_date = value
    if not isinstance(match_date, datetime):
        raw_value = str(value).strip()
        if not raw_value:
            return None
        match_date = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))

    if match_date.tzinfo is not None:
        match_date = match_date.astimezone().replace(tzinfo=None)

    return match_date


def parse_match_day(value: str | calendar_date | datetime | None) -> calendar_date | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, calendar_date):
        return value

    raw_value = str(value).strip()
    if not raw_value:
        return None

    return calendar_date.fromisoformat(raw_value[:10])


def day_bounds(reference: datetime | None = None, days_ahead: int = 0) -> tuple[datetime, datetime]:
    current = reference or datetime.now().astimezone().replace(tzinfo=None)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=max(days_ahead, 0) + 1)
    return start, end


def day_bounds_for_date(target_date: calendar_date | datetime | str) -> tuple[datetime, datetime]:
    match_day = parse_match_day(target_date)
    if match_day is None:
        raise ValueError("A valid match date is required")

    start = datetime(match_day.year, match_day.month, match_day.day)
    end = start + timedelta(days=1)
    return start, end


def within_day_window(match_date: datetime | None, start: datetime, end: datetime) -> bool:
    return match_date is not None and start <= match_date < end


def current_season_year(reference: datetime | None = None, season_start_month: int = 7) -> str:
    current = reference or datetime.now()
    season_year = current.year if current.month >= season_start_month else current.year - 1
    return str(season_year)


_STATUS_MAP = {
    "scheduled": "scheduled",
    "timed": "scheduled",
    "not started": "scheduled",
    "ns": "scheduled",
    "postponed": "postponed",
    "suspended": "postponed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "abgesagt": "cancelled",
    "live": "live",
    "in_play": "live",
    "in play": "live",
    "running": "live",
    "finished": "finished",
    "ft": "finished",
    "full-time": "finished",
}


def normalize_status(raw_status: str | None, *, is_finished: bool | None = None) -> str:
    if is_finished:
        return "finished"

    normalized = str(raw_status or "").strip().lower()
    if not normalized:
        return "scheduled"

    return _STATUS_MAP.get(normalized, normalized)


def _points(goals_for: int, goals_against: int) -> int:
    if goals_for > goals_against:
        return 3
    if goals_for == goals_against:
        return 1
    return 0


def calculate_team_metrics(
    team_id: int,
    home_matches: list,
    away_matches: list,
    baseline_goals: float = 1.25,
) -> dict | None:
    all_matches = []
    for match in home_matches:
        all_matches.append(
            {
                "date": match.date,
                "goals_for": match.home_goals or 0,
                "goals_against": match.away_goals or 0,
                "btts": bool(match.btts),
                "over15": bool(match.over_1_5),
                "over25": bool(match.over_2_5),
            }
        )

    for match in away_matches:
        all_matches.append(
            {
                "date": match.date,
                "goals_for": match.away_goals or 0,
                "goals_against": match.home_goals or 0,
                "btts": bool(match.btts),
                "over15": bool(match.over_1_5),
                "over25": bool(match.over_2_5),
            }
        )

    all_matches.sort(key=lambda item: item["date"], reverse=True)
    total_matches = len(all_matches)
    if total_matches == 0:
        return None

    goals_for_values = [match["goals_for"] for match in all_matches]
    goals_against_values = [match["goals_against"] for match in all_matches]
    btts_values = [1.0 if match["btts"] else 0.0 for match in all_matches]
    over15_values = [1.0 if match["over15"] else 0.0 for match in all_matches]
    over25_values = [1.0 if match["over25"] else 0.0 for match in all_matches]
    form_values = [
        _points(match["goals_for"], match["goals_against"]) / 3.0
        for match in all_matches
    ]

    total_scored = int(sum(goals_for_values))
    total_conceded = int(sum(goals_against_values))

    avg_scored = max(weighted_average(goals_for_values, fallback=baseline_goals, decay=0.92), 0.1)
    avg_conceded = max(weighted_average(goals_against_values, fallback=baseline_goals, decay=0.92), 0.1)
    baseline = baseline_goals if baseline_goals > 0 else 1.25

    return {
        "avg_scored": round(avg_scored, 3),
        "avg_conceded": round(avg_conceded, 3),
        "attack_strength": round(avg_scored / baseline, 3),
        "defense_strength": round(avg_conceded / baseline, 3),
        "btts_rate": round(weighted_average(btts_values, fallback=0.5, decay=0.92), 3),
        "over15_rate": round(weighted_average(over15_values, fallback=0.7, decay=0.92), 3),
        "over25_rate": round(weighted_average(over25_values, fallback=0.5, decay=0.92), 3),
        "form": round(weighted_average(form_values, fallback=0.5, decay=0.92), 3),
        "matches_played": total_matches,
        "total_goals_scored": total_scored,
        "total_goals_conceded": total_conceded,
    }
