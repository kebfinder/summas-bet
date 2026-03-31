def summas_filter(home, away, probs, data_quality=1.0):
    if min(home.matches_played or 0, away.matches_played or 0) < 8:
        return None

    if probs["over15"] < 0.68:
        return None

    score = 0

    if probs["over15"] > 0.8:
        score += 3
    elif probs["over15"] > 0.74:
        score += 2

    if probs.get("over25", 0) > 0.56:
        score += 2

    if probs["btts"] > 0.58:
        score += 2

    if (home.over15_rate or 0) > 0.72 and (away.over15_rate or 0) > 0.72:
        score += 2

    if (home.form or 0) > 0.58:
        score += 1

    if (away.form or 0) > 0.52:
        score += 1

    if data_quality > 0.72:
        score += 1

    return score if score >= 4 else None
