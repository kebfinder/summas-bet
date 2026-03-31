from __future__ import annotations

import html
import logging
import re
from datetime import date as calendar_date

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
        return self._extract_candidates(lines, requested_date, url)

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


def get_default_browser_adapters(allowed_sources: tuple[str, ...] = ()) -> list[BrowserSourceAdapter]:
    allowed = {source.strip().lower() for source in allowed_sources if source.strip()}
    adapters: list[BrowserSourceAdapter] = []

    if not allowed or "conmebol_libertadores" in allowed:
        adapters.append(ConmebolLibertadoresAdapter())

    return adapters
