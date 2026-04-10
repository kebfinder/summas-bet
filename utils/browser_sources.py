from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import date as calendar_date
from datetime import datetime, timedelta, timezone

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

_CACHE_TTL_SECONDS = 600
_FAILURE_COOLDOWN_SECONDS = 180
_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 3
_CIRCUIT_BREAKER_OPEN_SECONDS = 900
_SCOREBOARD_CACHE: dict[str, tuple[datetime, list[BrowserFixtureCandidate]]] = {}
_SCOREBOARD_COOLDOWN: dict[str, datetime] = {}
_PROVIDER_HEALTH: dict[str, dict[str, object]] = {}
_PROVIDER_EVENT_LOG: dict[str, list[dict[str, str]]] = {}
_MAX_PROVIDER_EVENTS = 200


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cache_get(key: str) -> list[BrowserFixtureCandidate] | None:
    cached = _SCOREBOARD_CACHE.get(key)
    if not cached:
        return None
    expires_at, payload = cached
    if expires_at <= _utc_now():
        _SCOREBOARD_CACHE.pop(key, None)
        return None
    return list(payload)


def _cache_set(key: str, payload: list[BrowserFixtureCandidate]) -> None:
    _SCOREBOARD_CACHE[key] = (_utc_now() + timedelta(seconds=_CACHE_TTL_SECONDS), list(payload))


def _cooldown_active(key: str) -> bool:
    until = _SCOREBOARD_COOLDOWN.get(key)
    if not until:
        return False
    if until <= _utc_now():
        _SCOREBOARD_COOLDOWN.pop(key, None)
        return False
    return True


def _start_cooldown(key: str) -> None:
    _SCOREBOARD_COOLDOWN[key] = _utc_now() + timedelta(seconds=_FAILURE_COOLDOWN_SECONDS)


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


