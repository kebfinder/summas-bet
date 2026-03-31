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
    if LogisticRegression is None:
        raise RuntimeError("scikit-learn is not available in the current environment.")

    if not X or len(X) != len(y):
        raise ValueError("Training data and labels must be non-empty and the same length.")

    if len(set(y)) < 2:
        raise ValueError("Training labels must contain at least two classes.")

    model = LogisticRegression(max_iter=1000)
    model.fit(X, y)
    joblib.dump(model, MODEL_PATH)
    return MODEL_PATH


def predict(features):
    try:
        model = joblib.load(MODEL_PATH)
    except FileNotFoundError:
        return 0.5
    except Exception as exc:
        logger.warning("Unable to load self-learning model: %s", exc)
        return 0.5

    try:
        return float(model.predict_proba([features])[0][1])
    except Exception as exc:
        logger.warning("Unable to score features with self-learning model: %s", exc)
        return 0.5
