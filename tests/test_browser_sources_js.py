from datetime import date as calendar_date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.browser_assist import BrowserAssistPlan
import utils.browser_sources as browser_sources


def _plan() -> BrowserAssistPlan:
    return BrowserAssistPlan(
        requested_date="2026-04-09",
        coverage_status="debug",
        trigger_eligible=True,
        trigger_reasons=(),
        adapters_configured=(),
        target_fixture_count=0,
        target_analyzable_count=0,
        min_active_leagues=0,
        note="test",
    )


def test_default_browser_adapters_include_priority_js_competitions():
    adapters = browser_sources.get_default_browser_adapters(())
    source_names = {adapter.source_name for adapter in adapters}
    assert "official_js_premier_league" in source_names
    assert "official_js_la_liga" in source_names
    assert "official_js_serie_a" in source_names
    assert "official_js_bundesliga" in source_names
    assert "official_js_ligue_1" in source_names
    assert "official_js_championship" in source_names
    assert "official_js_mls" in source_names
    assert "official_js_liga_mx" in source_names


def test_js_rendered_adapter_parses_scoreboard_payload():
    adapter = browser_sources.JsRenderedScoreboardAdapter(
        source_name="official_js_premier_league",
        league_slug="eng.1",
        competition_name="Premier League",
    )
    payload = {
        "events": [
            {
                "date": "2026-04-09T18:30Z",
                "competitions": [
                    {
                        "status": {"type": {"state": "pre"}},
                        "competitors": [
                            {"homeAway": "home", "team": {"displayName": "Arsenal"}},
                            {"homeAway": "away", "team": {"displayName": "Liverpool"}},
                        ],
                    }
                ],
            }
        ]
    }
    out = adapter._parse_scoreboard(payload, calendar_date(2026, 4, 9), "https://example.com")
    assert len(out) == 1
    assert out[0].competition_name == "Premier League"
    assert out[0].home_team == "Arsenal"
    assert out[0].away_team == "Liverpool"
    assert out[0].kickoff_time == "18:30"


def test_js_rendered_adapter_respects_failure_cooldown(monkeypatch):
    adapter = browser_sources.JsRenderedScoreboardAdapter(
        source_name="official_js_premier_league",
        league_slug="eng.1",
        competition_name="Premier League",
    )

    class _FailingSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        trust_env = False

        def get(self, *_args, **_kwargs):
            raise RuntimeError("network down")

    monkeypatch.setattr(browser_sources.requests, "Session", lambda: _FailingSession())
    browser_sources._SCOREBOARD_CACHE.clear()
    browser_sources._SCOREBOARD_COOLDOWN.clear()
    browser_sources._PROVIDER_HEALTH.clear()

    first = adapter.discover(calendar_date(2026, 4, 9), _plan())
    assert first == []
    assert browser_sources._SCOREBOARD_COOLDOWN
    second = adapter.discover(calendar_date(2026, 4, 9), _plan())
    assert second == []


def test_js_rendered_adapter_falls_back_to_secondary_provider(monkeypatch):
    adapter = browser_sources.JsRenderedScoreboardAdapter(
        source_name="official_js_premier_league",
        league_slug="eng.1",
        competition_name="Premier League",
    )
    browser_sources._SCOREBOARD_CACHE.clear()
    browser_sources._SCOREBOARD_COOLDOWN.clear()
    browser_sources._PROVIDER_HEALTH.clear()

    class _Response:
        def __init__(self, payload):
            self._payload = payload
            self.content = b"x"

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Session:
        trust_env = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, timeout=30):
            if "site.api.espn.com" in url:
                raise RuntimeError("primary down")
            return _Response(
                {
                    "events": [
                        {
                            "date": "2026-04-09T18:30Z",
                            "competitions": [
                                {
                                    "status": {"type": {"state": "pre"}},
                                    "competitors": [
                                        {"homeAway": "home", "team": {"displayName": "Arsenal"}},
                                        {"homeAway": "away", "team": {"displayName": "Liverpool"}},
                                    ],
                                }
                            ],
                        }
                    ]
                }
            )

    monkeypatch.setattr(browser_sources.requests, "Session", lambda: _Session())
    out = adapter.discover(calendar_date(2026, 4, 9), _plan())
    assert len(out) == 1
    assert adapter.last_run_diagnostics["provider_used"] == "espn_sports_core_api"
    assert "espn_site_api" in adapter.last_run_diagnostics["provider_failures"]
    assert "provider_order" in adapter.last_run_diagnostics


def test_provider_health_snapshot_tracks_window_counts(monkeypatch):
    adapter = browser_sources.JsRenderedScoreboardAdapter(
        source_name="official_js_premier_league",
        league_slug="eng.1",
        competition_name="Premier League",
    )
    browser_sources._SCOREBOARD_CACHE.clear()
    browser_sources._SCOREBOARD_COOLDOWN.clear()
    browser_sources._PROVIDER_HEALTH.clear()
    browser_sources._PROVIDER_EVENT_LOG.clear()

    class _Response:
        content = b"x"

        def raise_for_status(self):
            return None

        def json(self):
            return {"events": []}

    class _Session:
        trust_env = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(browser_sources.requests, "Session", lambda: _Session())
    adapter.discover(calendar_date(2026, 4, 9), _plan())
    snapshot = browser_sources.provider_health_snapshot()
    assert "official_js_premier_league:espn_site_api" in snapshot
    assert snapshot["official_js_premier_league:espn_site_api"]["events_24h"]["total"] >= 1


def test_provider_weight_biases_order(monkeypatch):
    adapter = browser_sources.JsRenderedScoreboardAdapter(
        source_name="official_js_premier_league",
        league_slug="eng.1",
        competition_name="Premier League",
    )
    monkeypatch.setenv(
        "PUBLIC_WEB_PROVIDER_WEIGHTS",
        '{"official_js_premier_league":{"espn_sports_core_api":2.0,"espn_site_api":0.0}}',
    )
    browser_sources._PROVIDER_HEALTH.clear()
    ordered = adapter._ordered_provider_templates()
    assert ordered[0][0] == "espn_sports_core_api"
