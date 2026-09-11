from datetime import date as calendar_date
from datetime import datetime
from pathlib import Path
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from database.database import Base
from database.models import Fixture, Prediction, Team


def _new_fixture_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    home = Team(name="New Home", league="League A", avg_scored=1.25, avg_conceded=1.25)
    away = Team(name="New Away", league="League A", avg_scored=1.25, avg_conceded=1.25)
    db.add_all([home, away])
    db.flush()
    fixture = Fixture(
        home_id=home.id,
        away_id=away.id,
        league="League A",
        date=datetime(2026, 9, 11, 19, 0),
        status="scheduled",
    )
    db.add(fixture)
    db.commit()
    db.refresh(fixture)
    return db, fixture


def test_generate_predictions_keeps_fallback_forecasts_for_new_teams():
    db, fixture = _new_fixture_session()
    try:
        # Newly discovered teams have no local history, so this is the minimum
        # context quality that previously caused generation to be skipped.
        assert main.build_match_context(db, fixture)["data_quality"] == 0.22

        generated = main.generate_predictions(fixture_id=fixture.id, db=db)

        assert generated["count"] > 0
        assert db.query(Prediction).filter(Prediction.fixture_id == fixture.id).count() == generated["count"]
    finally:
        db.close()


def test_predictions_by_date_shows_low_evidence_forecasts():
    db, fixture = _new_fixture_session()
    try:
        main.generate_predictions(fixture_id=fixture.id, db=db)

        response = main.get_todays_predictions(match_date=calendar_date(2026, 9, 11), db=db)

        assert response["count"] == 1
        assert response["predictions"][0]["match"] == "New Home vs New Away"
        assert response["predictions"][0]["predictions"]
    finally:
        db.close()
