from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from config import RECENT_MATCH_SAMPLE_SIZE
from database.models import Fixture, HistoricalMatch


@dataclass
class TeamRecentMetrics:
    sample_size: int
    form_sequence: str
    points_per_match: Optional[float]
    avg_scored: Optional[float]
    avg_conceded: Optional[float]
    over_1_5_rate: Optional[float]
    over_2_5_rate: Optional[float]
    btts_rate: Optional[float]


REQUIRED_ENRICHMENT_FIELDS = (
    "home_recent_form",
    "away_recent_form",
    "home_avg_goals_scored_recent",
    "home_avg_goals_conceded_recent",
    "away_avg_goals_scored_recent",
    "away_avg_goals_conceded_recent",
    "over_1_5_rate",
    "over_2_5_rate",
    "btts_rate",
)


def _safe_rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 3)


def _recent_team_matches(db: Session, team_id: Optional[int], before_dt: Optional[datetime], sample_size: int) -> List[HistoricalMatch]:
    if team_id is None:
        return []

    query = db.query(HistoricalMatch).filter(
        or_(HistoricalMatch.home_id == team_id, HistoricalMatch.away_id == team_id)
    )
    if before_dt is not None:
        query = query.filter(HistoricalMatch.date < before_dt)

    return query.order_by(HistoricalMatch.date.desc()).limit(sample_size).all()


def _team_metrics(db: Session, team_id: Optional[int], before_dt: Optional[datetime], sample_size: int) -> TeamRecentMetrics:
    matches = _recent_team_matches(db, team_id, before_dt, sample_size)
    if not matches:
        return TeamRecentMetrics(0, "", None, None, None, None, None, None)

    sequence: List[str] = []
    points = 0
    goals_for = 0
    goals_against = 0
    over_15_count = 0
    over_25_count = 0
    btts_count = 0

    for match in matches:
        is_home = match.home_id == team_id
        team_goals = int(match.home_goals if is_home else match.away_goals)
        opp_goals = int(match.away_goals if is_home else match.home_goals)

        goals_for += team_goals
        goals_against += opp_goals

        total_goals = int(match.home_goals) + int(match.away_goals)
        if total_goals > 1:
            over_15_count += 1
        if total_goals > 2:
            over_25_count += 1
        if int(match.home_goals) > 0 and int(match.away_goals) > 0:
            btts_count += 1

        if team_goals > opp_goals:
            sequence.append("W")
            points += 3
        elif team_goals == opp_goals:
            sequence.append("D")
            points += 1
        else:
            sequence.append("L")

    sample = len(matches)
    return TeamRecentMetrics(
        sample_size=sample,
        form_sequence="".join(sequence),
        points_per_match=round(points / sample, 3),
        avg_scored=round(goals_for / sample, 3),
        avg_conceded=round(goals_against / sample, 3),
        over_1_5_rate=_safe_rate(over_15_count, sample),
        over_2_5_rate=_safe_rate(over_25_count, sample),
        btts_rate=_safe_rate(btts_count, sample),
    )


def _h2h_summary(db: Session, home_id: Optional[int], away_id: Optional[int], before_dt: Optional[datetime], limit: int = 5) -> Optional[dict]:
    if home_id is None or away_id is None:
        return None

    query = db.query(HistoricalMatch).filter(
        or_(
            (HistoricalMatch.home_id == home_id) & (HistoricalMatch.away_id == away_id),
            (HistoricalMatch.home_id == away_id) & (HistoricalMatch.away_id == home_id),
        )
    )
    if before_dt is not None:
        query = query.filter(HistoricalMatch.date < before_dt)

    matches = query.order_by(HistoricalMatch.date.desc()).limit(limit).all()
    if not matches:
        return None

    home_wins = away_wins = draws = 0
    total_goals = 0
    for match in matches:
        if match.home_id == home_id:
            home_goals, away_goals = int(match.home_goals), int(match.away_goals)
        else:
            home_goals, away_goals = int(match.away_goals), int(match.home_goals)

        total_goals += home_goals + away_goals
        if home_goals > away_goals:
            home_wins += 1
        elif home_goals < away_goals:
            away_wins += 1
        else:
            draws += 1

    return {
        "matches": len(matches),
        "home_wins": home_wins,
        "away_wins": away_wins,
        "draws": draws,
        "avg_total_goals": round(total_goals / len(matches), 3),
    }


