from sqlalchemy.orm import Session

from config import LEAGUE_AVG_GOALS, RECENT_MATCH_SAMPLE_SIZE
from database.models import HistoricalMatch, Team
from utils.football import calculate_team_metrics


def refresh_team_statistics(
    db: Session,
    sample_size: int = RECENT_MATCH_SAMPLE_SIZE,
) -> int:
    updated = 0
    match_limit = max(sample_size, 1)

    for team in db.query(Team).all():
        home_matches = (
            db.query(HistoricalMatch)
            .filter(HistoricalMatch.home_id == team.id)
            .order_by(HistoricalMatch.date.desc())
            .limit(match_limit)
            .all()
        )
        away_matches = (
            db.query(HistoricalMatch)
            .filter(HistoricalMatch.away_id == team.id)
            .order_by(HistoricalMatch.date.desc())
            .limit(match_limit)
            .all()
        )

        metrics = calculate_team_metrics(
            team.id,
            home_matches,
            away_matches,
            baseline_goals=LEAGUE_AVG_GOALS,
        )
        if not metrics:
            continue

        for field, value in metrics.items():
            setattr(team, field, value)

        updated += 1

    db.commit()
    return updated