class JsRenderedScoreboardAdapter:
    provider_templates = (
        ("espn_site_api", "https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/scoreboard?dates={ymd}"),
        ("espn_sports_core_api", "https://sports.core.api.espn.com/v2/sports/soccer/leagues/{league_slug}/events?dates={ymd}"),
    )

    def __init__(
        self,
        *,
        source_name: str,
        league_slug: str,
        competition_name: str,
        confidence: float = 0.84,
    ):
        self.source_name = source_name
        self.league_slug = league_slug
        self.competition_name = competition_name
        self.confidence = confidence
        self.last_run_diagnostics: dict[str, object] = {}

    def discover(self, requested_date: calendar_date, plan: BrowserAssistPlan):
        date_key = requested_date.strftime("%Y%m%d")
        cache_key = f"{self.source_name}:{date_key}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        if _cooldown_active(cache_key):
            logger.info("Browser source %s cooldown active for %s", self.source_name, requested_date.isoformat())
            return []

        failures: dict[str, str] = {}
        total_raw_rows = 0
        ordered_templates = self._ordered_provider_templates()
        ordered_provider_names = [name for name, _template in ordered_templates]
        for provider_name, template in ordered_templates:
            provider_key = f"{self.source_name}:{provider_name}"
            health = _provider_health(provider_key)
            if _provider_circuit_open(health):
                failures[provider_name] = "circuit_open"
                continue
            url = template.format(league_slug=self.league_slug, ymd=date_key)
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.get(url, timeout=30)
                response.raise_for_status()
                payload = response.json() if response.content else {}
                candidates, raw_rows = self._parse_payload_by_provider(provider_name, payload, requested_date, url)
                total_raw_rows += int(raw_rows)
                _cache_set(cache_key, candidates)
                _provider_mark_success(provider_key)
                self.last_run_diagnostics = {
                    "provider_used": provider_name,
                    "provider_failures": failures,
                    "raw_rows_found": int(raw_rows),
                    "parsed_fixtures": len(candidates),
                    "provider_order": ordered_provider_names,
                }
                return candidates
            except Exception as exc:
                failures[provider_name] = str(exc)
                _provider_mark_failure(provider_key)
                logger.warning("Browser source %s failed to fetch %s (%s): %s", self.source_name, url, provider_name, exc)

        _start_cooldown(cache_key)
        self.last_run_diagnostics = {
            "provider_used": None,
            "provider_failures": failures,
            "raw_rows_found": int(total_raw_rows),
            "parsed_fixtures": 0,
            "provider_order": ordered_provider_names,
        }
        return []

    def _ordered_provider_templates(self) -> list[tuple[str, str]]:
        ranked: list[tuple[float, tuple[str, str]]] = []
        for provider_name, template in self.provider_templates:
            provider_key = f"{self.source_name}:{provider_name}"
            health = _provider_health(provider_key)
            score = _provider_score(health) + provider_weight_for(self.source_name, provider_name)
            ranked.append((score, (provider_name, template)))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [row for _score, row in ranked]

    def _parse_payload_by_provider(
        self,
        provider_name: str,
        payload: dict,
        requested_date: calendar_date,
        source_url: str,
    ) -> tuple[list[BrowserFixtureCandidate], int]:
        if provider_name == "espn_sports_core_api":
            events = payload.get("items") or payload.get("events") or []
            normalized = {"events": events}
            out = self._parse_scoreboard(normalized, requested_date, source_url)
            return out, len(events)
        events = payload.get("events") or []
        out = self._parse_scoreboard(payload, requested_date, source_url)
        return out, len(events)

    def _parse_scoreboard(self, payload: dict, requested_date: calendar_date, source_url: str) -> list[BrowserFixtureCandidate]:
        events = payload.get("events") or []
        candidates: list[BrowserFixtureCandidate] = []
        for event in events:
            competition_data = ((event.get("competitions") or [{}])[0]) if isinstance(event, dict) else {}
            competitors = competition_data.get("competitors") or []
            if len(competitors) < 2:
                continue
            home_name = ""
            away_name = ""
            for side in competitors:
                team_name = str((side.get("team") or {}).get("displayName") or "").strip()
                if str(side.get("homeAway") or "").lower() == "home":
                    home_name = team_name
                elif str(side.get("homeAway") or "").lower() == "away":
                    away_name = team_name
            if not home_name or not away_name:
                continue
            kickoff_iso = str(event.get("date") or "").strip()
            kickoff_time = None
            if kickoff_iso:
                try:
                    kickoff_time = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00")).strftime("%H:%M")
                except Exception:
                    kickoff_time = None
            status = str(((competition_data.get("status") or {}).get("type") or {}).get("state") or "pre")
            if status in {"in", "post"}:
                match_status = "finished"
            else:
                match_status = "scheduled"
            candidates.append(
                BrowserFixtureCandidate(
                    source_name=self.source_name,
                    source_url=source_url,
                    competition_name=self.competition_name,
                    match_date=requested_date.isoformat(),
                    kickoff_time=kickoff_time,
                    home_team=home_name,
                    away_team=away_name,
                    match_status=match_status,
                    extraction_confidence=self.confidence,
                )
            )
        return candidates


def _provider_health(provider_key: str) -> dict[str, object]:
    state = _PROVIDER_HEALTH.get(provider_key)
    if state is None:
        state = {
            "consecutive_failures": 0,
            "last_success_at": None,
            "last_failure_at": None,
            "circuit_open_until": None,
        }
        _PROVIDER_HEALTH[provider_key] = state
    return state


def _provider_circuit_open(health_state: dict[str, object]) -> bool:
    until = health_state.get("circuit_open_until")
    if not isinstance(until, datetime):
        return False
    return until > _utc_now()


def _provider_mark_success(provider_key: str) -> None:
    state = _provider_health(provider_key)
    state["consecutive_failures"] = 0
    state["last_success_at"] = _utc_now().isoformat()
    state["circuit_open_until"] = None
    _provider_log_event(provider_key, "success")


def _provider_mark_failure(provider_key: str) -> None:
    state = _provider_health(provider_key)
    failures = int(state.get("consecutive_failures") or 0) + 1
    state["consecutive_failures"] = failures
    state["last_failure_at"] = _utc_now().isoformat()
    if failures >= _CIRCUIT_BREAKER_FAILURE_THRESHOLD:
        state["circuit_open_until"] = _utc_now() + timedelta(seconds=_CIRCUIT_BREAKER_OPEN_SECONDS)
    _provider_log_event(provider_key, "failure")


def _provider_score(health_state: dict[str, object]) -> float:
    consecutive_failures = int(health_state.get("consecutive_failures") or 0)
    circuit_penalty = 5.0 if _provider_circuit_open(health_state) else 0.0
    recent_success_rate = float(health_state.get("recent_success_rate") or 0.5)
    return (recent_success_rate * 10.0) - float(consecutive_failures) - circuit_penalty


