from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.match_model import calibrate_probability


CONTEXT = {
    "data_quality": 0.72,
    "competition": {"calibration_shrink": 0.0},
    "empirical": {
        "btts": 0.55,
        "over15": 0.78,
        "over25": 0.70,
        "over35": 0.32,
        "home_scoring": 0.80,
        "away_scoring": 0.68,
    },
}


def test_under_goal_market_calibrates_against_the_under_prior():
    calibrated = calibrate_probability("Under2.5", "Under", 0.40, CONTEXT)

    # At this quality, the Poisson signal receives a 0.7064 weight. The
    # remaining weight must use the complementary historical under rate (0.30).
    assert calibrated == 0.3706
    assert calibrated < 0.40


def test_team_goal_under_calibrates_against_the_non_scoring_prior():
    calibrated = calibrate_probability("HomeGoals0.5", "Under", 0.35, CONTEXT)

    # The complement of the home-scoring rate is the probability the home team
    # fails to score; it must not reuse the "Over" prior.
    assert calibrated == 0.306
    assert calibrated < 0.35


def test_team_goal_over_retains_the_scoring_prior():
    calibrated = calibrate_probability("HomeGoals0.5", "Over", 0.35, CONTEXT)

    assert calibrated == 0.4821
    assert calibrated > 0.35
