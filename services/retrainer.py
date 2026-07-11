"""Retrainer service: Learn from prediction outcomes.

This module handles model retraining with real historical prediction results.
It persists evaluations to the database and feeds back outcome data into the
self-learning model.
"""

from datetime import datetime, timedelta
from typing import Optional
import logging

from sqlalchemy.orm import Session
from database.models import Prediction, Fixture
from engine.self_learning_model import train
from services.result_evaluator import evaluate_prediction

logger = logging.getLogger(__name__)


def collect_resolved_predictions(
    db: Session,
    since: Optional[datetime] = None,
    limit: int = 1000,
) -> list[dict]:
    """Collect predictions for resolved fixtures (with known outcomes).
    
    Args:
        db: Database session
        since: Only include predictions created after this timestamp (for incremental retraining)
        limit: Max number of predictions to return
    
    Returns:
        List of dicts with prediction data and match outcomes
    """
    query = (
        db.query(Prediction, Fixture)
        .join(Fixture, Prediction.fixture_id == Fixture.id)
        .filter(Fixture.status == "completed")  # Only resolved matches
        .filter(Fixture.home_goals.isnot(None))  # Has final score
        .filter(Fixture.away_goals.isnot(None))
    )
    
    if since is not None:
        query = query.filter(Prediction.created_at >= since)
    
    query = query.order_by(Prediction.created_at.desc()).limit(limit)
    
    results = []
    for prediction, fixture in query.all():
        results.append({
            "prediction": prediction,
            "fixture": fixture,
            "home_goals": fixture.home_goals,
            "away_goals": fixture.away_goals,
        })
    
    return results


def prepare_training_data(
    db: Session,
    resolved_predictions: list[dict],
) -> tuple[list[list[float]], list[int]]:
    """Prepare training data from resolved predictions.
    
    Args:
        db: Database session
        resolved_predictions: Output from collect_resolved_predictions()
    
    Returns:
        Tuple of (features_list, labels_list) for model training
        
    The model currently uses two features: home avg_scored, away avg_scored.
    Labels: 1 if prediction won, 0 if lost (or correct market selection vs actual outcome).
    """
    X = []  # Features
    y = []  # Labels
    
    for item in resolved_predictions:
        prediction = item["prediction"]
        fixture = item["fixture"]
        
        # Skip predictions without required stats
        if not fixture.home or not fixture.away:
            continue
        
        home_avg = fixture.home.avg_scored or 0.0
        away_avg = fixture.away.avg_scored or 0.0
        
        # Evaluate if this prediction was correct
        # (This depends on your evaluation logic; simplified here)
        evaluation = evaluate_prediction(
            prediction_market=prediction.market,
            prediction_selection=prediction.selection or "over",
            predicted_probability=prediction.probability,
            home_goals=fixture.home_goals or 0,
            away_goals=fixture.away_goals or 0,
        )
        
        # Convert outcome to binary label
        outcome_correct = evaluation.get("outcome") == "won" if evaluation else False
        label = 1 if outcome_correct else 0
        
        X.append([home_avg, away_avg])
        y.append(label)
    
    return X, y


def retrain_from_outcomes(
    db: Session,
    min_samples: int = 20,
    lookback_days: int = 30,
) -> dict:
    """Retrain the self-learning model from real prediction outcomes.
    
    CRITICAL FIX (F2): This replaces the facade that trained on 3 hardcoded dummy rows.
    Now retraining actually uses real resolved predictions from the database.
    
    Args:
        db: Database session
        min_samples: Only retrain if we have at least this many resolved predictions
        lookback_days: Only consider predictions from the last N days
    
    Returns:
        Dict with retraining status and statistics
    """
    logger.info("Starting retraining from prediction outcomes...")
    
    # Only retrain if enough data is available
    since = datetime.utcnow() - timedelta(days=lookback_days)
    resolved = collect_resolved_predictions(db, since=since, limit=10000)
    
    if len(resolved) < min_samples:
        logger.warning(
            f"Insufficient resolved predictions for retraining. "
            f"Found {len(resolved)}, need {min_samples}."
        )
        return {
            "status": "skipped",
            "reason": "insufficient_data",
            "resolved_predictions": len(resolved),
            "minimum_required": min_samples,
        }
    
    # Prepare features and labels
    X, y = prepare_training_data(db, resolved)
    
    if not X or len(set(y)) < 2:
        logger.warning(
            f"Cannot train: insufficient unique labels or empty feature set. "
            f"Samples: {len(X)}, Unique labels: {len(set(y)) if X else 0}"
        )
        return {
            "status": "skipped",
            "reason": "insufficient_training_data",
            "samples": len(X),
            "unique_labels": len(set(y)) if X else 0,
        }
    
    # Train the model
    try:
        model_path = train(X, y)
        logger.info(
            f"Retraining succeeded. Trained on {len(X)} samples, "
            f"label distribution: {len([1 for label in y if label == 1])} wins, "
            f"{len([0 for label in y if label == 0])} losses. Model saved to {model_path}"
        )
        return {
            "status": "success",
            "samples_trained": len(X),
            "wins": len([1 for label in y if label == 1]),
            "losses": len([0 for label in y if label == 0]),
            "model_path": str(model_path),
        }
    except Exception as exc:
        logger.error(f"Retraining failed: {exc}", exc_info=True)
        return {
            "status": "failed",
            "error": str(exc),
        }


def retrain():
    """Legacy entry point for backwards compatibility.
    
    Kept for scripts/tests that call retrain() directly.
    Use retrain_from_outcomes() with a db session for production.
    """
    from database.database import SessionLocal
    db = SessionLocal()
    try:
        return retrain_from_outcomes(db)
    finally:
        db.close()