def provider_weight_for(adapter_name: str, provider_name: str) -> float:
    raw = os.getenv("PUBLIC_WEB_PROVIDER_WEIGHTS", "").strip()
    if not raw:
        return 0.0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0.0
    adapter_weights = payload.get(adapter_name) if isinstance(payload, dict) else None
    if not isinstance(adapter_weights, dict):
        return 0.0
    try:
        return float(adapter_weights.get(provider_name, 0.0) or 0.0)
    except Exception:
        return 0.0


def _provider_log_event(provider_key: str, event: str) -> None:
    events = _PROVIDER_EVENT_LOG.setdefault(provider_key, [])
    events.append({"event": str(event), "timestamp": _utc_now().isoformat()})
    if len(events) > _MAX_PROVIDER_EVENTS:
        del events[: len(events) - _MAX_PROVIDER_EVENTS]
    success_count = sum(1 for item in events if item.get("event") == "success")
    total = len(events)
    state = _provider_health(provider_key)
    state["recent_success_rate"] = round((success_count / total), 3) if total > 0 else 0.5


def provider_health_snapshot() -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    for provider_key, state in _PROVIDER_HEALTH.items():
        events = list(_PROVIDER_EVENT_LOG.get(provider_key, []))
        snapshot[provider_key] = {
            "consecutive_failures": int(state.get("consecutive_failures") or 0),
            "last_success_at": state.get("last_success_at"),
            "last_failure_at": state.get("last_failure_at"),
            "circuit_open_until": (
                state.get("circuit_open_until").isoformat()
                if isinstance(state.get("circuit_open_until"), datetime)
                else state.get("circuit_open_until")
            ),
            "recent_success_rate": float(state.get("recent_success_rate") or 0.5),
            "events_24h": _event_counts_within(events, timedelta(hours=24)),
            "events_7d": _event_counts_within(events, timedelta(days=7)),
        }
    return snapshot


def _event_counts_within(events: list[dict[str, str]], lookback: timedelta) -> dict[str, int]:
    cutoff = _utc_now() - lookback
    total = 0
    success = 0
    failure = 0
    for item in events:
        ts = str(item.get("timestamp") or "")
        try:
            when = datetime.fromisoformat(ts)
        except Exception:
            continue
        if when < cutoff:
            continue
        total += 1
        if item.get("event") == "success":
            success += 1
        elif item.get("event") == "failure":
            failure += 1
    return {"total": total, "success": success, "failure": failure}


def get_default_browser_adapters(allowed_sources: tuple[str, ...] = ()) -> list[BrowserSourceAdapter]:
    allowed = {source.strip().lower() for source in allowed_sources if source.strip()}
    adapters: list[BrowserSourceAdapter] = []

    if not allowed or "conmebol_libertadores" in allowed:
        adapters.append(ConmebolLibertadoresAdapter())
    js_priority_adapters = (
        JsRenderedScoreboardAdapter(
            source_name="official_js_premier_league",
            league_slug="eng.1",
            competition_name="Premier League",
        ),
        JsRenderedScoreboardAdapter(
            source_name="official_js_la_liga",
            league_slug="esp.1",
            competition_name="La Liga",
        ),
        JsRenderedScoreboardAdapter(
            source_name="official_js_serie_a",
            league_slug="ita.1",
            competition_name="Serie A",
        ),
        JsRenderedScoreboardAdapter(
            source_name="official_js_bundesliga",
            league_slug="ger.1",
            competition_name="Bundesliga",
        ),
        JsRenderedScoreboardAdapter(
            source_name="official_js_ligue_1",
            league_slug="fra.1",
            competition_name="Ligue 1",
        ),
        JsRenderedScoreboardAdapter(
            source_name="official_js_championship",
            league_slug="eng.2",
            competition_name="Championship",
        ),
        JsRenderedScoreboardAdapter(
            source_name="official_js_mls",
            league_slug="usa.1",
            competition_name="Major League Soccer",
        ),
        JsRenderedScoreboardAdapter(
            source_name="official_js_liga_mx",
            league_slug="mex.1",
            competition_name="Liga MX",
        ),
    )
    for adapter in js_priority_adapters:
        if not allowed or adapter.source_name in allowed:
            adapters.append(adapter)

    return adapters
