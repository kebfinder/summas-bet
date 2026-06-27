"""World Cup 2026 real-time data layer for the prediction engine.

This module fetches match data from the public World Cup 2026 API, caches the
cleaned response in memory, and falls back to a local fixture file when the
network source is unavailable. The public functions are intentionally small:

* ``get_live_data()`` returns the normalized match DataFrame.
* ``get_team_form(team_name)`` returns the latest five cached matches for a team.

The parser accepts several common API/fixture JSON shapes so the data layer can
survive minor upstream payload changes without breaking the engine.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

PRIMARY_API_URL = "https://worldcup26.ir/get/games"
REQUEST_TIMEOUT_SECONDS = 5
CACHE_TTL_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
FALLBACK_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures_backup.json"

OUTPUT_COLUMNS = [
    "match_id",
    "stage",
    "home_team",
    "away_team",
    "home_score",
    "away_score",
    "status",
    "kickoff_time",
    "venue",
    "host_city",
]

_CACHE: Dict[str, Any] = {"expires_at": 0.0, "dataframe": None, "source": None}


class DataUnavailableError(RuntimeError):
    """Raised when neither live API, stale cache, nor fallback fixtures exist."""


def _first_value(record: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return default


def _nested_value(record: Dict[str, Any], *paths: Iterable[str], default: Any = None) -> Any:
    for path in paths:
        cursor: Any = record
        for key in path:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(key)
        if cursor not in (None, ""):
            return cursor
    return default


def _team_name(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return _first_value(value, "name", "team_name", "country", "title")
    if value not in (None, ""):
        return str(value)
    return None


def _score_value(record: Dict[str, Any], side: str) -> Optional[int]:
    value = _first_value(
        record,
        f"{side}_score",
        f"{side}Score",
        f"{side}_goals",
        f"{side}Goals",
        default=None,
    )
    if value is None:
        value = _nested_value(
            record,
            ("score", side),
            ("scores", side),
            ("goals", side),
            default=None,
        )
    if isinstance(value, dict):
        value = _first_value(value, "current", "total", "score", "goals")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        logger.debug("Unable to parse %s score value %r", side, value)
        return None


def _normalize_status(raw_status: Any) -> str:
    status = str(raw_status or "scheduled").strip().lower().replace("_", " ")
    if status in {"live", "in progress", "playing", "first half", "second half", "halftime"}:
        return "live"
    if status in {"finished", "complete", "completed", "full time", "ft", "ended"}:
        return "finished"
    return "scheduled"


def _extract_games(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("games", "matches", "fixtures", "data", "response"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_games(value)
            if nested:
                return nested
    return []


def _normalize_match(record: Dict[str, Any]) -> Dict[str, Any]:
    home_obj = _first_value(record, "home", "home_team", "homeTeam", "team1")
    away_obj = _first_value(record, "away", "away_team", "awayTeam", "team2")
    venue_obj = _first_value(record, "venue", "stadium", default={})

    kickoff_raw = _first_value(
        record,
        "kickoff_time",
        "kickoffTime",
        "kickoff",
        "date",
        "datetime",
        "match_time",
        "matchTime",
    )
    kickoff_time = pd.to_datetime(kickoff_raw, utc=True, errors="coerce")

    return {
        "match_id": str(_first_value(record, "match_id", "matchId", "id", "game_id", "gameId", default="")),
        "stage": str(_first_value(record, "stage", "round", "phase", "group", default="Group")),
        "home_team": _team_name(home_obj) or _first_value(record, "home_name", "homeName", "team1_name"),
        "away_team": _team_name(away_obj) or _first_value(record, "away_name", "awayName", "team2_name"),
        "home_score": _score_value(record, "home"),
        "away_score": _score_value(record, "away"),
        "status": _normalize_status(_first_value(record, "status", "match_status", "state")),
        "kickoff_time": kickoff_time,
        "venue": _team_name(venue_obj) or _first_value(record, "venue_name", "venueName", "stadium_name", default=""),
        "host_city": _nested_value(record, ("venue", "city"), ("stadium", "city"), default=_first_value(record, "host_city", "hostCity", "city", default="")),
    }


def _payload_to_dataframe(payload: Any) -> pd.DataFrame:
    games = _extract_games(payload)
    rows = [_normalize_match(game) for game in games]
    df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    df = df.dropna(subset=["home_team", "away_team"], how="any")
    df["kickoff_time"] = pd.to_datetime(df["kickoff_time"], utc=True, errors="coerce")
    df = df.sort_values("kickoff_time", na_position="last").reset_index(drop=True)
    return df[OUTPUT_COLUMNS]


def _fetch_primary_payload() -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("Fetching World Cup data from primary API (attempt %s/%s)", attempt, MAX_RETRIES)
            response = requests.get(PRIMARY_API_URL, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            logger.info("Primary World Cup API healthy: HTTP %s", response.status_code)
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning("Primary World Cup API attempt %s failed: %s", attempt, exc)
            if attempt < MAX_RETRIES:
                sleep_seconds = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.info("Retrying primary API after %.1f seconds", sleep_seconds)
                time.sleep(sleep_seconds)
    raise DataUnavailableError(f"Primary API unavailable after {MAX_RETRIES} attempts: {last_error}")


def _load_fallback_payload(path: Path = FALLBACK_FIXTURE_PATH) -> Any:
    logger.info("Loading World Cup fallback fixtures from %s", path)
    with path.open("r", encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def _set_cache(df: pd.DataFrame, source: str) -> pd.DataFrame:
    _CACHE.update({"expires_at": time.time() + CACHE_TTL_SECONDS, "dataframe": df.copy(), "source": source})
    logger.info("Cached %s World Cup matches from %s for %s seconds", len(df), source, CACHE_TTL_SECONDS)
    return df


def get_live_data() -> pd.DataFrame:
    """Return a normalized DataFrame of World Cup matches from API or fallback."""
    cached_df = _CACHE.get("dataframe")
    if cached_df is not None and time.time() < float(_CACHE.get("expires_at", 0)):
        logger.info("World Cup data cache hit from %s", _CACHE.get("source"))
        return cached_df.copy()

    logger.info("World Cup data cache miss")
    try:
        df = _payload_to_dataframe(_fetch_primary_payload())
        if not df.empty:
            return _set_cache(df, "primary_api")
        logger.warning("Primary API returned no usable matches; trying fallback fixtures")
    except Exception as exc:
        logger.warning("Primary source unavailable; falling back to fixtures_backup.json: %s", exc)

    try:
        df = _payload_to_dataframe(_load_fallback_payload())
        if not df.empty:
            return _set_cache(df, "fallback_file")
        logger.warning("Fallback fixture file contained no usable matches")
    except Exception as exc:
        logger.error("Fallback fixture load failed: %s", exc)

    if cached_df is not None and not cached_df.empty:
        logger.warning("Serving stale World Cup cache because all sources failed")
        return cached_df.copy()

    raise DataUnavailableError("No World Cup match data available from primary API, fallback file, or cache")


def get_team_form(team_name: str) -> pd.DataFrame:
    """Return the latest five cached matches involving ``team_name``."""
    if not team_name or not team_name.strip():
        raise ValueError("team_name must be a non-empty string")

    cached_df = _CACHE.get("dataframe")
    df = cached_df.copy() if cached_df is not None else get_live_data()
    normalized_team = team_name.strip().casefold()
    mask = (df["home_team"].astype(str).str.casefold() == normalized_team) | (
        df["away_team"].astype(str).str.casefold() == normalized_team
    )
    return df.loc[mask].sort_values("kickoff_time", ascending=False, na_position="last").head(5).reset_index(drop=True)
