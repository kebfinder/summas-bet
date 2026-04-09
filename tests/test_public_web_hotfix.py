from datetime import date as calendar_date
from pathlib import Path
import sys
import time

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Query
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from database.database import Base
from database.models import Fixture
from utils.browser_assist import BrowserFixtureCandidate
from utils.browser_sources import get_default_browser_adapters, get_public_web_support_matrix


def _reset_public_web_state():
    main._PUBLIC_WEB_BUNDLE_CACHE.clear()
    main._PUBLIC_WEB_INFLIGHT.clear()
    main._PUBLIC_WEB_ADAPTER_COOLDOWNS.clear()


def _minimal_source_bundle(source_name: str, failed: bool = False) -> dict:
    return {
        "source": source_name,
        "source_breakdown": {source_name: 0},
        "raw_fixtures": 0,
        "fixtures": [],
        "source_status": {
            source_name: {
                "enabled": True,
                "requested_date": "2026-04-09",
                "ran": True,
                "raw_fixtures": 0,
                "survived_unique_fixtures": 0,
                "duplicates_removed": 0,
                "failed": failed,
                "failure_reason": "boom" if failed else None,
            }
        },
        "note": "public_web failed safely: boom" if failed else None,
    }


def _merge_status_bundle(_db, *bundles):
    source_status = {}
    source_breakdown = {}
    for bundle in bundles:
        source_status.update(bundle.get("source_status", {}))
        source_breakdown.update(bundle.get("source_breakdown", {}))
    return {
        "source": "hybrid",
        "raw_fixtures": 0,
        "fixtures": [],
        "source_breakdown": source_breakdown,
        "source_status": source_status,
    }


