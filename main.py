import base64
import json
import logging
import os
import sys
from datetime import date as calendar_date, datetime, timedelta
from html import escape
from pathlib import Path
from secrets import compare_digest
from typing import Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
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
from utils.competitions import is_competition_admitted, resolve_competition
from utils.football import (
    clamp,
    current_season_year,
    day_bounds,
    day_bounds_for_date,
    normalize_team_name,
)


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_VERSION = "1.6.4"
ARTIFACTS_DIR = Path("artifacts")
_MARKET_EVIDENCE_CACHE: Dict[Tuple[str, int], Dict[str, dict]] = {}

SITE_ACCESS_USERNAME = os.getenv("SITE_ACCESS_USERNAME", "").strip()
SITE_ACCESS_PASSWORD = os.getenv("SITE_ACCESS_PASSWORD", "").strip()
SITE_ACCESS_ENABLED = bool(SITE_ACCESS_USERNAME and SITE_ACCESS_PASSWORD)
PUBLIC_PATHS = {"/healthz"}

app = FastAPI(title="Football Prediction API", version=APP_VERSION)

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

@app.on_event("startup")
async def startup_event():
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created")


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


def _collect_openligadb_bundle(db: Session, match_date: Optional[calendar_date] = None, days_ahead: int = 0):
    from scrapers.openligadb_scraper import OpenLigaDBScraper

    scraper = OpenLigaDBScraper()
    try:
        scraped = scraper.scrape_todays_matches(days_ahead=days_ahead, target_date=match_date)
    finally:
        scraper.close()

    fixture_ids = _scraped_fixture_ids(scraped)
    return {
        "source": "openligadb",
        "source_breakdown": {"openligadb": len(fixture_ids)},
        "raw_fixtures": len(fixture_ids),
        "fixtures": _reload_fixtures_by_ids(db, fixture_ids),
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
    }


