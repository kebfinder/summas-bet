import base64
import copy
import json
import logging
import os
import threading
import time
import sys
from contextlib import asynccontextmanager
from datetime import date as calendar_date, datetime, timedelta
from html import escape
from pathlib import Path
from secrets import compare_digest
from typing import Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from config import (
    CALIBRATED_DECISION_ENGINE_MODE,
    CALIBRATED_ENGINE_EVIDENCE_LOOKBACK_DAYS,
    CALIBRATED_ENGINE_LOW_EVIDENCE_PENALTY,
    CALIBRATED_ENGINE_MIN_CONFIDENCE,
    CALIBRATED_ENGINE_MIN_MARKET_HIT_RATE,
    CALIBRATED_ENGINE_MIN_MARKET_SAMPLES,
    CALIBRATED_ENGINE_SUPPRESS_UNDERPERFORMING_MARKETS,
    CALIBRATED_ENGINE_UNDERPERFORMANCE_PENALTY_SCALE,
    FOOTBALLDATA_COMPETITION_SWEEP_LIMIT,
    DEFAULT_FOOTBALLDATA_EXPANSION_CODES,
    DEFAULT_FOOTBALLDATA_PRODUCTION_CODES,
    FOOTBALLDATA_TARGET_FIXTURE_COUNT,
    ENABLE_CALIBRATED_DECISION_ENGINE,
    ENABLE_ENGINE_SHADOW_COMPARE_ARTIFACT,
    PUBLIC_WEB_ALLOWED_SOURCES,
    PUBLIC_WEB_ADAPTER_COOLDOWN_SECONDS,
    PUBLIC_WEB_ADAPTER_RETRY_ATTEMPTS,
    PUBLIC_WEB_ADAPTER_RETRY_BACKOFF_SECONDS,
    PUBLIC_WEB_CACHE_TTL_SECONDS,
    PUBLIC_WEB_DEDUP_WAIT_SECONDS,
    PUBLIC_WEB_DEFAULT_KICKOFF_TIME,
    PUBLIC_WEB_ENABLED,
    PUBLIC_WEB_REQUIRED_SUCCESS,
)
from database.database import Base, engine, get_db
from database.models import Fixture, HistoricalMatch, Prediction, Team
from engine.match_model import (
    blend_with_market_probability,
    build_match_context,
    build_score_matrix,
    calibrate_probability,
    market_display_name,
    market_confidence,
    market_edge,
    market_group_key,
    market_implied_probability,
    market_is_qualified,
    market_stake,
    market_stability,
    market_threshold,
    market_volatility,
)
from utils.competitions import competition_profile_for_name
from schemas import (
    AvailableDatesResponse,
    AvailableDateSummary,
    AppStatusResponse,
    CollectionSummaryResponse,
    FixtureResponse,
    HistoricalScrapeResponse,
    MatchPredictionsResponse,
    PipelineResponse,
    PredictionGenerationResponse,
    PredictionsTodayResponse,
    PredictionSummaryResponse,
    ScrapeFixturesResponse,
    TeamCreateResponse,
    TeamResponse,
    TopPredictionResponse,
    TopPredictionsListResponse,
    UpdateStatsResponse,
)
from services.stats_service import refresh_team_statistics
from services.fixture_enrichment import enrich_fixtures, summarize_enrichment
from services.decision_preview import preview_for_enriched_fixture
from services.decision_backtest import resolve_market_outcome, run_backtest_with_calibration_comparison
from services.preview_calibration import CalibrationConfig, select_best_candidate_with_calibration
from services.result_evaluator import compute_performance, evaluate_prediction
from utils.competitions import is_competition_admitted, resolve_competition
from utils.football import (
    clamp,
    current_season_year,
    day_bounds,
    day_bounds_for_date,
    normalize_team_identity,
    normalize_status,
    normalize_team_name,
)


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_VERSION = "1.6.4"
ARTIFACTS_DIR = Path("artifacts")
_MARKET_EVIDENCE_CACHE: Dict[Tuple[str, int], Dict[str, dict]] = {}
_PUBLIC_WEB_BUNDLE_CACHE: Dict[str, tuple[float, dict]] = {}
_PUBLIC_WEB_INFLIGHT: Dict[str, threading.Event] = {}
_PUBLIC_WEB_ADAPTER_COOLDOWNS: Dict[str, float] = {}
_PUBLIC_WEB_CACHE_LOCK = threading.Lock()

SITE_ACCESS_USERNAME = os.getenv("SITE_ACCESS_USERNAME", "").strip()
SITE_ACCESS_PASSWORD = os.getenv("SITE_ACCESS_PASSWORD", "").strip()
SITE_ACCESS_ENABLED = bool(SITE_ACCESS_USERNAME and SITE_ACCESS_PASSWORD)
PUBLIC_PATHS = {"/healthz"}


def _clear_runtime_caches() -> dict:
    """Clear in-memory data that can make collection look stale or empty."""
    with _PUBLIC_WEB_CACHE_LOCK:
        public_web_bundles = len(_PUBLIC_WEB_BUNDLE_CACHE)
        public_web_inflight = len(_PUBLIC_WEB_INFLIGHT)
        public_web_cooldowns = len(_PUBLIC_WEB_ADAPTER_COOLDOWNS)
        _PUBLIC_WEB_BUNDLE_CACHE.clear()
        _PUBLIC_WEB_INFLIGHT.clear()
        _PUBLIC_WEB_ADAPTER_COOLDOWNS.clear()

    market_evidence = len(_MARKET_EVIDENCE_CACHE)
    _MARKET_EVIDENCE_CACHE.clear()
    return {
        "public_web_bundles": public_web_bundles,
        "public_web_inflight": public_web_inflight,
        "public_web_adapter_cooldowns": public_web_cooldowns,
        "market_evidence": market_evidence,
    }