def test_hybrid_required_success_fails_closed_when_public_web_fails(monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_WEB_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_REQUIRED_SUCCESS", True)
    monkeypatch.setattr(main, "_collect_footballdata_bundle", lambda *args, **kwargs: _minimal_source_bundle("footballdata"))
    monkeypatch.setattr(main, "_collect_openligadb_bundle", lambda *args, **kwargs: _minimal_source_bundle("openligadb"))
    monkeypatch.setattr(main, "_collect_public_web_bundle", lambda *args, **kwargs: _minimal_source_bundle("public_web", failed=True))
    monkeypatch.setattr(main, "_combine_collection_bundles", _merge_status_bundle)
    monkeypatch.setattr(main, "enrich_fixtures", lambda db, fixtures: {})

    try:
        main._collect_fixture_bundle("hybrid", db=object(), match_date=calendar_date(2026, 4, 9))
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "public_web failed" in exc.detail
    else:
        raise AssertionError("Expected hybrid run to fail closed when PUBLIC_WEB_REQUIRED_SUCCESS=true")


def test_hybrid_optional_public_web_failure_stays_fail_open(monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_WEB_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_REQUIRED_SUCCESS", False)
    monkeypatch.setattr(main, "_collect_footballdata_bundle", lambda *args, **kwargs: _minimal_source_bundle("footballdata"))
    monkeypatch.setattr(main, "_collect_openligadb_bundle", lambda *args, **kwargs: _minimal_source_bundle("openligadb"))
    monkeypatch.setattr(main, "_collect_public_web_bundle", lambda *args, **kwargs: _minimal_source_bundle("public_web", failed=True))
    monkeypatch.setattr(main, "_combine_collection_bundles", _merge_status_bundle)
    monkeypatch.setattr(main, "enrich_fixtures", lambda db, fixtures: {})

    bundle = main._collect_fixture_bundle("hybrid", db=object(), match_date=calendar_date(2026, 4, 9))
    assert bundle["source"] == "hybrid"
    assert bundle["source_status"]["public_web"]["failed"] is True


def test_public_web_candidate_failure_does_not_rollback_prior_success(monkeypatch):
    _reset_public_web_state()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    class _Adapter:
        source_name = "test_adapter"

        def discover(self, requested_date, plan):
            return [
                BrowserFixtureCandidate(
                    source_name=self.source_name,
                    source_url="https://example.com",
                    competition_name="League A",
                    match_date=requested_date.isoformat(),
                    kickoff_time="10:00",
                    home_team="Good One FC",
                    away_team="Good Two FC",
                    match_status="scheduled",
                    extraction_confidence=0.9,
                ),
                BrowserFixtureCandidate(
                    source_name=self.source_name,
                    source_url="https://example.com",
                    competition_name="League A",
                    match_date=requested_date.isoformat(),
                    kickoff_time="12:00",
                    home_team="Boom FC",
                    away_team="Good Three FC",
                    match_status="scheduled",
                    extraction_confidence=0.9,
                ),
                BrowserFixtureCandidate(
                    source_name=self.source_name,
                    source_url="https://example.com",
                    competition_name="League A",
                    match_date=requested_date.isoformat(),
                    kickoff_time="14:00",
                    home_team="Good Four FC",
                    away_team="Good Five FC",
                    match_status="scheduled",
                    extraction_confidence=0.9,
                ),
            ]

    original_get_or_create = main._get_or_create_team_for_public_web

    def _flaky_get_or_create(session, name, league, resolver=None):
        if name == "Boom FC":
            raise RuntimeError("simulated candidate ingest failure")
        return original_get_or_create(session, name, league, resolver)

    monkeypatch.setattr(main, "PUBLIC_WEB_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_ALLOWED_SOURCES", [])
    monkeypatch.setattr("utils.browser_sources.get_default_browser_adapters", lambda allowed_sources: [_Adapter()])
    monkeypatch.setattr(main, "_get_or_create_team_for_public_web", _flaky_get_or_create)

    bundle = main._collect_public_web_bundle(db, match_date=calendar_date(2026, 4, 9))
    stored = db.query(Fixture).all()
    db.close()

    assert len(stored) == 2
    assert bundle["source_status"]["public_web"]["survived_unique_fixtures"] == 2
    assert bundle["source_status"]["public_web"]["failed"] is True


def test_public_web_bundle_exposes_per_adapter_diagnostics(monkeypatch):
    _reset_public_web_state()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    class _Adapter:
        source_name = "test_adapter_diag"

        def discover(self, requested_date, plan):
            return [
                BrowserFixtureCandidate(
                    source_name=self.source_name,
                    source_url="https://example.com",
                    competition_name="League A",
                    match_date=requested_date.isoformat(),
                    kickoff_time="10:00",
                    home_team="One FC",
                    away_team="Two FC",
                    match_status="scheduled",
                    extraction_confidence=0.9,
                )
            ]

    monkeypatch.setattr(main, "PUBLIC_WEB_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_ALLOWED_SOURCES", [])
    monkeypatch.setattr("utils.browser_sources.get_default_browser_adapters", lambda allowed_sources: [_Adapter()])

    bundle = main._collect_public_web_bundle(db, match_date=calendar_date(2026, 4, 9))
    db.close()

    adapter = bundle["source_status"]["public_web"]["adapters"]["test_adapter_diag"]
    assert adapter["ran"] is True
    assert adapter["raw_rows_found"] == 1
    assert adapter["parsed_fixtures"] == 1
    assert adapter["persisted_fixtures"] == 1
    assert bundle["source_status"]["public_web"]["supported_competitions"]


def test_public_web_collection_uses_short_lived_cache(monkeypatch):
    _reset_public_web_state()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    calls = {"count": 0}

    class _Adapter:
        source_name = "test_adapter_cache"

        def discover(self, requested_date, plan):
            calls["count"] += 1
            return [
                BrowserFixtureCandidate(
                    source_name=self.source_name,
                    source_url="https://example.com",
                    competition_name="League A",
                    match_date=requested_date.isoformat(),
                    kickoff_time="10:00",
                    home_team="Cache One FC",
                    away_team="Cache Two FC",
                    match_status="scheduled",
                    extraction_confidence=0.9,
                )
            ]

    monkeypatch.setattr(main, "PUBLIC_WEB_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_ALLOWED_SOURCES", [])
    monkeypatch.setattr(main, "PUBLIC_WEB_CACHE_TTL_SECONDS", 120)
    monkeypatch.setattr("utils.browser_sources.get_default_browser_adapters", lambda allowed_sources: [_Adapter()])

    first = main._collect_public_web_bundle(db, match_date=calendar_date(2026, 4, 9))
    second = main._collect_public_web_bundle(db, match_date=calendar_date(2026, 4, 9))
    db.close()

    assert calls["count"] == 1
    assert first["source_status"]["public_web"]["cache_hit"] is False
    assert second["source_status"]["public_web"]["cache_hit"] is True


def test_public_web_adapters_expand_beyond_conmebol():
    names = [adapter.source_name for adapter in get_default_browser_adapters(())]
    assert names == [
        "uefa_champions_league",
        "uefa_europa_league",
        "uefa_conference_league",
        "conmebol_libertadores",
        "conmebol_sudamericana",
    ]


def test_public_web_support_matrix_exposes_phased_explicit_competition_plan():
    matrix = get_public_web_support_matrix(())
    assert len(matrix) == 28
    assert matrix[0]["competition_name"] == "Premier League"
    assert matrix[0]["priority"] == 1
    assert matrix[9]["competition_name"] == "Copa Sudamericana"
    assert matrix[9]["implemented"] is True
    assert matrix[10]["competition_name"] == "CAF Champions League"
    assert matrix[10]["implemented"] is False
    assert matrix[10]["eligible_for_run"] is False


def test_public_web_team_identity_matching_reuses_existing_team(monkeypatch):
    _reset_public_web_state()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    legacy = main.Team(name="Atletico Nacional", league="Copa Libertadores")
    db.add(legacy)
    db.commit()

    resolved = main._get_or_create_team_for_public_web(db, "Atlético Nacional", "Copa Libertadores")
    db.commit()
    teams = db.query(main.Team).all()
    db.close()

    assert resolved.id == legacy.id
    assert len(teams) == 1


def test_public_web_team_identity_matching_does_not_cross_league_merge():
    _reset_public_web_state()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    legacy = main.Team(name="Atletico Nacional", league="Copa Libertadores")
    db.add(legacy)
    db.commit()

    resolved = main._get_or_create_team_for_public_web(db, "Atlético Nacional", "Brasileirão")
    db.commit()
    teams = db.query(main.Team).order_by(main.Team.id.asc()).all()
    db.close()

    assert resolved.id != legacy.id
    assert len(teams) == 2


def test_public_web_ingestion_scans_team_table_once_per_bundle(monkeypatch):
    _reset_public_web_state()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    class _Adapter:
        source_name = "test_adapter_perf"

        def discover(self, requested_date, plan):
            return [
                BrowserFixtureCandidate(
                    source_name=self.source_name,
                    source_url="https://example.com",
                    competition_name="League A",
                    match_date=requested_date.isoformat(),
                    kickoff_time="10:00",
                    home_team="Atletico Nacional",
                    away_team="Millonarios FC",
                    match_status="scheduled",
                    extraction_confidence=0.9,
                ),
                BrowserFixtureCandidate(
                    source_name=self.source_name,
                    source_url="https://example.com",
                    competition_name="League A",
                    match_date=requested_date.isoformat(),
                    kickoff_time="12:00",
                    home_team="Atlético Nacional",
                    away_team="Santa Fe",
                    match_status="scheduled",
                    extraction_confidence=0.9,
                ),
            ]

    original_query_all = Query.all
    team_all_calls = {"count": 0}

    def _counting_all(self):
        entities = [description.get("entity") for description in self.column_descriptions]
        if main.Team in entities:
            team_all_calls["count"] += 1
        return original_query_all(self)

    monkeypatch.setattr(main, "PUBLIC_WEB_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_ALLOWED_SOURCES", [])
    monkeypatch.setattr("utils.browser_sources.get_default_browser_adapters", lambda allowed_sources: [_Adapter()])
    monkeypatch.setattr(Query, "all", _counting_all)

    try:
        main._collect_public_web_bundle(db, match_date=calendar_date(2026, 4, 9))
    finally:
        db.close()

    assert team_all_calls["count"] == 1


def test_public_web_required_success_fails_when_adapter_is_cooling_down(monkeypatch):
    _reset_public_web_state()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    class _Adapter:
        source_name = "test_adapter_cooldown"

        def discover(self, requested_date, plan):
            return []

    monkeypatch.setattr(main, "PUBLIC_WEB_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_REQUIRED_SUCCESS", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_ALLOWED_SOURCES", [])
    monkeypatch.setattr(
        "utils.browser_sources.get_default_browser_adapters",
        lambda allowed_sources: [_Adapter()],
    )
    monkeypatch.setitem(
        main._PUBLIC_WEB_ADAPTER_COOLDOWNS,
        "test_adapter_cooldown",
        time.time() + 60,
    )

    try:
        main._collect_fixture_bundle("public_web", db=db, match_date=calendar_date(2026, 4, 9))
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "public_web" in exc.detail
        assert "cooldown" in exc.detail
    else:
        raise AssertionError("Expected public_web source to fail closed during adapter cooldown")
    finally:
        db.close()


def test_public_web_reports_unknown_allowlist_sources(monkeypatch):
    _reset_public_web_state()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    monkeypatch.setattr(main, "PUBLIC_WEB_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_ALLOWED_SOURCES", ["not_real_adapter"])

    bundle = main._collect_public_web_bundle(db, match_date=calendar_date(2026, 4, 9))
    status = bundle["source_status"]["public_web"]
    db.close()

    assert status["failed"] is True
    assert "not_real_adapter" in status["unsupported_allowed_sources"]
    assert "Unknown allowlist sources" in (status["failure_reason"] or "")