def enrich_fixture(db: Session, fixture: Fixture, sample_size: int = RECENT_MATCH_SAMPLE_SIZE) -> dict:
    before_dt = fixture.date
    home_metrics = _team_metrics(db, fixture.home_id, before_dt, sample_size)
    away_metrics = _team_metrics(db, fixture.away_id, before_dt, sample_size)

    over_15_values = [value for value in (home_metrics.over_1_5_rate, away_metrics.over_1_5_rate) if value is not None]
    over_25_values = [value for value in (home_metrics.over_2_5_rate, away_metrics.over_2_5_rate) if value is not None]
    btts_values = [value for value in (home_metrics.btts_rate, away_metrics.btts_rate) if value is not None]

    payload = {
        "fixture_id": fixture.id,
        "fixture_identity": {
            "competition": fixture.league,
            "home_team": fixture.home.name if fixture.home else None,
            "away_team": fixture.away.name if fixture.away else None,
            "kickoff": fixture.date.isoformat() if fixture.date else None,
        },
        "home_recent_form": {
            "sequence": home_metrics.form_sequence,
            "sample_size": home_metrics.sample_size,
            "points_per_match": home_metrics.points_per_match,
        },
        "away_recent_form": {
            "sequence": away_metrics.form_sequence,
            "sample_size": away_metrics.sample_size,
            "points_per_match": away_metrics.points_per_match,
        },
        "home_avg_goals_scored_recent": home_metrics.avg_scored,
        "home_avg_goals_conceded_recent": home_metrics.avg_conceded,
        "away_avg_goals_scored_recent": away_metrics.avg_scored,
        "away_avg_goals_conceded_recent": away_metrics.avg_conceded,
        "over_1_5_rate": round(sum(over_15_values) / len(over_15_values), 3) if over_15_values else None,
        "over_2_5_rate": round(sum(over_25_values) / len(over_25_values), 3) if over_25_values else None,
        "btts_rate": round(sum(btts_values) / len(btts_values), 3) if btts_values else None,
        "h2h_summary": _h2h_summary(db, fixture.home_id, fixture.away_id, before_dt=before_dt),
    }

    missing_fields = [field for field in REQUIRED_ENRICHMENT_FIELDS if payload.get(field) is None]
    if len(missing_fields) == len(REQUIRED_ENRICHMENT_FIELDS):
        completeness = "missing"
    elif missing_fields:
        completeness = "partial"
    else:
        completeness = "full"

    payload["enrichment_completeness"] = completeness
    payload["missing_fields"] = missing_fields
    return payload


def enrich_fixtures(db: Session, fixtures: Iterable[Fixture], sample_size: int = RECENT_MATCH_SAMPLE_SIZE) -> Dict[int, dict]:
    enriched: Dict[int, dict] = {}
    for fixture in fixtures or []:
        if fixture is None or fixture.id is None:
            continue
        enriched[fixture.id] = enrich_fixture(db, fixture, sample_size=sample_size)
    return enriched


def summarize_enrichment(enriched_by_fixture_id: Dict[int, dict]) -> dict:
    items = list((enriched_by_fixture_id or {}).values())
    counter = Counter(item.get("enrichment_completeness", "missing") for item in items)
    missing_counter = Counter()
    for item in items:
        for field in item.get("missing_fields", []):
            missing_counter[field] += 1

    return {
        "total_fixtures_inspected": len(items),
        "fully_enriched_count": counter.get("full", 0),
        "partially_enriched_count": counter.get("partial", 0),
        "missing_enrichment_count": counter.get("missing", 0),
        "common_missing_fields": [
            {"field": field, "count": count}
            for field, count in missing_counter.most_common(10)
        ],
    }
