# scrapers/statsbomb_scraper.py
import logging

import requests

from database.database import SessionLocal
from database.models import HistoricalMatch, Team
from utils.football import normalize_team_name, parse_match_datetime


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class StatsBombScraper:
    """
    StatsBomb Open Data - Free, no API key required.
    High-quality event data for 30+ competitions.
    https://github.com/statsbomb/open-data
    """

    BASE_URL = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"

    def __init__(self):
        self.db = SessionLocal()

    def close(self):
        self.db.close()

    def get_competitions(self):
        """Get available competitions."""
        try:
            response = requests.get(f"{self.BASE_URL}/competitions.json", timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.error("Error fetching competitions: %s", exc)
            return []

    def get_matches(self, competition_id, season_id):
        """Get matches for a competition/season."""
        try:
            url = f"{self.BASE_URL}/matches/{competition_id}/{season_id}.json"
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.error("Error fetching matches: %s", exc)
            return []

    def scrape_competition(self, competition_id, season_id):
        """Scrape and save all matches from a competition."""
        matches = self.get_matches(competition_id, season_id)
        saved = 0

        for match in matches:
            try:
                home_name = normalize_team_name(match.get("home_team", {}).get("home_team_name"))
                away_name = normalize_team_name(match.get("away_team", {}).get("away_team_name"))
                if not home_name or not away_name or home_name == away_name:
                    continue

                match_date = parse_match_datetime(match.get("match_date"))
                if match_date is None:
                    continue

                home_score = int(match.get("home_score", 0))
                away_score = int(match.get("away_score", 0))

                home = self._get_or_create_team(home_name)
                away = self._get_or_create_team(away_name)

                existing = (
                    self.db.query(HistoricalMatch)
                    .filter(
                        HistoricalMatch.home_id == home.id,
                        HistoricalMatch.away_id == away.id,
                        HistoricalMatch.date == match_date,
                    )
                    .first()
                )
                if existing:
                    continue

                hist = HistoricalMatch(
                    home_id=home.id,
                    away_id=away.id,
                    date=match_date,
                    home_goals=home_score,
                    away_goals=away_score,
                    btts=(home_score > 0 and away_score > 0),
                    over_2_5=(home_score + away_score) > 2.5,
                    over_1_5=(home_score + away_score) > 1.5,
                )
                self.db.add(hist)
                saved += 1

            except Exception as exc:
                logger.error("Error processing match: %s", exc)

        self.db.commit()
        logger.info("Saved %s matches from StatsBomb", saved)
        return saved

    def _get_or_create_team(self, name):
        normalized_name = normalize_team_name(name)
        team = self.db.query(Team).filter_by(name=normalized_name).first()
        if not team:
            team = Team(name=normalized_name)
            self.db.add(team)
            self.db.flush()
        return team
