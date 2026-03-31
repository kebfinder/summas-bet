# scrapers/football_data_api.py
import logging
import os
from datetime import timedelta

import requests

from config import DEFAULT_LOOKAHEAD_DAYS
from database.database import SessionLocal
from database.models import Fixture, Team
from utils.competitions import canonical_display_name, is_competition_admitted, resolve_competition
from utils.football import (
    day_bounds,
    day_bounds_for_date,
    normalize_status,
    normalize_team_name,
    parse_match_datetime,
    within_day_window,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class FootballDataAPI:
    """
    Football-Data.org API wrapper.
    Free tier: 10 requests/minute
    Get API key at: https://www.football-data.org/client/register
    """

    BASE_URL = "https://api.football-data.org/v4"
    _accessible_codes_cache = {}

    def __init__(self, api_key=None):
        self.db = SessionLocal()
        self.api_key = api_key or os.getenv("FOOTBALL_DATA_API_KEY")
        self.headers = {}
        self.session = requests.Session()
        self.session.trust_env = False
        if self.api_key:
            self.headers["X-Auth-Token"] = self.api_key

    def close(self):
        self.session.close()
        self.db.close()

    def get_competitions(self):
        """Get available competitions."""
        try:
            response = self.session.get(
                f"{self.BASE_URL}/competitions",
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
            return response.json().get("competitions", [])
        except Exception as exc:
            logger.error("Error fetching competitions: %s", exc)
            return []

    def get_accessible_competition_codes(self):
        cache_key = self.api_key or "__no_key__"
        cached = self._accessible_codes_cache.get(cache_key)
        if cached is not None:
            return cached

        competitions = self.get_competitions()
        codes = {
            str(item.get("code") or "").strip().upper()
            for item in competitions
            if str(item.get("code") or "").strip()
        }
        self._accessible_codes_cache[cache_key] = codes
        return codes

    def get_matches_today(self, days_ahead: int = DEFAULT_LOOKAHEAD_DAYS, target_date=None):
        """Get matches in the configured day window."""
        try:
            start, end = day_bounds_for_date(target_date) if target_date is not None else day_bounds(days_ahead=days_ahead)
            params = {
                "dateFrom": start.strftime("%Y-%m-%d"),
                "dateTo": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
            }
            response = self.session.get(
                f"{self.BASE_URL}/matches",
                headers=self.headers,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            return self._process_matches(data.get("matches", []), start, end)
        except Exception as exc:
            logger.error("Error fetching today's matches: %s", exc)
            return []

    def get_matches_by_competition(self, competition_code, days_ahead: int = DEFAULT_LOOKAHEAD_DAYS, target_date=None):
        """
        Get matches for a specific competition.
        Codes: PL (Premier League), BL1 (Bundesliga), SA (Serie A),
               PD (La Liga), FL1 (Ligue 1), CL (Champions League)
        """
        try:
            start, end = day_bounds_for_date(target_date) if target_date is not None else day_bounds(days_ahead=days_ahead)
            response = self.session.get(
                f"{self.BASE_URL}/competitions/{competition_code}/matches",
                headers=self.headers,
                params={
                    "dateFrom": start.strftime("%Y-%m-%d"),
                    "dateTo": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            return self._process_matches(data.get("matches", []), start, end)
        except Exception as exc:
            logger.error("Error fetching competition matches: %s", exc)
            return []

    def get_matches_for_competitions(
        self,
        competition_codes,
        days_ahead: int = DEFAULT_LOOKAHEAD_DAYS,
        target_date=None,
    ):
        """Get matches for a filtered competition set inside the requested day window."""
        competition_codes = [str(code).strip().upper() for code in competition_codes or [] if str(code).strip()]
        if not competition_codes:
            return self.get_matches_today(days_ahead=days_ahead, target_date=target_date)

        accessible_codes = self.get_accessible_competition_codes()
        if accessible_codes:
            competition_codes = [code for code in competition_codes if code in accessible_codes]
        if not competition_codes:
            logger.info("No requested football-data competitions are accessible to this API token.")
            return []

        combined = {}
        for competition_code in competition_codes:
            try:
                matches = self.get_matches_by_competition(
                    competition_code,
                    days_ahead=days_ahead,
                    target_date=target_date,
                )
                for fixture in matches:
                    identity = fixture.id if fixture is not None else None
                    if identity:
                        combined[identity] = fixture
            except Exception as exc:
                logger.error("Error fetching %s competition matches: %s", competition_code, exc)

        return list(combined.values())

    def _process_matches(self, matches, start, end):
        """Process API matches into database fixtures."""
        fixtures = []

        for match in matches:
            try:
                home_name = normalize_team_name(match.get("homeTeam", {}).get("name"))
                away_name = normalize_team_name(match.get("awayTeam", {}).get("name"))
                if not home_name or not away_name or home_name == away_name:
                    continue

                raw_competition = match.get("competition", {}) or {}
                raw_code = raw_competition.get("code")
                raw_name = raw_competition.get("name", "Unknown")
                if not is_competition_admitted("footballdata", code=raw_code, name=raw_name):
                    continue

                competition = resolve_competition("footballdata", code=raw_code, name=raw_name)
                league = canonical_display_name("footballdata", code=raw_code, name=raw_name)
                match_date = parse_match_datetime(match.get("utcDate"))
                if not within_day_window(match_date, start, end):
                    continue

                home_team = self._get_or_create_team(home_name, competition or league)
                away_team = self._get_or_create_team(away_name, competition or league)

                day_start = match_date.replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = day_start + timedelta(days=1)
                raw_status = str(match.get("status", "SCHEDULED"))
                status = normalize_status(raw_status, is_finished=raw_status.upper() == "FINISHED")

                existing = (
                    self.db.query(Fixture)
                    .filter(
                        Fixture.home_id == home_team.id,
                        Fixture.away_id == away_team.id,
                        Fixture.date >= day_start,
                        Fixture.date < day_end,
                    )
                    .first()
                )

                if existing:
                    existing.date = match_date
                    existing.status = status
                    existing.league = league
                    self.db.commit()
                    self.db.refresh(existing)
                    fixtures.append(existing)
                    continue

                fixture = Fixture(
                    date=match_date,
                    home_id=home_team.id,
                    away_id=away_team.id,
                    league=league,
                    status=status,
                )
                self.db.add(fixture)
                self.db.commit()
                self.db.refresh(fixture)
                fixtures.append(fixture)
                logger.info("Saved: %s vs %s", home_name, away_name)

            except Exception as exc:
                logger.error("Error processing match: %s", exc)
                self.db.rollback()

        return fixtures

    def _get_or_create_team(self, name, competition):
        """Get or create a normalized team."""
        normalized_name = normalize_team_name(name)
        team = self.db.query(Team).filter_by(name=normalized_name).first()
        league = competition.display_name if hasattr(competition, "display_name") else str(competition or "Unknown")
        country = getattr(competition, "country", None) or self._extract_country(league)

        if not team:
            team = Team(
                name=normalized_name,
                league=league,
                country=country,
                avg_scored=1.25,
                avg_conceded=1.25,
                attack_strength=1.0,
                defense_strength=1.0,
            )
            self.db.add(team)
            self.db.flush()
            return team

        if league and (not team.league or team.league == "Unknown"):
            team.league = league
        if country != "Unknown" and (not team.country or team.country == "Unknown"):
            team.country = country

        return team

    def _extract_country(self, league):
        """Extract country from league name."""
        league_lower = str(league or "").lower()
        mapping = {
            "premier league": "England",
            "bundesliga": "Germany",
            "serie a": "Italy",
            "la liga": "Spain",
            "ligue 1": "France",
            "champions league": "Europe",
            "europa league": "Europe",
        }
        for key, country in mapping.items():
            if key in league_lower:
                return country
        return "Unknown"


def scrape_today_footballdata(api_key=None, days_ahead: int = DEFAULT_LOOKAHEAD_DAYS, target_date=None):
    """Scrape upcoming matches using Football-Data.org."""
    api = FootballDataAPI(api_key)
    try:
        return api.get_matches_today(days_ahead=days_ahead, target_date=target_date)
    finally:
        api.close()


def scrape_competition(competition_code, api_key=None, days_ahead: int = DEFAULT_LOOKAHEAD_DAYS):
    """Scrape a specific competition."""
    api = FootballDataAPI(api_key)
    try:
        return api.get_matches_by_competition(competition_code, days_ahead=days_ahead)
    finally:
        api.close()
