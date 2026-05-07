import logging
import os
from datetime import timedelta

import requests

from config import DEFAULT_LOOKAHEAD_DAYS
from database.database import SessionLocal
from database.models import Fixture, Team
from utils.competitions import canonical_display_name, is_competition_admitted, resolve_competition
from utils.football import day_bounds, day_bounds_for_date, normalize_status, normalize_team_name, parse_match_datetime, within_day_window

logger = logging.getLogger(__name__)


class APIFootballAPI:
    BASE_URL = "https://v3.football.api-sports.io"

    def __init__(self, api_key=None):
        self.db = SessionLocal()
        self.api_key = api_key or os.getenv("API_FOOTBALL_API_KEY") or os.getenv("RAPIDAPI_KEY")
        self.session = requests.Session()
        self.session.trust_env = False
        self.headers = {"x-apisports-key": self.api_key} if self.api_key else {}

    def close(self):
        self.session.close()
        self.db.close()

    def get_matches_today(self, days_ahead: int = DEFAULT_LOOKAHEAD_DAYS, target_date=None):
        start, end = day_bounds_for_date(target_date) if target_date is not None else day_bounds(days_ahead=days_ahead)
        try:
            response = self.session.get(
                f"{self.BASE_URL}/fixtures",
                headers=self.headers,
                params={"from": start.strftime("%Y-%m-%d"), "to": (end - timedelta(days=1)).strftime("%Y-%m-%d")},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            return self._process_matches(payload.get("response", []), start, end)
        except Exception as exc:
            logger.error("Error fetching API-Football fixtures: %s", exc)
            return []

    def _process_matches(self, matches, start, end):
        fixtures = []
        for item in matches:
            try:
                teams = item.get("teams", {}) or {}
                home_name = normalize_team_name((teams.get("home") or {}).get("name"))
                away_name = normalize_team_name((teams.get("away") or {}).get("name"))
                if not home_name or not away_name or home_name == away_name:
                    continue

                league_info = item.get("league", {}) or {}
                raw_code = league_info.get("id")
                raw_name = league_info.get("name", "Unknown")
                if not is_competition_admitted("api_football", code=raw_code, name=raw_name):
                    continue

                competition = resolve_competition("api_football", code=raw_code, name=raw_name)
                league = canonical_display_name("api_football", code=raw_code, name=raw_name)
                fixture_info = item.get("fixture", {}) or {}
                match_date = parse_match_datetime(fixture_info.get("date"))
                if not within_day_window(match_date, start, end):
                    continue

                home_team = self._get_or_create_team(home_name, competition or league)
                away_team = self._get_or_create_team(away_name, competition or league)

                day_start = match_date.replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = day_start + timedelta(days=1)
                raw_status = ((fixture_info.get("status") or {}).get("short") or "NS").upper()
                status = normalize_status(raw_status, is_finished=raw_status in {"FT", "AET", "PEN"})

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

                fixture = Fixture(date=match_date, home_id=home_team.id, away_id=away_team.id, league=league, status=status)
                self.db.add(fixture)
                self.db.commit()
                self.db.refresh(fixture)
                fixtures.append(fixture)
            except Exception as exc:
                logger.error("Error processing API-Football match: %s", exc)
                self.db.rollback()
        return fixtures

    def _get_or_create_team(self, name, competition):
        normalized_name = normalize_team_name(name)
        team = self.db.query(Team).filter_by(name=normalized_name).first()
        league = competition.display_name if hasattr(competition, "display_name") else str(competition or "Unknown")
        country = getattr(competition, "country", None) or "Unknown"
        if not team:
            team = Team(name=normalized_name, league=league, country=country, avg_scored=1.25, avg_conceded=1.25, attack_strength=1.0, defense_strength=1.0)
            self.db.add(team)
            self.db.flush()
            return team
        if league and (not team.league or team.league == "Unknown"):
            team.league = league
        if country != "Unknown" and (not team.country or team.country == "Unknown"):
            team.country = country
        return team
