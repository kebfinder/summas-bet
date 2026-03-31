from config import DEFAULT_LOOKAHEAD_DAYS
from engine.summas_engine import run_engine
from scrapers.comprehensive_scraper import scrape_today


def run_pipeline(
    source: str = "openligadb",
    api_key: str | None = None,
    days_ahead: int = DEFAULT_LOOKAHEAD_DAYS,
):
    fixtures = scrape_today(source=source, api_key=api_key, days_ahead=days_ahead)
    results = []

    for fixture in fixtures:
        result = run_engine(fixture)
        if result:
            results.append(result)

    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:3]
