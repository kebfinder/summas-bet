from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import date as calendar_date
from datetime import datetime
from typing import Callable

import requests

from utils.browser_assist import BrowserFixtureCandidate, BrowserSourceAdapter, BrowserAssistPlan


logger = logging.getLogger(__name__)

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


class ConmebolLibertadoresAdapter:
    source_name = "conmebol_libertadores"
    page_template = (
        "https://gol.conmebol.com/libertadores/en/news/"
        "conmebol-libertadores-{year}-schedule-dates-kick-times-and-venues-group-stage"
    )

    _heading_pattern = re.compile(
        r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+([A-Za-z]+)\s+(\d{1,2})$"
    )
    _fixture_pattern = re.compile(
        r"^(\d{1,2}:\d{2})\s+[–-]\s+(.+?)\s+vs\s+(.+?)\s+[–-]\s+(Group\s+[A-H])\s+[–-]\s+(.+)$"
    )

    def discover(self, requested_date: calendar_date, plan: BrowserAssistPlan):
        url = self.page_template.format(year=requested_date.year)
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(url, timeout=30)
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Browser source %s failed to fetch %s: %s", self.source_name, url, exc)
            return []

        lines = self._extract_lines(response.text)
        candidates = self._extract_candidates(lines, requested_date, url)
        if candidates:
            return candidates
        return self._extract_candidates_from_json_ld(response.text, requested_date, url)

    def _extract_lines(self, raw_html: str) -> list[str]:
        text = re.sub(r"(?i)<br\s*/?>", "\n", raw_html)
        text = re.sub(r"(?i)</(p|div|h1|h2|h3|h4|li|section|article|tr|td|th)>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        lines = []
        for line in text.splitlines():
            cleaned = " ".join(line.strip().split())
            if cleaned:
                lines.append(cleaned)
        return lines

    def _extract_candidates(self, lines: list[str], requested_date: calendar_date, url: str):
        current_day = None
        candidates: list[BrowserFixtureCandidate] = []

        for line in lines:
            heading_match = self._heading_pattern.match(line)
            if heading_match:
                month_name = heading_match.group(2).lower()
                month = _MONTHS.get(month_name)
                day = int(heading_match.group(3))
                if month:
                    current_day = calendar_date(requested_date.year, month, day)
                continue

            if current_day != requested_date:
                continue

            fixture_match = self._fixture_pattern.match(line)
            if not fixture_match:
                continue

            kickoff, home_team, away_team, stage_name, _venue = fixture_match.groups()
            candidates.append(
                BrowserFixtureCandidate(
                    source_name=self.source_name,
                    source_url=url,
                    competition_name="Copa Libertadores",
                    match_date=requested_date.isoformat(),
                    kickoff_time=kickoff,
                    home_team=home_team,
                    away_team=away_team,
                    match_status="scheduled",
                    extraction_confidence=0.88,
                    stage_name=stage_name,
                )
            )

        return candidates

    def _extract_candidates_from_json_ld(self, raw_html: str, requested_date: calendar_date, url: str):
        candidates: list[BrowserFixtureCandidate] = []
        script_pattern = re.compile(
            r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in script_pattern.finditer(raw_html):
            payload = html.unescape(match.group(1) or "").strip()
            if not payload:
                continue
            try:
                json_payload = json.loads(payload)
            except Exception:
                continue
            items = json_payload if isinstance(json_payload, list) else [json_payload]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("@type", "")).lower() not in {"sportsevent", "event"}:
                    continue
                start_date = str(item.get("startDate") or "").strip()
                if not start_date:
                    continue
                kickoff_dt = self._parse_event_datetime(start_date)
                if not kickoff_dt or kickoff_dt.date() != requested_date:
                    continue
                home_team = self._participant_name(item.get("homeTeam"))
                away_team = self._participant_name(item.get("awayTeam"))
                if not home_team or not away_team:
                    continue
                candidates.append(
                    BrowserFixtureCandidate(
                        source_name=self.source_name,
                        source_url=url,
                        competition_name="Copa Libertadores",
                        match_date=requested_date.isoformat(),
                        kickoff_time=kickoff_dt.strftime("%H:%M"),
                        home_team=home_team,
                        away_team=away_team,
                        match_status="scheduled",
                        extraction_confidence=0.93,
                        stage_name=str(item.get("description") or item.get("name") or "").strip() or None,
                    )
                )
        return candidates

    @staticmethod
    def _participant_name(value):
        if isinstance(value, dict):
            return str(value.get("name") or "").strip()
        if isinstance(value, str):
            return value.strip()
        return ""

    @staticmethod
    def _parse_event_datetime(value: str) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        normalized = raw.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except Exception:
            return None


class ConmebolSudamericanaAdapter(ConmebolLibertadoresAdapter):
    source_name = "conmebol_sudamericana"
    page_template = (
        "https://gol.conmebol.com/sudamericana/en/news/"
        "conmebol-sudamericana-{year}-schedule-dates-kick-times-and-venues-group-stage"
    )

    def _extract_candidates(self, lines: list[str], requested_date: calendar_date, url: str):
        base = super()._extract_candidates(lines, requested_date, url)
        return [
            BrowserFixtureCandidate(
                source_name=item.source_name,
                source_url=item.source_url,
                competition_name="Copa Sudamericana",
                match_date=item.match_date,
                kickoff_time=item.kickoff_time,
                home_team=item.home_team,
                away_team=item.away_team,
                match_status=item.match_status,
                extraction_confidence=0.87,
                stage_name=item.stage_name,
            )
            for item in base
        ]

    def _extract_candidates_from_json_ld(self, raw_html: str, requested_date: calendar_date, url: str):
        base = super()._extract_candidates_from_json_ld(raw_html, requested_date, url)
        return [
            BrowserFixtureCandidate(
                source_name=item.source_name,
                source_url=item.source_url,
                competition_name="Copa Sudamericana",
                match_date=item.match_date,
                kickoff_time=item.kickoff_time,
                home_team=item.home_team,
                away_team=item.away_team,
                match_status=item.match_status,
                extraction_confidence=0.91,
                stage_name=item.stage_name,
            )
            for item in base
        ]


class GenericJsonLdAdapter:
    source_name = "generic_jsonld"
    competition_name = ""
    source_url = ""
    extraction_confidence = 0.84

    def discover(self, requested_date: calendar_date, plan: BrowserAssistPlan):
        if not self.source_url:
            return []
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(self.source_url, timeout=30)
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Browser source %s failed to fetch %s: %s", self.source_name, self.source_url, exc)
            return []
        return self._extract_candidates_from_json_ld(response.text, requested_date, self.source_url)

    def _extract_candidates_from_json_ld(self, raw_html: str, requested_date: calendar_date, url: str):
        candidates: list[BrowserFixtureCandidate] = []
        script_pattern = re.compile(
            r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in script_pattern.finditer(raw_html):
            payload = html.unescape(match.group(1) or "").strip()
            if not payload:
                continue
            try:
                json_payload = json.loads(payload)
            except Exception:
                continue
            items = json_payload if isinstance(json_payload, list) else [json_payload]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("@type", "")).lower() not in {"sportsevent", "event"}:
                    continue
                start_date = str(item.get("startDate") or "").strip()
                if not start_date:
                    continue
                kickoff_dt = ConmebolLibertadoresAdapter._parse_event_datetime(start_date)
                if not kickoff_dt or kickoff_dt.date() != requested_date:
                    continue
                home_team = ConmebolLibertadoresAdapter._participant_name(item.get("homeTeam"))
                away_team = ConmebolLibertadoresAdapter._participant_name(item.get("awayTeam"))
                if not home_team or not away_team:
                    continue
                candidates.append(
                    BrowserFixtureCandidate(
                        source_name=self.source_name,
                        source_url=url,
                        competition_name=self.competition_name,
                        match_date=requested_date.isoformat(),
                        kickoff_time=kickoff_dt.strftime("%H:%M"),
                        home_team=home_team,
                        away_team=away_team,
                        match_status="scheduled",
                        extraction_confidence=self.extraction_confidence,
                        stage_name=str(item.get("description") or item.get("name") or "").strip() or None,
                    )
                )
        return candidates


class UefaChampionsLeagueAdapter(GenericJsonLdAdapter):
    source_name = "uefa_champions_league"
    competition_name = "UEFA Champions League"
    source_url = "https://www.uefa.com/uefachampionsleague/fixtures-results/"
    extraction_confidence = 0.82


class UefaEuropaLeagueAdapter(GenericJsonLdAdapter):
    source_name = "uefa_europa_league"
    competition_name = "UEFA Europa League"
    source_url = "https://www.uefa.com/uefaeuropaleague/fixtures-results/"
    extraction_confidence = 0.82


class UefaConferenceLeagueAdapter(GenericJsonLdAdapter):
    source_name = "uefa_conference_league"
    competition_name = "UEFA Conference League"
    source_url = "https://www.uefa.com/uefaconferenceleague/fixtures-results/"
    extraction_confidence = 0.82


@dataclass(frozen=True)
class PublicWebCompetitionSupport:
    priority: int
    tier: int
    competition_key: str
    competition_name: str
    source_name: str
    source_url: str
    implemented: bool
    implementation_note: str | None = None
    adapter_factory: Callable[[], BrowserSourceAdapter] | None = None


_PUBLIC_WEB_COMPETITION_PLAN: tuple[PublicWebCompetitionSupport, ...] = (
    PublicWebCompetitionSupport(1, 1, "premier_league", "Premier League", "premier_league_official", "https://www.premierleague.com/fixtures", False, "Official fixture page is JS-rendered; no validated static parser in this pass."),
    PublicWebCompetitionSupport(2, 1, "la_liga", "LaLiga", "laliga_official", "https://www.laliga.com/en-GB/laliga-easports/results", False, "Official fixture page is JS-rendered; no validated static parser in this pass."),
    PublicWebCompetitionSupport(3, 1, "serie_a", "Serie A", "serie_a_official", "https://www.legaseriea.it/en/serie-a/fixture-and-results", False, "Official fixture page is JS-rendered; no validated static parser in this pass."),
    PublicWebCompetitionSupport(4, 1, "bundesliga", "Bundesliga", "bundesliga_official", "https://www.bundesliga.com/en/bundesliga/matchday", False, "Official fixture page is JS-rendered; no validated static parser in this pass."),
    PublicWebCompetitionSupport(5, 1, "ligue_1", "Ligue 1", "ligue_1_official", "https://ligue1.com/fixtures-results", False, "Official fixture page is JS-rendered; no validated static parser in this pass."),
    PublicWebCompetitionSupport(6, 1, "champions_league", "UEFA Champions League", "uefa_champions_league", "https://www.uefa.com/uefachampionsleague/fixtures-results/", True, adapter_factory=UefaChampionsLeagueAdapter),
    PublicWebCompetitionSupport(7, 1, "europa_league", "UEFA Europa League", "uefa_europa_league", "https://www.uefa.com/uefaeuropaleague/fixtures-results/", True, adapter_factory=UefaEuropaLeagueAdapter),
    PublicWebCompetitionSupport(8, 1, "conference_league", "UEFA Conference League", "uefa_conference_league", "https://www.uefa.com/uefaconferenceleague/fixtures-results/", True, adapter_factory=UefaConferenceLeagueAdapter),
    PublicWebCompetitionSupport(9, 1, "copa_libertadores", "Copa Libertadores", "conmebol_libertadores", "https://gol.conmebol.com/libertadores/en/news/", True, adapter_factory=ConmebolLibertadoresAdapter),
    PublicWebCompetitionSupport(10, 1, "copa_sudamericana", "Copa Sudamericana", "conmebol_sudamericana", "https://gol.conmebol.com/sudamericana/en/news/", True, adapter_factory=ConmebolSudamericanaAdapter),
    PublicWebCompetitionSupport(11, 1, "caf_champions_league", "CAF Champions League", "caf_champions_league_official", "https://www.cafonline.com/caf-champions-league/", False, "Public fixtures path exists but no stable static extraction contract validated yet."),
    PublicWebCompetitionSupport(12, 1, "afc_champions_league_elite", "AFC Champions League Elite", "afc_champions_league_elite_official", "https://www.the-afc.com/en/club/afc_champions_league_elite/fixtures.html", False, "Public fixtures path exists but no stable static extraction contract validated yet."),
    PublicWebCompetitionSupport(13, 1, "concacaf_champions_cup", "Concacaf Champions Cup", "concacaf_champions_cup_official", "https://www.concacaf.com/champions-cup/schedule-results/", False, "Public fixtures path exists but no stable static extraction contract validated yet."),
    PublicWebCompetitionSupport(14, 2, "championship", "Championship", "efl_championship_official", "https://www.efl.com/competitions/sky-bet-championship/fixtures-results/", False, "Official fixture page is JS-rendered; no validated static parser in this pass."),
    PublicWebCompetitionSupport(15, 2, "eredivisie", "Eredivisie", "eredivisie_official", "https://eredivisie.eu/competition-programme/", False, "Official fixture page is JS-rendered; no validated static parser in this pass."),
    PublicWebCompetitionSupport(16, 2, "primeira_liga", "Primeira Liga", "primeira_liga_official", "https://www.ligaportugal.pt/en/liga/fixture", False, "Official fixture page is JS-rendered; no validated static parser in this pass."),
    PublicWebCompetitionSupport(17, 2, "turkish_super_lig", "Turkish Süper Lig", "super_lig_official", "https://www.tff.org/default.aspx?pageID=198", False, "Official path is public but extraction is not yet standardized in adapter form."),
    PublicWebCompetitionSupport(18, 2, "mls", "MLS", "mls_official", "https://www.mlssoccer.com/schedule/fixtures", False, "Official fixture page is JS-rendered; no validated static parser in this pass."),
    PublicWebCompetitionSupport(19, 2, "liga_mx", "Liga MX", "liga_mx_official", "https://ligamx.net/", False, "Public path exists but fixtures are JS-driven and currently unvalidated for stable extraction."),
    PublicWebCompetitionSupport(20, 2, "brazil_serie_a", "Brasileirão Série A", "cbf_brasileirao_serie_a_official", "https://www.cbf.com.br/futebol-brasileiro/competicoes/campeonato-brasileiro-serie-a", False, "Public path exists but no stable extraction contract validated yet."),
    PublicWebCompetitionSupport(21, 2, "argentina_primera_division", "Argentine Primera", "afa_primera_official", "https://www.afa.com.ar/en/", False, "Public path exists but no stable extraction contract validated yet."),
    PublicWebCompetitionSupport(22, 2, "saudi_pro_league", "Saudi Pro League", "saudi_pro_league_official", "https://spl.com.sa/en/fixtures", False, "Public path exists but no stable extraction contract validated yet."),
    PublicWebCompetitionSupport(23, 2, "j1_league", "J1 League", "j_league_official", "https://www.jleague.co/fixtures", False, "Public path exists but no stable extraction contract validated yet."),
    PublicWebCompetitionSupport(24, 2, "k_league_1", "K League 1", "k_league_1_official", "https://www.kleague.com/schedule.do", False, "Public path exists but no stable extraction contract validated yet."),
    PublicWebCompetitionSupport(25, 3, "south_african_premiership", "South African Premiership", "south_african_premiership_official", "https://www.psl.co.za/fixtures", False, "Public path exists but no stable extraction contract validated yet."),
    PublicWebCompetitionSupport(26, 3, "egyptian_premier_league", "Egyptian Premier League", "egyptian_premier_league_official", "https://www.efa.eg/", False, "Public path exists but no stable extraction contract validated yet."),
    PublicWebCompetitionSupport(27, 3, "fkf_premier_league", "FKF Premier League", "fkf_premier_league_official", "https://footballkenya.org/", False, "Public path exists but no stable extraction contract validated yet."),
    PublicWebCompetitionSupport(28, 3, "caf_confed_cup", "CAF Confederation Cup", "caf_confed_cup_official", "https://www.cafonline.com/caf-confederation-cup/", False, "Public fixtures path exists but no stable static extraction contract validated yet."),
)


def get_public_web_competition_plan() -> tuple[PublicWebCompetitionSupport, ...]:
    return _PUBLIC_WEB_COMPETITION_PLAN


def get_public_web_support_matrix(allowed_sources: tuple[str, ...] = ()) -> list[dict]:
    allowed = {source.strip().lower() for source in allowed_sources if source.strip()}
    rows: list[dict] = []
    for spec in _PUBLIC_WEB_COMPETITION_PLAN:
        allowed_for_run = (not allowed) or (spec.source_name in allowed)
        rows.append(
            {
                "priority": spec.priority,
                "tier": spec.tier,
                "competition_key": spec.competition_key,
                "competition_name": spec.competition_name,
                "source_name": spec.source_name,
                "source_url": spec.source_url,
                "implemented": spec.implemented,
                "selected_by_allowlist": allowed_for_run,
                "eligible_for_run": bool(allowed_for_run and spec.implemented),
                "implementation_note": spec.implementation_note,
            }
        )
    return rows


def get_default_browser_adapters(allowed_sources: tuple[str, ...] = ()) -> list[BrowserSourceAdapter]:
    allowed = {source.strip().lower() for source in allowed_sources if source.strip()}
    adapters: list[BrowserSourceAdapter] = []

    for spec in _PUBLIC_WEB_COMPETITION_PLAN:
        if not spec.implemented or spec.adapter_factory is None:
            continue
        if allowed and spec.source_name not in allowed:
            continue
        adapters.append(spec.adapter_factory())

    return adapters
