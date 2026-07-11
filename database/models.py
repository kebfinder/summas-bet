from sqlalchemy import Column, Integer, Float, String, DateTime, ForeignKey, Boolean, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from database.database import Base
from datetime import datetime

class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False, index=True)
    league = Column(String, nullable=True, index=True)
    country = Column(String, nullable=True)
    
    avg_scored = Column(Float, default=1.25)
    avg_conceded = Column(Float, default=1.25)
    attack_strength = Column(Float, default=1.0)
    defense_strength = Column(Float, default=1.0)
    btts_rate = Column(Float, default=0)
    over15_rate = Column(Float, default=0)
    over25_rate = Column(Float, default=0)
    form = Column(Float, default=0)
    matches_played = Column(Integer, default=0)
    total_goals_scored = Column(Integer, default=0)
    total_goals_conceded = Column(Integer, default=0)

class Fixture(Base):
    __tablename__ = "fixtures"

    id = Column(Integer, primary_key=True)
    date = Column(DateTime, nullable=False, index=True)
    league = Column(String, nullable=True, index=True)
    status = Column(String, default='scheduled', index=True)
    
    home_id = Column(Integer, ForeignKey("teams.id"), nullable=False, index=True)
    away_id = Column(Integer, ForeignKey("teams.id"), nullable=False, index=True)

    home = relationship("Team", foreign_keys=[home_id])
    away = relationship("Team", foreign_keys=[away_id])
    
    home_goals = Column(Integer, nullable=True)
    away_goals = Column(Integer, nullable=True)
    
    # HIGH FIX (F5): Add unique constraint to prevent duplicate fixtures
    __table_args__ = (
        UniqueConstraint('home_id', 'away_id', 'date', 'league', 
                        name='uq_fixture_matchup'),
        Index('ix_fixture_date_league', 'date', 'league'),
    )

class HistoricalMatch(Base):
    __tablename__ = "historical_matches"
    
    id = Column(Integer, primary_key=True)
    home_id = Column(Integer, ForeignKey("teams.id"), nullable=False, index=True)
    away_id = Column(Integer, ForeignKey("teams.id"), nullable=False, index=True)
    date = Column(DateTime, nullable=False, index=True)
    season = Column(String(10), nullable=True, index=True)
    
    home_goals = Column(Integer)
    away_goals = Column(Integer)
    home_shots = Column(Integer, nullable=True)
    away_shots = Column(Integer, nullable=True)
    home_sot = Column(Integer, nullable=True)
    away_sot = Column(Integer, nullable=True)
    home_corners = Column(Integer, nullable=True)
    away_corners = Column(Integer, nullable=True)
    
    btts = Column(Boolean, nullable=True)
    over_2_5 = Column(Boolean, nullable=True)
    over_1_5 = Column(Boolean, nullable=True)
    
    # HIGH FIX (F5): Add unique constraint and composite index to prevent duplicates
    # and enable efficient team+venue lookups
    __table_args__ = (
        UniqueConstraint('home_id', 'away_id', 'date', 'season',
                        name='uq_historical_match'),
        Index('ix_historical_home_date', 'home_id', 'date'),
        Index('ix_historical_away_date', 'away_id', 'date'),
        Index('ix_historical_season', 'season'),
    )

class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, ForeignKey("fixtures.id"), nullable=False, index=True)
    
    market = Column(String, nullable=False)
    selection = Column(String, nullable=True)
    probability = Column(Float)
    odds = Column(Float, nullable=True)
    edge = Column(Float)
    stake = Column(Float)
    confidence = Column(String, nullable=True)
    
    expected_home_goals = Column(Float, nullable=True)
    expected_away_goals = Column(Float, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    fixture = relationship("Fixture")
    
    # HIGH FIX (F5): Index on created_at for backtest queries
    __table_args__ = (
        Index('ix_prediction_created_market', 'created_at', 'market'),
    )
