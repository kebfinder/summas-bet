from config import DEFAULT_LOOKAHEAD_DAYS
from engine.summas_engine import run_engine
from scrapers.comprehensive_scraper import scrape_today
from services.retrainer import retrain_from_outcomes
from database.database import SessionLocal
import logging

logger = logging.getLogger(__name__)


def run_pipeline(
    source: str = "openligadb",
    api_key: str | None = None,
    days_ahead: int = DEFAULT_LOOKAHEAD_DAYS,
    retrain: bool = True,
):
    """Run the full prediction pipeline: scrape, predict, and optionally retrain.
    
    Args:
        source: Data source for fixtures (e.g., "openligadb", "footballdata")
        api_key: API key if required by source
        days_ahead: Number of days to look ahead
        retrain: If True, retrain the model from resolved predictions after generating predictions
    
    Returns:
        List of top 3 predictions sorted by score
    """
    fixtures = scrape_today(source=source, api_key=api_key, days_ahead=days_ahead)
    results = []

    for fixture in fixtures:
        result = run_engine(fixture)
        if result:
            results.append(result)

    results.sort(key=lambda item: item["score"], reverse=True)
    top_results = results[:3]
    
    # CRITICAL FIX (F2): Call retrain_from_outcomes after prediction to enable feedback loop
    # This was missing in the original implementation, breaking the learning mechanism
    if retrain:
        try:
            db = SessionLocal()
            try:
                retrain_status = retrain_from_outcomes(db, min_samples=10, lookback_days=30)
                logger.info(f"Retraining result: {retrain_status}")
            finally:
                db.close()
        except Exception as exc:
            logger.error(f"Retraining failed in pipeline: {exc}", exc_info=True)
    
    return top_results
