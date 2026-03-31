from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date as calendar_date
import logging
from typing import Iterable, Protocol, Sequence

from config import (
    BROWSER_ASSIST_ALLOWED_SOURCES,
    BROWSER_ASSIST_ENABLED,
    BROWSER_ASSIST_MIN_ACTIVE_LEAGUES,
    BROWSER_ASSIST_TARGET_ANALYZABLE_COUNT,
    BROWSER_ASSIST_TARGET_FIXTURE_COUNT,
)
from utils.competitions import resolve_competition
from utils.football import clamp, normalize_team_name


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrowserAssistConfig:
    enabled: bool
    allowed_sources: tuple[str, ...]
    target_fixture_count: int
    target_analyzable_count: int
    min_active_leagues: int


@dataclass(frozen=True)
class PrimaryFixtureRecord:
    fixture_id: int | None
    match_date: str
    competition_name: str | None
    canonical_competition_id: str | None
    home_team: str
    away_team: str
    status: str | None
    source_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrowserFixtureCandidate:
    source_name: str
    source_url: str
    competition_name: str
    match_date: str
    kickoff_time: str | None
    home_team: str
    away_team: str
    match_status: str | None
    extraction_confidence: float
    stage_name: str | None = None
    canonical_competition_id: str | None = None
    canonical_competition_name: str | None = None
    canonical_home_team: str | None = None
    canonical_away_team: str | None = None
    mapping_confidence: float = 0.0


@dataclass(frozen=True)
class BrowserAssistPlan:
    requested_date: str
    coverage_status: str
    trigger_eligible: bool
    trigger_reasons: tuple[str, ...]
    adapters_configured: tuple[str, ...]
    target_fixture_count: int
    target_analyzable_count: int
    min_active_leagues: int
    note: str | None = None


@dataclass(frozen=True)
class BrowserAssistResult:
    enabled: bool
    requested_date: str
    coverage_status: str
    trigger_eligible: bool
    triggered: bool
    trigger_reasons: tuple[str, ...]
    adapters_configured: tuple[str, ...]
    target_fixture_count: int
    target_analyzable_count: int
    min_active_leagues: int
    browser_candidates: int
    mapped_candidates: int
    promoted_fixtures: int
    rejected_candidates: int
    note: str | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["trigger_reasons"] = list(self.trigger_reasons)
        payload["adapters_configured"] = list(self.adapters_configured)
        return payload


class BrowserSourceAdapter(Protocol):
    source_name: str

    def discover(self, requested_date: calendar_date, plan: BrowserAssistPlan) -> Sequence[BrowserFixtureCandidate]:
        ...


def browser_assist_config() -> BrowserAssistConfig:
    return BrowserAssistConfig(
        enabled=BROWSER_ASSIST_ENABLED,
        allowed_sources=tuple(source.strip().lower() for source in BROWSER_ASSIST_ALLOWED_SOURCES if source.strip()),
        target_fixture_count=BROWSER_ASSIST_TARGET_FIXTURE_COUNT,
        target_analyzable_count=BROWSER_ASSIST_TARGET_ANALYZABLE_COUNT,
        min_active_leagues=BROWSER_ASSIST_MIN_ACTIVE_LEAGUES,
    )


def default_browser_adapters(config: BrowserAssistConfig | None = None) -> list[BrowserSourceAdapter]:
    active_config = config or browser_assist_config()
    from utils.browser_sources import get_default_browser_adapters

    return get_default_browser_adapters(active_config.allowed_sources)


def build_primary_fixture_record(fixture, source_names: Iterable[str] = ()) -> PrimaryFixtureRecord:
    competition = resolve_competition("storage", name=getattr(fixture, "league", None))
    home_name = normalize_team_name(getattr(getattr(fixture, "home", None), "name", None))
    away_name = normalize_team_name(getattr(getattr(fixture, "away", None), "name", None))
    match_date = str(getattr(getattr(fixture, "date", None), "date", lambda: getattr(fixture, "date", ""))())

    return PrimaryFixtureRecord(
        fixture_id=getattr(fixture, "id", None),
        match_date=match_date,
        competition_name=getattr(fixture, "league", None),
        canonical_competition_id=competition.canonical_id if competition else None,
        home_team=home_name,
        away_team=away_name,
        status=getattr(fixture, "status", None),
        source_names=tuple(source_names),
    )


