import math


def _poisson_pmf(goals, lam):
    return (lam ** goals * math.exp(-lam)) / math.factorial(goals)


def probabilities(home_xg, away_xg):
    over15 = 0.0
    over25 = 0.0
    btts = 0.0

    for hg in range(8):
        for ag in range(8):
            p = _poisson_pmf(hg, home_xg) * _poisson_pmf(ag, away_xg)

            if hg + ag >= 2:
                over15 += p
            if hg + ag >= 3:
                over25 += p
            if hg > 0 and ag > 0:
                btts += p

    return {
        "over15": over15,
        "over25": over25,
        "btts": btts,
    }