def _combine_collection_bundles(db: Session, *bundles):
    fixture_ids = set()
    raw_total = 0
    source_breakdown = {}

    for bundle in bundles:
        raw_total += int(bundle.get("raw_fixtures", 0) or 0)
        for key, value in (bundle.get("source_breakdown") or {}).items():
            source_breakdown[key] = source_breakdown.get(key, 0) + int(value or 0)
        for fixture in bundle.get("fixtures", []):
            if fixture and fixture.id:
                fixture_ids.add(fixture.id)

    return {
        "raw_fixtures": raw_total,
        "source_breakdown": source_breakdown,
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
    elif normalized_source in {"hybrid", "auto"}:
        football_bundle = _collect_footballdata_bundle(db, match_date=match_date, days_ahead=days_ahead)
        openligadb_bundle = _collect_openligadb_bundle(db, match_date=match_date, days_ahead=days_ahead)
        bundle = _combine_collection_bundles(db, football_bundle, openligadb_bundle)
        bundle["source"] = "hybrid"
        if football_bundle.get("note"):
            bundle["note"] = football_bundle["note"]
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported source: {source}")

    fixtures = bundle.get("fixtures", [])
    fixture_ids = sorted({fixture.id for fixture in fixtures if fixture is not None and fixture.id is not None})
    unique_count = len(fixture_ids)
    bundle["fixture_ids"] = fixture_ids
    bundle["unique_fixtures"] = unique_count
    bundle["duplicates_removed"] = max(int(bundle.get("raw_fixtures", 0)) - unique_count, 0)
    bundle["fixture_enrichment"] = enrich_fixtures(db, fixtures)
    return bundle


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
            "/debug/decision-engine/mode",
            "/debug/decision-engine/compare",
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
    return {
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


def _ranked_predictions_for_fixture(db: Session, fixture: Fixture, min_edge: float = 0.0):
    context = build_match_context(db, fixture)
    predictions = db.query(Prediction).filter(Prediction.fixture_id == fixture.id).all()
    ranked = []

    for prediction in predictions:
        if prediction.edge < min_edge:
            continue
        details = _prediction_details(prediction, context)
        if prediction.stake <= 0 and not details["qualified"]:
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
    available_dates = _available_date_summaries(db, predicted_only=True)

    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Football Prediction API</title>
        <style>
          :root {{
            --bg: #f3efe5;
            --ink: #17221f;
            --card: #fffaf2;
            --line: #d7cfbf;
            --accent: #0e8f68;
            --accent-soft: #d9f3ea;
            --muted: #5d695f;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: "Segoe UI", Tahoma, sans-serif;
            color: var(--ink);
            background:
              radial-gradient(circle at top left, rgba(14, 143, 104, 0.14), transparent 28%),
              linear-gradient(180deg, #fbf7ef 0%, var(--bg) 100%);
          }}
          .wrap {{
            max-width: 1160px;
            margin: 0 auto;
            padding: 18px 14px 28px;
          }}
          .hero {{
            display: grid;
            gap: 10px;
            margin-bottom: 16px;
          }}
          .hero h1 {{
            margin: 0;
            font-size: clamp(1.7rem, 7vw, 2.8rem);
            line-height: 1.05;
          }}
          .hero p {{
            margin: 0;
            max-width: 760px;
            color: var(--muted);
            font-size: 0.97rem;
          }}
          .actions {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
          }}
          .actions a {{
            text-decoration: none;
            color: var(--ink);
            padding: 9px 12px;
            border-radius: 999px;
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, 0.72);
            font-size: 0.9rem;
          }}
          .actions a.primary {{
            background: var(--accent);
            color: white;
            border-color: var(--accent);
          }}
          .actions a:focus-visible, .date-pill:focus-visible {{
            outline: 2px solid var(--accent);
            outline-offset: 2px;
          }}
          .stats-grid {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 10px;
            margin: 14px 0 18px;
          }}
          .stat {{
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 14px 15px;
          }}
          .stat .label {{
            color: var(--muted);
            font-size: 0.9rem;
          }}
          .stat .value {{
            margin-top: 6px;
            font-size: 1.7rem;
            font-weight: 700;
          }}
          section {{
            background: rgba(255, 255, 255, 0.35);
            border: 1px solid #e4dbca;
            border-radius: 18px;
            padding: 14px;
            margin-bottom: 14px;
          }}
          .section-head {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            flex-direction: column;
            gap: 16px;
            margin-bottom: 10px;
          }}
          .section-head h2 {{
            margin: 0;
            font-size: 1.35rem;
          }}
          .section-head span {{
            color: var(--muted);
            font-size: 0.88rem;
          }}
          .date-strip {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 8px;
            margin-bottom: 10px;
          }}
          .date-pill {{
            display: flex;
            flex-direction: column;
            gap: 2px;
            min-width: 0;
            padding: 10px 12px;
            border-radius: 12px;
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, 0.78);
            color: var(--ink);
            cursor: pointer;
            text-align: left;
            transition: transform 120ms ease, border-color 120ms ease, background 120ms ease;
          }}
          .date-pill:hover {{
            transform: translateY(-1px);
            border-color: #b9af98;
          }}
          .date-pill.active {{
            background: var(--accent);
            border-color: var(--accent);
            color: white;
          }}
          .date-pill .date-label {{
            font-weight: 700;
            font-size: 0.98rem;
          }}
          .date-pill .date-meta {{
            font-size: 0.82rem;
            opacity: 0.82;
          }}
          .picks {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 10px;
          }}
          .pick-card, .empty-card {{
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 14px;
            box-shadow: 0 10px 20px rgba(23, 34, 31, 0.04);
          }}
          .pick-head {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 8px;
            margin-bottom: 12px;
          }}
          .badge {{
            display: inline-flex;
            align-items: center;
            padding: 6px 10px;
            border-radius: 999px;
            background: var(--accent-soft);
            color: #0e6f52;
            font-size: 0.8rem;
            font-weight: 700;
            letter-spacing: 0.04em;
          }}
          .edge {{
            font-weight: 700;
            color: var(--accent);
          }}
          .pick-card h3, .empty-card h3 {{
            margin: 0 0 8px;
            font-size: 1.15rem;
            line-height: 1.15;
          }}
          .meta {{
            margin: 0 0 12px;
            color: var(--muted);
            font-size: 0.92rem;
          }}
          .selection {{
            margin: 0 0 14px;
            font-size: 1rem;
          }}
          .stats {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            color: var(--muted);
            font-size: 0.9rem;
          }}
          .toolbar {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 8px;
            align-items: stretch;
            margin-bottom: 10px;
          }}
          .toolbar input[type="date"] {{
            width: 100%;
            padding: 11px 12px;
            border-radius: 10px;
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, 0.9);
            color: var(--ink);
            font: inherit;
          }}
          .toolbar button {{
            width: 100%;
            padding: 11px 14px;
            border-radius: 10px;
            border: 1px solid var(--accent);
            background: var(--accent);
            color: white;
            font-weight: 700;
            font: inherit;
            cursor: pointer;
          }}
          .toolbar button:disabled {{ opacity: 0.6; cursor: not-allowed; }}
          .top-board {{
            border: 1px solid #d6cebd;
            background: #fffdf8;
          }}
          .status-line {{
            color: var(--muted);
            font-size: 0.88rem;
            margin-bottom: 8px;
          }}
          .muted-link {{
            border: none;
            background: transparent;
            color: var(--muted);
            padding: 4px 2px;
            font-size: 0.84rem;
            text-align: left;
            cursor: pointer;
          }}
          .hidden {{
            display: none !important;
          }}
          code {{
            padding: 0.15rem 0.35rem;
            border-radius: 6px;
            background: #efe8da;
          }}
          @media (min-width: 700px) {{
            .wrap {{ padding: 28px 20px 42px; }}
            .stats-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
            .date-strip {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
            .section-head {{ flex-direction: row; align-items: center; }}
            .toolbar {{ grid-template-columns: 1fr auto; }}
            .toolbar button {{ width: auto; min-width: 220px; }}
            .picks {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
          }}
          @media (min-width: 1024px) {{
            .stats-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
            .picks {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
          }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <section class="hero">
            <h1>Football Prediction Dashboard</h1>
            <p>
              Mobile-first live board for fixture coverage, prediction health, and top-ranked picks.
              Select a date, run the pipeline once, and monitor the strongest outcomes with clean
              status context in one place.
            </p>
            <div class="actions">
              <a id="top-json-link" class="primary" href="/board?date={today}">Readable Board</a>
              <a id="all-json-link" href="/matchday?date={today}">Readable Matchday</a>
              <a id="fixtures-json-link" href="/fixtures-view?date={today}">Readable Fixtures</a>
              <a id="summary-json-link" href="/summary?date={today}">Readable Summary</a>
              <a href="/docs">Swagger Docs</a>
              <a href="/status">API Status</a>
            </div>
          </section>

          <section class="stats-grid">
            <div class="stat">
              <div class="label">Upcoming Fixtures (7 days)</div>
              <div class="value">{fixture_count}</div>
            </div>
            <div class="stat">
              <div class="label">Stored Predictions (7 days)</div>
              <div class="value">{prediction_count}</div>
            </div>
            <div class="stat">
              <div class="label">Tracked Teams</div>
              <div class="value">{team_count}</div>
            </div>
            <div class="stat">
              <div class="label">App Version</div>
              <div class="value">{escape(APP_VERSION)}</div>
            </div>
          </section>

          <section>
            <div class="section-head">
              <h2>Available Dates</h2>
              <span id="current-date-label">Loading dates...</span>
            </div>
            <div class="toolbar">
              <input id="date-input" type="date" value="{today}" />
              <button id="run-date-btn" type="button">Run Selected Date</button>
            </div>
            <p id="status-line" class="status-line">Checking the latest stored fixtures and predictions...</p>
            <div id="date-strip" class="date-strip">
              <article class="empty-card">
                <h3>Loading dates</h3>
                <p>The dashboard is preparing your available match days.</p>
              </article>
            </div>
            <button id="toggle-dates-btn" class="muted-link hidden" type="button">View more dates</button>
          </section>

          <section class="top-board">
            <div class="section-head">
              <h2>Top 20 Board</h2>
              <span id="top-picks-caption">Live qualified board for the selected date</span>
            </div>
            <div id="picks" class="picks">
              <article class="empty-card">
                <h3>Loading picks</h3>
                <p>The dashboard will pull the strongest selections for the chosen date.</p>
              </article>
            </div>
          </section>
        </main>
        <script>
          const initialToday = {today!r};
          const initialDates = {available_dates!r};
          const refreshMs = 30000;

          const dateStrip = document.getElementById("date-strip");
          const picksNode = document.getElementById("picks");
          const dateInput = document.getElementById("date-input");
          const runDateBtn = document.getElementById("run-date-btn");
          const toggleDatesBtn = document.getElementById("toggle-dates-btn");
          const statusLine = document.getElementById("status-line");
          const currentDateLabel = document.getElementById("current-date-label");
          const topJsonLink = document.getElementById("top-json-link");
          const allJsonLink = document.getElementById("all-json-link");
          const fixturesJsonLink = document.getElementById("fixtures-json-link");
          const summaryJsonLink = document.getElementById("summary-json-link");
          const topPicksCaption = document.getElementById("top-picks-caption");

          let currentDate = initialToday;
          let availableDates = initialDates;
          let datesExpanded = false;
          let refreshHandle = null;
          const autoCollectAttempts = new Set();
          const runInFlightDates = new Set();
          let refreshInFlight = false;

          function escapeHtml(value) {{
            return String(value ?? "")
              .replace(/&/g, "&amp;")
              .replace(/</g, "&lt;")
              .replace(/>/g, "&gt;")
              .replace(/"/g, "&quot;")
              .replace(/'/g, "&#39;");
          }}

          function formatPercent(value) {{
            const numeric = Number(value ?? 0);
            return `${{(numeric * 100).toFixed(1)}}%`;
          }}

          function updateLinks(dateValue) {{
            const encoded = encodeURIComponent(dateValue);
            topJsonLink.href = `/board?date=${{encoded}}`;
            allJsonLink.href = `/matchday?date=${{encoded}}`;
            fixturesJsonLink.href = `/fixtures-view?date=${{encoded}}`;
            summaryJsonLink.href = `/summary?date=${{encoded}}`;
          }}

          function chooseInitialDate(dateRows) {{
            if (!Array.isArray(dateRows) || dateRows.length === 0) {{
              return initialToday;
            }}

            const queryDate = new URLSearchParams(window.location.search).get("date");
            if (queryDate) {{
              return queryDate;
            }}

            const exactToday = dateRows.find((item) => item.date === initialToday);
            if (exactToday) {{
              return exactToday.date;
            }}

            const nextFuture = dateRows.find((item) => item.date >= initialToday);
            if (nextFuture) {{
              return nextFuture.date;
            }}

            return dateRows[0].date;
          }}

          function renderDateStrip(dateRows) {{
            if (!Array.isArray(dateRows) || dateRows.length === 0) {{
              toggleDatesBtn.classList.add("hidden");
              dateStrip.innerHTML = `
                <article class="empty-card">
                  <h3>No stored dates yet</h3>
                  <p>Run <code>POST /run?date=YYYY-MM-DD</code> from <a href="/docs">Swagger</a> to populate the database.</p>
                </article>
              `;
              return;
            }}

            const sortedRows = [...dateRows].sort((a, b) => String(b.date).localeCompare(String(a.date)));
            const visibleRows = datesExpanded ? sortedRows : sortedRows.slice(0, 3);
            toggleDatesBtn.classList.toggle("hidden", sortedRows.length <= 3);
            toggleDatesBtn.textContent = datesExpanded ? "Show fewer dates" : `View more (${{sortedRows.length - 3}} more)`;

            dateStrip.innerHTML = visibleRows.map((item) => `
              <button class="date-pill ${{item.date === currentDate ? "active" : ""}}" type="button" data-date="${{escapeHtml(item.date)}}">
                <span class="date-label">${{escapeHtml(item.date)}}</span>
                <span class="date-meta">${{item.fixtures}} fixtures | ${{item.predictions}} predicted</span>
              </button>
            `).join("");

            dateStrip.querySelectorAll("[data-date]").forEach((button) => {{
              button.addEventListener("click", () => loadDate(button.dataset.date, false));
            }});
          }}

          function renderPicks(payload) {{
            if (!payload || !Array.isArray(payload.picks) || payload.picks.length === 0) {{
              const note = payload?.note ? escapeHtml(payload.note) : "No picks stored for this date yet.";
              picksNode.innerHTML = `
                <article class="empty-card">
                  <h3>No ranked picks for ${{escapeHtml(currentDate)}}</h3>
                  <p>${{note}}</p>
                  <p class="meta">Try running the selected date to refresh fixtures, stats, and predictions for this board.</p>
                </article>
              `;
              return;
            }}

            picksNode.innerHTML = payload.picks.map((pick) => `
              <article class="pick-card">
                <div class="pick-head">
                  <span class="badge">${{escapeHtml((pick.confidence || "medium").toUpperCase())}}</span>
                  <span class="edge">${{pick.edge >= 0 ? "+" : ""}}${{formatPercent(pick.edge)}} edge</span>
                </div>
                <h3>${{escapeHtml(pick.match)}}</h3>
                <p class="meta">${{escapeHtml(pick.league || "Unknown league")}} | ${{escapeHtml(pick.date || "")}}</p>
                <p class="selection">${{escapeHtml(pick.market)}}: <strong>${{escapeHtml(pick.selection || "")}}</strong></p>
                <div class="stats">
                  <span>Prob ${{formatPercent(pick.probability)}}</span>
                  <span>Stake ${{Number(pick.stake ?? 0).toFixed(2)}}</span>
                  <span>xG ${{Number(pick.expected_home_goals ?? 0).toFixed(2)}}-${{Number(pick.expected_away_goals ?? 0).toFixed(2)}}</span>
                </div>
              </article>
            `).join("");
          }}

          async function fetchJson(url) {{
            const response = await fetch(url, {{ cache: "no-store" }});
            if (!response.ok) {{
              throw new Error(`Request failed: ${{response.status}}`);
            }}
            return response.json();
          }}

          function setBusyState(isBusy) {{
            runDateBtn.disabled = isBusy;
            dateInput.disabled = isBusy;
            toggleDatesBtn.disabled = isBusy;
          }}

          async function refreshDates(preferredDate = null) {{
            const payload = await fetchJson("/dates/available?predicted_only=false");
            availableDates = Array.isArray(payload?.dates) ? payload.dates : [];
            if (preferredDate) {{
              currentDate = preferredDate;
            }} else if (!availableDates.some((item) => item.date === currentDate)) {{
              currentDate = chooseInitialDate(availableDates);
            }}
            dateInput.value = currentDate;
            renderDateStrip(availableDates);
            updateLinks(currentDate);
            currentDateLabel.textContent = availableDates.length
              ? `Showing ${{currentDate}}`
              : "No stored match dates yet";
          }}

          async function loadDate(dateValue, refreshDatesFirst = true) {{
            currentDate = dateValue || initialToday;
            dateInput.value = currentDate;
            updateLinks(currentDate);
            statusLine.textContent = `Refreshing picks for ${{currentDate}}...`;
            currentDateLabel.textContent = `Showing ${{currentDate}}`;
            window.history.replaceState(null, "", `/?date=${{encodeURIComponent(currentDate)}}`);

            if (refreshDatesFirst) {{
              await refreshDates(currentDate);
            }} else {{
              renderDateStrip(availableDates);
            }}

            let daySummary = await fetchJson(`/day-summary?date=${{encodeURIComponent(currentDate)}}`);
            const needsAutoCollect = (
              currentDate >= initialToday &&
              !autoCollectAttempts.has(currentDate) &&
              !runInFlightDates.has(currentDate) &&
              (
                Number(daySummary?.unique_fixtures ?? 0) === 0 ||
                Number(daySummary?.predicted_fixtures ?? 0) < Number(daySummary?.unique_fixtures ?? 0)
              )
            );

            if (needsAutoCollect) {{
              autoCollectAttempts.add(currentDate);
              runInFlightDates.add(currentDate);
              statusLine.textContent = `Auto-collecting fixtures and predictions for ${{currentDate}}...`;
              try {{
                await fetchJson(`/run?source=hybrid&date=${{encodeURIComponent(currentDate)}}`);
                await refreshDates(currentDate);
                daySummary = await fetchJson(`/day-summary?date=${{encodeURIComponent(currentDate)}}`);
              }} catch (error) {{
                statusLine.textContent = `Auto-collection failed for ${{currentDate}}: ${{error.message || "Unknown error"}}`;
              }} finally {{
                runInFlightDates.delete(currentDate);
              }}
            }}

            const payload = await fetchJson(`/predictions/top?date=${{encodeURIComponent(currentDate)}}`);
            renderDateStrip(availableDates);
            renderPicks(payload);
            topPicksCaption.textContent = payload?.count
              ? `Showing ${{payload.count}} ranked picks for ${{currentDate}}`
              : `No qualified picks stored for ${{currentDate}}`;

            const summary = availableDates.find((item) => item.date === currentDate);
            if (daySummary) {{
              statusLine.textContent = `Live for ${{currentDate}}: ${{daySummary.raw_fixtures ?? 0}} collected, ${{daySummary.analyzable_fixtures ?? 0}} analyzable, ${{daySummary.predicted_fixtures ?? 0}} predicted fixtures, ${{daySummary.ranked_picks ?? 0}} ranked picks. Auto-refresh every 30 seconds.`;
            }} else if (summary) {{
              statusLine.textContent = `Live from local data: ${{summary.fixtures}} fixtures, ${{summary.predictions}} fixture-level predictions on ${{currentDate}}. Auto-refresh every 30 seconds.`;
            }} else if (payload.note) {{
              statusLine.textContent = payload.note;
            }} else {{
              statusLine.textContent = `No picks stored for ${{currentDate}} yet. Auto-refresh every 30 seconds.`;
            }}
          }}

          async function refreshDashboard() {{
            if (refreshInFlight) {{
              return;
            }}
            refreshInFlight = true;
            try {{
              await refreshDates();
              await loadDate(currentDate, false);
            }} catch (error) {{
              picksNode.innerHTML = `
                <article class="empty-card">
                  <h3>Dashboard refresh failed</h3>
                  <p>${{escapeHtml(error.message || "Unknown error")}}</p>
                </article>
              `;
              statusLine.textContent = "The dashboard could not refresh right now.";
            }} finally {{
              refreshInFlight = false;
            }}
          }}

          async function runSelectedDate() {{
            const targetDate = dateInput.value || currentDate || initialToday;
            currentDate = targetDate;
            updateLinks(currentDate);
            setBusyState(true);
            runInFlightDates.add(currentDate);
            statusLine.textContent = `Running scrape and predictions for ${{currentDate}}...`;

            try {{
              const payload = await fetchJson(`/run?source=hybrid&date=${{encodeURIComponent(currentDate)}}`);
              await refreshDates(currentDate);
              await loadDate(currentDate, false);

              if (payload?.note) {{
                statusLine.textContent = payload.note;
              }} else {{
                const scraped = payload?.results?.scraped ?? 0;
                const created = payload?.results?.predictions_created ?? 0;
                const analyzable = payload?.collection?.analyzable_fixtures ?? 0;
                const ranked = payload?.collection?.ranked_picks ?? 0;
                statusLine.textContent = `Run complete for ${{currentDate}}: ${{scraped}} fixtures collected, ${{analyzable}} analyzable, ${{created}} predictions stored, ${{ranked}} ranked picks.`;
              }}
            }} catch (error) {{
              statusLine.textContent = `Run failed for ${{currentDate}}: ${{error.message || "Unknown error"}}`;
            }} finally {{
              runInFlightDates.delete(currentDate);
              setBusyState(false);
            }}
          }}

          runDateBtn.addEventListener("click", () => runSelectedDate());
          toggleDatesBtn.addEventListener("click", () => {{
            datesExpanded = !datesExpanded;
            renderDateStrip(availableDates);
          }});
          dateInput.addEventListener("change", () => {{
            dateInput.value = dateInput.value || currentDate || initialToday;
            loadDate(dateInput.value, true);
          }});

          refreshDashboard();
          refreshHandle = window.setInterval(refreshDashboard, refreshMs);
          window.addEventListener("beforeunload", () => {{
            if (refreshHandle) {{
              window.clearInterval(refreshHandle);
            }}
          }});
        </script>
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
            --bg: #f3efe5;
            --ink: #17221f;
            --card: #fffaf2;
            --line: #d7cfbf;
            --accent: #0e8f68;
            --accent-soft: #d9f3ea;
            --muted: #5d695f;
            --warn: #8a5d15;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: "Segoe UI", Tahoma, sans-serif;
            color: var(--ink);
            background:
              radial-gradient(circle at top left, rgba(14, 143, 104, 0.14), transparent 28%),
              linear-gradient(180deg, #fbf7ef 0%, var(--bg) 100%);
          }}
          .wrap {{
            max-width: 1100px;
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
            background: rgba(255,255,255,0.78);
          }}
          .actions a.primary {{
            background: var(--accent);
            color: white;
            border-color: var(--accent);
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
            border-radius: 22px;
            padding: 18px;
            box-shadow: 0 18px 30px rgba(23, 34, 31, 0.05);
          }}
          .pill {{
            display: inline-flex;
            align-items: center;
            padding: 5px 10px;
            border-radius: 999px;
            background: var(--accent-soft);
            color: #0e6f52;
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
            border-radius: 18px;
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
            border-radius: 18px;
          }}
          code {{
            padding: 0.15rem 0.35rem;
            border-radius: 6px;
            background: #efe8da;
          }}
        </style>
      </head>
      <body>
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
    reasons = "".join(f"<li>{escape(reason)}</li>" for reason in details["main_reasons"]) or "<li>No major supporting reasons recorded.</li>"
    risks = "".join(f"<li>{escape(note)}</li>" for note in details["risk_notes"]) if details["risk_notes"] else "<li>No major risk flags on this board view.</li>"
    return f"""
      <article class="card">
        <div class="section-head">
          <span class="pill">Rank {rank}</span>
          <span class="pill">{escape(details["confidence_grade"])}</span>
        </div>
        <h3>{escape(match)}</h3>
        <p class="meta">{escape(str(league))} | {escape(str(fixture.date) if fixture else "")}</p>
        <p style="margin-top:12px;"><strong>{escape(market_display_name(prediction.market))}</strong>: {escape(prediction.selection or "")}</p>
        <div class="kv">
          <div><span>Model Prob</span><br />{prediction.probability:.1%}</div>
          <div><span>Market Prob</span><br />{details["market_probability"]:.1%}</div>
          <div><span>Edge</span><br />{prediction.edge:+.1%}</div>
          <div><span>Best Odds</span><br />{prediction.odds or "-"}</div>
          <div><span>Fair Odds</span><br />{details["fair_odds"] or "-"}</div>
          <div><span>Rank Score</span><br />{details["rank_score"]}</div>
          <div><span>Stake</span><br />{prediction.stake:.2f}</div>
          <div><span>xG</span><br />{prediction.expected_home_goals:.2f}-{prediction.expected_away_goals:.2f}</div>
        </div>
        <div class="section">
          <h3 style="font-size:1rem;">Why It Made The Board</h3>
          <ul class="list">{reasons}</ul>
        </div>
        <div class="section">
          <h3 style="font-size:1rem;">Risk Notes</h3>
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
        <h1>Readable Board</h1>
        <p class="muted">Human-friendly ranked board for {escape(resolved_date.isoformat())}. This uses the same scoring logic as <code>/predictions/top</code>, just in a cleaner readable page.</p>
        <div class="actions">
          <a class="primary" href="/?date={escape(resolved_date.isoformat())}">Back To Dashboard</a>
          <a href="/matchday?date={escape(resolved_date.isoformat())}">Readable Matchday</a>
          <a href="/summary?date={escape(resolved_date.isoformat())}">Readable Summary</a>
          <a href="/predictions/top?date={escape(resolved_date.isoformat())}">Raw JSON</a>
        </div>
      </section>
      <section class="section">
        <div class="section-head">
          <h2>Top Board</h2>
          <span class="muted">{len(picks)} ranked picks | minimum edge {min_edge:.1%}</span>
        </div>
        <div class="grid">
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
        <h1>Readable Day Summary</h1>
        <p class="muted">Collection-to-board funnel for {escape(resolved_date.isoformat())}, so you can see where the day is getting thinner.</p>
        <div class="actions">
          <a class="primary" href="/?date={escape(resolved_date.isoformat())}">Back To Dashboard</a>
          <a href="/board?date={escape(resolved_date.isoformat())}">Readable Board</a>
          <a href="/day-summary?date={escape(resolved_date.isoformat())}">Raw JSON</a>
        </div>
      </section>
      <section class="section">
        <div class="section-head">
          <h2>Funnel Metrics</h2>
          <span class="muted">How much survived each stage</span>
        </div>
        <div class="grid">
          <article class="card"><h3>Raw Collected</h3><p class="meta">{summary["raw_fixtures"]} fixtures before dedupe</p></article>
          <article class="card"><h3>Unique Fixtures</h3><p class="meta">{summary["unique_fixtures"]} fixtures after dedupe</p></article>
          <article class="card"><h3>Analyzable</h3><p class="meta">{summary["analyzable_fixtures"]} fixtures passed the data gate</p></article>
          <article class="card"><h3>Predicted Fixtures</h3><p class="meta">{summary["predicted_fixtures"]} fixtures have stored predictions</p></article>
          <article class="card"><h3>Qualified Markets</h3><p class="meta">{summary["qualified_markets"]} markets survived qualification</p></article>
          <article class="card"><h3>Ranked Board</h3><p class="meta">{summary["ranked_picks"]} picks made the final board</p></article>
        </div>
      </section>
      <section class="section">
        <div class="section-head">
          <h2>Coverage Notes</h2>
          <span class="muted">{summary["active_leagues"]} active leagues | {summary["duplicates_removed"]} duplicates removed</span>
        </div>
        {"<table class='table'><thead><tr><th>Source</th><th>Fixtures</th></tr></thead><tbody>" + source_rows + "</tbody></table>" if source_rows else "<div class='empty'><h3>No source breakdown stored</h3><p class='muted'>Run the hybrid collector for this date to see per-source contribution.</p></div>"}
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
        <h1>Readable Fixtures</h1>
        <p class="muted">Fixture list for {escape(resolved_date.isoformat())}.</p>
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
          <span class="muted">{len(fixtures)} fixtures</span>
        </div>
        {"<table class='table'><thead><tr><th>Kickoff</th><th>Home</th><th>Away</th><th>Competition</th><th>Status</th></tr></thead><tbody>" + rows + "</tbody></table>" if fixtures else f'<div class="empty"><h3>No fixtures</h3><p class="muted">{escape(_empty_date_note(db, resolved_date))}</p></div>'}
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
            prediction_html = "<p class='muted' style='margin-top:12px;'>No qualified picks stored for this fixture yet.</p>"
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
        <h1>Readable Matchday</h1>
        <p class="muted">Fixture-by-fixture readable page for {escape(resolved_date.isoformat())}, with the top stored predictions under each match.</p>
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
          <span class="muted">{len(fixtures)} fixtures</span>
        </div>
        <div class="grid">
          {"".join(match_cards) if match_cards else f'<div class="empty"><h3>No stored matchday</h3><p class="muted">{escape(_empty_date_note(db, resolved_date))}</p></div>'}
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
    source: str = Query("hybrid", description="Source: hybrid, openligadb or footballdata"),
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
    source: str = Query("hybrid", description="Source: hybrid, openligadb or footballdata"),
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    db: Session = Depends(get_db),
):
    """
    Scrape fixtures for the requested day.
    """
    requested_date = _resolve_requested_date(match_date)
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
    prediction_engine = PredictionEngine(db)
    
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
    predictions_created = 0
    
    for fixture in fixtures:
        db.query(Prediction).filter(Prediction.fixture_id == fixture.id).delete()
        if fixture.status != "scheduled":
            continue
        context = build_match_context(db, fixture)
        if context["data_quality"] < 0.42:
            continue
        predictions = prediction_engine.predict_match(fixture)
        for pred in predictions:
            db.add(pred)
            predictions_created += 1
    
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
        ranked = _ranked_predictions_for_fixture(db, fixture)
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
    return {
        "count": len(picks),
        "requested_date": _requested_date_value(match_date),
        "note": note,
        "available_dates": _available_date_labels(db, predicted_only=True) if not picks else [],
        "picks": [_serialize_top_prediction(prediction, details) for prediction, details in picks],
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
    source: str = Query("hybrid", description="Source: hybrid, openligadb or footballdata"),
    match_date: Optional[calendar_date] = Query(None, alias="date", description="Day in YYYY-MM-DD format; defaults to today"),
    db: Session = Depends(get_db),
):
    """Run full pipeline for the requested day."""
    requested_date = _resolve_requested_date(match_date)
    collection_bundle = _collect_fixture_bundle(source, db, match_date=requested_date)
    fixtures = collection_bundle["fixtures"]
    start, end = _resolve_fixture_window(requested_date)
    visible_fixtures = [fixture for fixture in fixtures if start <= fixture.date < end]
    results = {"scraped": len(visible_fixtures)}
    results["stats_updated"] = refresh_team_statistics(db)
    
    # Step 3: Generate predictions
    prediction_engine = PredictionEngine(db)
    fixtures_to_predict = [fixture for fixture in visible_fixtures if fixture.status == "scheduled"]
    predictions_created = 0
    for fixture in fixtures_to_predict:
        db.query(Prediction).filter(Prediction.fixture_id == fixture.id).delete()
        context = build_match_context(db, fixture)
        if context["data_quality"] < 0.42:
            continue
        predictions = prediction_engine.predict_match(fixture)
        for pred in predictions:
            db.add(pred)
            predictions_created += 1
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