def map_browser_candidate(candidate: BrowserFixtureCandidate) -> BrowserFixtureCandidate:
    competition = resolve_competition("storage", name=candidate.competition_name)
    canonical_home = normalize_team_name(candidate.home_team)
    canonical_away = normalize_team_name(candidate.away_team)

    mapping_confidence = 0.0
    if competition:
        mapping_confidence += 0.45 + (0.20 * competition.source_confidence)
    if canonical_home:
        mapping_confidence += 0.15
    if canonical_away:
        mapping_confidence += 0.15

    return BrowserFixtureCandidate(
        source_name=candidate.source_name,
        source_url=candidate.source_url,
        competition_name=candidate.competition_name,
        match_date=candidate.match_date,
        kickoff_time=candidate.kickoff_time,
        home_team=candidate.home_team,
        away_team=candidate.away_team,
        match_status=candidate.match_status,
        extraction_confidence=clamp(candidate.extraction_confidence, 0.0, 1.0),
        stage_name=candidate.stage_name,
        canonical_competition_id=competition.canonical_id if competition else None,
        canonical_competition_name=competition.display_name if competition else None,
        canonical_home_team=canonical_home or None,
        canonical_away_team=canonical_away or None,
        mapping_confidence=round(clamp(mapping_confidence, 0.0, 0.99), 3),
    )


def fixture_identity(
    match_date: str,
    canonical_competition_id: str | None,
    home_team: str | None,
    away_team: str | None,
) -> str:
    return "|".join(
        (
            str(match_date or ""),
            str(canonical_competition_id or ""),
            normalize_team_name(home_team),
            normalize_team_name(away_team),
        )
    )


def evaluate_browser_plan(
    requested_date: calendar_date,
    *,
    raw_fixtures: int,
    unique_fixtures: int,
    analyzable_fixtures: int,
    active_leagues: int,
    adapters: Sequence[BrowserSourceAdapter] = (),
    config: BrowserAssistConfig | None = None,
) -> BrowserAssistPlan:
    active_config = config or browser_assist_config()
    reasons: list[str] = []

    if unique_fixtures == 0:
        reasons.append("no_primary_fixtures")
    if unique_fixtures < active_config.target_fixture_count:
        reasons.append("thin_fixture_count")
    if analyzable_fixtures < active_config.target_analyzable_count:
        reasons.append("thin_analyzable_pool")
    if active_leagues < active_config.min_active_leagues:
        reasons.append("thin_league_coverage")

    if unique_fixtures == 0:
        coverage_status = "incomplete"
    elif reasons:
        coverage_status = "thin"
    else:
        coverage_status = "healthy"

    adapter_names = tuple(adapter.source_name for adapter in adapters)
    if not reasons:
        note = "Primary APIs already meet the browser-assist coverage targets."
    elif not active_config.enabled:
        note = "Browser assist is disabled, so the app will stay on the official API pool."
    elif not adapter_names:
        note = "Browser assist would trigger for this date, but no approved browser adapters are configured."
    else:
        note = "Browser assist is eligible and approved adapters are available."

    return BrowserAssistPlan(
        requested_date=requested_date.isoformat(),
        coverage_status=coverage_status,
        trigger_eligible=bool(reasons),
        trigger_reasons=tuple(reasons),
        adapters_configured=adapter_names,
        target_fixture_count=active_config.target_fixture_count,
        target_analyzable_count=active_config.target_analyzable_count,
        min_active_leagues=active_config.min_active_leagues,
        note=note,
    )


