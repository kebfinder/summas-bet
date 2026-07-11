from engine.features import expected_goals
from engine.match_model import build_match_context, calibrate_probability
from engine.poisson_model import probabilities
from engine.summas_filters import summas_filter
from engine.self_learning_model import predict, extract_features_from_context
from sqlalchemy.orm import object_session
from utils.football import clamp

def run_engine(fixture):
    """Run the complete prediction engine for a fixture.
    
    Args:
        fixture: Fixture object with home, away, date, league attributes
    
    Returns:
        Dict with prediction result or None if filtered out
    """

    home = fixture.home
    away = fixture.away
    db = object_session(fixture)

    if db is not None and home and away:
        context = build_match_context(db, fixture)
        home_xg, away_xg = context["home_xg"], context["away_xg"]
    else:
        home_xg, away_xg = expected_goals(home, away)
        context = {
            "data_quality": clamp(min(home.matches_played or 0, away.matches_played or 0) / 16.0, 0.25, 1.0),
            "empirical": {
                "btts": clamp((home.btts_rate + away.btts_rate) / 2.0, 0.08, 0.92),
                "over15": clamp((home.over15_rate + away.over15_rate) / 2.0, 0.15, 0.97),
                "over25": clamp((home.over25_rate + away.over25_rate) / 2.0, 0.08, 0.90),
            },
        }

    probs = probabilities(home_xg, away_xg)
    probs["over15"] = calibrate_probability("Over1.5", "Over", probs["over15"], context)
    probs["over25"] = calibrate_probability("Over2.5", "Over", probs["over25"], context)
    probs["btts"] = calibrate_probability("BTTS", "Yes", probs["btts"], context)

    score = summas_filter(home, away, probs, data_quality=context["data_quality"])

    if not score:
        return None

    # HIGH FIX (F3): Use rich feature extraction instead of 2 dummy features
    # Extract meaningful features from the full prediction context
    features = extract_features_from_context(context)
    ml_prob = predict(features) if min(home.matches_played or 0, away.matches_played or 0) >= 8 else 0.5

    final_prob = (0.75 * probs["over15"]) + (0.15 * probs["btts"]) + (0.10 * ml_prob)

    return {
        "match": f"{home.name} vs {away.name}",
        "market": "Over 1.5",
        "probability": round(final_prob, 3),
        "score": score
    }


def get_worldcup2026_live_data():
    """Return normalized World Cup 2026 live data for engine consumers.

    Kept as a lazy import so the core prediction engine can still start in
    environments before optional data-layer dependencies are installed.
    """
    from engine.worldcup2026_data_layer import get_live_data

    return get_live_data()