def _purge_expired_fixture_data(db: Session, cutoff_date: Optional[calendar_date] = None) -> dict:
    """Remove stale scheduled fixture rows and their predictions before refreshing."""
    resolved_cutoff = cutoff_date or datetime.now().date()
    cutoff_start = datetime.combine(resolved_cutoff, datetime.min.time())
    expired_fixture_ids = [
        row[0]
        for row in db.query(Fixture.id)
        .filter(Fixture.date < cutoff_start)
        .all()
    ]
    if not expired_fixture_ids:
        return {
            "cutoff_date": resolved_cutoff.isoformat(),
            "expired_fixtures_removed": 0,
            "expired_predictions_removed": 0,
        }

    expired_predictions_removed = (
        db.query(Prediction)
        .filter(Prediction.fixture_id.in_(expired_fixture_ids))
        .delete(synchronize_session=False)
    )
    expired_fixtures_removed = (
        db.query(Fixture)
        .filter(Fixture.id.in_(expired_fixture_ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return {
        "cutoff_date": resolved_cutoff.isoformat(),
        "expired_fixtures_removed": expired_fixtures_removed,
        "expired_predictions_removed": expired_predictions_removed,
    }

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created")
    yield


app = FastAPI(title="Football Prediction API", version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _site_access_allowed(request: Request) -> bool:
    if not SITE_ACCESS_ENABLED:
        return True
    if request.url.path in PUBLIC_PATHS:
        return True

    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Basic "):
        return False

    try:
        encoded = authorization.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return False

    return compare_digest(username, SITE_ACCESS_USERNAME) and compare_digest(password, SITE_ACCESS_PASSWORD)


@app.middleware("http")
async def site_access_middleware(request: Request, call_next):
    if _site_access_allowed(request):
        return await call_next(request)

    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="SUMMAS Test Access"'},
    )

class PredictionEngine:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _matrix_probability(matrix, predicate) -> float:
        probability = 0.0
        for home_goals, row in enumerate(matrix):
            for away_goals, value in enumerate(row):
                if predicate(home_goals, away_goals):
                    probability += value
        return probability

    @staticmethod
    def _fallback_context(home: Team, away: Team) -> dict:
        data_quality = max(min(min(home.matches_played or 0, away.matches_played or 0) / 16.0, 1.0), 0.25)
        competition = competition_profile_for_name(home.league or away.league)
        return {
            "home_xg": max((home.avg_scored + away.avg_conceded) / 2, 0.55),
            "away_xg": max((away.avg_scored + home.avg_conceded) / 2, 0.45),
            "data_quality": data_quality,
            "venue_support": data_quality,
            "league_support": 0.45,
            "form_gap": (home.form or 0.5) - (away.form or 0.5),
            "competition_penalty": 0.0,
            "league": {
                "matches": 0,
                "volatility": 0.56,
            },
            "competition": competition,
            "empirical": {
                "btts": max(min((home.btts_rate + away.btts_rate) / 2.0, 0.92), 0.08),
                "over15": max(min((home.over15_rate + away.over15_rate) / 2.0, 0.97), 0.15),
                "over25": max(min((home.over25_rate + away.over25_rate) / 2.0, 0.90), 0.08),
                "over35": max(min(((home.over25_rate + away.over25_rate) / 2.0) * 0.62, 0.72), 0.04),
                "home_scoring": max(min(home.attack_strength / max(away.defense_strength or 1.0, 0.45) / 2.0, 0.96), 0.25),
                "away_scoring": max(min(away.attack_strength / max(home.defense_strength or 1.0, 0.45) / 2.0, 0.90), 0.18),
            },
        }

    def predict_match(self, fixture: Fixture) -> List[Prediction]:
        predictions = []
        home = fixture.home
        away = fixture.away

        if not home or not away or home.id == away.id:
            return predictions

        if not home.avg_scored or not away.avg_scored:
            return predictions

        if self.db is not None:
            context = build_match_context(self.db, fixture)
        else:
            context = self._fallback_context(home, away)

        exp_home_goals = context["home_xg"]
        exp_away_goals = context["away_xg"]
        data_quality = context["data_quality"]

        matrix = build_score_matrix(exp_home_goals, exp_away_goals, max_goals=7)

        home_win_prob = self._matrix_probability(matrix, lambda h, a: h > a)
        draw_prob = self._matrix_probability(matrix, lambda h, a: h == a)
        away_win_prob = self._matrix_probability(matrix, lambda h, a: h < a)
        btts_prob = self._matrix_probability(matrix, lambda h, a: h > 0 and a > 0)
        over_1_5_prob = self._matrix_probability(matrix, lambda h, a: h + a >= 2)
        over_2_5_prob = self._matrix_probability(matrix, lambda h, a: h + a >= 3)
        over_3_5_prob = self._matrix_probability(matrix, lambda h, a: h + a >= 4)
        home_over_0_5_prob = self._matrix_probability(matrix, lambda h, a: h >= 1)
        home_over_1_5_prob = self._matrix_probability(matrix, lambda h, a: h >= 2)
        away_over_0_5_prob = self._matrix_probability(matrix, lambda h, a: a >= 1)
        away_over_1_5_prob = self._matrix_probability(matrix, lambda h, a: a >= 2)

        non_draw = max(1.0 - draw_prob, 0.001)
        dnb_home_prob = home_win_prob / non_draw
        dnb_away_prob = away_win_prob / non_draw

        candidate_markets = [
            ("1X2", "Home Win", home_win_prob),
            ("1X2", "Draw", draw_prob),
            ("1X2", "Away Win", away_win_prob),
            ("DoubleChance", "1X", home_win_prob + draw_prob),
            ("DoubleChance", "X2", away_win_prob + draw_prob),
            ("DoubleChance", "12", 1 - draw_prob),
            ("DrawNoBet", "Home", dnb_home_prob),
            ("DrawNoBet", "Away", dnb_away_prob),
            ("BTTS", "Yes", calibrate_probability("BTTS", "Yes", btts_prob, context)),
            ("BTTS", "No", calibrate_probability("BTTS", "No", 1 - btts_prob, context)),
            ("Over1.5", "Over", calibrate_probability("Over1.5", "Over", over_1_5_prob, context)),
            ("Under1.5", "Under", calibrate_probability("Over1.5", "Under", 1 - over_1_5_prob, context)),
            ("Over2.5", "Over", calibrate_probability("Over2.5", "Over", over_2_5_prob, context)),
            ("Under2.5", "Under", calibrate_probability("Over2.5", "Under", 1 - over_2_5_prob, context)),
            ("Over3.5", "Over", calibrate_probability("Over3.5", "Over", over_3_5_prob, context)),
            ("Under3.5", "Under", calibrate_probability("Over3.5", "Under", 1 - over_3_5_prob, context)),
            ("HomeGoals0.5", "Over", calibrate_probability("HomeGoals0.5", "Over", home_over_0_5_prob, context)),
            ("HomeGoals1.5", "Over", home_over_1_5_prob),
            ("AwayGoals0.5", "Over", calibrate_probability("AwayGoals0.5", "Over", away_over_0_5_prob, context)),
            ("AwayGoals1.5", "Over", away_over_1_5_prob),
        ]

        for market, selection, model_probability in candidate_markets:
            probability = blend_with_market_probability(market, selection, model_probability, data_quality, context)
            if probability <= 0.06:
                continue

            edge, assumed_odds = market_edge(market, selection, probability, data_quality)
            stake = market_stake(market, edge, data_quality)
            if edge <= -0.03 and stake == 0:
                continue

            pred = Prediction(
                fixture_id=fixture.id,
                market=market,
                selection=selection,
                probability=round(probability, 3),
                odds=assumed_odds,
                expected_home_goals=round(exp_home_goals, 2),
                expected_away_goals=round(exp_away_goals, 2),
                edge=round(edge, 3),
                stake=stake,
                confidence=market_confidence(probability, edge, data_quality, market),
            )
            predictions.append(pred)

        return predictions


def _reload_fixtures_by_ids(db: Session, fixture_ids):
    if not fixture_ids:
        return []
    return (
        db.query(Fixture)
        .filter(Fixture.id.in_(fixture_ids))
        .order_by(Fixture.date.asc())
        .all()
    )


def _store_fixture_predictions(db: Session, fixtures: List[Fixture]) -> int:
    """Regenerate stored model outputs for scheduled fixtures.

    ``build_match_context`` deliberately supplies league priors for newly
    discovered teams.  Do not discard those outputs just because the fixture
    has not accumulated enough local history yet: they are low-confidence
    forecasts, not automatically qualified betting recommendations.
    """
    prediction_engine = PredictionEngine(db)
    predictions_created = 0

    for fixture in fixtures:
        db.query(Prediction).filter(Prediction.fixture_id == fixture.id).delete()
        if fixture.status != "scheduled":
            continue

        for prediction in prediction_engine.predict_match(fixture):
            db.add(prediction)
            predictions_created += 1

    return predictions_created


def _parse_public_web_kickoff(requested_date: calendar_date, kickoff_time: Optional[str]) -> datetime:
    kickoff_dt, _ = _parse_public_web_kickoff_with_meta(requested_date, kickoff_time)
    return kickoff_dt


def _parse_public_web_kickoff_with_meta(requested_date: calendar_date, kickoff_time: Optional[str]) -> tuple[datetime, bool]:
    raw_time = (kickoff_time or PUBLIC_WEB_DEFAULT_KICKOFF_TIME or "12:00").strip()
    fallback_used = False
    try:
        hour_str, minute_str = raw_time.split(":", 1)
        hour = max(min(int(hour_str), 23), 0)
        minute = max(min(int(minute_str), 59), 0)
    except Exception:
        hour, minute = 12, 0
        fallback_used = True
    return datetime.combine(requested_date, datetime.min.time()).replace(hour=hour, minute=minute), fallback_used


def _public_web_cache_key(requested_date: calendar_date, allowed_sources: tuple[str, ...], days_ahead: int) -> str:
    allowed = ",".join(sorted(allowed_sources)) if allowed_sources else "ALL"
    return f"{requested_date.isoformat()}|{days_ahead}|{allowed}|enabled={int(bool(PUBLIC_WEB_ENABLED))}"


def _public_web_cached_bundle(cache_key: str) -> Optional[dict]:
    if PUBLIC_WEB_CACHE_TTL_SECONDS <= 0:
        return None
    with _PUBLIC_WEB_CACHE_LOCK:
        cached = _PUBLIC_WEB_BUNDLE_CACHE.get(cache_key)
        if not cached:
            return None
        cached_at, payload = cached
        age_seconds = time.time() - cached_at
        if age_seconds > PUBLIC_WEB_CACHE_TTL_SECONDS:
            _PUBLIC_WEB_BUNDLE_CACHE.pop(cache_key, None)
            return None
        bundle = copy.deepcopy(payload)
    status = bundle.get("source_status", {}).get("public_web", {})
    status["cache_hit"] = True
    status["cache_age_seconds"] = round(age_seconds, 3)
    return bundle


def _store_public_web_cached_bundle(cache_key: str, bundle: dict) -> None:
    if PUBLIC_WEB_CACHE_TTL_SECONDS <= 0:
        return
    status = (bundle.get("source_status") or {}).get("public_web") or {}
    if status.get("failed") or int(bundle.get("raw_fixtures", 0) or 0) <= 0:
        return
    with _PUBLIC_WEB_CACHE_LOCK:
        _PUBLIC_WEB_BUNDLE_CACHE[cache_key] = (time.time(), copy.deepcopy(bundle))


def _get_or_create_team_for_public_web(
    db: Session,
    name: str,
    league: Optional[str],
    resolver: Optional["_PublicWebTeamResolver"] = None,
) -> Team:
    active_resolver = resolver or _PublicWebTeamResolver(db)
    return active_resolver.resolve(name, league)


def _normalize_league_key(league: Optional[str]) -> str:
    normalized = normalize_team_name(league)
    return normalized or "Unknown"


class _PublicWebTeamResolver:
    def __init__(self, db: Session):
        self.db = db
        self._teams = db.query(Team).all()
        self._by_exact_league: dict[tuple[str, str], list[Team]] = {}
        self._by_identity_league: dict[tuple[str, str], list[Team]] = {}
        self._build_indexes()

    def _build_indexes(self) -> None:
        for team in self._teams:
            normalized_name = normalize_team_name(team.name)
            if not normalized_name:
                continue
            league_key = _normalize_league_key(team.league)
            self._by_exact_league.setdefault((league_key, normalized_name), []).append(team)
            normalized_identity = normalize_team_identity(normalized_name)
            if normalized_identity:
                self._by_identity_league.setdefault((league_key, normalized_identity), []).append(team)

    @staticmethod
    def _pick_unique(candidates: list[Team]) -> Optional[Team]:
        unique = {candidate.id: candidate for candidate in candidates if candidate.id is not None}
        if len(unique) == 1:
            return next(iter(unique.values()))
        return None

    def _candidates_for(self, mapping: dict[tuple[str, str], list[Team]], league_key: str, key: str) -> list[Team]:
        return list(mapping.get((league_key, key), []))

    def _reassign_if_unknown_league(self, team: Team, league: Optional[str]) -> Team:
        if league and (not team.league or team.league == "Unknown"):
            previous_league_key = _normalize_league_key(team.league)
            updated_league_key = _normalize_league_key(league)
            normalized_name = normalize_team_name(team.name)
            normalized_identity = normalize_team_identity(normalized_name)
            self._by_exact_league[(previous_league_key, normalized_name)] = [
                candidate
                for candidate in self._by_exact_league.get((previous_league_key, normalized_name), [])
                if candidate.id != team.id
            ]
            if normalized_identity:
                self._by_identity_league[(previous_league_key, normalized_identity)] = [
                    candidate
                    for candidate in self._by_identity_league.get((previous_league_key, normalized_identity), [])
                    if candidate.id != team.id
                ]
            team.league = league
            self._by_exact_league.setdefault((updated_league_key, normalized_name), []).append(team)
            if normalized_identity:
                self._by_identity_league.setdefault((updated_league_key, normalized_identity), []).append(team)
        return team

    def resolve(self, name: str, league: Optional[str]) -> Team:
        normalized_name = normalize_team_name(name)
        normalized_identity = normalize_team_identity(normalized_name)
        league_key = _normalize_league_key(league)
        unknown_key = "Unknown"

        exact_league_match = self._pick_unique(self._candidates_for(self._by_exact_league, league_key, normalized_name))
        if exact_league_match:
            return exact_league_match

        exact_unknown_match = self._pick_unique(self._candidates_for(self._by_exact_league, unknown_key, normalized_name))
        if exact_unknown_match:
            return self._reassign_if_unknown_league(exact_unknown_match, league)

        if normalized_identity:
            identity_league_match = self._pick_unique(
                self._candidates_for(self._by_identity_league, league_key, normalized_identity)
            )
            if identity_league_match:
                return identity_league_match

            identity_unknown_match = self._pick_unique(
                self._candidates_for(self._by_identity_league, unknown_key, normalized_identity)
            )
            if identity_unknown_match:
                return self._reassign_if_unknown_league(identity_unknown_match, league)

        team = Team(
            name=normalized_name,
            league=league or "Unknown",
            country=None,
            avg_scored=1.25,
            avg_conceded=1.25,
            attack_strength=1.0,
            defense_strength=1.0,
        )
        self.db.add(team)
        self.db.flush()
        self._teams.append(team)
        self._by_exact_league.setdefault((_normalize_league_key(team.league), normalized_name), []).append(team)
        if normalized_identity:
            self._by_identity_league.setdefault((_normalize_league_key(team.league), normalized_identity), []).append(team)
        return team


def _collect_public_web_bundle(db: Session, match_date: Optional[calendar_date] = None, days_ahead: int = 0):
    resolved_days_ahead = max(int(days_ahead or 0), 0)
    requested_date = _resolve_requested_date(match_date) + timedelta(days=resolved_days_ahead)
    configured_allowed_sources = tuple(source.strip().lower() for source in PUBLIC_WEB_ALLOWED_SOURCES if source.strip())
    cache_key = _public_web_cache_key(requested_date, configured_allowed_sources, resolved_days_ahead)
    cached_bundle = _public_web_cached_bundle(cache_key)
    if cached_bundle is not None:
        return cached_bundle

    wait_for_other_collector = False
    inflight_event: Optional[threading.Event] = None
    with _PUBLIC_WEB_CACHE_LOCK:
        inflight_event = _PUBLIC_WEB_INFLIGHT.get(cache_key)
        if inflight_event is None:
            inflight_event = threading.Event()
            _PUBLIC_WEB_INFLIGHT[cache_key] = inflight_event
        else:
            wait_for_other_collector = True

    if wait_for_other_collector and inflight_event is not None:
        inflight_event.wait(timeout=max(PUBLIC_WEB_DEDUP_WAIT_SECONDS, 0.1))
        cached_after_wait = _public_web_cached_bundle(cache_key)
        if cached_after_wait is not None:
            status = cached_after_wait.get("source_status", {}).get("public_web", {})
            status["request_deduped_wait"] = True
            status["request_dedup_wait_seconds"] = PUBLIC_WEB_DEDUP_WAIT_SECONDS
            return cached_after_wait

    allowlist_active = bool(configured_allowed_sources)
    allowlist_mode = "explicit_allowlist" if allowlist_active else "all_supported_sources"
    base_status = {
        "enabled": bool(PUBLIC_WEB_ENABLED),
        "requested_date": requested_date.isoformat(),
        "ran": False,
        "raw_fixtures": 0,
        "survived_unique_fixtures": 0,
        "duplicates_removed": 0,
        "failed": False,
        "failure_reason": None,
        "adapter_failures": {},
        "allowlist_mode": allowlist_mode,
        "allowed_sources": list(configured_allowed_sources),
        "cache_hit": False,
        "cache_ttl_seconds": PUBLIC_WEB_CACHE_TTL_SECONDS,
        "request_deduped_wait": False,
        "adapters": {},
        "supported_competitions": [],
        "implemented_adapter_sources": [],
        "unsupported_allowed_sources": [],
    }
    try:
        if not PUBLIC_WEB_ENABLED:
            base_status["failure_reason"] = "PUBLIC_WEB_ENABLED is false"
            result = {
                "source": "public_web",
                "source_breakdown": {"public_web": 0},
                "raw_fixtures": 0,
                "fixtures": [],
                "source_status": {"public_web": base_status},
                "note": "public_web is disabled by config.",
            }
            _store_public_web_cached_bundle(cache_key, result)
            return result

        from utils.browser_assist import BrowserAssistPlan
        from utils.browser_sources import get_default_browser_adapters, get_public_web_support_matrix

        support_matrix = get_public_web_support_matrix(configured_allowed_sources)
        supported_source_names = {row["source_name"] for row in support_matrix}
        implemented_supported_sources = [
            row["source_name"] for row in support_matrix if row.get("implemented")
        ]
        unsupported_allowed_sources = sorted(
            source for source in configured_allowed_sources if source not in supported_source_names
        )
        base_status["supported_competitions"] = support_matrix
        base_status["implemented_adapter_sources"] = implemented_supported_sources
        base_status["unsupported_allowed_sources"] = unsupported_allowed_sources
        adapters = get_default_browser_adapters(configured_allowed_sources)
        if not adapters:
            if unsupported_allowed_sources:
                unsupported_msg = f" Unknown allowlist sources: {', '.join(unsupported_allowed_sources)}."
            else:
                unsupported_msg = ""
            base_status.update(
                {
                    "ran": True,
                    "failed": True,
                    "failure_reason": (
                        "No public_web adapters matched PUBLIC_WEB_ALLOWED_SOURCES."
                        if allowlist_active
                        else "No implemented public_web adapters are currently available."
                    ) + unsupported_msg,
                }
            )
            result = {
                "source": "public_web",
                "source_breakdown": {"public_web": 0},
                "raw_fixtures": 0,
                "fixtures": [],
                "source_status": {"public_web": base_status},
                "note": base_status["failure_reason"],
            }
            _store_public_web_cached_bundle(cache_key, result)
            return result

        plan = BrowserAssistPlan(
            requested_date=requested_date.isoformat(),
            coverage_status="debug",
            trigger_eligible=True,
            trigger_reasons=(),
            adapters_configured=tuple(adapter.source_name for adapter in adapters),
            target_fixture_count=0,
            target_analyzable_count=0,
            min_active_leagues=0,
            note="public_web direct collection",
        )

        fixture_ids: list[int] = []
        raw_candidates = 0
        failures: dict[str, str] = {}
        unavailable_adapters: dict[str, str] = {}
        fixture_source_index: dict[int, str] = {}
        kickoff_fallback_fixtures: list[int] = []

        team_resolver = _PublicWebTeamResolver(db)
        for adapter in adapters:
            adapter_status = {
                "enabled": True,
                "skipped": False,
                "skip_reason": None,
                "ran": False,
                "raw_rows_found": 0,
                "parsed_fixtures": 0,
                "deduplicated_away_count": 0,
                "persisted_fixtures": 0,
                "analyzable_fixtures": 0,
                "predicted_fixtures": 0,
                "failed": False,
                "failure_reason": None,
                "retry_attempts": 0,
                "cooldown_active": False,
                "cooldown_remaining_seconds": 0,
            }
            base_status["adapters"][adapter.source_name] = adapter_status
            cooldown_until = _PUBLIC_WEB_ADAPTER_COOLDOWNS.get(adapter.source_name, 0)
            now_ts = time.time()
            if cooldown_until > now_ts:
                remaining_seconds = int(max(cooldown_until - now_ts, 0))
                cooldown_reason = f"adapter_cooldown_active ({remaining_seconds}s remaining)"
                adapter_status.update(
                    {
                        "skipped": True,
                        "skip_reason": "adapter_cooldown_active",
                        "cooldown_active": True,
                        "cooldown_remaining_seconds": remaining_seconds,
                        "failed": True,
                        "failure_reason": cooldown_reason,
                    }
                )
                unavailable_adapters[adapter.source_name] = cooldown_reason
                continue

            discovered = []
            attempts = max(PUBLIC_WEB_ADAPTER_RETRY_ATTEMPTS, 1)
            for attempt in range(1, attempts + 1):
                adapter_status["retry_attempts"] = attempt
                try:
                    discovered = adapter.discover(requested_date, plan) or []
                    adapter_status["ran"] = True
                    _PUBLIC_WEB_ADAPTER_COOLDOWNS.pop(adapter.source_name, None)
                    break
                except Exception as exc:
                    adapter_status["failure_reason"] = str(exc)
                    if attempt >= attempts:
                        failures[adapter.source_name] = str(exc)
                        adapter_status["failed"] = True
                        cooldown_seconds = max(PUBLIC_WEB_ADAPTER_COOLDOWN_SECONDS, 0)
                        if cooldown_seconds:
                            _PUBLIC_WEB_ADAPTER_COOLDOWNS[adapter.source_name] = time.time() + cooldown_seconds
                            adapter_status["cooldown_active"] = True
                            adapter_status["cooldown_remaining_seconds"] = cooldown_seconds
                        logger.warning("public_web adapter %s failed for %s: %s", adapter.source_name, requested_date.isoformat(), exc)
                    else:
                        sleep_seconds = PUBLIC_WEB_ADAPTER_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                        logger.warning(
                            "public_web adapter %s retry %s/%s after failure: %s",
                            adapter.source_name,
                            attempt,
                            attempts,
                            exc,
                        )
                        if sleep_seconds > 0:
                            time.sleep(sleep_seconds)

            adapter_status["raw_rows_found"] = len(discovered)
            raw_candidates += len(discovered)
            persisted_ids_for_adapter: list[int] = []
            for candidate in discovered:
                home_name = normalize_team_name(candidate.home_team)
                away_name = normalize_team_name(candidate.away_team)
                if not home_name or not away_name or home_name == away_name:
                    continue
                adapter_status["parsed_fixtures"] += 1

                competition = resolve_competition("storage", name=candidate.competition_name)
                league_name = (
                    competition.display_name
                    if competition
                    else (candidate.competition_name or "Public Web")
                )
                kickoff_dt, kickoff_fallback_used = _parse_public_web_kickoff_with_meta(requested_date, candidate.kickoff_time)
                day_start = datetime.combine(requested_date, datetime.min.time())
                day_end = day_start + timedelta(days=1)

                try:
                    with db.begin_nested():
                        home_team = _get_or_create_team_for_public_web(db, home_name, league_name, team_resolver)
                        away_team = _get_or_create_team_for_public_web(db, away_name, league_name, team_resolver)

                        existing = (
                            db.query(Fixture)
                            .filter(
                                Fixture.home_id == home_team.id,
                                Fixture.away_id == away_team.id,
                                Fixture.date >= day_start,
                                Fixture.date < day_end,
                            )
                            .first()
                        )
                        if existing:
                            existing.date = kickoff_dt
                            existing.status = normalize_status(candidate.match_status, is_finished=False)
                            if not existing.league or existing.league == "Unknown":
                                existing.league = league_name
                            db.flush()
                            fixture_ids.append(existing.id)
                            if existing.id is not None:
                                fixture_source_index[existing.id] = adapter.source_name
                                persisted_ids_for_adapter.append(existing.id)
                                if kickoff_fallback_used:
                                    kickoff_fallback_fixtures.append(existing.id)
                            continue

                        fixture = Fixture(
                            date=kickoff_dt,
                            home_id=home_team.id,
                            away_id=away_team.id,
                            league=league_name,
                            status=normalize_status(candidate.match_status, is_finished=False),
                        )
                        db.add(fixture)
                        db.flush()
                        if fixture.id is not None:
                            fixture_ids.append(fixture.id)
                            fixture_source_index[fixture.id] = adapter.source_name
                            persisted_ids_for_adapter.append(fixture.id)
                            if kickoff_fallback_used:
                                kickoff_fallback_fixtures.append(fixture.id)
                except Exception as exc:
                    failures[adapter.source_name] = str(exc)
                    adapter_status["failed"] = True
                    adapter_status["failure_reason"] = str(exc)
                    logger.warning("public_web candidate ingest failed (%s): %s", adapter.source_name, exc)
            persisted_unique = sorted(set(persisted_ids_for_adapter))
            adapter_status["persisted_fixtures"] = len(persisted_unique)
            adapter_status["deduplicated_away_count"] = max(
                int(adapter_status["parsed_fixtures"]) - len(persisted_unique),
                0,
            )

        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            failures["commit"] = str(exc)

        unique_ids = sorted(set(fixture_ids))
        stored_fixtures = _reload_fixtures_by_ids(db, unique_ids)
        source_fixture_ids: dict[str, set[int]] = {}
        for fixture in stored_fixtures:
            source_name = fixture_source_index.get(fixture.id)
            if source_name:
                source_fixture_ids.setdefault(source_name, set()).add(fixture.id)

        for source_name, fixture_id_set in source_fixture_ids.items():
            adapter_status = base_status["adapters"].get(source_name)
            if not adapter_status:
                continue
            fixture_list = [fixture for fixture in stored_fixtures if fixture.id in fixture_id_set]
            analyzable_count = 0
            predicted_count = 0
            for fixture in fixture_list:
                if fixture.status != "scheduled":
                    continue
                context = build_match_context(db, fixture)
                if context["data_quality"] >= 0.42:
                    analyzable_count += 1
                if db.query(Prediction.id).filter(Prediction.fixture_id == fixture.id).first():
                    predicted_count += 1
            adapter_status["analyzable_fixtures"] = analyzable_count
            adapter_status["predicted_fixtures"] = predicted_count

        status = dict(base_status)
        adapter_rows = status.get("adapters", {})
        ran_adapters = sum(1 for row in adapter_rows.values() if row.get("ran"))
        skipped_cooldown = sum(
            1 for row in adapter_rows.values() if row.get("skip_reason") == "adapter_cooldown_active"
        )
        combined_failures = {**unavailable_adapters, **failures}
        status.update(
            {
                "ran": True,
                "raw_fixtures": int(raw_candidates),
                "survived_unique_fixtures": len(unique_ids),
                "duplicates_removed": max(raw_candidates - len(unique_ids), 0),
                "failed": bool(combined_failures),
                "failure_reason": "; ".join(f"{k}: {v}" for k, v in combined_failures.items()) if combined_failures else None,
                "adapter_failures": failures,
                "adapter_unavailable": unavailable_adapters,
                "adapters_total": len(adapter_rows),
                "adapters_ran": ran_adapters,
                "adapters_skipped_cooldown": skipped_cooldown,
                "kickoff_parse_fallback_count": len(set(kickoff_fallback_fixtures)),
            }
        )
        for adapter_name, adapter_status in sorted(status.get("adapters", {}).items()):
            logger.info(
                "public_web adapter=%s enabled=%s skipped=%s ran=%s raw=%s parsed=%s dedupe_away=%s persisted=%s analyzable=%s predicted=%s failed=%s reason=%s",
                adapter_name,
                adapter_status.get("enabled"),
                adapter_status.get("skipped"),
                adapter_status.get("ran"),
                adapter_status.get("raw_rows_found"),
                adapter_status.get("parsed_fixtures"),
                adapter_status.get("deduplicated_away_count"),
                adapter_status.get("persisted_fixtures"),
                adapter_status.get("analyzable_fixtures"),
                adapter_status.get("predicted_fixtures"),
                adapter_status.get("failed"),
                adapter_status.get("failure_reason"),
            )
        logger.info(
            "public_web collection %s | mode=%s allowed=%s raw=%s unique=%s dedupe_removed=%s failed=%s reason=%s",
            requested_date.isoformat(),
            allowlist_mode,
            ",".join(configured_allowed_sources) if configured_allowed_sources else "ALL",
            status["raw_fixtures"],
            status["survived_unique_fixtures"],
            status["duplicates_removed"],
            status["failed"],
            status["failure_reason"],
        )
        result = {
            "source": "public_web",
            "source_breakdown": {"public_web": len(unique_ids)},
            "raw_fixtures": int(raw_candidates),
            "fixtures": stored_fixtures,
            "source_status": {"public_web": status},
            "note": None if not status["failed"] else f"public_web partial failure: {status['failure_reason']}",
        }
        _store_public_web_cached_bundle(cache_key, result)
        return result
    finally:
        with _PUBLIC_WEB_CACHE_LOCK:
            active_event = _PUBLIC_WEB_INFLIGHT.pop(cache_key, None)
            if active_event is not None:
                active_event.set()


def _public_web_failure_bundle(match_date: Optional[calendar_date], reason: str) -> dict:
    safe_reason = str(reason or "unknown public_web failure")
    configured_allowed_sources = tuple(source.strip().lower() for source in PUBLIC_WEB_ALLOWED_SOURCES if source.strip())
    allowlist_active = bool(configured_allowed_sources)
    from utils.browser_sources import get_public_web_support_matrix

    support_matrix = get_public_web_support_matrix(configured_allowed_sources)
    supported_source_names = {row["source_name"] for row in support_matrix}
    unsupported_allowed_sources = sorted(
        source for source in configured_allowed_sources if source not in supported_source_names
    )
    return {
        "source": "public_web",
        "source_breakdown": {"public_web": 0},
        "raw_fixtures": 0,
        "fixtures": [],
        "source_status": {
            "public_web": {
                "enabled": bool(PUBLIC_WEB_ENABLED),
                "requested_date": _requested_date_value(match_date),
                "ran": True,
                "raw_fixtures": 0,
                "survived_unique_fixtures": 0,
                "duplicates_removed": 0,
                "failed": True,
                "failure_reason": safe_reason,
                "allowlist_mode": "explicit_allowlist" if allowlist_active else "all_supported_sources",
                "allowed_sources": list(configured_allowed_sources),
                "cache_hit": False,
                "cache_ttl_seconds": PUBLIC_WEB_CACHE_TTL_SECONDS,
                "request_deduped_wait": False,
                "adapters": {},
                "supported_competitions": support_matrix,
                "implemented_adapter_sources": [
                    row["source_name"] for row in support_matrix if row.get("implemented")
                ],
                "unsupported_allowed_sources": unsupported_allowed_sources,
            }
        },
        "note": f"public_web failed safely: {safe_reason}",
    }


def _collect_openligadb_bundle(db: Session, match_date: Optional[calendar_date] = None, days_ahead: int = 0):
    from scrapers.openligadb_scraper import OpenLigaDBScraper

    scraper = OpenLigaDBScraper()
    try:
        scraped = scraper.scrape_todays_matches(days_ahead=days_ahead, target_date=match_date)
    finally:
        scraper.close()

    fixture_ids = _scraped_fixture_ids(scraped)
    status = {
        "enabled": True,
        "requested_date": _requested_date_value(match_date),
        "ran": True,
        "raw_fixtures": len(fixture_ids),
        "survived_unique_fixtures": len(set(fixture_ids)),
        "duplicates_removed": max(len(fixture_ids) - len(set(fixture_ids)), 0),
        "failed": False,
        "failure_reason": None,
    }
    return {
        "source": "openligadb",
        "source_breakdown": {"openligadb": len(fixture_ids)},
        "raw_fixtures": len(fixture_ids),
        "fixtures": _reload_fixtures_by_ids(db, fixture_ids),
        "source_status": {"openligadb": status},
    }


def _collect_footballdata_bundle(db: Session, match_date: Optional[calendar_date] = None, days_ahead: int = 0):
    from scrapers.football_data_api import FootballDataAPI

    api_key = os.getenv("FOOTBALL_DATA_API_KEY")
    if not api_key:
        return {
            "source": "footballdata",
            "source_breakdown": {},
            "raw_fixtures": 0,
            "fixtures": [],
            "note": "FOOTBALL_DATA_API_KEY not set",
            "missing_api_key": True,
            "source_status": {
                "footballdata": {
                    "enabled": True,
                    "requested_date": _requested_date_value(match_date),
                    "ran": False,
                    "raw_fixtures": 0,
                    "survived_unique_fixtures": 0,
                    "duplicates_removed": 0,
                    "failed": True,
                    "failure_reason": "FOOTBALL_DATA_API_KEY not set",
                }
            },
        }

    scraper = FootballDataAPI(api_key)
    try:
        accessible_codes = scraper.get_accessible_competition_codes()
        broad_scraped = scraper.get_matches_today(days_ahead=days_ahead, target_date=match_date)
        broad_fixture_ids = _scraped_fixture_ids(broad_scraped)

        production_codes = [
            code
            for code in DEFAULT_FOOTBALLDATA_PRODUCTION_CODES
            if is_competition_admitted("footballdata", code=code, name=code)
            and (not accessible_codes or code in accessible_codes)
        ]
        expansion_codes = [
            code
            for code in DEFAULT_FOOTBALLDATA_EXPANSION_CODES
            if is_competition_admitted("footballdata", code=code, name=code)
            and (not accessible_codes or code in accessible_codes)
        ]
        skipped_production_codes = [
            code for code in DEFAULT_FOOTBALLDATA_PRODUCTION_CODES if code not in production_codes
        ]
        skipped_expansion_codes = [
            code for code in DEFAULT_FOOTBALLDATA_EXPANSION_CODES if code not in expansion_codes
        ]
        prioritized_production_codes = _prioritize_footballdata_codes(production_codes, match_date)
        prioritized_expansion_codes = _prioritize_footballdata_codes(expansion_codes, match_date)
        competition_request_budget = max(int(FOOTBALLDATA_COMPETITION_SWEEP_LIMIT), 0)

        production_scraped = []
        selected_production_codes = prioritized_production_codes[:competition_request_budget] if competition_request_budget else []
        if len(broad_fixture_ids) < FOOTBALLDATA_TARGET_FIXTURE_COUNT and selected_production_codes:
            production_scraped = scraper.get_matches_for_competitions(
                selected_production_codes,
                days_ahead=days_ahead,
                target_date=match_date,
            )
            logger.info(
                "Football-Data production sweep collected %s fixtures for %s using %s prioritized competitions",
                len(production_scraped),
                _requested_date_value(match_date),
                len(selected_production_codes),
            )

        expansion_scraped = []
        combined_primary = len({*broad_fixture_ids, *_scraped_fixture_ids(production_scraped)})
        remaining_budget = max(competition_request_budget - len(selected_production_codes), 0)
        selected_expansion_codes = prioritized_expansion_codes[:remaining_budget] if remaining_budget else []
        if combined_primary < FOOTBALLDATA_TARGET_FIXTURE_COUNT and selected_expansion_codes:
            logger.info(
                "Football-Data expansion sweep activated for %s (%s/%s fixtures after broad+production)",
                _requested_date_value(match_date),
                combined_primary,
                FOOTBALLDATA_TARGET_FIXTURE_COUNT,
            )
            expansion_scraped = scraper.get_matches_for_competitions(
                selected_expansion_codes,
                days_ahead=days_ahead,
                target_date=match_date,
            )
    finally:
        scraper.close()

    combined = {}
    for fixture in list(broad_scraped) + list(production_scraped) + list(expansion_scraped):
        identity = sa_inspect(fixture).identity if fixture is not None else None
        if identity:
            combined[identity[0]] = fixture

    fixture_ids = list(combined.keys())
    note = None
    if skipped_production_codes or skipped_expansion_codes:
        note_parts = []
        if skipped_production_codes:
            note_parts.append(f"{len(skipped_production_codes)} production codes unavailable to the current football-data token")
        if skipped_expansion_codes:
            note_parts.append(f"{len(skipped_expansion_codes)} expansion codes unavailable to the current football-data token")
        note = "football-data collection used a broad daily sweep plus targeted competition passes. Token limitations: " + "; ".join(note_parts) + "."
    elif broad_fixture_ids:
        note = "football-data collection used a broad daily sweep plus targeted competition passes."

    unique_fixture_ids = list(combined.keys())
    status = {
        "enabled": True,
        "requested_date": _requested_date_value(match_date),
        "ran": True,
        "raw_fixtures": len(broad_fixture_ids) + len(_scraped_fixture_ids(production_scraped)) + len(_scraped_fixture_ids(expansion_scraped)),
        "survived_unique_fixtures": len(unique_fixture_ids),
        "duplicates_removed": max(
            (len(broad_fixture_ids) + len(_scraped_fixture_ids(production_scraped)) + len(_scraped_fixture_ids(expansion_scraped))) - len(unique_fixture_ids),
            0,
        ),
        "failed": False,
        "failure_reason": None,
    }
    return {
        "source": "footballdata",
        "source_breakdown": {
            "footballdata_broad": len(broad_fixture_ids),
            "footballdata_production": len(_scraped_fixture_ids(production_scraped)),
            "footballdata_expansion": len(_scraped_fixture_ids(expansion_scraped)),
        },
        "raw_fixtures": len(broad_fixture_ids) + len(_scraped_fixture_ids(production_scraped)) + len(_scraped_fixture_ids(expansion_scraped)),
        "fixtures": _reload_fixtures_by_ids(db, fixture_ids),
        "note": note,
        "missing_api_key": False,
        "source_status": {"footballdata": status},
    }




def _collect_apifootball_bundle(db: Session, match_date: Optional[calendar_date] = None, days_ahead: int = 0):
    from scrapers.api_football_api import APIFootballAPI

    api_key = os.getenv("API_FOOTBALL_API_KEY") or os.getenv("RAPIDAPI_KEY")
    if not api_key:
        status = {
            "source": "api_football",
            "failed": True,
            "missing_api_key": True,
            "raw_fixtures": 0,
            "unique_fixtures": 0,
            "duplicates_removed": 0,
            "fixtures_visible": 0,
            "fixture_enrichment_summary": None,
            "note": "API_FOOTBALL_API_KEY (or RAPIDAPI_KEY) not configured",
        }
        return {
            "source": "api_football",
            "raw_fixtures": 0,
            "source_breakdown": {"api_football": 0},
            "fixtures": [],
            "note": status["note"],
            "missing_api_key": True,
            "source_status": {"api_football": status},
        }

    scraper = APIFootballAPI(api_key)
    try:
        scraped = scraper.get_matches_today(days_ahead=days_ahead, target_date=match_date)
    finally:
        scraper.close()

    fixture_ids = _scraped_fixture_ids(scraped)
    status = _build_source_status("api_football", raw_count=len(scraped), unique_count=len(fixture_ids), missing_api_key=False)
    return {
        "source": "api_football",
        "raw_fixtures": len(scraped),
        "source_breakdown": {"api_football": len(fixture_ids)},
        "fixtures": _reload_fixtures_by_ids(db, fixture_ids),
        "note": "api-football collection completed.",
        "missing_api_key": False,
        "source_status": {"api_football": status},
    }
def _combine_collection_bundles(db: Session, *bundles):
    fixture_ids = set()
    raw_total = 0
    source_breakdown = {}
    source_status = {}

    for bundle in bundles:
        raw_total += int(bundle.get("raw_fixtures", 0) or 0)
        for key, value in (bundle.get("source_status") or {}).items():
            source_status[key] = value
        for key, value in (bundle.get("source_breakdown") or {}).items():
            source_breakdown[key] = source_breakdown.get(key, 0) + int(value or 0)
        for fixture in bundle.get("fixtures", []):
            if fixture and fixture.id:
                fixture_ids.add(fixture.id)

    return {
        "raw_fixtures": raw_total,
        "source_breakdown": source_breakdown,
        "source_status": source_status,
        "fixture_ids": sorted(fixture_ids),
        "fixtures": _reload_fixtures_by_ids(db, sorted(fixture_ids)),
    }


def _collect_fixture_bundle(source: str, db: Session, match_date: Optional[calendar_date] = None, days_ahead: int = 0):
    normalized_source = source.strip().lower() if isinstance(source, str) and source.strip() else "hybrid"

    if normalized_source == "openligadb":
        bundle = _collect_openligadb_bundle(db, match_date=match_date, days_ahead=days_ahead)
    elif normalized_source in {"footballdata", "football-data", "football_data"}:
        bundle = _collect_footballdata_bundle(db, match_date=match_date, days_ahead=days_ahead)
        if bundle.get("missing_api_key"):
            raise HTTPException(status_code=400, detail="FOOTBALL_DATA_API_KEY not set")
    elif normalized_source in {"apifootball", "api-football", "api_football"}:
        bundle = _collect_apifootball_bundle(db, match_date=match_date, days_ahead=days_ahead)
        if bundle.get("missing_api_key"):
            raise HTTPException(status_code=400, detail="API_FOOTBALL_API_KEY (or RAPIDAPI_KEY) not set")
    elif normalized_source in {"public_web", "public-web", "publicweb"}:
        try:
            bundle = _collect_public_web_bundle(db, match_date=match_date, days_ahead=days_ahead)
        except Exception as exc:
            logger.exception("public_web collection crashed unexpectedly; returning safe failure bundle")
            bundle = _public_web_failure_bundle(match_date, str(exc))
        if PUBLIC_WEB_REQUIRED_SUCCESS and bundle.get("source_status", {}).get("public_web", {}).get("failed"):
            raise HTTPException(status_code=503, detail=bundle.get("note") or "public_web collection failed")
    elif normalized_source in {"hybrid", "auto"}:
        football_bundle = _collect_footballdata_bundle(db, match_date=match_date, days_ahead=days_ahead)
        openligadb_bundle = _collect_openligadb_bundle(db, match_date=match_date, days_ahead=days_ahead)
        bundles = [football_bundle, openligadb_bundle]
        public_web_bundle = None
        if PUBLIC_WEB_ENABLED:
            try:
                public_web_bundle = _collect_public_web_bundle(db, match_date=match_date, days_ahead=days_ahead)
            except Exception as exc:
                logger.exception("public_web collection crashed in hybrid mode; continuing with API sources")
                public_web_bundle = _public_web_failure_bundle(match_date, str(exc))
            bundles.append(public_web_bundle)
            if PUBLIC_WEB_REQUIRED_SUCCESS and public_web_bundle.get("source_status", {}).get("public_web", {}).get("failed"):
                raise HTTPException(
                    status_code=503,
                    detail=public_web_bundle.get("note") or "public_web collection failed",
                )
        bundle = _combine_collection_bundles(db, *bundles)
        bundle["source"] = "hybrid"
        notes = [football_bundle.get("note")]
        if public_web_bundle:
            notes.append(public_web_bundle.get("note"))
        notes = [note for note in notes if note]
        if notes:
            bundle["note"] = " | ".join(notes)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported source: {source}")

    fixtures = bundle.get("fixtures", [])
    fixture_ids = sorted({fixture.id for fixture in fixtures if fixture is not None and fixture.id is not None})
    unique_count = len(fixture_ids)
    bundle["fixture_ids"] = fixture_ids
    bundle["unique_fixtures"] = unique_count
    bundle["duplicates_removed"] = max(int(bundle.get("raw_fixtures", 0)) - unique_count, 0)
    bundle["fixture_enrichment"] = enrich_fixtures(db, fixtures)
    if bundle.get("source_status"):
        for source_name, status in bundle["source_status"].items():
            logger.info(
                "source_status[%s] ran=%s raw=%s unique=%s duplicates_removed=%s failed=%s reason=%s",
                source_name,
                status.get("ran"),
                status.get("raw_fixtures"),
                status.get("survived_unique_fixtures"),
                status.get("duplicates_removed"),
                status.get("failed"),
                status.get("failure_reason"),
            )
    return bundle


def _source_probe_for_date(
    db: Session,
    source: str,
    requested_date: calendar_date,
) -> dict:
    normalized_source = (source or "").strip().lower()
    result = {
        "source": source,
        "enabled": True,
        "ran": False,
        "raw_fixtures": 0,
        "survived_deduplication": 0,
        "duplicates_removed": 0,
        "failed": False,
        "failure_reason": None,
        "allowlist_mode": None,
        "allowed_sources": [],
        "source_breakdown": {},
    }

    try:
        bundle = _collect_fixture_bundle(normalized_source, db, match_date=requested_date)
        status_payload = dict(bundle.get("source_status", {}).get(normalized_source, {}))
        result.update(
            {
                "enabled": bool(status_payload.get("enabled", True)),
                "ran": bool(status_payload.get("ran", True)),
                "raw_fixtures": int(status_payload.get("raw_fixtures", bundle.get("raw_fixtures", 0))),
                "survived_deduplication": int(status_payload.get("survived_unique_fixtures", bundle.get("unique_fixtures", 0))),
                "duplicates_removed": int(status_payload.get("duplicates_removed", bundle.get("duplicates_removed", 0))),
                "failed": bool(status_payload.get("failed", False)),
                "source_breakdown": dict(bundle.get("source_breakdown", {})),
                "failure_reason": status_payload.get("failure_reason") or bundle.get("note"),
                "allowlist_mode": status_payload.get("allowlist_mode"),
                "allowed_sources": list(status_payload.get("allowed_sources", [])),
            }
        )
    except HTTPException as exc:
        result.update(
            {
                "ran": False,
                "failed": True,
                "failure_reason": str(exc.detail),
            }
        )
    except Exception as exc:
        result.update(
            {
                "ran": False,
                "failed": True,
                "failure_reason": str(exc),
            }
        )

    return result


def _scrape_fixtures(source: str, db: Session, match_date: Optional[calendar_date] = None, days_ahead: int = 0):
    return _collect_fixture_bundle(source, db, match_date=match_date, days_ahead=days_ahead)["fixtures"]


def _app_status_payload():
    return {
        "message": "Football Prediction API",
        "status": "running",
        "version": APP_VERSION,
        "endpoints": [
            "/healthz",
            "/dates/available",
            "/day-summary",
            "/debug/enrichment",
            "/debug/sources/status",
            "/debug/decision-engine/mode",
            "/debug/decision-engine/compare",
            "/maintenance/refresh-state",
            "/status",
            "/scrape/today",
            "/fixtures",
            "/predictions/today",
            "/predictions/by-date",
            "/predictions/top",
            "/summary",
            "/docs",
        ],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/maintenance/refresh-state")
def refresh_runtime_state(
    cutoff_date: Optional[calendar_date] = Query(None, alias="date", description="Remove fixture rows before this day; defaults to today"),
    db: Session = Depends(get_db),
):
    """Clear runtime caches and remove expired fixture/prediction data."""
    return {
        "status": "refreshed",
        "cache_cleanup": _clear_runtime_caches(),
        "expired_data_cleanup": _purge_expired_fixture_data(db, cutoff_date=cutoff_date),
    }


def _resolve_requested_date(match_date: Optional[calendar_date]) -> calendar_date:
    if isinstance(match_date, datetime):
        return match_date.date()
    if isinstance(match_date, calendar_date):
        return match_date
    if isinstance(match_date, str) and match_date.strip():
        return calendar_date.fromisoformat(match_date[:10])
    return datetime.now().date()


def _requested_date_value(match_date: Optional[calendar_date]) -> str:
    return _resolve_requested_date(match_date).isoformat()


def _resolve_fixture_window(match_date: Optional[calendar_date]):
    return day_bounds_for_date(_resolve_requested_date(match_date))


def _scraped_fixture_ids(scraped):
    fixture_ids = []
    for fixture in scraped or []:
        identity = sa_inspect(fixture).identity if fixture is not None else None
        if identity:
            fixture_ids.append(identity[0])
    return fixture_ids


def _prioritize_footballdata_codes(codes, match_date: Optional[calendar_date] = None):
    requested_date = _resolve_requested_date(match_date)
    weekday = requested_date.weekday()

    if weekday in {1, 2, 3}:  # Tue-Thu tends to favor continental/cup schedules
        class_order = {
            "continental_club": 0,
            "domestic_cup": 1,
            "secondary_domestic": 2,
            "elite_domestic": 3,
            "third_tier_domestic": 4,
            "womens_domestic": 5,
            "global_cup": 6,
            "experimental": 7,
        }
        special_priority = {"CLI": 0, "CL": 1, "EL": 2, "UCL": 3, "DFB": 4}
    elif weekday == 0:  # Monday is usually thinner; favor cups/continental and secondary leagues
        class_order = {
            "continental_club": 0,
            "secondary_domestic": 1,
            "domestic_cup": 2,
            "elite_domestic": 3,
            "third_tier_domestic": 4,
            "womens_domestic": 5,
            "global_cup": 6,
            "experimental": 7,
        }
        special_priority = {"CLI": 0, "BSA": 1, "ASL": 2, "CLP": 3, "CPD": 4, "MLS": 5, "LMX": 6}
    else:  # Fri-Sun lean toward domestic league volume
        class_order = {
            "elite_domestic": 0,
            "secondary_domestic": 1,
            "third_tier_domestic": 2,
            "womens_domestic": 3,
            "continental_club": 4,
            "domestic_cup": 5,
            "global_cup": 6,
            "experimental": 7,
        }
        special_priority = {"BL1": 0, "PD": 1, "SA": 2, "FL1": 3, "DED": 4, "PPL": 5, "PL": 6}

    ordered = []
    for index, code in enumerate(codes or []):
        normalized_code = str(code or "").strip().upper()
        resolution = resolve_competition("footballdata", code=normalized_code, name=normalized_code)
        competition_class = resolution.competition_class if resolution else "experimental"
        ordered.append(
            (
                class_order.get(competition_class, 9),
                special_priority.get(normalized_code, 50),
                index,
                normalized_code,
            )
        )

    ordered.sort()
    return [item[3] for item in ordered]


def _scrape_days_ahead_for_date(match_date: Optional[calendar_date]) -> int:
    requested_date = _resolve_requested_date(match_date)
    offset = (requested_date - datetime.now().date()).days
    if offset < 0:
        raise HTTPException(status_code=400, detail="date must be today or a future day when scraping")
    return offset


def _available_date_summaries(db: Session, predicted_only: bool = False):
    fixtures = db.query(Fixture).order_by(Fixture.date.asc()).all()
    date_counts = {}

    for fixture in fixtures:
        date_key = str(fixture.date.date())
        summary = date_counts.setdefault(date_key, {"date": date_key, "fixtures": 0, "predictions": 0})
        summary["fixtures"] += 1

        prediction_count = db.query(Prediction).filter(Prediction.fixture_id == fixture.id).count()
        if prediction_count:
            summary["predictions"] += 1

    summaries = list(date_counts.values())
    if predicted_only:
        summaries = [item for item in summaries if item["predictions"] > 0]
    return summaries


def _available_date_labels(db: Session, predicted_only: bool = False):
    return [item["date"] for item in _available_date_summaries(db, predicted_only=predicted_only)]


def _empty_date_note(db: Session, requested_date: Optional[calendar_date], source: Optional[str] = None):
    resolved_date = _resolve_requested_date(requested_date)
    available_dates = _available_date_labels(db, predicted_only=True)
    if available_dates:
        source_text = f" from {source}" if source else ""
        preview = ", ".join(available_dates[:6])
        fixture_count = (
            db.query(Fixture)
            .filter(Fixture.date >= _resolve_fixture_window(resolved_date)[0], Fixture.date < _resolve_fixture_window(resolved_date)[1])
            .count()
        )
        if fixture_count:
            return f"Fixtures exist for {resolved_date.isoformat()}{source_text}, but predictions have not been generated yet."
        return f"No fixtures found for {resolved_date.isoformat()}{source_text}. Available predicted dates: {preview}"

    return f"No fixtures found for {resolved_date.isoformat()}."


def _resolve_limit(limit, default: int = 20) -> int:
    try:
        return int(limit)
    except (TypeError, ValueError):
        return default


def _resolve_min_edge(min_edge, default: float = 0.025) -> float:
    try:
        return float(min_edge)
    except (TypeError, ValueError):
        return default


def _serialize_fixture(fixture: Fixture):
    return {
        "id": fixture.id,
        "home": fixture.home.name if fixture.home else None,
        "away": fixture.away.name if fixture.away else None,
        "league": fixture.league,
        "date": str(fixture.date),
        "status": fixture.status,
    }


def _write_enrichment_debug_report(report: dict, requested_date: calendar_date) -> str:
    output_dir = ARTIFACTS_DIR / "enrichment_debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"enrichment_debug_{requested_date.isoformat()}_{timestamp}.json"
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path.as_posix()


def _write_engine_compare_artifact(report: dict, requested_date: calendar_date) -> str:
    output_dir = ARTIFACTS_DIR / "engine_compare"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"engine_compare_{requested_date.isoformat()}_{timestamp}.json"
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path.as_posix()


def _market_mapping(candidate_market: str) -> tuple[str, str]:
    key = str(candidate_market or "").strip().lower()
    if key == "over 1.5":
        return "Over1.5", "Over 1.5"
    if key == "over 2.5":
        return "Over2.5", "Over 2.5"
    if key == "under 3.5":
        return "Under3.5", "Under 3.5"
    if key == "btts":
        return "BTTS", "BTTS"
    if key == "home or draw (1x)":
        return "DoubleChance", "1X"
    if key == "away or draw (x2)":
        return "DoubleChance", "X2"
    return "Preview", str(candidate_market or "Preview")


def _calibrated_engine_config() -> CalibrationConfig:
    min_conf_raw = str(CALIBRATED_ENGINE_MIN_CONFIDENCE or "").strip()
    min_conf = None
    if min_conf_raw:
        try:
            min_conf = max(min(float(min_conf_raw), 1.0), 0.0)
        except ValueError:
            min_conf = None
    return CalibrationConfig(
        enabled=bool(ENABLE_CALIBRATED_DECISION_ENGINE),
        min_market_samples=max(int(CALIBRATED_ENGINE_MIN_MARKET_SAMPLES), 0),
        min_market_hit_rate=max(min(float(CALIBRATED_ENGINE_MIN_MARKET_HIT_RATE), 1.0), 0.0),
        suppress_underperforming_markets=bool(CALIBRATED_ENGINE_SUPPRESS_UNDERPERFORMING_MARKETS),
        underperformance_penalty_scale=max(float(CALIBRATED_ENGINE_UNDERPERFORMANCE_PENALTY_SCALE), 0.0),
        low_evidence_penalty=max(float(CALIBRATED_ENGINE_LOW_EVIDENCE_PENALTY), 0.0),
        min_confidence_after_calibration=min_conf,
    )


def _lightweight_market_evidence(
    db: Session,
    start_date: calendar_date,
    end_date: calendar_date,
) -> Dict[str, dict]:
    start_dt, _ = _resolve_fixture_window(start_date)
    _, end_dt = _resolve_fixture_window(end_date)
    completed = (
        db.query(Fixture)
        .filter(Fixture.date >= start_dt, Fixture.date < end_dt)
        .filter(Fixture.home_goals.isnot(None), Fixture.away_goals.isnot(None))
        .all()
    )
    tracked_markets = (
        "Over 1.5",
        "Over 2.5",
        "BTTS",
        "Under 3.5",
        "Home or Draw (1X)",
        "Away or Draw (X2)",
    )
    counters = {market: {"samples": 0, "hits": 0} for market in tracked_markets}
    for fixture in completed:
        home_goals = int(fixture.home_goals or 0)
        away_goals = int(fixture.away_goals or 0)
        for market in tracked_markets:
            outcome = resolve_market_outcome(market, home_goals=home_goals, away_goals=away_goals)
            counters[market]["samples"] += 1
            if outcome == "hit":
                counters[market]["hits"] += 1

    return {
        market: {
            "samples": stat["samples"],
            "hits": stat["hits"],
            "hit_rate": round((stat["hits"] / stat["samples"]), 4) if stat["samples"] else 0.0,
        }
        for market, stat in counters.items()
    }


def _cached_market_evidence(db: Session, resolved_date: calendar_date) -> Dict[str, dict]:
    lookback_days = max(int(CALIBRATED_ENGINE_EVIDENCE_LOOKBACK_DAYS), 1)
    lookback_start = resolved_date - timedelta(days=lookback_days)
    cache_key = (resolved_date.isoformat(), lookback_days)
    cached = _MARKET_EVIDENCE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    evidence = _lightweight_market_evidence(db, lookback_start, resolved_date)
    _MARKET_EVIDENCE_CACHE[cache_key] = evidence
    return evidence


def _resolved_decision_engine_mode() -> str:
    explicit_mode = str(CALIBRATED_DECISION_ENGINE_MODE or "").strip().lower()
    if explicit_mode in {"active", "shadow"}:
        return explicit_mode
    if ENABLE_CALIBRATED_DECISION_ENGINE:
        return "active"
    return "legacy"


def _calibrated_engine_picks(
    db: Session,
    match_date: Optional[calendar_date],
    limit: int = 20,
    min_edge: float = 0.025,
):
    resolved_date = _resolve_requested_date(match_date)
    start, end = _resolve_fixture_window(resolved_date)
    fixtures = (
        db.query(Fixture)
        .filter(Fixture.date >= start, Fixture.date < end, Fixture.status == "scheduled")
        .order_by(Fixture.date.asc())
        .all()
    )
    if not fixtures:
        return {"picks": [], "engine_path": "calibrated_engine_no_fixtures", "fallback_reason": "no_scheduled_fixtures"}

    evidence = _cached_market_evidence(db, resolved_date)
    calibration_config = _calibrated_engine_config()

    enrichment_map = enrich_fixtures(db, fixtures)
    picks = []
    skipped = 0
    for fixture in fixtures:
        enrichment = enrichment_map.get(fixture.id) or {}
        fixture_preview = preview_for_enriched_fixture(enrichment)
        best = select_best_candidate_with_calibration(
            fixture_preview=fixture_preview,
            enrichment_completeness=str(enrichment.get("enrichment_completeness", "missing")),
            config=calibration_config,
            market_evidence=evidence,
        )
        if not best:
            skipped += 1
            continue

        market, selection = _market_mapping(str(best.get("candidate_market") or "Preview"))
        confidence = float(best.get("confidence_score", 0.0) or 0.0)
        fair_odds = round(1.0 / confidence, 2) if confidence > 0 else None
        confidence_score = round(confidence * 100, 1)
        edge = round(confidence - 0.5, 3)
        if edge < float(min_edge):
            skipped += 1
            continue
        picks.append(
            {
                "fixture_id": fixture.id,
                "match": f"{fixture.home.name} vs {fixture.away.name}" if fixture.home and fixture.away else "Unknown",
                "league": fixture.league,
                "date": str(fixture.date),
                "market": market,
                "selection": selection,
                "probability": round(confidence, 3),
                "market_probability": round(confidence, 3),
                "edge": edge,
                "fair_odds": fair_odds,
                "rank_score": confidence_score,
                "stake": round(max(confidence - 0.45, 0.0) * 10, 2),
                "confidence": best.get("confidence_band", "low"),
                "confidence_score": confidence_score,
                "confidence_grade": _confidence_grade(confidence_score),
                "odds": None,
                "expected_home_goals": None,
                "expected_away_goals": None,
                "main_reasons": [str(best.get("explanation") or "Calibrated preview selection.")],
                "risk_notes": [f"enrichment_completeness={enrichment.get('enrichment_completeness', 'missing')}"],
            }
        )

    picks.sort(key=lambda item: item["confidence_score"], reverse=True)
    return {
        "picks": picks[:limit],
        "engine_path": "calibrated_engine",
        "fallback_reason": "insufficient_calibrated_candidates_or_edge" if (not picks and skipped > 0) else None,
        "skipped_fixtures": skipped,
        "evidence_summary": {
            "markets_with_samples": sum(1 for row in evidence.values() if int(row.get("samples", 0) or 0) > 0),
            "lookback_days": max(int(CALIBRATED_ENGINE_EVIDENCE_LOOKBACK_DAYS), 1),
        },
    }


def _shadow_engine_comparison(
    db: Session,
    match_date: Optional[calendar_date],
    legacy_count: int,
    calibrated: Optional[dict] = None,
) -> dict:
    calibrated_result = calibrated or {}
    calibrated_picks = calibrated_result.get("picks", []) if isinstance(calibrated_result, dict) else []
    if calibrated_result and calibrated_picks:
        status = "available"
    elif calibrated_result and calibrated_result.get("fallback_reason"):
        status = "empty"
    else:
        status = "unavailable"
    report = {
        "date": _requested_date_value(match_date),
        "engine_mode": "shadow",
        "legacy_count": int(legacy_count),
        "calibrated_count": len(calibrated_picks),
        "shadow_status": status,
        "calibrated_engine_path": calibrated_result.get("engine_path"),
        "calibrated_fallback_reason": calibrated_result.get("fallback_reason"),
    }
    if ENABLE_ENGINE_SHADOW_COMPARE_ARTIFACT:
        resolved_date = _resolve_requested_date(match_date)
        report["artifact_path"] = _write_engine_compare_artifact(report, resolved_date)
    return report


def _competition_flags(league_name: Optional[str]) -> dict:
    league_text = str(league_name or "").lower()
    return {
        "cup_like": any(token in league_text for token in ("cup", "pokal", "knockout", "playoff", "play-off")),
        "final_like": "final" in league_text,
    }


def _price_quality_score(odds: Optional[float]) -> float:
    if not odds or odds <= 1.0:
        return 0.35
    centered = abs(odds - 2.1)
    return round(clamp(1.0 - (centered / 2.8), 0.25, 1.0), 3)


def _confidence_grade(confidence_score: float) -> str:
    if confidence_score >= 85:
        return "A"
    if confidence_score >= 78:
        return "B"
    if confidence_score >= 70:
        return "C"
    return "Watchlist"


def _prediction_details(prediction: Prediction, context: dict) -> dict:
    market_probability, _ = market_implied_probability(prediction.market, prediction.selection)
    market_probability = round(market_probability or 0.0, 3)
    fair_odds = round(1.0 / prediction.probability, 2) if prediction.probability else None
    stability = market_stability(prediction.market)
    threshold = market_threshold(prediction.market)
    volatility = market_volatility(prediction.market)
    flags = _competition_flags(prediction.fixture.league if prediction.fixture else None)
    competition = context.get("competition", {})

    edge_score = clamp(prediction.edge / 0.18, 0.0, 1.0)
    data_quality = context["data_quality"]
    venue_strength = context.get("venue_support", data_quality)
    calibration_fit = clamp((0.55 * data_quality) + (0.45 * stability), 0.0, 1.0)

    lineup_certainty = 0.62
    if flags["cup_like"]:
        lineup_certainty -= 0.08
    if flags["final_like"]:
        lineup_certainty -= 0.05
    if data_quality < 0.55:
        lineup_certainty -= 0.08
    lineup_certainty = clamp(lineup_certainty, 0.30, 0.85)

    confidence_score = 100 * (
        0.35 * edge_score
        + 0.20 * data_quality
        + 0.15 * calibration_fit
        + 0.10 * venue_strength
        + 0.10 * lineup_certainty
        + 0.10 * stability
    )

    if context["league"]["matches"] < 60:
        confidence_score -= 6
    if flags["cup_like"]:
        confidence_score -= 4
    if flags["final_like"]:
        confidence_score -= 5
    if volatility == "high":
        confidence_score -= 3
    confidence_score += competition.get("confidence_delta", 0.0)

    confidence_score = round(clamp(confidence_score, 0.0, 99.0), 1)
    confidence_grade = _confidence_grade(confidence_score)
    price_quality = _price_quality_score(prediction.odds)
    rank_score = 100 * (
        0.40 * edge_score
        + 0.20 * (confidence_score / 100.0)
        + 0.15 * calibration_fit
        + 0.10 * stability
        + 0.10 * data_quality
        + 0.05 * price_quality
    )
    rank_score = round(clamp(rank_score, 0.0, 99.0), 1)

    total_xg = (prediction.expected_home_goals or 0.0) + (prediction.expected_away_goals or 0.0)
    xg_gap = (prediction.expected_home_goals or 0.0) - (prediction.expected_away_goals or 0.0)
    reasons = []
    if prediction.market in {"1X2", "DrawNoBet", "DoubleChance"}:
        if prediction.selection in {"Home Win", "Home", "1X"} and xg_gap > 0.22:
            reasons.append(f"Home side projects better on xG ({prediction.expected_home_goals:.2f} vs {prediction.expected_away_goals:.2f}).")
        if prediction.selection in {"Away Win", "Away", "X2"} and xg_gap < -0.22:
            reasons.append(f"Away side projects better on xG ({prediction.expected_away_goals:.2f} vs {prediction.expected_home_goals:.2f}).")
        if abs(context.get("form_gap", 0.0)) >= 0.12:
            stronger_side = "home" if context["form_gap"] > 0 else "away"
            reasons.append(f"Recent form drift also leans toward the {stronger_side} side.")
    if prediction.market in {"BTTS", "Over1.5", "Over2.5", "Over3.5", "HomeGoals0.5", "HomeGoals1.5", "AwayGoals0.5", "AwayGoals1.5"} and total_xg >= 2.65:
        reasons.append(f"Projected match total is strong at {total_xg:.2f} goals.")
    if prediction.market in {"Under1.5", "Under2.5", "Under3.5"} and total_xg <= 2.25:
        reasons.append(f"Projected match total is muted at {total_xg:.2f} goals.")
    if data_quality >= 0.72:
        reasons.append("Recent team and league samples are reasonably stable.")
    if prediction.edge >= threshold + 0.05:
        reasons.append(f"Edge clears the {threshold:.1%} qualification line by a healthy margin.")
    reasons = reasons[:3]

    risk_notes = []
    if data_quality < 0.55:
        risk_notes.append("Thin recent sample lowers confidence.")
    if context["league"]["matches"] < 60:
        risk_notes.append("League prior is still light for this competition.")
    if competition.get("experimental"):
        risk_notes.append("This competition is in the experimental pool.")
    if competition.get("source_confidence", 0.0) < 0.75:
        risk_notes.append("Source confidence is lower than the core competition set.")
    if flags["cup_like"]:
        risk_notes.append("Cup or knockout context can distort normal league priors.")
    if flags["final_like"]:
        risk_notes.append("Final-like matches carry extra tactical variance.")
    if volatility == "high":
        risk_notes.append("This market is naturally higher variance.")
    if prediction.odds and prediction.odds >= 3.0:
        risk_notes.append("Longer prices increase result volatility.")
    risk_notes = risk_notes[:3]

    return {
        "market_probability": market_probability,
        "fair_odds": fair_odds,
        "confidence_score": confidence_score,
        "confidence_grade": confidence_grade,
        "rank_score": rank_score,
        "main_reasons": reasons,
        "risk_notes": risk_notes,
        "qualified": market_is_qualified(prediction.market, prediction.edge, data_quality),
    }


def _serialize_prediction_summary(prediction: Prediction):
    return {
        "market": market_display_name(prediction.market),
        "selection": prediction.selection,
        "probability": f"{prediction.probability:.1%}",
        "edge": f"{prediction.edge:+.1%}",
        "stake": prediction.stake,
        "confidence": prediction.confidence,
    }


def _serialize_match_predictions(fixture: Fixture, predictions: List[Prediction]):
    return {
        "match": f"{fixture.home.name} vs {fixture.away.name}" if fixture.home and fixture.away else "Unknown",
        "league": fixture.league,
        "date": str(fixture.date),
        "predictions": [_serialize_prediction_summary(prediction) for prediction in predictions],
    }


def _serialize_top_prediction(prediction: Prediction, details: dict):
    fixture = prediction.fixture
    payload = {
        "fixture_id": prediction.fixture_id,
        "match": f"{fixture.home.name} vs {fixture.away.name}" if fixture and fixture.home and fixture.away else "Unknown",
        "league": fixture.league if fixture else None,
        "date": str(fixture.date) if fixture else "",
        "market": market_display_name(prediction.market),
        "selection": prediction.selection,
        "probability": round(prediction.probability, 3),
        "market_probability": details["market_probability"],
        "edge": round(prediction.edge, 3),
        "fair_odds": details["fair_odds"],
        "rank_score": details["rank_score"],
        "stake": prediction.stake,
        "confidence": prediction.confidence,
        "confidence_score": details["confidence_score"],
        "confidence_grade": details["confidence_grade"],
        "odds": prediction.odds,
        "expected_home_goals": prediction.expected_home_goals,
        "expected_away_goals": prediction.expected_away_goals,
        "main_reasons": details["main_reasons"],
        "risk_notes": details["risk_notes"],
    }

    if fixture and fixture.home_goals is not None and fixture.away_goals is not None:
        payload["status"] = evaluate_prediction(prediction.market, prediction.selection, fixture.home_goals, fixture.away_goals)
        payload["result_score"] = f"{fixture.home_goals}-{fixture.away_goals}"
    else:
        payload["status"] = "pending"
        payload["result_score"] = None

    return payload


def _top_predictions_from_fixtures(
    db: Session,
    fixtures: List[Fixture],
    limit: int,
    min_edge: float,
):
    fixture_best = []
    for fixture in fixtures:
        ranked = _ranked_predictions_for_fixture(db, fixture, min_edge=min_edge)
        if ranked:
            fixture_best.append(ranked[0])

    fixture_best.sort(
        key=lambda item: (item[1]["rank_score"], item[0].edge, item[0].probability, item[0].stake),
        reverse=True,
    )

    picks = []
    league_limits = {}
    market_limits = {}
    for prediction, details in fixture_best:
        league_key = prediction.fixture.league if prediction.fixture else None
        current_count = league_limits.get(league_key, 0)
        market_key = market_group_key(prediction.market)
        market_count = market_limits.get(market_key, 0)
        if league_key and current_count >= 4:
            continue
        if market_count >= 6:
            continue
        if league_key:
            league_limits[league_key] = current_count + 1
        market_limits[market_key] = market_count + 1
        picks.append((prediction, details))
        if len(picks) >= limit:
            break

    return picks


def _top_predictions(
    db: Session,
    limit: int,
    min_edge: float,
    match_date: Optional[calendar_date] = None,
):
    start, end = _resolve_fixture_window(match_date)
    fixtures = (
        db.query(Fixture)
        .filter(Fixture.date >= start, Fixture.date < end)
        .order_by(Fixture.date.asc())
        .all()
    )
    return _top_predictions_from_fixtures(db, fixtures, limit, min_edge)


def _day_collection_summary(
    db: Session,
    match_date: Optional[calendar_date] = None,
    *,
    source: str = "stored",
    collection_bundle: Optional[dict] = None,
    min_edge: float = 0.025,
    limit: int = 20,
):
    start, end = _resolve_fixture_window(match_date)
    scoped_ids = list(collection_bundle.get("fixture_ids", [])) if collection_bundle else []
    if scoped_ids:
        fixtures = (
            db.query(Fixture)
            .filter(Fixture.id.in_(scoped_ids))
            .order_by(Fixture.date.asc())
            .all()
        )
    else:
        fixtures = (
            db.query(Fixture)
            .filter(Fixture.date >= start, Fixture.date < end)
            .order_by(Fixture.date.asc())
            .all()
        )

    analyzable_fixtures = 0
    predicted_fixtures = 0
    qualified_markets = 0

    for fixture in fixtures:
        if fixture.status != "scheduled":
            continue

        context = build_match_context(db, fixture)
        if context["data_quality"] >= 0.42:
            analyzable_fixtures += 1

        predictions = db.query(Prediction).filter(Prediction.fixture_id == fixture.id).all()
        if predictions:
            predicted_fixtures += 1

        for prediction in predictions:
            if market_is_qualified(prediction.market, prediction.edge, context["data_quality"]):
                qualified_markets += 1

    ranked_picks = len(_top_predictions_from_fixtures(db, fixtures, limit, min_edge))
    raw_fixtures = int(collection_bundle.get("raw_fixtures", len(fixtures))) if collection_bundle else len(fixtures)
    unique_fixtures = int(collection_bundle.get("unique_fixtures", len(fixtures))) if collection_bundle else len(fixtures)
    duplicates_removed = int(collection_bundle.get("duplicates_removed", 0)) if collection_bundle else 0
    source_breakdown = dict(collection_bundle.get("source_breakdown", {})) if collection_bundle else {}

    return {
        "requested_date": _requested_date_value(match_date),
        "source": source,
        "raw_fixtures": raw_fixtures,
        "unique_fixtures": unique_fixtures,
        "duplicates_removed": duplicates_removed,
        "active_leagues": len({fixture.league for fixture in fixtures if fixture.league}),
        "analyzable_fixtures": analyzable_fixtures,
        "predicted_fixtures": predicted_fixtures,
        "qualified_markets": qualified_markets,
        "ranked_picks": ranked_picks,
        "source_breakdown": source_breakdown,
    }


def _fixture_analyzability_diagnostic(db: Session, fixture: Fixture, enrichment: Optional[dict] = None) -> dict:
    reasons: list[str] = []
    if fixture.status != "scheduled":
        reasons.append("fixture_not_scheduled")

    competition = competition_profile_for_name(fixture.league)
    if competition.get("experimental"):
        reasons.append("competition_mapping_gap_or_experimental")
    if float(competition.get("admission_score", 0.0) or 0.0) < 0.62:
        reasons.append("unsupported_adapter_or_competition_coverage")

    home_name = getattr(getattr(fixture, "home", None), "name", None)
    away_name = getattr(getattr(fixture, "away", None), "name", None)
    if normalize_team_name(home_name) == normalize_team_name(away_name):
        reasons.append("team_normalization_mismatch")

    context = build_match_context(db, fixture)
    if context["data_quality"] < 0.42:
        reasons.append("missing_stats_or_history")
        if context.get("sample_support", 0) < 0.45:
            reasons.append("missing_recent_team_history")
        if context.get("league_support", 0) < 0.45:
            reasons.append("missing_league_history")

    if enrichment is None:
        enrichment = enrich_fixtures(db, [fixture]).get(fixture.id or -1, {})
    missing_fields = list(enrichment.get("missing_fields", []))
    if missing_fields:
        reasons.append("missing_enrichment_fields")

    unique_reasons = sorted(set(reasons))
    analyzable = fixture.status == "scheduled" and context["data_quality"] >= 0.42
    return {
        "fixture_id": fixture.id,
        "match": f"{home_name or 'Unknown'} vs {away_name or 'Unknown'}",
        "league": fixture.league,
        "kickoff": fixture.date.isoformat() if fixture.date else None,
        "analyzable": analyzable,
        "data_quality": context["data_quality"],
        "sample_support": context.get("sample_support"),
        "league_support": context.get("league_support"),
        "venue_support": context.get("venue_support"),
        "enrichment_completeness": enrichment.get("enrichment_completeness", "missing"),
        "missing_fields": missing_fields,
        "reasons": unique_reasons if unique_reasons else ["analyzable"],
    }


def _ranked_predictions_for_fixture(
    db: Session,
    fixture: Fixture,
    min_edge: float = 0.0,
    include_unqualified: bool = False,
):
    """Rank stored forecasts, optionally retaining low-evidence model outputs.

    The public match feed is a forecast feed and should show fallback forecasts
    for newly collected fixtures.  Recommendation surfaces retain the default
    behaviour and exclude unqualified picks.
    """
    context = build_match_context(db, fixture)
    predictions = db.query(Prediction).filter(Prediction.fixture_id == fixture.id).all()
    ranked = []

    for prediction in predictions:
        if prediction.edge < min_edge:
            continue
        details = _prediction_details(prediction, context)
        if not include_unqualified and prediction.stake <= 0 and not details["qualified"]:
            continue
        ranked.append((prediction, details))

    ranked.sort(
        key=lambda item: (item[1]["rank_score"], item[0].edge, item[0].probability, item[0].stake),
        reverse=True,
    )
    return ranked


def _render_homepage(db: Session):
    today_date = datetime.now().date()
    today = today_date.isoformat()
    window_start = datetime.combine(today_date, datetime.min.time())
    window_end = window_start + timedelta(days=7)
    fixture_count = (
        db.query(Fixture)
        .filter(
            Fixture.date >= window_start,
            Fixture.date < window_end,
        )
        .count()
    )
    prediction_count = (
        db.query(Prediction)
        .join(Fixture, Prediction.fixture_id == Fixture.id)
        .filter(
            Fixture.date >= window_start,
            Fixture.date < window_end,
        )
        .count()
    )
    team_count = db.query(Team).count()
    coverage_ratio = (prediction_count / fixture_count * 100.0) if fixture_count else 0.0
    available_dates = _available_date_summaries(db, predicted_only=True)

    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>SUMMAS | Premium Football Analytics</title>
        <link rel="stylesheet" href="/static/dashboard.css" />
      </head>
      <body>
        <nav class="top-bar">
          <div class="brand"><div class="brand-icon">S</div><div class="brand-text">SUMMAS <span>Analyst Desk</span></div></div>
          <div class="system-status"><span class="status-item"><span class="status-dot"></span>Live Feed</span><span class="model-badge">Model <strong>v2</strong></span></div>
        </nav>
        <main class="wrap">
          <section class="hero">
            <h1>SUMMAS Analyst Desk</h1>
            <p>
              A professional, dark-mode match intelligence board for serious decision-making.
              Select a date, run collection, and review the strongest opportunities in one premium workflow.
            </p>
            <div class="actions">
              <a id="top-json-link" class="primary" href="/board?date={today}">Readable Board</a>
              <a id="all-json-link" href="/matchday?date={today}">Readable Matchday</a>
              <a id="fixtures-json-link" href="/fixtures-view?date={today}">Readable Fixtures</a>
              <a id="summary-json-link" href="/summary?date={today}">Readable Summary</a>
              <a href="/docs">API Docs</a>
            </div>
          </section>

          <section class="stats-grid">
            <div class="stats">
              <div class="stat-card">📅 {fixture_count} Matches</div>
              <div class="stat-card">🎯 {prediction_count} Predictions</div>
              <div class="stat-card">👥 {team_count} Teams</div>
              <div class="stat-card">📊 {coverage_ratio:.0f}% Coverage</div>
            </div>
          </section>

          <section>
            <div class="section-head">
              <h2>Matchday control</h2>
              <span id="current-date-label">Loading dates...</span>
            </div>
            <div class="toolbar">
              <input id="date-input" type="date" value="{today}" />
              <button id="run-date-btn" type="button">Run Selected Date</button>
            </div>
            <p id="status-line" class="status-line">Preparing your live board...</p>
            <div id="date-strip" class="date-strip">
              <article class="empty-card">
                <h3>Loading dates</h3>
                <p>We’re preparing your available dates.</p>
              </article>
            </div>
            <button id="toggle-dates-btn" class="muted-link hidden" type="button">View more matchdays</button>
          </section>

          <section class="top-board">
            <div class="section-head">
              <h2>Top 20 opportunities</h2>
              <span id="top-picks-caption">Highest-ranked opportunities for the selected date</span>
              <span id="performance-summary" class="performance-summary">Win Rate: 0.0% · Wins: 0 | Losses: 0 | Pending: 0</span>
              <div class="actions">
                <button id="compare-toggle" class="pill" type="button">Comparison: OFF</button>
              </div>
            </div>
            <div class="filters">
              <select id="filter-league"><option value="all">All leagues</option></select>
              <select id="filter-risk"><option value="all">All risk tiers</option><option>Safe</option><option>Balanced</option><option>Bold</option></select>
              <select id="filter-stars"><option value="0">All stars</option><option value="5">5 stars</option><option value="4">4 stars+</option><option value="3">3 stars+</option></select>
              <select id="filter-value"><option value="all">All value statuses</option><option>Value</option><option>Fair</option><option>Poor value</option><option>Odds unavailable</option></select>
            </div>
            <div id="compare-panel" class="compare-panel hidden">
              <strong>Comparison</strong>
              <div id="compare-grid" class="compare-grid"></div>
            </div>
            <div class="calc calculator">
              <h3>🧮 Quick Bet Calculator</h3>
              <div class="filters" style="margin-top:8px;">
                <input id="calc-stake" type="number" min="0" step="0.01" placeholder="Stake" />
                <input id="calc-odds" type="number" min="1" step="0.01" placeholder="Odds" />
              </div>
              <p class="meta" id="calc-output">Payout = stake × odds | Profit = payout − stake</p>
            </div>
            <div id="picks" class="picks">
              <article class="empty-card">
                <h3>Loading picks</h3>
                <p>Finding the strongest options for this date.</p>
              </article>
            </div>
          </section>
        </main>
        <!-- dashboard-js-template: `View more (${{sortedRows.length - 3}} more)` | Show fewer dates -->
        <script>window.__DASHBOARD_BOOTSTRAP__ = {{today: {today!r}, available_dates: {available_dates!r}}};</script>
        <script src="/static/dashboard.js"></script>
      </body>
    </html>
    """


def _layout_shell(title: str, body: str) -> str:
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{escape(title)}</title>
        <style>
          :root {{
            --bg: #10131d;
            --ink: #F5F6FA;
            --card: #141419;
            --line: #2A2A35;
            --accent: #D4AF37;
            --accent-soft: rgba(212, 175, 55, 0.14);
            --success: #1FA971;
            --muted: #B8B6C5;
            --warn: #D1972A;
            --surface: #17171F;
            --shadow: 0 10px 28px rgba(0, 0, 0, 0.28);
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: Inter, "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--ink);
            background:
              radial-gradient(circle at 18% 0%, rgba(40, 119, 166, 0.14), transparent 38%),
              radial-gradient(circle at top right, rgba(212, 175, 55, 0.05), transparent 34%),
              var(--bg);
          }}
          .wrap {{
            max-width: 1560px;
            margin: 0 auto;
            padding: 36px 20px 56px;
          }}
          .hero {{
            display: grid;
            gap: 12px;
            margin-bottom: 28px;
          }}
          h1, h2, h3, p {{ margin: 0; }}
          h1 {{
            font-size: clamp(2rem, 4vw, 3rem);
            line-height: 0.98;
          }}
          .muted {{
            color: var(--muted);
          }}
          .actions {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 8px;
          }}
          .actions a {{
            text-decoration: none;
            color: var(--ink);
            padding: 11px 15px;
            border-radius: 999px;
            border: 1px solid var(--line);
            background: var(--card);
            transition: border-color .22s ease, transform .22s ease;
          }}
          .actions a:hover {{
            border-color: #3a3a47;
            transform: translateY(-1px);
          }}
          .actions a.primary {{
            background: linear-gradient(180deg, #1d8b62 0%, #176f4f 100%);
            color: #E8FFF4;
            border-color: rgba(31, 169, 113, .72);
          }}
          .section {{
            margin-top: 26px;
          }}
          .section-head {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 14px;
          }}
          .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 16px;
          }}
          .card {{
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 20px;
            box-shadow: var(--shadow);
          }}
          .pill {{
            display: inline-flex;
            align-items: center;
            padding: 5px 10px;
            border-radius: 999px;
            background: rgba(31, 169, 113, 0.16);
            color: #DDF9EE;
            font-size: 0.82rem;
            font-weight: 700;
            letter-spacing: 0.04em;
          }}
          .warn {{
            color: var(--warn);
          }}
          .meta {{
            color: var(--muted);
            font-size: 0.92rem;
            margin-top: 8px;
          }}
          .kv {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 8px 14px;
            margin-top: 14px;
            font-size: 0.94rem;
          }}
          .kv span {{
            color: var(--muted);
          }}
          .list {{
            margin-top: 12px;
            padding-left: 18px;
          }}
          .list li {{
            margin-top: 6px;
          }}
          .table {{
            width: 100%;
            border-collapse: collapse;
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 14px;
            overflow: hidden;
          }}
          .table th, .table td {{
            padding: 12px 14px;
            border-bottom: 1px solid var(--line);
            text-align: left;
            vertical-align: top;
          }}
          .table th {{
            color: var(--muted);
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
          }}
          .table tr:last-child td {{
            border-bottom: none;
          }}
          .empty {{
            padding: 20px;
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 14px;
          }}
          code {{
            padding: 0.15rem 0.35rem;
            border-radius: 6px;
            background: #1F1F29;
            color: #E6D8AE;
          }}
          @media (min-width: 1024px) {{
            .card {{
              border-radius: 18px;
              padding: 16px;
            }}
            .grid.compact {{
              grid-template-columns: repeat(3, minmax(0, 1fr));
              gap: 12px;
            }}
            .grid.wide {{
              grid-template-columns: repeat(4, minmax(0, 1fr));
              gap: 12px;
            }}
            .table th, .table td {{
              padding: 10px 12px;
            }}
          }}
          @media (min-width: 1280px) {{
            .grid.compact {{
              grid-template-columns: repeat(4, minmax(0, 1fr));
            }}
          }}
        </style>
      </head>
      <body>
        <nav class="top-bar">
          <div class="brand"><div class="brand-icon">S</div><div class="brand-text">SUMMAS <span>Analyst Desk</span></div></div>
          <div class="system-status"><span class="status-item"><span class="status-dot"></span>Live Feed</span><span class="model-badge">Model <strong>v2</strong></span></div>
        </nav>
        <main class="wrap">
          {body}
        </main>
      </body>
    </html>
    """


def _format_pick_card(rank: int, prediction: Prediction, details: dict) -> str:
    fixture = prediction.fixture
    match = f"{fixture.home.name} vs {fixture.away.name}" if fixture and fixture.home and fixture.away else "Unknown"
    league = fixture.league if fixture else "Unknown league"
    reasons = "".join(f"<li>{escape(reason)}</li>" for reason in details["main_reasons"]) or "<li>No standout signals were recorded for this pick.</li>"
    risks = "".join(f"<li>{escape(note)}</li>" for note in details["risk_notes"]) if details["risk_notes"] else "<li>No major warning signs were flagged here.</li>"
    grade_value = str(details.get("confidence_grade", "MEDIUM")).upper()
    edge_color = "var(--warn)" if prediction.edge < 0.03 else "var(--success)"
    grade_border = "#C64F5C" if "LOW" in grade_value else ("#1FA971" if "HIGH" in grade_value else "rgba(212, 175, 55, .6)")
    probability_width = max(0.0, min(1.0, float(prediction.probability or 0.0))) * 100.0
    rank_pill_style = (
        "background: var(--accent-soft); color: #F4E5BF;"
        if rank == 1
        else "background: rgba(31, 169, 113, 0.16); color: #DDF9EE;"
    )
    grade_pill_style = (
        "background: rgba(198, 79, 92, 0.18); color: #F4D3D8;"
        if "LOW" in grade_value
        else (
            "background: rgba(31, 169, 113, 0.2); color: #DDF9EE;"
            if "HIGH" in grade_value
            else "background: rgba(209, 151, 42, 0.2); color: #F2E2BE;"
        )
    )
    return f"""
      <article class="card" style="border-left:4px solid {grade_border};">
        <div class="section-head">
          <span class="pill" style="{rank_pill_style}">Rank {rank}</span>
          <span class="pill" style="{grade_pill_style}">{escape(grade_value)}</span>
        </div>
        <h3>{escape(match)}</h3>
        <p class="meta">{escape(str(league))} | {escape(str(fixture.date) if fixture else "")}</p>
        <p style="margin-top:12px;"><strong>{escape(market_display_name(prediction.market))}</strong>: {escape(prediction.selection or "")}</p>
        <div style="height:5px;border-radius:999px;background:#262631;overflow:hidden;margin-top:12px;">
          <div style="height:100%;width:{probability_width:.1f}%;background:linear-gradient(90deg,#177957 0%,#24b67e 100%);"></div>
        </div>
        <div class="kv">
          <div><span>Model probability</span><br />{prediction.probability:.1%}</div>
          <div><span>Market probability</span><br />{details["market_probability"]:.1%}</div>
          <div><span>Edge</span><br /><span style="color:{edge_color};font-weight:700;">{prediction.edge:+.1%}</span></div>
          <div><span>Best odds</span><br />{prediction.odds or "-"}</div>
          <div><span>Fair odds</span><br />{details["fair_odds"] or "-"}</div>
          <div><span>Rank score</span><br />{details["rank_score"]}</div>
          <div><span>Stake</span><br />{prediction.stake:.2f}</div>
          <div><span>xG</span><br />{prediction.expected_home_goals:.2f}-{prediction.expected_away_goals:.2f}</div>
        </div>
        <div class="section">
          <h3 style="font-size:1rem;">Why this pick stands out</h3>
          <ul class="list">{reasons}</ul>
        </div>
        <div class="section">
          <h3 style="font-size:1rem;">Things to watch</h3>
          <ul class="list">{risks}</ul>
        </div>
      </article>
    """


def _render_board_page(
    db: Session,
    match_date: Optional[calendar_date] = None,
    *,
    limit: int = 20,
    min_edge: float = 0.025,
) -> str:
    resolved_date = _resolve_requested_date(match_date)
    picks = _top_predictions(db, limit, min_edge, resolved_date)
    note = _empty_date_note(db, resolved_date) if not picks else None
    cards = (
        "".join(_format_pick_card(index, prediction, details) for index, (prediction, details) in enumerate(picks, start=1))
        if picks
        else f'<div class="empty"><h3>No qualified board for {escape(resolved_date.isoformat())}</h3><p class="muted">{escape(note or "No picks stored for this date yet.")}</p></div>'
    )
    body = f"""
      <section class="hero">
        <h1>Top Picks Board</h1>
        <p class="muted">Your highest-priority opportunities for {escape(resolved_date.isoformat())}, ranked with the same engine behind the API feed.</p>
        <div class="actions">
          <a class="primary" href="/?date={escape(resolved_date.isoformat())}">Back To Dashboard</a>
          <a href="/matchday?date={escape(resolved_date.isoformat())}">Readable Matchday</a>
          <a href="/summary?date={escape(resolved_date.isoformat())}">Readable Summary</a>
          <a href="/predictions/top?date={escape(resolved_date.isoformat())}">Raw JSON</a>
        </div>
      </section>
      <section class="section">
        <div class="section-head">
          <h2>Ranked opportunities</h2>
          <span class="muted">{len(picks)} ranked picks | minimum edge {min_edge:.1%}</span>
        </div>
        <div class="grid compact">
          {cards}
        </div>
      </section>
    """
    return _layout_shell(f"Readable Board - {resolved_date.isoformat()}", body)


def _render_day_summary_page(
    db: Session,
    match_date: Optional[calendar_date] = None,
    *,
    min_edge: float = 0.025,
    limit: int = 20,
) -> str:
    resolved_date = _resolve_requested_date(match_date)
    summary = _day_collection_summary(db, resolved_date, source="stored", min_edge=min_edge, limit=limit)
    source_rows = "".join(
        f"<tr><td>{escape(key)}</td><td>{value}</td></tr>"
        for key, value in sorted(summary["source_breakdown"].items())
    )
    body = f"""
      <section class="hero">
        <h1>Daily Performance Snapshot</h1>
        <p class="muted">A clear view of {escape(resolved_date.isoformat())}, from fixture coverage through final opportunities.</p>
        <div class="actions">
          <a class="primary" href="/?date={escape(resolved_date.isoformat())}">Back To Dashboard</a>
          <a href="/board?date={escape(resolved_date.isoformat())}">Readable Board</a>
          <a href="/day-summary?date={escape(resolved_date.isoformat())}">Raw JSON</a>
        </div>
      </section>
      <section class="section">
        <div class="section-head">
          <h2>Coverage journey</h2>
          <span class="muted">How much moved through each stage</span>
        </div>
        <div class="grid wide">
          <article class="card"><h3>Collected</h3><p class="meta">{summary["raw_fixtures"]} fixtures gathered</p></article>
          <article class="card"><h3>Unique Matches</h3><p class="meta">{summary["unique_fixtures"]} distinct fixtures</p></article>
          <article class="card"><h3>Ready for analysis</h3><p class="meta">{summary["analyzable_fixtures"]} met quality standards</p></article>
          <article class="card"><h3>Predicted</h3><p class="meta">{summary["predicted_fixtures"]} matches with saved predictions</p></article>
          <article class="card"><h3>Qualified markets</h3><p class="meta">{summary["qualified_markets"]} markets passed strategy thresholds</p></article>
          <article class="card"><h3>Published to board</h3><p class="meta">{summary["ranked_picks"]} final ranked picks</p></article>
        </div>
      </section>
      <section class="section">
        <div class="section-head">
          <h2>Source contribution</h2>
          <span class="muted">{summary["active_leagues"]} active leagues | {summary["duplicates_removed"]} duplicate rows removed</span>
        </div>
        {"<table class='table'><thead><tr><th>Source</th><th>Fixtures</th></tr></thead><tbody>" + source_rows + "</tbody></table>" if source_rows else "<div class='empty'><h3>No source breakdown yet</h3><p class='muted'>Refresh this date to see how each source contributed.</p></div>"}
      </section>
    """
    return _layout_shell(f"Readable Day Summary - {resolved_date.isoformat()}", body)


def _render_fixtures_page(db: Session, match_date: Optional[calendar_date] = None) -> str:
    resolved_date = _resolve_requested_date(match_date)
    start, end = _resolve_fixture_window(resolved_date)
    fixtures = (
        db.query(Fixture)
        .filter(Fixture.date >= start, Fixture.date < end)
        .order_by(Fixture.date.asc())
        .all()
    )
    rows = "".join(
        f"""
        <tr>
          <td>{escape(str(fixture.date))}</td>
          <td>{escape(fixture.home.name if fixture.home else "Unknown")}</td>
          <td>{escape(fixture.away.name if fixture.away else "Unknown")}</td>
          <td>{escape(str(fixture.league or ""))}</td>
          <td>{escape(str(fixture.status or ""))}</td>
        </tr>
        """
        for fixture in fixtures
    )
    body = f"""
      <section class="hero">
        <h1>Fixture Register</h1>
        <p class="muted">All stored fixtures for {escape(resolved_date.isoformat())}, ordered by kickoff.</p>
        <div class="actions">
          <a class="primary" href="/?date={escape(resolved_date.isoformat())}">Back To Dashboard</a>
          <a href="/matchday?date={escape(resolved_date.isoformat())}">Readable Matchday</a>
          <a href="/summary?date={escape(resolved_date.isoformat())}">Readable Summary</a>
          <a href="/fixtures?date={escape(resolved_date.isoformat())}">Raw JSON</a>
        </div>
      </section>
      <section class="section">
        <div class="section-head">
          <h2>Fixtures</h2>
          <span class="muted">{len(fixtures)} total</span>
        </div>
        {"<table class='table'><thead><tr><th>Kickoff</th><th>Home</th><th>Away</th><th>Competition</th><th>Status</th></tr></thead><tbody>" + rows + "</tbody></table>" if fixtures else f'<div class="empty"><h3>No matches found</h3><p class="muted">{escape(_empty_date_note(db, resolved_date))}</p></div>'}
      </section>
    """
    return _layout_shell(f"Readable Fixtures - {resolved_date.isoformat()}", body)


def _render_matchday_page(db: Session, match_date: Optional[calendar_date] = None) -> str:
    resolved_date = _resolve_requested_date(match_date)
    start, end = _resolve_fixture_window(resolved_date)
    fixtures = (
        db.query(Fixture)
        .filter(Fixture.date >= start, Fixture.date < end)
        .order_by(Fixture.date.asc())
        .all()
    )

    match_cards = []
    for fixture in fixtures:
        ranked = _ranked_predictions_for_fixture(db, fixture)
        top_predictions = ranked[:3]
        if not top_predictions:
            prediction_html = "<p class='muted' style='margin-top:12px;'>No ranked recommendation is available for this fixture yet.</p>"
        else:
            prediction_html = "".join(
                f"""
                <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--line);">
                  <strong>{escape(market_display_name(prediction.market))}</strong>: {escape(prediction.selection or "")}<br />
                  <span class="muted">Prob {prediction.probability:.1%} | Edge {prediction.edge:+.1%} | Grade {escape(details["confidence_grade"])}</span>
                </div>
                """
                for prediction, details in top_predictions
            )
        match_cards.append(
            f"""
            <article class="card">
              <h3>{escape(fixture.home.name if fixture.home else "Unknown")} vs {escape(fixture.away.name if fixture.away else "Unknown")}</h3>
              <p class="meta">{escape(str(fixture.league or ""))} | {escape(str(fixture.date))}</p>
              {prediction_html}
            </article>
            """
        )

    body = f"""
      <section class="hero">
        <h1>Matchday Intelligence</h1>
        <p class="muted">Fixture-by-fixture view for {escape(resolved_date.isoformat())}, including the strongest saved recommendations for each game.</p>
        <div class="actions">
          <a class="primary" href="/?date={escape(resolved_date.isoformat())}">Back To Dashboard</a>
          <a href="/board?date={escape(resolved_date.isoformat())}">Readable Board</a>
          <a href="/summary?date={escape(resolved_date.isoformat())}">Readable Summary</a>
          <a href="/predictions/by-date?date={escape(resolved_date.isoformat())}">Raw JSON</a>
        </div>
      </section>
      <section class="section">
        <div class="section-head">
          <h2>Matches</h2>
          <span class="muted">{len(fixtures)} total</span>
        </div>
        <div class="grid compact">
          {"".join(match_cards) if match_cards else f'<div class="empty"><h3>No matchday data yet</h3><p class="muted">{escape(_empty_date_note(db, resolved_date))}</p></div>'}
        </div>
      </section>
    """
    return _layout_shell(f"Readable Matchday - {resolved_date.isoformat()}", body)


@app.get("/", response_class=HTMLResponse)
def read_root(db: Session = Depends(get_db)):
    return HTMLResponse(_render_homepage(db))


@app.get("/board", response_class=HTMLResponse)
def readable_board(
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    limit: int = Query(20, ge=1, le=20),
    min_edge: float = Query(0.025, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
):
    return HTMLResponse(_render_board_page(db, match_date, limit=limit, min_edge=min_edge))


@app.get("/summary", response_class=HTMLResponse)
def readable_day_summary(
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    min_edge: float = Query(0.025, ge=0.0, le=1.0),
    limit: int = Query(20, ge=1, le=20),
    db: Session = Depends(get_db),
):
    return HTMLResponse(_render_day_summary_page(db, match_date, min_edge=min_edge, limit=limit))


@app.get("/matchday", response_class=HTMLResponse)
def readable_matchday(
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    db: Session = Depends(get_db),
):
    return HTMLResponse(_render_matchday_page(db, match_date))


@app.get("/fixtures-view", response_class=HTMLResponse)
def readable_fixtures(
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    db: Session = Depends(get_db),
):
    return HTMLResponse(_render_fixtures_page(db, match_date))


@app.get("/status", response_model=AppStatusResponse)
def read_status():
    return _app_status_payload()


@app.get("/dates/available", response_model=AvailableDatesResponse)
def get_available_dates(
    predicted_only: bool = Query(False, description="When true, only return dates that already have predictions"),
    db: Session = Depends(get_db),
):
    dates = _available_date_summaries(db, predicted_only=predicted_only)
    return {"count": len(dates), "dates": dates}


@app.get("/day-summary", response_model=CollectionSummaryResponse)
def get_day_summary(
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    min_edge: float = Query(0.025, ge=0.0, le=1.0),
    limit: int = Query(20, ge=1, le=20),
    db: Session = Depends(get_db),
):
    return _day_collection_summary(db, match_date, source="stored", min_edge=min_edge, limit=limit)


@app.get("/debug/enrichment")
def debug_fixture_enrichment(
    source: str = Query("hybrid", description="Source: hybrid, openligadb, footballdata, api_football or public_web"),
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    sample_limit: int = Query(200, ge=1, le=1000, description="Maximum enriched fixtures to include in the response"),
    db: Session = Depends(get_db),
):
    requested_date = _resolve_requested_date(match_date)
    collection_bundle = _collect_fixture_bundle(source, db, match_date=requested_date)
    enriched_map = collection_bundle.get("fixture_enrichment", {})
    enriched_rows = list(enriched_map.values())[:sample_limit]
    summary = summarize_enrichment({row.get("fixture_id"): row for row in enriched_rows if row.get("fixture_id") is not None})

    report = {
        "mode": "read_only_debug",
        "requested_date": requested_date.isoformat(),
        "source": source,
        "collection": {
            "raw_fixtures": int(collection_bundle.get("raw_fixtures", 0)),
            "unique_fixtures": int(collection_bundle.get("unique_fixtures", 0)),
            "duplicates_removed": int(collection_bundle.get("duplicates_removed", 0)),
            "source_breakdown": dict(collection_bundle.get("source_breakdown", {})),
        },
        "summary": summary,
        "fixtures": enriched_rows,
    }
    report["artifact_path"] = _write_enrichment_debug_report(report, requested_date)
    return report


@app.get("/debug/sources/status")
def debug_source_status(
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    db: Session = Depends(get_db),
):
    requested_date = _resolve_requested_date(match_date)
    probes = {
        "public_web": _source_probe_for_date(db, "public_web", requested_date),
        "footballdata": _source_probe_for_date(db, "footballdata", requested_date),
        "openligadb": _source_probe_for_date(db, "openligadb", requested_date),
        "hybrid": _source_probe_for_date(db, "hybrid", requested_date),
    }
    probes["public_web"]["called_by"] = {
        "/predictions/top": False,
        "/day-summary": False,
        "dashboard_auto_collection_via_/run?source=hybrid": bool(PUBLIC_WEB_ENABLED),
    }
    shared_hybrid_flow = {
        "/predictions/top": False,
        "/day-summary": False,
        "dashboard_auto_collection_via_/run?source=hybrid": True,
    }
    probes["footballdata"]["called_by"] = dict(shared_hybrid_flow)
    probes["api_football"] = _source_probe_for_date(db, "api_football", requested_date)
    probes["api_football"]["called_by"] = {"dashboard": "on-demand source via /run?source=api_football"}
    probes["openligadb"]["called_by"] = dict(shared_hybrid_flow)

    return {
        "mode": "read_only_debug",
        "requested_date": requested_date.isoformat(),
        "pipeline": {
            "/predictions/top": "reads stored predictions only; does not invoke fixture collection",
            "/day-summary": "reads stored fixtures/predictions only (source=stored); does not invoke fixture collection",
            "dashboard": "auto-collect path calls /run?source=hybrid, which invokes football-data + OpenLigaDB (+ public_web when enabled)",
        },
        "sources": probes,
        "next_step": f"GET /debug/sources/status?date={requested_date.isoformat()}",
    }


@app.get("/debug/public-web/diagnostics")
def debug_public_web_diagnostics(
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    sample_limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    requested_date = _resolve_requested_date(match_date)
    bundle = _collect_fixture_bundle("public_web", db, match_date=requested_date)
    fixtures = list(bundle.get("fixtures", []))
    enrichment_map = bundle.get("fixture_enrichment", {})
    diagnostics = [
        _fixture_analyzability_diagnostic(db, fixture, enrichment=enrichment_map.get(fixture.id))
        for fixture in fixtures[:sample_limit]
        if fixture is not None
    ]
    reason_counts: Dict[str, int] = {}
    for row in diagnostics:
        for reason in row.get("reasons", []):
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    not_analyzable = [row for row in diagnostics if not row.get("analyzable")]
    analyzable = [row for row in diagnostics if row.get("analyzable")]
    return {
        "mode": "read_only_debug",
        "requested_date": requested_date.isoformat(),
        "collection": {
            "raw_fixtures": int(bundle.get("raw_fixtures", 0)),
            "unique_fixtures": int(bundle.get("unique_fixtures", 0)),
            "duplicates_removed": int(bundle.get("duplicates_removed", 0)),
            "source_breakdown": dict(bundle.get("source_breakdown", {})),
            "source_status": bundle.get("source_status", {}),
        },
        "summary": {
            "fixtures_inspected": len(diagnostics),
            "analyzable_fixtures": len(analyzable),
            "not_analyzable_fixtures": len(not_analyzable),
            "top_not_analyzable_reasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)
                if reason != "analyzable"
            ][:10],
        },
        "fixtures": diagnostics,
        "next_step": f"GET /debug/public-web/diagnostics?date={requested_date.isoformat()}",
    }


@app.get("/debug/decision-engine/compare")
def debug_decision_engine_compare(
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    limit: int = Query(20, ge=1, le=50),
    min_edge: float = Query(0.025, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
):
    requested_date = _resolve_requested_date(match_date)
    resolved_limit = _resolve_limit(limit)
    resolved_min_edge = _resolve_min_edge(min_edge)

    legacy_pairs = _top_predictions(db, resolved_limit, resolved_min_edge, requested_date)
    legacy = [_serialize_top_prediction(prediction, details) for prediction, details in legacy_pairs]

    calibrated = _calibrated_engine_picks(db, requested_date, limit=resolved_limit, min_edge=resolved_min_edge)
    calibrated_picks = list(calibrated.get("picks", []))

    report = {
        "mode": "read_only_engine_compare",
        "requested_date": requested_date.isoformat(),
        "config": {
            "enable_calibrated_decision_engine": bool(ENABLE_CALIBRATED_DECISION_ENGINE),
            "calibrated_engine_min_market_samples": CALIBRATED_ENGINE_MIN_MARKET_SAMPLES,
            "calibrated_engine_min_market_hit_rate": CALIBRATED_ENGINE_MIN_MARKET_HIT_RATE,
            "calibrated_engine_suppress_underperforming_markets": bool(CALIBRATED_ENGINE_SUPPRESS_UNDERPERFORMING_MARKETS),
            "calibrated_engine_evidence_lookback_days": CALIBRATED_ENGINE_EVIDENCE_LOOKBACK_DAYS,
        },
        "summary": {
            "legacy_count": len(legacy),
            "calibrated_count": len(calibrated_picks),
            "legacy_avg_confidence_score": round(sum(item.get("confidence_score", 0.0) for item in legacy) / len(legacy), 3) if legacy else 0.0,
            "calibrated_avg_confidence_score": round(sum(item.get("confidence_score", 0.0) for item in calibrated_picks) / len(calibrated_picks), 3) if calibrated_picks else 0.0,
            "engine_path": calibrated.get("engine_path", "calibrated_engine"),
            "fallback_reason": calibrated.get("fallback_reason"),
        },
        "legacy_picks": legacy,
        "calibrated_picks": calibrated_picks,
    }
    report["artifact_path"] = _write_engine_compare_artifact(report, requested_date)
    return report


@app.get("/debug/decision-engine/mode")
def debug_decision_engine_mode():
    mode = _resolved_decision_engine_mode()
    return {
        "mode": mode,
        "legacy_default": mode == "legacy",
        "config": {
            "CALIBRATED_DECISION_ENGINE_MODE": CALIBRATED_DECISION_ENGINE_MODE,
            "ENABLE_CALIBRATED_DECISION_ENGINE": bool(ENABLE_CALIBRATED_DECISION_ENGINE),
            "ENABLE_ENGINE_SHADOW_COMPARE_ARTIFACT": bool(ENABLE_ENGINE_SHADOW_COMPARE_ARTIFACT),
        },
        "paths": {
            "legacy": "Legacy engine only.",
            "shadow": "Legacy output with calibrated comparison metadata only.",
            "active": "Calibrated output with fallback to legacy if needed.",
        },
    }


@app.get("/scrape/today", response_model=ScrapeFixturesResponse)
@app.post("/scrape/today", response_model=ScrapeFixturesResponse)
def scrape_today_fixtures(
    source: str = Query("hybrid", description="Source: hybrid, openligadb, footballdata, api_football or public_web"),
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    refresh: bool = Query(True, description="Clear runtime caches and remove expired fixture rows before scraping"),
    db: Session = Depends(get_db),
):
    """
    Scrape fixtures for the requested day.
    """
    requested_date = _resolve_requested_date(match_date)
    if refresh:
        _clear_runtime_caches()
        _purge_expired_fixture_data(db, cutoff_date=requested_date)
    collection_bundle = _collect_fixture_bundle(source, db, match_date=requested_date)
    fixtures = collection_bundle["fixtures"]
    start, end = _resolve_fixture_window(requested_date)
    visible_fixtures = [fixture for fixture in fixtures if start <= fixture.date < end]
    logger.info("Scraped %s fixtures from %s", len(fixtures), source)

    return {
        "source": source,
        "requested_date": _requested_date_value(requested_date),
        "note": _empty_date_note(db, requested_date, source) if not visible_fixtures else None,
        "available_dates": _available_date_labels(db, predicted_only=True) if not visible_fixtures else [],
        "scraped": len(visible_fixtures),
        "collection": _day_collection_summary(
            db,
            requested_date,
            source=source,
            collection_bundle=collection_bundle,
        ),
        "fixtures": [
            {
                "id": fixture.id,
                "home": fixture.home.name if fixture.home else "Unknown",
                "away": fixture.away.name if fixture.away else "Unknown",
                "league": fixture.league,
                "status": fixture.status,
                "date": str(fixture.date),
            }
            for fixture in visible_fixtures[:20]
        ],
    }


@app.get("/teams", response_model=List[TeamResponse])
def get_teams(db: Session = Depends(get_db)):
    teams = db.query(Team).all()
    return [{"id": t.id, "name": t.name, "league": t.league, "country": t.country, "attack": t.attack_strength, "defense": t.defense_strength} for t in teams]


@app.post("/teams", response_model=TeamCreateResponse)
def create_team(name: str, league: str = None, country: str = None, db: Session = Depends(get_db)):
    normalized_name = normalize_team_name(name)
    if not normalized_name:
        raise HTTPException(status_code=400, detail="Team name is required")

    existing = db.query(Team).filter(Team.name == normalized_name).first()
    if existing:
        return {"id": existing.id, "name": existing.name, "status": "existing"}

    team = Team(name=normalized_name, league=league, country=country)
    db.add(team)
    db.commit()
    db.refresh(team)
    return {"id": team.id, "name": team.name, "status": "created"}


@app.get("/fixtures", response_model=List[FixtureResponse])
def get_fixtures(
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    db: Session = Depends(get_db),
):
    start, end = _resolve_fixture_window(match_date)
    fixtures = (
        db.query(Fixture)
        .filter(Fixture.date >= start, Fixture.date < end)
        .order_by(Fixture.date.asc())
        .all()
    )
    return [_serialize_fixture(fixture) for fixture in fixtures]


@app.post("/fixtures", response_model=FixtureResponse)
def create_fixture(home_id: int, away_id: int, date: datetime, league: str = None, db: Session = Depends(get_db)):
    if home_id == away_id:
        raise HTTPException(status_code=400, detail="Home and away teams must be different")

    home_team = db.query(Team).filter(Team.id == home_id).first()
    away_team = db.query(Team).filter(Team.id == away_id).first()
    if not home_team or not away_team:
        raise HTTPException(status_code=404, detail="Both teams must exist before creating a fixture")

    fixture = Fixture(home_id=home_id, away_id=away_id, date=date, league=league, status="scheduled")
    db.add(fixture)
    db.commit()
    db.refresh(fixture)
    return _serialize_fixture(fixture)


@app.get("/scrape/historical", response_model=HistoricalScrapeResponse)
@app.post("/scrape/historical", response_model=HistoricalScrapeResponse)
def scrape_historical(
    league: str = Query("bl1", description="League code (bl1=Bundesliga, e0=Premier League)"),
    season: Optional[str] = Query(None, description="Season year; defaults to current football season"),
    source: str = Query("openligadb"),
):
    """Scrape historical data"""
    season_code = season or current_season_year()

    if source != "openligadb":
        raise HTTPException(status_code=400, detail=f"Unsupported source: {source}")

    from scrapers.openligadb_scraper import scrape_historical_openligadb

    count = scrape_historical_openligadb(league, season_code)
    return {"message": f"Scraped {count} historical matches", "count": count, "season": season_code}


@app.get("/predictions/generate", response_model=PredictionGenerationResponse)
@app.post("/predictions/generate", response_model=PredictionGenerationResponse)
def generate_predictions(
    fixture_id: Optional[int] = None,
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    db: Session = Depends(get_db),
):
    """Generate predictions for fixtures on the requested day."""
    if fixture_id:
        fixtures = db.query(Fixture).filter(Fixture.id == fixture_id).all()
    else:
        requested_date = _resolve_requested_date(match_date)
        start, end = _resolve_fixture_window(requested_date)
        fixtures = (
            db.query(Fixture)
            .filter(
                Fixture.date >= start,
                Fixture.date < end,
                Fixture.status == 'scheduled',
            )
            .all()
        )
    predictions_created = _store_fixture_predictions(db, fixtures)
    db.commit()
    return {
        "message": f"Created {predictions_created} predictions",
        "count": predictions_created,
        "fixtures_processed": len(fixtures),
        "requested_date": None if fixture_id else _requested_date_value(match_date),
        "note": _empty_date_note(db, match_date) if (not fixtures and not fixture_id) else None,
        "available_dates": _available_date_labels(db, predicted_only=True) if (not fixtures and not fixture_id) else [],
    }


@app.get("/predictions/today", response_model=PredictionsTodayResponse)
@app.get("/predictions/by-date", response_model=PredictionsTodayResponse)
def get_todays_predictions(
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    db: Session = Depends(get_db),
):
    start, end = _resolve_fixture_window(match_date)
    fixtures = (
        db.query(Fixture)
        .filter(Fixture.date >= start, Fixture.date < end)
        .order_by(Fixture.date.asc())
        .all()
    )
    
    results = []
    for fixture in fixtures:
        ranked = _ranked_predictions_for_fixture(db, fixture, include_unqualified=True)
        if ranked:
            results.append(_serialize_match_predictions(fixture, [prediction for prediction, _ in ranked[:3]]))
    
    return {
        "count": len(results),
        "requested_date": _requested_date_value(match_date),
        "note": _empty_date_note(db, match_date) if not results else None,
        "available_dates": _available_date_labels(db, predicted_only=True) if not results else [],
        "predictions": results,
    }


@app.get("/predictions/top", response_model=TopPredictionsListResponse)
def get_top_predictions(
    limit: int = Query(20, ge=1, le=20),
    min_edge: float = Query(0.025, ge=0.0, le=1.0),
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    db: Session = Depends(get_db),
):
    resolved_limit = _resolve_limit(limit)
    resolved_min_edge = _resolve_min_edge(min_edge)
    engine_mode = _resolved_decision_engine_mode()
    engine_path = "legacy_engine"
    fallback_reason = None

    if engine_mode == "active":
        calibrated = _calibrated_engine_picks(db, match_date, limit=resolved_limit, min_edge=resolved_min_edge)
        calibrated_picks = calibrated.get("picks", [])
        if calibrated_picks:
            logger.info("Top predictions served via calibrated engine path")
            return {
                "count": len(calibrated_picks),
                "requested_date": _requested_date_value(match_date),
                "note": (
                    f"engine_mode={engine_mode}; "
                    f"engine_path={calibrated.get('engine_path', 'calibrated_engine')}"
                ),
                "available_dates": _available_date_labels(db, predicted_only=True) if not calibrated_picks else [],
                "performance": compute_performance(calibrated_picks),
                "picks": calibrated_picks,
            }
        engine_path = "calibrated_engine_fallback_to_legacy"
        fallback_reason = calibrated.get("fallback_reason") or "no_calibrated_picks"
        logger.info("Calibrated engine fallback triggered; using legacy top predictions (%s)", fallback_reason)

    picks = _top_predictions(db, resolved_limit, resolved_min_edge, match_date)
    note = _empty_date_note(db, match_date) if not picks else None
    if engine_mode == "shadow":
        calibrated = _calibrated_engine_picks(db, match_date, limit=resolved_limit, min_edge=resolved_min_edge)
        shadow = _shadow_engine_comparison(
            db=db,
            match_date=match_date,
            legacy_count=len(picks),
            calibrated=calibrated,
        )
        note = (
            f"engine_mode=shadow; engine_path=legacy_engine; "
            f"shadow_status={shadow.get('shadow_status')}; "
            f"calibrated_count={shadow.get('calibrated_count')}"
        )
        if shadow.get("calibrated_fallback_reason"):
            note = f"{note}; calibrated_reason={shadow.get('calibrated_fallback_reason')}"
        logger.info(
            "Shadow mode served legacy picks: legacy=%s calibrated=%s status=%s",
            shadow.get("legacy_count"),
            shadow.get("calibrated_count"),
            shadow.get("shadow_status"),
        )
    elif engine_path != "legacy_engine":
        note = (
            f"engine_mode={engine_mode}; engine_path={engine_path}; fallback_reason={fallback_reason}"
            if fallback_reason
            else f"engine_mode={engine_mode}; engine_path={engine_path}"
        )
    elif note:
        note = f"engine_mode={engine_mode}; {note}"
    else:
        note = f"engine_mode={engine_mode}"
    serialized_picks = [_serialize_top_prediction(prediction, details) for prediction, details in picks]
    return {
        "count": len(picks),
        "requested_date": _requested_date_value(match_date),
        "note": note,
        "available_dates": _available_date_labels(db, predicted_only=True) if not picks else [],
        "performance": compute_performance(serialized_picks),
        "picks": serialized_picks,
    }


@app.get("/update-stats", response_model=UpdateStatsResponse)
@app.post("/update-stats", response_model=UpdateStatsResponse)
def update_team_statistics(db: Session = Depends(get_db)):
    """Update all team statistics from historical data"""
    updated = refresh_team_statistics(db)
    return {"message": f"Updated {updated} teams", "updated": updated}


@app.get("/run", response_model=PipelineResponse)
@app.post("/run", response_model=PipelineResponse)
def run_full_pipeline(
    source: str = Query("hybrid", description="Source: hybrid, openligadb, footballdata, api_football or public_web"),
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    refresh: bool = Query(True, description="Clear runtime caches and remove expired fixture rows before running"),
    db: Session = Depends(get_db),
):
    """Run full pipeline for the requested day."""
    requested_date = _resolve_requested_date(match_date)
    if refresh:
        _clear_runtime_caches()
        _purge_expired_fixture_data(db, cutoff_date=requested_date)
    collection_bundle = _collect_fixture_bundle(source, db, match_date=requested_date)
    fixtures = collection_bundle["fixtures"]
    start, end = _resolve_fixture_window(requested_date)
    visible_fixtures = [fixture for fixture in fixtures if start <= fixture.date < end]
    results = {"scraped": len(visible_fixtures)}
    results["stats_updated"] = refresh_team_statistics(db)
    
    # Step 3: Generate predictions
    fixtures_to_predict = [fixture for fixture in visible_fixtures if fixture.status == "scheduled"]
    predictions_created = _store_fixture_predictions(db, fixtures_to_predict)
    db.commit()
    results["predictions_created"] = predictions_created
    collection_summary = _day_collection_summary(
        db,
        requested_date,
        source=source,
        collection_bundle=collection_bundle,
    )
    
    return {
        "status": "complete",
        "requested_date": _requested_date_value(requested_date),
        "note": _empty_date_note(db, requested_date, source) if not visible_fixtures else None,
        "available_dates": _available_date_labels(db, predicted_only=True) if not visible_fixtures else [],
        "collection": collection_summary,
        "results": results,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
