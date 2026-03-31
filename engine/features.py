from utils.football import clamp


def expected_goals(home, away):
    home_xg = (
        (0.45 * (home.avg_scored or 1.25))
        + (0.30 * (away.avg_conceded or 1.25))
        + (0.15 * (1.4 + ((home.form or 0.5) - 0.5)))
        + (0.10 * (1.0 + (home.over15_rate or 0.7)))
    )
    away_xg = (
        (0.45 * (away.avg_scored or 1.25))
        + (0.30 * (home.avg_conceded or 1.25))
        + (0.15 * (1.15 + ((away.form or 0.5) - 0.5)))
        + (0.10 * (0.95 + (away.over15_rate or 0.7)))
    )
    return clamp(home_xg, 0.55, 3.0), clamp(away_xg, 0.45, 2.8)
