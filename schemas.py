from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class AppStatusResponse(BaseModel):
    message: str
    status: str
    version: str
    endpoints: List[str]


class ScrapedFixtureItem(BaseModel):
    id: int
    home: str
    away: str
    league: Optional[str] = None
    status: Optional[str] = None
    date: str


class ScrapeFixturesResponse(BaseModel):
    source: str
    requested_date: Optional[str] = None
    note: Optional[str] = None
    available_dates: List[str] = []
    scraped: int
    collection: Optional[CollectionSummaryResponse] = None
    fixtures: List[ScrapedFixtureItem]


class TeamResponse(BaseModel):
    id: int
    name: str
    league: Optional[str] = None
    country: Optional[str] = None
    attack: float
    defense: float


class TeamCreateResponse(BaseModel):
    id: int
    name: str
    status: str


class FixtureResponse(BaseModel):
    id: int
    home: Optional[str] = None
    away: Optional[str] = None
    league: Optional[str] = None
    date: str
    status: Optional[str] = None


class HistoricalScrapeResponse(BaseModel):
    message: str
    count: int
    season: str


class PredictionGenerationResponse(BaseModel):
    message: str
    count: int
    fixtures_processed: int
    requested_date: Optional[str] = None
    note: Optional[str] = None
    available_dates: List[str] = []


class PredictionSummaryResponse(BaseModel):
    market: str
    selection: str
    probability: str
    edge: str
    stake: float
    confidence: str


class MatchPredictionsResponse(BaseModel):
    match: str
    league: Optional[str] = None
    date: str
    predictions: List[PredictionSummaryResponse]


class PredictionsTodayResponse(BaseModel):
    count: int
    requested_date: Optional[str] = None
    note: Optional[str] = None
    available_dates: List[str] = []
    predictions: List[MatchPredictionsResponse]


class TopPredictionResponse(BaseModel):
    fixture_id: int
    match: str
    league: Optional[str] = None
    date: str
    market: str
    selection: str
    probability: float
    market_probability: float
    edge: float
    fair_odds: Optional[float] = None
    rank_score: float
    stake: float
    confidence: str
    confidence_score: float
    confidence_grade: str
    odds: Optional[float] = None
    expected_home_goals: Optional[float] = None
    expected_away_goals: Optional[float] = None
    main_reasons: List[str] = []
    risk_notes: List[str] = []


class TopPredictionsListResponse(BaseModel):
    count: int
    requested_date: Optional[str] = None
    note: Optional[str] = None
    available_dates: List[str] = []
    picks: List[TopPredictionResponse]


class UpdateStatsResponse(BaseModel):
    message: str
    updated: int


class PipelineResultsResponse(BaseModel):
    scraped: int
    stats_updated: int
    predictions_created: int


class CollectionSummaryResponse(BaseModel):
    requested_date: str
    source: str
    raw_fixtures: int
    unique_fixtures: int
    duplicates_removed: int
    active_leagues: int
    analyzable_fixtures: int
    predicted_fixtures: int
    qualified_markets: int
    ranked_picks: int
    source_breakdown: dict[str, int] = {}


class PipelineResponse(BaseModel):
    status: str
    requested_date: Optional[str] = None
    note: Optional[str] = None
    available_dates: List[str] = []
    collection: Optional[CollectionSummaryResponse] = None
    results: PipelineResultsResponse


class AvailableDateSummary(BaseModel):
    date: str
    fixtures: int
    predictions: int


class AvailableDatesResponse(BaseModel):
    count: int
    dates: List[AvailableDateSummary]
