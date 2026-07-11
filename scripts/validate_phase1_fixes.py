#!/usr/bin/env python3
"""
Validation script for Phase 1 fixes.

Run this after applying the fixes to verify correctness:
  python scripts/validate_phase1_fixes.py
"""

import sys
import logging
from pathlib import Path

# Add repo to path
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def check_imports():
    """Verify all new/modified modules import correctly."""
    logger.info("Checking imports...")
    try:
        from engine.match_model import build_match_context, _team_match_rows
        from services.retrainer import retrain_from_outcomes, collect_resolved_predictions
        from engine.self_learning_model import extract_features_from_context, predict
        from services.decision_preview import preview_for_enriched_fixture
        from database.models import Team, Fixture, HistoricalMatch, Prediction
        logger.info("✓ All imports successful")
        return True
    except Exception as e:
        logger.error(f"✗ Import failed: {e}")
        return False


def check_temporal_cutoff():
    """Verify temporal cutoff parameter exists in match_model functions."""
    logger.info("Checking temporal cutoff implementation...")
    try:
        from engine.match_model import _team_match_rows, team_profile, build_match_context
        import inspect
        
        # Check _team_match_rows signature
        sig = inspect.signature(_team_match_rows)
        if 'before_date' not in sig.parameters:
            logger.error("✗ _team_match_rows missing 'before_date' parameter")
            return False
        
        # Check team_profile signature
        sig = inspect.signature(team_profile)
        if 'before_date' not in sig.parameters:
            logger.error("✗ team_profile missing 'before_date' parameter")
            return False
        
        # Check build_match_context signature
        sig = inspect.signature(build_match_context)
        if 'before_date' not in sig.parameters:
            logger.error("✗ build_match_context missing 'before_date' parameter")
            return False
        
        logger.info("✓ Temporal cutoff parameters present in all functions")
        return True
    except Exception as e:
        logger.error(f"✗ Temporal cutoff check failed: {e}")
        return False


def check_retraining_integration():
    """Verify retrain_from_outcomes is called from auto_updater."""
    logger.info("Checking retraining integration...")
    try:
        with open('services/auto_updater.py', 'r') as f:
            content = f.read()
        
        if 'retrain_from_outcomes' not in content:
            logger.error("✗ auto_updater.py doesn't call retrain_from_outcomes")
            return False
        
        if 'from services.retrainer import' not in content:
            logger.error("✗ auto_updater.py doesn't import retrainer")
            return False
        
        logger.info("✓ Retraining integration verified")
        return True
    except Exception as e:
        logger.error(f"✗ Retraining integration check failed: {e}")
        return False


def check_feature_extraction():
    """Verify feature extraction returns consistent vector."""
    logger.info("Checking feature extraction...")
    try:
        from engine.self_learning_model import extract_features_from_context
        
        # Test with dummy context
        dummy_context = {
            "home_xg": 1.5,
            "away_xg": 1.2,
            "data_quality": 0.8,
            "home_profile": {"recent_form": 0.6, "short_conceded": 1.0},
            "away_profile": {"recent_form": 0.5, "short_conceded": 1.1},
            "venue_support": 0.7,
            "empirical": {"btts": 0.5, "over15": 0.65},
            "competition_penalty": 0.0,
        }
        
        features = extract_features_from_context(dummy_context)
        
        if not isinstance(features, list):
            logger.error(f"✗ Features is {type(features)}, expected list")
            return False
        
        if len(features) != 11:
            logger.error(f"✗ Features has {len(features)} elements, expected 11")
            return False
        
        if not all(isinstance(f, (int, float)) for f in features):
            logger.error("✗ Not all features are numeric")
            return False
        
        logger.info(f"✓ Feature extraction verified (11 features: {features})")
        return True
    except Exception as e:
        logger.error(f"✗ Feature extraction check failed: {e}")
        return False


def check_db_constraints():
    """Verify database models have constraints and indexes."""
    logger.info("Checking database constraints...")
    try:
        from database.models import Fixture, HistoricalMatch, Prediction
        
        # Check Fixture
        fixture_constraints = str(Fixture.__table__.constraints)
        if 'uq_fixture_matchup' not in fixture_constraints:
            logger.error("✗ Fixture missing uq_fixture_matchup constraint")
            return False
        
        fixture_indexes = str(Fixture.__table__.indexes)
        if not fixture_indexes:
            logger.error("✗ Fixture has no indexes")
            return False
        
        # Check HistoricalMatch
        hist_constraints = str(HistoricalMatch.__table__.constraints)
        if 'uq_historical_match' not in hist_constraints:
            logger.error("✗ HistoricalMatch missing uq_historical_match constraint")
            return False
        
        hist_indexes = str(HistoricalMatch.__table__.indexes)
        if not hist_indexes:
            logger.error("✗ HistoricalMatch has no indexes")
            return False
        
        logger.info("✓ Database constraints and indexes verified")
        return True
    except Exception as e:
        logger.error(f"✗ Database constraints check failed: {e}")
        return False


def check_confidence_penalty():
    """Verify confidence penalty applied proportionally."""
    logger.info("Checking confidence penalty logic...")
    try:
        with open('services/decision_preview.py', 'r') as f:
            content = f.read()
        
        # Check for proportional penalty logic (not floor)
        if 'penalty = 0.10 if completeness == "partial" else 0.15' not in content:
            logger.error("✗ Confidence penalty not using proportional approach")
            return False
        
        if 'candidate["confidence_score"] = round(max(score - penalty' not in content:
            logger.error("✗ Penalty not being subtracted from score")
            return False
        
        logger.info("✓ Confidence penalty logic verified")
        return True
    except Exception as e:
        logger.error(f"✗ Confidence penalty check failed: {e}")
        return False


def main():
    """Run all validation checks."""
    logger.info("=" * 60)
    logger.info("PHASE 1 FIXES VALIDATION")
    logger.info("=" * 60)
    
    checks = [
        ("Imports", check_imports),
        ("Temporal Cutoff", check_temporal_cutoff),
        ("Retraining Integration", check_retraining_integration),
        ("Feature Extraction", check_feature_extraction),
        ("DB Constraints", check_db_constraints),
        ("Confidence Penalty", check_confidence_penalty),
    ]
    
    results = []
    for name, check_func in checks:
        try:
            result = check_func()
            results.append((name, result))
        except Exception as e:
            logger.error(f"✗ {name} check crashed: {e}")
            results.append((name, False))
        logger.info("")
    
    # Summary
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        logger.info(f"{status}: {name}")
    
    logger.info(f"\nTotal: {passed}/{total} checks passed")
    
    if passed == total:
        logger.info("\n🎉 All validation checks passed! Ready for deployment.")
        return 0
    else:
        logger.error(f"\n❌ {total - passed} check(s) failed. Review above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
