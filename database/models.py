from sqlalchemy import Column, Integer, Float, String, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from database.database import Base
from datetime import datetime

class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)
    league = Column(String, nullable=True)
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
    date = Column(DateTime)
    league = Column(String, nullable=True)
    status = Column(String, default='scheduled')
    
    home_id = Column(Integer, ForeignKey("teams.id"))
    away_id = Column(Integer, ForeignKey("teams.id"))

    home = relationship("Team", foreign_keys=[home_id])
    away = relationship("Team", foreign_keys=[away_id])
    
    home_goals = Column(Integer, nullable=True)
    away_goals = Column(Integer, nullable=True)

class HistoricalMatch(Base):
    __tablename__ = "historical_matches"
    
    id = Column(Integer, primary_key=True)
    home_id = Column(Integer, ForeignKey("teams.id"))
    away_id = Column(Integer, ForeignKey("teams.id"))
    date = Column(DateTime)
    season = Column(String(10), nullable=True)
    
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

class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, ForeignKey("fixtures.id"))
    
    market = Column(String)
    selection = Column(String, nullable=True)
    probability = Column(Float)
    odds = Column(Float, nullable=True)
    edge = Column(Float)
    stake = Column(Float)
    confidence = Column(String, nullable=True)
    
    expected_home_goals = Column(Float, nullable=True)
    expected_away_goals = Column(Float, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    fixture = relationship("Fixture")