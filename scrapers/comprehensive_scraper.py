from config import DEFAULT_LOOKAHEAD_DAYS
from scrapers.football_data_api import FootballDataAPI
from scrapers.openligadb_scraper import OpenLigaDBScraper


def scrape_today(
    source: str = "openligadb",
    api_key: str | None = None,
    days_ahead: int = DEFAULT_LOOKAHEAD_DAYS,
):
    normalized_source = (source or "openligadb").strip().lower()

    if normalized_source == "openligadb":
        scraper = OpenLigaDBScraper()
        try:
            return scraper.scrape_todays_matches(days_ahead=days_ahead)
        finally:
            scraper.close()

    if normalized_source in {"footballdata", "football-data", "football_data"}:
        scraper = FootballDataAPI(api_key)
        try:
            return scraper.get_matches_today(days_ahead=days_ahead)
        finally:
            scraper.close()

    raise ValueError(f"Unsupported source: {source}")
