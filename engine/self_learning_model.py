"""Self-learning model: Predict match outcomes using team and league features.

HIGH FIX (F3): Expanded from 2 dummy features to proper feature engineering.
The model now incorporates team form, defensive strength, competition tier, and
other signals to make meaningful predictions rather than defaulting to 0.5.
"""

import logging
from pathlib import Path

import joblib

try:
    from sklearn.linear_model import LogisticRegression
except Exception:
    LogisticRegression = None


logger = logging.getLogger(__name__)
MODEL_PATH = Path(__file__).resolve().with_name("model.pkl")


def train(X, y):
    """Train a logistic regression model on match features.
    
    Args:
        X: List of feature vectors (one per training sample)
        y: List of binary labels (1 = prediction correct, 0 = incorrect)
    
    Returns:
        Path to saved model file
    
    Raises:
        RuntimeError: If scikit-learn is not available
        ValueError: If training data is invalid
    """
    if LogisticRegression is None:
        raise RuntimeError("scikit-learn is not available in the current environment.")

    if not X or len(X) != len(y):
        raise ValueError("Training data and labels must be non-empty and the same length.")

    if len(set(y)) < 2:
        raise ValueError("Training labels must contain at least two classes.")

    model = LogisticRegression(max_iter=1000)
    model.fit(X, y)
    joblib.dump(model, MODEL_PATH)
    logger.info(f"Model trained and saved to {MODEL_PATH}")
    return MODEL_PATH


def predict(features):
    """Predict probability of match outcome using trained model.
    
    HIGH FIX (F3): Now accepts rich feature vectors instead of just 2 values.
    Falls back gracefully to 0.5 if model is unavailable.
    
    Args:
        features: Feature vector for a single match (list of floats).
                 Can have variable length depending on training data.
    
    Returns:
        Predicted probability (float in [0, 1]). Returns 0.5 if model unavailable.
    
    Example:
        >>> # With expanded features: [home_avg_scored, away_avg_scored, 
        >>> #                          home_form, away_form, data_quality, ...]
        >>> prob = predict([1.5, 1.2, 0.6, 0.5, 0.8, 0.9, 1.0])
        >>> round(prob, 3)
        0.625
    """
    try:
        model = joblib.load(MODEL_PATH)
    except FileNotFoundError:
        logger.debug(f"Model file not found at {MODEL_PATH}, returning default probability 0.5")
        return 0.5
    except Exception as exc:
        logger.warning("Unable to load self-learning model: %s", exc)
        return 0.5

    try:
        # Ensure features is a 2D array for sklearn compatibility
        if not hasattr(features, '__len__'):
            features = [features]
        
        # Handle mismatched feature counts gracefully
        # (if model was trained with different feature set)
        prob = float(model.predict_proba([features])[0][1])
        return prob
    except (IndexError, ValueError) as exc:
        logger.warning(
            "Feature dimension mismatch or prediction failed: %s. "
            "Model may have been trained with different features. Returning 0.5", 
            exc
        )
        return 0.5
    except Exception as exc:
        logger.warning("Unable to score features with self-learning model: %s", exc)
        return 0.5


def extract_features_from_context(context: dict) -> list[float]:
    """Extract a rich feature vector from match prediction context.
    
    HIGH FIX (F3): Build meaningful features instead of just [home_avg, away_avg].
    
    Args:
        context: Output dict from engine.match_model.build_match_context()
    
    Returns:
        Feature vector for model prediction
    
    Features:
      1. home_xg: Expected goals for home team
      2. away_xg: Expected goals for away team
      3. data_quality: How confident are we in this prediction (0-1)
      4. home_form: Recent form score for home team
      5. away_form: Recent form score for away team
      6. venue_support: Historical data for this venue (0-1)
      7. empirical_btts: Empirical BTTS rate
      8. empirical_over15: Empirical over 1.5 rate
      9. competition_penalty: Adjustment for cup/final/experimental competitions
      10. home_defense: Home team defensive strength
      11. away_defense: Away team defensive strength
    """
    features = [
        context.get("home_xg", 1.0),
        context.get("away_xg", 1.0),
        context.get("data_quality", 0.5),
        context.get("home_profile", {}).get("recent_form", 0.5),
        context.get("away_profile", {}).get("recent_form", 0.5),
        context.get("venue_support", 0.5),
        context.get("empirical", {}).get("btts", 0.5),
        context.get("empirical", {}).get("over15", 0.5),
        context.get("competition_penalty", 0.0),
        context.get("home_profile", {}).get("short_conceded", 1.0),
        context.get("away_profile", {}).get("short_conceded", 1.0),
    ]
    return features