def run_browser_assist(
    requested_date: calendar_date,
    primary_records: Sequence[PrimaryFixtureRecord],
    *,
    raw_fixtures: int,
    unique_fixtures: int,
    analyzable_fixtures: int,
    active_leagues: int,
    adapters: Sequence[BrowserSourceAdapter] = (),
    config: BrowserAssistConfig | None = None,
) -> BrowserAssistResult:
    active_config = config or browser_assist_config()
    if not adapters:
        adapters = default_browser_adapters(active_config)

    plan = evaluate_browser_plan(
        requested_date,
        raw_fixtures=raw_fixtures,
        unique_fixtures=unique_fixtures,
        analyzable_fixtures=analyzable_fixtures,
        active_leagues=active_leagues,
        adapters=adapters,
        config=active_config,
    )

    if not (active_config.enabled and plan.trigger_eligible and plan.adapters_configured):
        return BrowserAssistResult(
            enabled=active_config.enabled,
            requested_date=plan.requested_date,
            coverage_status=plan.coverage_status,
            trigger_eligible=plan.trigger_eligible,
            triggered=False,
            trigger_reasons=plan.trigger_reasons,
            adapters_configured=plan.adapters_configured,
            target_fixture_count=plan.target_fixture_count,
            target_analyzable_count=plan.target_analyzable_count,
            min_active_leagues=plan.min_active_leagues,
            browser_candidates=0,
            mapped_candidates=0,
            promoted_fixtures=0,
            rejected_candidates=0,
            note=plan.note,
        )

    mapped_candidates: list[BrowserFixtureCandidate] = []
    primary_index = {
        fixture_identity(
            record.match_date,
            record.canonical_competition_id,
            record.home_team,
            record.away_team,
        ): record
        for record in primary_records
    }

    promoted = 0
    rejected = 0
    raw_candidates = 0
    for adapter in adapters:
        try:
            discovered = adapter.discover(requested_date, plan)
        except Exception as exc:
            rejected += 1
            logger_note = f"Adapter {adapter.source_name} failed: {exc}"
            return BrowserAssistResult(
                enabled=active_config.enabled,
                requested_date=plan.requested_date,
                coverage_status=plan.coverage_status,
                trigger_eligible=plan.trigger_eligible,
                triggered=True,
                trigger_reasons=plan.trigger_reasons,
                adapters_configured=plan.adapters_configured,
                target_fixture_count=plan.target_fixture_count,
                target_analyzable_count=plan.target_analyzable_count,
                min_active_leagues=plan.min_active_leagues,
                browser_candidates=raw_candidates,
                mapped_candidates=len(mapped_candidates),
                promoted_fixtures=promoted,
                rejected_candidates=rejected,
                note=logger_note,
            )
        for candidate in discovered:
            raw_candidates += 1
            mapped = map_browser_candidate(candidate)
            if (
                not mapped.canonical_competition_id
                or not mapped.canonical_home_team
                or not mapped.canonical_away_team
                or mapped.mapping_confidence < 0.68
                or mapped.extraction_confidence < 0.60
            ):
                rejected += 1
                continue
            mapped_candidates.append(mapped)
            identity = fixture_identity(
                mapped.match_date,
                mapped.canonical_competition_id,
                mapped.canonical_home_team,
                mapped.canonical_away_team,
            )
            if identity not in primary_index:
                promoted += 1

    rejected += max(raw_candidates - len(mapped_candidates) - rejected, 0)
    return BrowserAssistResult(
        enabled=active_config.enabled,
        requested_date=plan.requested_date,
        coverage_status=plan.coverage_status,
        trigger_eligible=plan.trigger_eligible,
        triggered=True,
        trigger_reasons=plan.trigger_reasons,
        adapters_configured=plan.adapters_configured,
        target_fixture_count=plan.target_fixture_count,
        target_analyzable_count=plan.target_analyzable_count,
        min_active_leagues=plan.min_active_leagues,
        browser_candidates=raw_candidates,
        mapped_candidates=len(mapped_candidates),
        promoted_fixtures=promoted,
        rejected_candidates=rejected,
        note="Browser assist ran on approved adapters and staged mapped candidates for merge review.",
    )
