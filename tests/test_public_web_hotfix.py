from datetime import date as calendar_date
from pathlib import Path
import sys

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from database.database import Base
from database.models import Fixture, FixtureIngestionProvenance, PublicWebProviderHealthEvent
from utils.browser_assist import BrowserFixtureCandidate


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

    def _flaky_get_or_create(session, name, league):
        if name == "Boom FC":
            raise RuntimeError("simulated candidate ingest failure")
        return original_get_or_create(session, name, league)

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
    assert bundle["source_status"]["public_web"]["adapter_candidate_counts"]["test_adapter"] == 3
    assert bundle["source_status"]["public_web"]["adapter_persisted_counts"]["test_adapter"] == 2
    assert bundle["source_status"]["public_web"]["adapter_modes"]["test_adapter"] == "static_fetch"
    assert bundle["source_status"]["public_web"]["adapter_stage_counts"]["test_adapter"]["parsed_fixtures"] == 3
    assert bundle["source_status"]["public_web"]["adapter_stage_counts"]["test_adapter"]["persisted_fixtures"] == 2


def test_public_web_status_includes_competition_diagnostics(monkeypatch):
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
                    home_team="Alpha FC",
                    away_team="Beta FC",
                    match_status="scheduled",
                    extraction_confidence=0.9,
                )
            ]

    monkeypatch.setattr(main, "PUBLIC_WEB_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_ALLOWED_SOURCES", [])
    monkeypatch.setattr("utils.browser_sources.get_default_browser_adapters", lambda allowed_sources: [_Adapter()])

    bundle = main._collect_public_web_bundle(db, match_date=calendar_date(2026, 4, 9))
    db.close()

    status = bundle["source_status"]["public_web"]
    assert status["implemented_adapters"] == ["test_adapter"]
    assert status["adapter_modes"]["test_adapter"] == "static_fetch"
    assert status["adapter_stage_counts"]["test_adapter"]["raw_rows_found"] == 1
    assert status["adapter_provider_order"]["test_adapter"] == []
    assert status["competition_candidate_counts"]["League A"] == 1
    assert status["competition_persisted_counts"]["League A"] == 1
    assert "analyzable_candidates_estimate" in status


def test_public_web_records_provenance_and_alias_hits(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    class _Adapter:
        source_name = "official_js_premier_league"

        def discover(self, requested_date, plan):
            return [
                BrowserFixtureCandidate(
                    source_name=self.source_name,
                    source_url="https://example.com/match",
                    competition_name="Premier League",
                    match_date=requested_date.isoformat(),
                    kickoff_time="10:00",
                    home_team="Man Utd",
                    away_team="Spurs",
                    match_status="scheduled",
                    extraction_confidence=0.9,
                )
            ]

        last_run_diagnostics = {
            "provider_used": "espn_site_api",
            "provider_failures": {},
            "raw_rows_found": 1,
            "parsed_fixtures": 1,
        }

    monkeypatch.setattr(main, "PUBLIC_WEB_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_ALLOWED_SOURCES", [])
    monkeypatch.setattr("utils.browser_sources.get_default_browser_adapters", lambda allowed_sources: [_Adapter()])

    bundle = main._collect_public_web_bundle(db, match_date=calendar_date(2026, 4, 9))
    status = bundle["source_status"]["public_web"]
    provenance = db.query(FixtureIngestionProvenance).all()
    db.close()

    assert status["team_alias_hits"] >= 2
    assert status["adapter_provider_usage"]["official_js_premier_league"] == "espn_site_api"
    assert len(provenance) == 1
    assert provenance[0].source_name == "official_js_premier_league"


def test_debug_public_web_adapter_health(monkeypatch):
    monkeypatch.setattr(
        "utils.browser_sources.provider_health_snapshot",
        lambda: {"official_js_premier_league:espn_site_api": {"events_24h": {"total": 2}}},
    )
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    payload = main.debug_public_web_adapter_health(history_limit=200, db=db)
    db.close()
    assert payload["source"] == "public_web"
    assert payload["providers_tracked"] == 1


def test_public_web_persists_provider_health_events(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    class _Adapter:
        source_name = "official_js_premier_league"

        def discover(self, requested_date, plan):
            return []

        last_run_diagnostics = {
            "provider_used": "espn_sports_core_api",
            "provider_failures": {"espn_site_api": "timeout"},
            "raw_rows_found": 0,
            "parsed_fixtures": 0,
            "provider_order": ["espn_sports_core_api", "espn_site_api"],
        }

    monkeypatch.setattr(main, "PUBLIC_WEB_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_WEB_ALLOWED_SOURCES", [])
    monkeypatch.setattr("utils.browser_sources.get_default_browser_adapters", lambda allowed_sources: [_Adapter()])
    main._collect_public_web_bundle(db, match_date=calendar_date(2026, 4, 9))
    rows = db.query(PublicWebProviderHealthEvent).all()
    db.close()

    assert len(rows) == 2
    assert {row.event for row in rows} == {"success", "failure"}
