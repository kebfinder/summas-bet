# scrapers/openligadb_scraper.py
import logging
from datetime import timedelta

import requests

from config import (
    DEFAULT_LOOKAHEAD_DAYS,
    DEFAULT_OPENLIGADB_LEAGUES,
    DISCOVER_OPENLIGADB_LEAGUES,
    OPENLIGADB_ALLOWED_SPORTS,
)
from database.database import SessionLocal
from database.models import Fixture, HistoricalMatch, Team
from utils.competitions import (
    canonical_display_name,
    is_competition_admitted,
    preferred_provider_codes,
    resolve_competition,
)
from utils.football import (
    current_season_year,
    day_bounds,
    day_bounds_for_date,
    normalize_status,
    normalize_team_name,
    parse_match_datetime,
    within_day_window,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_NOISE_LEAGUE_KEYWORDS = (
    "anal",
    "betwish",
    "test",
    "debug",
    "mqtt",
    "fehler",
    "bastard",
    "emptystands",
    "spendenliga",
    "skoholm",
    "moisty",
    "kickercrew",
    "futsi",
    "senior",
)

_DISCOVERY_EXCLUDE_KEYWORDS = (
    "kreisliga",
    "bezirksliga",
    "landesliga",
    "oberliga",
    "regionalliga",
    "u17",
    "u19",
    "u21",
    "jugend",
    "reserve",
)

_DUPLICATE_LEAGUE_CODES = {
    "bsl",
    "ch1",
    "kwm",
    "rlno_n",
    "ucl2025",
}


class OpenLigaDBScraper:
    BASE_URL = "https://api.openligadb.de"

    def __init__(self):
        self.db = SessionLocal()

    def close(self):
        self.db.close()

    def get_available_leagues(self):
        """Get list of available leagues."""
        try:
            response = requests.get(f"{self.BASE_URL}/getavailableleagues", timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.error("Error fetching leagues: %s", exc)
            return []

    def get_match_data(self, league, season):
        """
        Get matches for a specific league and season.
        league: e.g., 'bl1' (Bundesliga), 'bl2', 'bl3', 'dfl' (DFB Pokal)
        season: e.g., '2025' for the 2025/26 season
        """
        try:
            url = f"{self.BASE_URL}/getmatchdata/{league}/{season}"
            logger.info("Fetching: %s", url)
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.error("Error fetching match data: %s", exc)
            return []

    def _is_noise_league(self, league_data):
        shortcut = str(league_data.get("leagueShortcut") or "").strip().lower()
        name = str(league_data.get("leagueName") or "").strip().lower()
        combined = f"{shortcut} {name}"
        if not shortcut or not name:
            return True
        if len(shortcut) > 18:
            return True
        return any(keyword in combined for keyword in _NOISE_LEAGUE_KEYWORDS)

    def _discover_current_league_codes(self, season_code):
        available = []
        seen_codes = set()

        for league_data in self.get_available_leagues():
            if str(league_data.get("leagueSeason")) != str(season_code):
                continue

            sport_name = str(league_data.get("sport", {}).get("sportName") or "").strip()
            if sport_name not in OPENLIGADB_ALLOWED_SPORTS:
                continue

            if self._is_noise_league(league_data):
                continue

            shortcut = str(league_data.get("leagueShortcut") or "").strip()
            league_name = str(league_data.get("leagueName") or "").strip()
            code_key = shortcut.lower()
            if code_key in _DUPLICATE_LEAGUE_CODES:
                continue
            if code_key in seen_codes:
                continue
            combined = f"{shortcut} {league_name}".lower()
            if any(keyword in combined for keyword in _DISCOVERY_EXCLUDE_KEYWORDS):
                continue
            if not is_competition_admitted("openligadb", code=shortcut, name=league_name):
                continue

            seen_codes.add(code_key)
            available.append(shortcut)

        return available

    def _resolve_daily_league_codes(self, season_code):
        if not DISCOVER_OPENLIGADB_LEAGUES:
            return [code for code in DEFAULT_OPENLIGADB_LEAGUES if is_competition_admitted("openligadb", code=code, name=code)]

        available_codes = self._discover_current_league_codes(season_code)
        if not available_codes:
            return [code for code in DEFAULT_OPENLIGADB_LEAGUES if is_competition_admitted("openligadb", code=code, name=code)]

        available_lookup = {code.lower(): code for code in available_codes}
        selected_codes = []

        preferred_codes = list(DEFAULT_OPENLIGADB_LEAGUES) or preferred_provider_codes("openligadb")
        for preferred in preferred_codes:
            actual_code = available_lookup.get(preferred.lower())
            if actual_code and actual_code not in selected_codes:
                selected_codes.append(actual_code)

        for code in available_codes:
            if code not in selected_codes:
                selected_codes.append(code)

        logger.info("Resolved %s OpenLigaDB leagues for season %s: %s", len(selected_codes), season_code, selected_codes)
        return selected_codes

    def _save_historical_match_record(self, match, season_code):
        try:
            results = match.get("matchResults") or []
            if not match.get("matchIsFinished") or not results:
                return 0

            result = next(
                (item for item in results if item.get("resultName", "").lower() == "endergebnis"),
                results[0],
            )

            home_name = normalize_team_name(match.get("team1", {}).get("teamName"))
            away_name = normalize_team_name(match.get("team2", {}).get("teamName"))
            if not home_name or not away_name or home_name == away_name:
                return 0

            raw_league_name = match.get("leagueName") or match.get("leagueShortcut") or "Unknown"
            raw_league_code = match.get("leagueShortcut")
            competition = resolve_competition("openligadb", code=raw_league_code, name=raw_league_name)
            if competition is None or not is_competition_admitted("openligadb", code=raw_league_code, name=raw_league_name):
                return 0

            league_name = competition.display_name
            home_team = self._get_or_create_team(home_name, competition)
            away_team = self._get_or_create_team(away_name, competition)

            match_date = parse_match_datetime(match.get("matchDateTime"))
            if match_date is None:
                return 0

            existing = (
                self.db.query(HistoricalMatch)
                .filter(
                    HistoricalMatch.home_id == home_team.id,
                    HistoricalMatch.away_id == away_team.id,
                    HistoricalMatch.date == match_date,
                )
                .first()
            )
            if existing:
                return 0

            home_goals = int(result.get("pointsTeam1", 0))
            away_goals = int(result.get("pointsTeam2", 0))

            hist = HistoricalMatch(
                home_id=home_team.id,
                away_id=away_team.id,
                date=match_date,
                season=season_code,
                home_goals=home_goals,
                away_goals=away_goals,
                btts=(home_goals > 0 and away_goals > 0),
                over_2_5=(home_goals + away_goals) > 2.5,
                over_1_5=(home_goals + away_goals) > 1.5,
            )
            self.db.add(hist)
            self.db.commit()
            return 1

        except Exception as exc:
            logger.error("Error saving historical match: %s", exc)
            self.db.rollback()
            return 0

    def _seed_historical_matches(self, matches, season_code):
        saved = 0
        for match in matches:
            saved += self._save_historical_match_record(match, season_code)
        return saved

    def scrape_todays_matches(
        self,
        leagues=None,
        season=None,
        days_ahead: int = DEFAULT_LOOKAHEAD_DAYS,
        target_date=None,
    ):
        """Scrape upcoming matches inside the configured day window."""
        all_fixtures = []
        season_code = str(season or current_season_year())
        start, end = day_bounds_for_date(target_date) if target_date is not None else day_bounds(days_ahead=days_ahead)
        league_codes = list(leagues or self._resolve_daily_league_codes(season_code))
        league_pairs = [(league, season_code) for league in league_codes]

        for league, league_season in league_pairs:
            try:
                matches = self.get_match_data(league, league_season)
                historical_saved = self._seed_historical_matches(matches, league_season)
                if historical_saved:
                    logger.info("Seeded %s historical matches for %s", historical_saved, league)
                for match in matches:
                    fixture = self._process_match(match, start, end)
                    if fixture:
                        all_fixtures.append(fixture)
            except Exception as exc:
                logger.error("Error processing %s: %s", league, exc)

        return all_fixtures

    def _process_match(self, match_data, start, end):
        """Process a single match from OpenLigaDB format."""
        try:
            match_date = parse_match_datetime(match_data.get("matchDateTime"))
            if not within_day_window(match_date, start, end):
                return None

            home_team_name = normalize_team_name(match_data.get("team1", {}).get("teamName"))
            away_team_name = normalize_team_name(match_data.get("team2", {}).get("teamName"))
            if not home_team_name or not away_team_name or home_team_name == away_team_name:
                return None

            raw_league_name = match_data.get("leagueName") or match_data.get("leagueShortcut") or "Unknown"
            raw_league_code = match_data.get("leagueShortcut")
            competition = resolve_competition("openligadb", code=raw_league_code, name=raw_league_name)
            if competition is None or not is_competition_admitted("openligadb", code=raw_league_code, name=raw_league_name):
                return None

            league_name = canonical_display_name("openligadb", code=raw_league_code, name=raw_league_name)
            logger.info("Found match: %s vs %s", home_team_name, away_team_name)

            home_team = self._get_or_create_team(home_team_name, competition)
            away_team = self._get_or_create_team(away_team_name, competition)

            day_start = match_date.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            status = normalize_status(None, is_finished=bool(match_data.get("matchIsFinished")))

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
                if not existing.league or existing.league == "Unknown":
                    existing.league = league_name
                self.db.commit()
                self.db.refresh(existing)
                return existing

            fixture = Fixture(
                date=match_date,
                home_id=home_team.id,
                away_id=away_team.id,
                league=league_name,
                status=status,
            )
            self.db.add(fixture)
            self.db.commit()
            self.db.refresh(fixture)
            return fixture

        except Exception as exc:
            logger.error("Error processing match: %s", exc)
            self.db.rollback()
            return None

    def _get_or_create_team(self, name, competition):
        """Get existing team or create a new normalized team record."""
        normalized_name = normalize_team_name(name)
        team = self.db.query(Team).filter_by(name=normalized_name).first()
        league = competition.display_name if hasattr(competition, "display_name") else str(competition or "Unknown")
        guessed_country = getattr(competition, "country", None) or self._guess_country(league)

        if not team:
            team = Team(
                name=normalized_name,
                league=league,
                country=guessed_country,
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
        if guessed_country != "Unknown" and (not team.country or team.country == "Unknown"):
            team.country = guessed_country

        return team

    def _guess_country(self, league):
        """Guess country from league name."""
        league_lower = str(league or "").lower()
        countries = {
            "bundesliga": "Germany",
            "dfb": "Germany",
            "champions": "Europe",
            "europa": "Europe",
            "premier": "England",
            "liga": "Spain",
            "serie": "Italy",
            "ligue": "France",
            "eredivisie": "Netherlands",
            "primeira": "Portugal",
        }
        for key, country in countries.items():
            if key in league_lower:
                return country
        return "Unknown"

    def get_historical_matches(self, league, season=None):
        """Get historical match results for statistics."""
        season_code = str(season or current_season_year())
        matches = self.get_match_data(league, season_code)
        saved = self._seed_historical_matches(matches, season_code)
        logger.info("Saved %s historical matches", saved)
        return saved


def scrape_today_openligadb(days_ahead: int = DEFAULT_LOOKAHEAD_DAYS, target_date=None):
    scraper = OpenLigaDBScraper()
    try:
        return scraper.scrape_todays_matches(days_ahead=days_ahead, target_date=target_date)
    finally:
        scraper.close()


def scrape_historical_openligadb(league="bl1", season=None):
    scraper = OpenLigaDBScraper()
    try:
        return scraper.get_historical_matches(league, season)
    finally:
        scraper.close()
