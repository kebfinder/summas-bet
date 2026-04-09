from pathlib import Path
import os


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    raw_value = os.getenv(name, default)
    return tuple(item.strip() for item in raw_value.split(",") if item.strip())


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


BASE_DIR = Path(__file__).resolve().parent
_load_dotenv(BASE_DIR / ".env")


def _normalize_database_url(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("postgres://"):
        return "postgresql://" + raw[len("postgres://") :]
    return raw


DATABASE_URL = _normalize_database_url(
    os.getenv(
        "DATABASE_URL",
        f"sqlite:///{(BASE_DIR / 'football.db').as_posix()}",
    )
)
DEFAULT_OPENLIGADB_LEAGUES = _csv_env(
    "DEFAULT_OPENLIGADB_LEAGUES",
    "bl1,bl2,bl3,dfb,epl,ucl,uel,uecl,ssl,fbl1,fbl2,cwm",
)
DEFAULT_FOOTBALLDATA_PRODUCTION_CODES = _csv_env(
    "DEFAULT_FOOTBALLDATA_PRODUCTION_CODES",
    "BL1,BL2,BL3,PL,PD,SA,FL1,DED,PPL,SSL,BSA,ASL,LMX,AAL,ABL,BJL,CPD,CLP,DSU,MLS,TSL,SPL,ELC,CL,EL,UCL,CLI",
)
DEFAULT_FOOTBALLDATA_EXPANSION_CODES = _csv_env(
    "DEFAULT_FOOTBALLDATA_EXPANSION_CODES",
    "CSL,PRVA,GSL,HNB,ILH,JJL,TIP,PPD,RL1,RFPL,ALL,UPL,EL1,EL2,ENL,FL2,SB,ISC,DJL,SD,BSB,DFB",
)
FOOTBALLDATA_TARGET_FIXTURE_COUNT = int(os.getenv("FOOTBALLDATA_TARGET_FIXTURE_COUNT", "40"))
FOOTBALLDATA_COMPETITION_SWEEP_LIMIT = int(os.getenv("FOOTBALLDATA_COMPETITION_SWEEP_LIMIT", "8"))
OPENLIGADB_ALLOWED_SPORTS = _csv_env(
    "OPENLIGADB_ALLOWED_SPORTS",
    "Fußball,Frauenfußball",
)
DISCOVER_OPENLIGADB_LEAGUES = os.getenv("DISCOVER_OPENLIGADB_LEAGUES", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
DEFAULT_LOOKAHEAD_DAYS = int(os.getenv("DEFAULT_LOOKAHEAD_DAYS", "0"))
RECENT_MATCH_SAMPLE_SIZE = int(os.getenv("RECENT_MATCH_SAMPLE_SIZE", "10"))
LEAGUE_AVG_GOALS = float(os.getenv("LEAGUE_AVG_GOALS", "1.25"))

ENABLE_CALIBRATED_DECISION_ENGINE = os.getenv("ENABLE_CALIBRATED_DECISION_ENGINE", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CALIBRATED_DECISION_ENGINE_MODE = os.getenv("CALIBRATED_DECISION_ENGINE_MODE", "legacy").strip().lower()
if CALIBRATED_DECISION_ENGINE_MODE not in {"legacy", "shadow", "active"}:
    CALIBRATED_DECISION_ENGINE_MODE = "legacy"
ENABLE_ENGINE_SHADOW_COMPARE_ARTIFACT = os.getenv("ENABLE_ENGINE_SHADOW_COMPARE_ARTIFACT", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CALIBRATED_ENGINE_MIN_MARKET_SAMPLES = int(os.getenv("CALIBRATED_ENGINE_MIN_MARKET_SAMPLES", "30"))
CALIBRATED_ENGINE_MIN_MARKET_HIT_RATE = float(os.getenv("CALIBRATED_ENGINE_MIN_MARKET_HIT_RATE", "0.5"))
CALIBRATED_ENGINE_SUPPRESS_UNDERPERFORMING_MARKETS = os.getenv(
    "CALIBRATED_ENGINE_SUPPRESS_UNDERPERFORMING_MARKETS", "false"
).strip().lower() in {"1", "true", "yes", "on"}
CALIBRATED_ENGINE_UNDERPERFORMANCE_PENALTY_SCALE = float(
    os.getenv("CALIBRATED_ENGINE_UNDERPERFORMANCE_PENALTY_SCALE", "0.2")
)
CALIBRATED_ENGINE_LOW_EVIDENCE_PENALTY = float(os.getenv("CALIBRATED_ENGINE_LOW_EVIDENCE_PENALTY", "0.03"))
CALIBRATED_ENGINE_MIN_CONFIDENCE = os.getenv("CALIBRATED_ENGINE_MIN_CONFIDENCE", "").strip()
CALIBRATED_ENGINE_EVIDENCE_LOOKBACK_DAYS = int(os.getenv("CALIBRATED_ENGINE_EVIDENCE_LOOKBACK_DAYS", "120"))
