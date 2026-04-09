from datetime import date as calendar_date

from utils.browser_sources import ConmebolLibertadoresAdapter


def test_conmebol_adapter_jsonld_fallback_extracts_sportsevent():
    adapter = ConmebolLibertadoresAdapter()
    html = '''
    <html><head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "SportsEvent",
      "name": "Group Stage Match",
      "startDate": "2026-04-09T19:30:00+00:00",
      "homeTeam": {"name": "Alpha FC"},
      "awayTeam": {"name": "Beta FC"}
    }
    </script>
    </head><body></body></html>
    '''

    rows = adapter._extract_candidates_from_json_ld(html, calendar_date(2026, 4, 9), "https://example.com")

    assert len(rows) == 1
    assert rows[0].home_team == "Alpha FC"
    assert rows[0].away_team == "Beta FC"
    assert rows[0].kickoff_time == "19:30"
