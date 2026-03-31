from __future__ import annotations

from dataclasses import dataclass


def _csv_env(name: str, default: str = "") -> tuple[str, ...]:
    import os

    raw_value = os.getenv(name, default)
    return tuple(item.strip() for item in raw_value.split(",") if item.strip())


def normalize_competition_text(value: str | None) -> str:
    text = str(value or "").strip().lower()
    replacements = {
        "ß": "ss",
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ãÿ": "ss",
        "/": " ",
        "-": " ",
        "_": " ",
        ".": " ",
        "(": " ",
        ")": " ",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return " ".join(text.split())


@dataclass(frozen=True)
class CompetitionDefinition:
    canonical_id: str
    display_name: str
    competition_class: str
    country: str
    provider_aliases: dict[str, dict[str, tuple[str, ...]]]
    source_confidence: dict[str, float]
    collection_tier: str = "production"
    preferred_rank: int = 100
    allow_experimental_aliases: bool = False


@dataclass(frozen=True)
class CompetitionResolution:
    canonical_id: str
    display_name: str
    competition_class: str
    country: str
    collection_tier: str
    provider: str
    provider_code: str | None
    provider_name: str | None
    source_confidence: float
    admission_score: float
    experimental: bool


def _merge_names(*groups: tuple[str, ...] | list[str] | str) -> tuple[str, ...]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        values = (group,) if isinstance(group, str) else tuple(group or ())
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            key = normalize_competition_text(text)
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)
    return tuple(merged)


def _footballdata_definition(
    *,
    canonical_id: str,
    display_name: str,
    code: str,
    competition_class: str,
    country: str,
    preferred_rank: int,
    source_confidence: float,
    collection_tier: str = "production",
    names: tuple[str, ...] = (),
    storage_names: tuple[str, ...] = (),
) -> CompetitionDefinition:
    return CompetitionDefinition(
        canonical_id=canonical_id,
        display_name=display_name,
        competition_class=competition_class,
        country=country,
        collection_tier=collection_tier,
        preferred_rank=preferred_rank,
        source_confidence={"footballdata": source_confidence},
        provider_aliases={
            "footballdata": {"codes": (code,), "names": _merge_names(display_name, names)},
            "storage": {"names": _merge_names(display_name, storage_names)},
        },
    )


COMPETITION_CLASS_PROFILES = {
    "elite_domestic": {
        "home_goals": 1.45,
        "away_goals": 1.22,
        "btts": 0.54,
        "over15": 0.78,
        "over25": 0.55,
        "over35": 0.30,
        "quality_multiplier": 1.02,
        "calibration_shrink": 0.00,
        "confidence_delta": 2.0,
        "league_sample_target": 120,
    },
    "secondary_domestic": {
        "home_goals": 1.43,
        "away_goals": 1.19,
        "btts": 0.53,
        "over15": 0.76,
        "over25": 0.53,
        "over35": 0.28,
        "quality_multiplier": 0.98,
        "calibration_shrink": 0.02,
        "confidence_delta": 0.0,
        "league_sample_target": 110,
    },
    "third_tier_domestic": {
        "home_goals": 1.40,
        "away_goals": 1.16,
        "btts": 0.52,
        "over15": 0.75,
        "over25": 0.50,
        "over35": 0.26,
        "quality_multiplier": 0.94,
        "calibration_shrink": 0.04,
        "confidence_delta": -1.0,
        "league_sample_target": 105,
    },
    "domestic_cup": {
        "home_goals": 1.38,
        "away_goals": 1.13,
        "btts": 0.50,
        "over15": 0.73,
        "over25": 0.48,
        "over35": 0.24,
        "quality_multiplier": 0.88,
        "calibration_shrink": 0.08,
        "confidence_delta": -4.0,
        "league_sample_target": 140,
    },
    "continental_club": {
        "home_goals": 1.41,
        "away_goals": 1.15,
        "btts": 0.51,
        "over15": 0.74,
        "over25": 0.50,
        "over35": 0.27,
        "quality_multiplier": 0.90,
        "calibration_shrink": 0.06,
        "confidence_delta": -2.0,
        "league_sample_target": 135,
    },
    "womens_domestic": {
        "home_goals": 1.55,
        "away_goals": 1.28,
        "btts": 0.56,
        "over15": 0.82,
        "over25": 0.60,
        "over35": 0.35,
        "quality_multiplier": 0.94,
        "calibration_shrink": 0.04,
        "confidence_delta": -1.0,
        "league_sample_target": 90,
    },
    "global_cup": {
        "home_goals": 1.37,
        "away_goals": 1.11,
        "btts": 0.49,
        "over15": 0.72,
        "over25": 0.47,
        "over35": 0.23,
        "quality_multiplier": 0.86,
        "calibration_shrink": 0.09,
        "confidence_delta": -4.0,
        "league_sample_target": 150,
    },
    "experimental": {
        "home_goals": 1.36,
        "away_goals": 1.09,
        "btts": 0.50,
        "over15": 0.71,
        "over25": 0.47,
        "over35": 0.23,
        "quality_multiplier": 0.78,
        "calibration_shrink": 0.12,
        "confidence_delta": -8.0,
        "league_sample_target": 180,
    },
}


_ADDITIONAL_FOOTBALLDATA_COMPETITIONS: tuple[CompetitionDefinition, ...] = (
    _footballdata_definition(
        canonical_id="argentina_primera_division",
        display_name="Argentine Primera Division",
        code="ASL",
        competition_class="elite_domestic",
        country="Argentina",
        preferred_rank=26,
        source_confidence=0.93,
        names=("liga profesional", "argentine primera division"),
        storage_names=("argentine primera division", "liga profesional"),
    ),
    _footballdata_definition(
        canonical_id="a_league_men",
        display_name="A-League Men",
        code="AAL",
        competition_class="secondary_domestic",
        country="Australia",
        preferred_rank=27,
        source_confidence=0.89,
        names=("a league", "a-league", "a league men"),
        storage_names=("a-league men", "a league"),
    ),
    _footballdata_definition(
        canonical_id="brazil_serie_a",
        display_name="Brazil Serie A",
        code="BSA",
        competition_class="elite_domestic",
        country="Brazil",
        preferred_rank=28,
        source_confidence=0.94,
        names=("campeonato brasileiro serie a", "brazil serie a"),
        storage_names=("brazil serie a", "serie a brazil"),
    ),
    _footballdata_definition(
        canonical_id="chile_primera_division",
        display_name="Chile Primera Division",
        code="CPD",
        competition_class="secondary_domestic",
        country="Chile",
        preferred_rank=29,
        source_confidence=0.89,
        names=("primera division", "chilean primera division"),
        storage_names=("chile primera division", "primera division chile"),
    ),
    _footballdata_definition(
        canonical_id="chinese_super_league",
        display_name="Chinese Super League",
        code="CSL",
        competition_class="secondary_domestic",
        country="China",
        preferred_rank=30,
        source_confidence=0.88,
        names=("chinese super league", "super league china"),
        storage_names=("chinese super league",),
    ),
    _footballdata_definition(
        canonical_id="colombia_primera_a",
        display_name="Colombia Primera A",
        code="CLP",
        competition_class="secondary_domestic",
        country="Colombia",
        preferred_rank=31,
        source_confidence=0.89,
        names=("liga postobon", "categoria primera a", "primera a"),
        storage_names=("colombia primera a", "liga postobon"),
    ),
    _footballdata_definition(
        canonical_id="croatia_prva_liga",
        display_name="Croatia Prva Liga",
        code="PRVA",
        competition_class="secondary_domestic",
        country="Croatia",
        preferred_rank=32,
        source_confidence=0.88,
        names=("prva liga", "super sport hnl"),
        storage_names=("croatia prva liga", "prva liga"),
    ),
    _footballdata_definition(
        canonical_id="greece_super_league",
        display_name="Greece Super League",
        code="GSL",
        competition_class="secondary_domestic",
        country="Greece",
        preferred_rank=33,
        source_confidence=0.88,
        names=("super league greece", "greek super league"),
        storage_names=("greece super league", "super league greece"),
    ),
    _footballdata_definition(
        canonical_id="hungary_nb_i",
        display_name="Hungary NB I",
        code="HNB",
        competition_class="secondary_domestic",
        country="Hungary",
        preferred_rank=34,
        source_confidence=0.87,
        names=("nb i", "hungary nb i"),
        storage_names=("hungary nb i", "nb i"),
    ),
    _footballdata_definition(
        canonical_id="israel_premier_league",
        display_name="Israel Premier League",
        code="ILH",
        competition_class="secondary_domestic",
        country="Israel",
        preferred_rank=35,
        source_confidence=0.87,
        names=("ligat haal", "ligat ha al", "israel premier league"),
        storage_names=("israel premier league", "ligat haal"),
    ),
    _footballdata_definition(
        canonical_id="j1_league",
        display_name="J1 League",
        code="JJL",
        competition_class="secondary_domestic",
        country="Japan",
        preferred_rank=36,
        source_confidence=0.90,
        names=("j league", "j1 league", "j league division 1"),
        storage_names=("j1 league", "j league"),
    ),
    _footballdata_definition(
        canonical_id="liga_mx",
        display_name="Liga MX",
        code="LMX",
        competition_class="elite_domestic",
        country="Mexico",
        preferred_rank=37,
        source_confidence=0.92,
        names=("liga mx",),
        storage_names=("liga mx",),
    ),
    _footballdata_definition(
        canonical_id="norway_eliteserien",
        display_name="Norway Eliteserien",
        code="TIP",
        competition_class="secondary_domestic",
        country="Norway",
        preferred_rank=38,
        source_confidence=0.88,
        names=("tippeligaen", "eliteserien"),
        storage_names=("norway eliteserien", "tippeligaen", "eliteserien"),
    ),
    _footballdata_definition(
        canonical_id="peru_primera_division",
        display_name="Peru Primera Division",
        code="PPD",
        competition_class="secondary_domestic",
        country="Peru",
        preferred_rank=39,
        source_confidence=0.87,
        names=("primera division peru", "peru primera division"),
        storage_names=("peru primera division",),
    ),
    _footballdata_definition(
        canonical_id="romania_liga_i",
        display_name="Romania Liga I",
        code="RL1",
        competition_class="secondary_domestic",
        country="Romania",
        preferred_rank=40,
        source_confidence=0.87,
        names=("liga i", "romania liga i"),
        storage_names=("romania liga i", "liga i romania"),
    ),
    _footballdata_definition(
        canonical_id="russia_premier_league",
        display_name="Russia Premier League",
        code="RFPL",
        competition_class="secondary_domestic",
        country="Russia",
        preferred_rank=41,
        source_confidence=0.87,
        names=("rfpl", "russian premier league"),
        storage_names=("russia premier league", "rfpl"),
    ),
    _footballdata_definition(
        canonical_id="sweden_allsvenskan",
        display_name="Sweden Allsvenskan",
        code="ALL",
        competition_class="secondary_domestic",
        country="Sweden",
        preferred_rank=42,
        source_confidence=0.89,
        names=("allsvenskan", "swedish allsvenskan"),
        storage_names=("sweden allsvenskan", "allsvenskan"),
    ),
    _footballdata_definition(
        canonical_id="ukraine_premier_liha",
        display_name="Ukraine Premier Liha",
        code="UPL",
        competition_class="secondary_domestic",
        country="Ukraine",
        preferred_rank=43,
        source_confidence=0.87,
        names=("premier liha", "ukraine premier liha"),
        storage_names=("ukraine premier liha", "premier liha"),
    ),
    _footballdata_definition(
        canonical_id="efl_league_one",
        display_name="EFL League One",
        code="EL1",
        competition_class="third_tier_domestic",
        country="England",
        preferred_rank=44,
        source_confidence=0.88,
        collection_tier="expansion",
        names=("league one", "efl league one"),
        storage_names=("efl league one", "league one"),
    ),
    _footballdata_definition(
        canonical_id="efl_league_two",
        display_name="EFL League Two",
        code="EL2",
        competition_class="third_tier_domestic",
        country="England",
        preferred_rank=45,
        source_confidence=0.87,
        collection_tier="expansion",
        names=("league two", "efl league two"),
        storage_names=("efl league two", "league two"),
    ),
    _footballdata_definition(
        canonical_id="national_league",
        display_name="National League",
        code="ENL",
        competition_class="third_tier_domestic",
        country="England",
        preferred_rank=46,
        source_confidence=0.84,
        collection_tier="expansion",
        names=("national league",),
        storage_names=("national league",),
    ),
    _footballdata_definition(
        canonical_id="ligue_2",
        display_name="Ligue 2",
        code="FL2",
        competition_class="secondary_domestic",
        country="France",
        preferred_rank=47,
        source_confidence=0.89,
        collection_tier="expansion",
        names=("ligue 2",),
        storage_names=("ligue 2",),
    ),
    _footballdata_definition(
        canonical_id="serie_b",
        display_name="Serie B",
        code="SB",
        competition_class="secondary_domestic",
        country="Italy",
        preferred_rank=48,
        source_confidence=0.89,
        collection_tier="expansion",
        names=("serie b",),
        storage_names=("serie b",),
    ),
    _footballdata_definition(
        canonical_id="serie_c",
        display_name="Serie C",
        code="ISC",
        competition_class="third_tier_domestic",
        country="Italy",
        preferred_rank=49,
        source_confidence=0.84,
        collection_tier="expansion",
        names=("serie c",),
        storage_names=("serie c",),
    ),
    _footballdata_definition(
        canonical_id="eerste_divisie",
        display_name="Eerste Divisie",
        code="DJL",
        competition_class="secondary_domestic",
        country="Netherlands",
        preferred_rank=50,
        source_confidence=0.88,
        collection_tier="expansion",
        names=("eerste divisie",),
        storage_names=("eerste divisie",),
    ),
    _footballdata_definition(
        canonical_id="segunda_division",
        display_name="Segunda Division",
        code="SD",
        competition_class="secondary_domestic",
        country="Spain",
        preferred_rank=51,
        source_confidence=0.89,
        collection_tier="expansion",
        names=("segunda division",),
        storage_names=("segunda division",),
    ),
    _footballdata_definition(
        canonical_id="brazil_serie_b",
        display_name="Brazil Serie B",
        code="BSB",
        competition_class="secondary_domestic",
        country="Brazil",
        preferred_rank=52,
        source_confidence=0.88,
        collection_tier="expansion",
        names=("campeonato brasileiro serie b", "brazil serie b"),
        storage_names=("brazil serie b", "serie b brazil"),
    ),
)


COMPETITION_REGISTRY: tuple[CompetitionDefinition, ...] = (
    CompetitionDefinition(
        canonical_id="bundesliga_1",
        display_name="Bundesliga",
        competition_class="elite_domestic",
        country="Germany",
        preferred_rank=1,
        source_confidence={"openligadb": 0.97, "footballdata": 0.97},
        provider_aliases={
            "openligadb": {"codes": ("bl1",), "names": ("1 fussball bundesliga", "bundesliga")},
            "footballdata": {"codes": ("BL1",), "names": ("bundesliga",)},
            "storage": {"names": ("bundesliga", "1 fussball bundesliga", "1 fussball bundesliga 2025 2026")},
        },
    ),
    CompetitionDefinition(
        canonical_id="bundesliga_2",
        display_name="2. Bundesliga",
        competition_class="secondary_domestic",
        country="Germany",
        preferred_rank=2,
        source_confidence={"openligadb": 0.96, "footballdata": 0.95},
        provider_aliases={
            "openligadb": {"codes": ("bl2",), "names": ("2 fussball bundesliga", "2 bundesliga")},
            "footballdata": {"codes": ("BL2",), "names": ("2 bundesliga",)},
            "storage": {"names": ("2 bundesliga", "2 fussball bundesliga", "2 fussball bundesliga 2025 2026")},
        },
    ),
    CompetitionDefinition(
        canonical_id="liga_3",
        display_name="3. Liga",
        competition_class="third_tier_domestic",
        country="Germany",
        preferred_rank=3,
        source_confidence={"openligadb": 0.92, "footballdata": 0.90},
        provider_aliases={
            "openligadb": {"codes": ("bl3",), "names": ("3 liga",)},
            "footballdata": {"codes": ("BL3",), "names": ("3 liga", "3. liga")},
            "storage": {"names": ("3 liga", "3. liga", "3 liga 2025 2026")},
        },
    ),
    CompetitionDefinition(
        canonical_id="dfb_pokal",
        display_name="DFB-Pokal",
        competition_class="domestic_cup",
        country="Germany",
        preferred_rank=4,
        source_confidence={"openligadb": 0.92, "footballdata": 0.90},
        provider_aliases={
            "openligadb": {"codes": ("dfb", "dfl"), "names": ("dfb pokal",)},
            "footballdata": {"codes": ("DFB",), "names": ("dfb pokal",)},
            "storage": {"names": ("dfb pokal",)},
        },
    ),
    CompetitionDefinition(
        canonical_id="premier_league",
        display_name="Premier League",
        competition_class="elite_domestic",
        country="England",
        preferred_rank=5,
        source_confidence={"openligadb": 0.88, "footballdata": 0.97},
        provider_aliases={
            "openligadb": {"codes": ("epl",), "names": ("premier league",)},
            "footballdata": {"codes": ("PL",), "names": ("premier league",)},
            "storage": {"names": ("premier league",)},
        },
    ),
    CompetitionDefinition(
        canonical_id="la_liga",
        display_name="La Liga",
        competition_class="elite_domestic",
        country="Spain",
        preferred_rank=6,
        source_confidence={"footballdata": 0.97},
        provider_aliases={
            "footballdata": {"codes": ("PD",), "names": ("primera division", "la liga")},
            "storage": {"names": ("la liga", "primera division")},
        },
    ),
    CompetitionDefinition(
        canonical_id="serie_a",
        display_name="Serie A",
        competition_class="elite_domestic",
        country="Italy",
        preferred_rank=7,
        source_confidence={"footballdata": 0.97},
        provider_aliases={
            "footballdata": {"codes": ("SA",), "names": ("serie a",)},
            "storage": {"names": ("serie a",)},
        },
    ),
    CompetitionDefinition(
        canonical_id="ligue_1",
        display_name="Ligue 1",
        competition_class="elite_domestic",
        country="France",
        preferred_rank=8,
        source_confidence={"footballdata": 0.96},
        provider_aliases={
            "footballdata": {"codes": ("FL1",), "names": ("ligue 1",)},
            "storage": {"names": ("ligue 1",)},
        },
    ),
    CompetitionDefinition(
        canonical_id="eredivisie",
        display_name="Eredivisie",
        competition_class="elite_domestic",
        country="Netherlands",
        preferred_rank=9,
        source_confidence={"footballdata": 0.95},
        provider_aliases={
            "footballdata": {"codes": ("DED",), "names": ("eredivisie",)},
            "storage": {"names": ("eredivisie",)},
        },
    ),
    CompetitionDefinition(
        canonical_id="primeira_liga",
        display_name="Primeira Liga",
        competition_class="elite_domestic",
        country="Portugal",
        preferred_rank=10,
        source_confidence={"footballdata": 0.95},
        provider_aliases={
            "footballdata": {"codes": ("PPL",), "names": ("primeira liga",)},
            "storage": {"names": ("primeira liga",)},
        },
    ),
    CompetitionDefinition(
        canonical_id="championship",
        display_name="Championship",
        competition_class="secondary_domestic",
        country="England",
        preferred_rank=11,
        source_confidence={"footballdata": 0.94},
        provider_aliases={
            "footballdata": {"codes": ("ELC",), "names": ("championship",)},
            "storage": {"names": ("championship",)},
        },
    ),
    CompetitionDefinition(
        canonical_id="champions_league",
        display_name="UEFA Champions League",
        competition_class="continental_club",
        country="Europe",
        preferred_rank=12,
        source_confidence={"openligadb": 0.90, "footballdata": 0.96},
        provider_aliases={
            "openligadb": {"codes": ("ucl",), "names": ("champions league",)},
            "footballdata": {"codes": ("CL",), "names": ("uefa champions league", "champions league")},
            "storage": {"names": ("uefa champions league", "champions league")},
        },
    ),
    CompetitionDefinition(
        canonical_id="europa_league",
        display_name="UEFA Europa League",
        competition_class="continental_club",
        country="Europe",
        preferred_rank=13,
        source_confidence={"openligadb": 0.89, "footballdata": 0.95},
        provider_aliases={
            "openligadb": {"codes": ("uel",), "names": ("europa league",)},
            "footballdata": {"codes": ("EL",), "names": ("uefa europa league", "europa league")},
            "storage": {"names": ("uefa europa league", "europa league")},
        },
    ),
    CompetitionDefinition(
        canonical_id="conference_league",
        display_name="UEFA Conference League",
        competition_class="continental_club",
        country="Europe",
        preferred_rank=14,
        source_confidence={"openligadb": 0.86, "footballdata": 0.93},
        provider_aliases={
            "openligadb": {"codes": ("uecl",), "names": ("conference league", "europa conference league")},
            "footballdata": {"codes": ("UCL", "ECL"), "names": ("uefa conference league", "conference league")},
            "storage": {"names": ("uefa conference league", "conference league", "europa conference league")},
        },
    ),
    CompetitionDefinition(
        canonical_id="copa_libertadores",
        display_name="Copa Libertadores",
        competition_class="continental_club",
        country="South America",
        collection_tier="expansion",
        preferred_rank=15,
        source_confidence={"footballdata": 0.91},
        provider_aliases={
            "footballdata": {"codes": ("CLI",), "names": ("copa libertadores",)},
            "storage": {"names": ("copa libertadores",)},
        },
    ),
    CompetitionDefinition(
        canonical_id="jupiler_pro_league",
        display_name="Belgian Pro League",
        competition_class="secondary_domestic",
        country="Belgium",
        collection_tier="expansion",
        preferred_rank=16,
        source_confidence={"footballdata": 0.92},
        provider_aliases={
            "footballdata": {"codes": ("BJL",), "names": ("jupiler pro league",)},
            "storage": {"names": ("belgian pro league", "jupiler pro league")},
        },
    ),
    CompetitionDefinition(
        canonical_id="austrian_bundesliga",
        display_name="Austrian Bundesliga",
        competition_class="secondary_domestic",
        country="Austria",
        collection_tier="expansion",
        preferred_rank=17,
        source_confidence={"footballdata": 0.91},
        provider_aliases={
            "footballdata": {"codes": ("ABL",), "names": ("bundesliga", "austrian bundesliga")},
            "storage": {"names": ("austrian bundesliga",)},
        },
    ),
    CompetitionDefinition(
        canonical_id="danish_superliga",
        display_name="Danish Superliga",
        competition_class="secondary_domestic",
        country="Denmark",
        collection_tier="expansion",
        preferred_rank=18,
        source_confidence={"footballdata": 0.90},
        provider_aliases={
            "footballdata": {"codes": ("DSU",), "names": ("superliga", "danish superliga")},
            "storage": {"names": ("danish superliga",)},
        },
    ),
    CompetitionDefinition(
        canonical_id="turkish_super_lig",
        display_name="Turkish Super Lig",
        competition_class="secondary_domestic",
        country="Turkey",
        collection_tier="expansion",
        preferred_rank=19,
        source_confidence={"footballdata": 0.90},
        provider_aliases={
            "footballdata": {"codes": ("TSL",), "names": ("super lig", "super ligue")},
            "storage": {"names": ("turkish super lig", "super lig")},
        },
    ),
    CompetitionDefinition(
        canonical_id="scottish_premiership",
        display_name="Scottish Premiership",
        competition_class="secondary_domestic",
        country="Scotland",
        collection_tier="expansion",
        preferred_rank=20,
        source_confidence={"footballdata": 0.89},
        provider_aliases={
            "footballdata": {"codes": ("SPL",), "names": ("premier league", "scottish premiership")},
            "storage": {"names": ("scottish premiership", "premier league scotland")},
        },
    ),
    CompetitionDefinition(
        canonical_id="major_league_soccer",
        display_name="Major League Soccer",
        competition_class="secondary_domestic",
        country="United States",
        collection_tier="expansion",
        preferred_rank=21,
        source_confidence={"footballdata": 0.88},
        provider_aliases={
            "footballdata": {"codes": ("MLS",), "names": ("mls", "major league soccer")},
            "storage": {"names": ("mls", "major league soccer")},
        },
    ),
    CompetitionDefinition(
        canonical_id="swiss_super_league",
        display_name="Swiss Super League",
        competition_class="elite_domestic",
        country="Switzerland",
        preferred_rank=22,
        source_confidence={"openligadb": 0.82, "footballdata": 0.90},
        provider_aliases={
            "openligadb": {"codes": ("ssl",), "names": ("super league schweiz", "swiss super league", "super league")},
            "footballdata": {"codes": ("SSL",), "names": ("swiss super league", "super league switzerland")},
            "storage": {"names": ("swiss super league", "super league schweiz", "super league")},
        },
    ),
    CompetitionDefinition(
        canonical_id="frauen_bundesliga_1",
        display_name="Frauen-Bundesliga",
        competition_class="womens_domestic",
        country="Germany",
        preferred_rank=23,
        source_confidence={"openligadb": 0.90},
        provider_aliases={
            "openligadb": {"codes": ("fbl1",), "names": ("1 frauen bundesliga", "frauen bundesliga")},
            "storage": {"names": ("frauen bundesliga", "1 frauen bundesliga")},
        },
    ),
    CompetitionDefinition(
        canonical_id="frauen_bundesliga_2",
        display_name="2. Frauen-Bundesliga",
        competition_class="womens_domestic",
        country="Germany",
        preferred_rank=24,
        source_confidence={"openligadb": 0.86},
        provider_aliases={
            "openligadb": {"codes": ("fbl2",), "names": ("2 frauen bundesliga",)},
            "storage": {"names": ("2 frauen bundesliga",)},
        },
    ),
    CompetitionDefinition(
        canonical_id="club_world_cup",
        display_name="FIFA Club World Cup",
        competition_class="global_cup",
        country="International",
        preferred_rank=25,
        source_confidence={"openligadb": 0.82},
        provider_aliases={
            "openligadb": {"codes": ("cwm",), "names": ("club world cup",)},
            "storage": {"names": ("fifa club world cup", "club world cup")},
        },
    ),
    *_ADDITIONAL_FOOTBALLDATA_COMPETITIONS,
)


COMPETITION_ADMISSION_THRESHOLD = float(_csv_env("COMPETITION_ADMISSION_THRESHOLD", "0.66")[0] if _csv_env("COMPETITION_ADMISSION_THRESHOLD", "0.66") else "0.66")
EXPERIMENTAL_ADMISSION_THRESHOLD = float(_csv_env("EXPERIMENTAL_ADMISSION_THRESHOLD", "0.58")[0] if _csv_env("EXPERIMENTAL_ADMISSION_THRESHOLD", "0.58") else "0.58")
EXPERIMENTAL_OPENLIGADB_LEAGUES = tuple(normalize_competition_text(item) for item in _csv_env("EXPERIMENTAL_OPENLIGADB_LEAGUES", ""))
EXPERIMENTAL_FOOTBALLDATA_COMPETITIONS = tuple(normalize_competition_text(item) for item in _csv_env("EXPERIMENTAL_FOOTBALLDATA_COMPETITIONS", ""))

_EXPERIMENTAL_HINT_KEYWORDS = (
    "league",
    "liga",
    "division",
    "championship",
    "cup",
    "super league",
    "frauen",
    "women",
)

_EXPERIMENTAL_EXCLUDE_KEYWORDS = (
    "kreisliga",
    "bezirksliga",
    "landesliga",
    "oberliga",
    "regionalliga",
    "u17",
    "u19",
    "u21",
    "jugend",
    "reserve",
    "ii",
    "iii",
    "test",
    "debug",
)


def _known_competition_score(definition: CompetitionDefinition, provider: str) -> float:
    source_confidence = (
        max(definition.source_confidence.values())
        if provider == "storage" and definition.source_confidence
        else definition.source_confidence.get(provider, 0.72)
    )
    class_bonus = {
        "elite_domestic": 0.14,
        "secondary_domestic": 0.11,
        "third_tier_domestic": 0.08,
        "domestic_cup": 0.08,
        "continental_club": 0.10,
        "womens_domestic": 0.07,
        "global_cup": 0.06,
        "experimental": -0.02,
    }.get(definition.competition_class, 0.0)
    return round(min(source_confidence + class_bonus, 0.99), 3)


def _iter_provider_aliases(definition: CompetitionDefinition, provider: str):
    provider_aliases = definition.provider_aliases.get(provider, {})
    for code in provider_aliases.get("codes", ()):
        yield "code", normalize_competition_text(code)
    for name in provider_aliases.get("names", ()):
        yield "name", normalize_competition_text(name)


def resolve_competition(
    provider: str,
    *,
    code: str | None = None,
    name: str | None = None,
) -> CompetitionResolution | None:
    normalized_code = normalize_competition_text(code)
    normalized_name = normalize_competition_text(name)

    for definition in COMPETITION_REGISTRY:
        matched = False
        for alias_type, alias_value in _iter_provider_aliases(definition, provider):
            if alias_type == "code" and normalized_code and alias_value == normalized_code:
                matched = True
                break
            if alias_type == "name" and normalized_name and alias_value and alias_value in normalized_name:
                matched = True
                break
        if matched:
            source_confidence = (
                max(definition.source_confidence.values())
                if provider == "storage" and definition.source_confidence
                else definition.source_confidence.get(provider, 0.72)
            )
            return CompetitionResolution(
                canonical_id=definition.canonical_id,
                display_name=definition.display_name,
                competition_class=definition.competition_class,
                country=definition.country,
                collection_tier=definition.collection_tier,
                provider=provider,
                provider_code=code,
                provider_name=name,
                source_confidence=source_confidence,
                admission_score=_known_competition_score(definition, provider),
                experimental=False,
            )

    return _resolve_experimental_competition(provider, code=code, name=name)


def _experimental_whitelist(provider: str) -> tuple[str, ...]:
    if provider == "openligadb":
        return EXPERIMENTAL_OPENLIGADB_LEAGUES
    if provider == "footballdata":
        return EXPERIMENTAL_FOOTBALLDATA_COMPETITIONS
    return ()


def _resolve_experimental_competition(
    provider: str,
    *,
    code: str | None = None,
    name: str | None = None,
) -> CompetitionResolution | None:
    normalized_code = normalize_competition_text(code)
    normalized_name = normalize_competition_text(name)
    combined = f"{normalized_code} {normalized_name}".strip()
    if not combined:
        return None
    if any(keyword in combined for keyword in _EXPERIMENTAL_EXCLUDE_KEYWORDS):
        return None

    score = 0.42
    if normalized_code in _experimental_whitelist(provider) or normalized_name in _experimental_whitelist(provider):
        score += 0.16
    if any(keyword in combined for keyword in _EXPERIMENTAL_HINT_KEYWORDS):
        score += 0.08
    if len(normalized_code) <= 8 and normalized_code:
        score += 0.03
    if "frauen" in combined or "women" in combined:
        score += 0.03

    score = round(min(score, 0.72), 3)
    if score < EXPERIMENTAL_ADMISSION_THRESHOLD:
        return None

    display_name = str(name or code or "Experimental Competition").strip()
    return CompetitionResolution(
        canonical_id=f"experimental::{normalized_code or normalized_name}",
        display_name=display_name,
        competition_class="experimental",
        country="Unknown",
        collection_tier="experimental",
        provider=provider,
        provider_code=code,
        provider_name=name,
        source_confidence=0.56,
        admission_score=score,
        experimental=True,
    )


def is_competition_admitted(
    provider: str,
    *,
    code: str | None = None,
    name: str | None = None,
) -> bool:
    resolution = resolve_competition(provider, code=code, name=name)
    if resolution is None:
        return False
    if resolution.experimental:
        return resolution.admission_score >= EXPERIMENTAL_ADMISSION_THRESHOLD
    return resolution.admission_score >= COMPETITION_ADMISSION_THRESHOLD


def preferred_provider_codes(provider: str, tiers: tuple[str, ...] | None = None) -> list[str]:
    codes = []
    allowed_tiers = set(tiers or ())
    for definition in sorted(COMPETITION_REGISTRY, key=lambda item: item.preferred_rank):
        if allowed_tiers and definition.collection_tier not in allowed_tiers:
            continue
        provider_aliases = definition.provider_aliases.get(provider, {})
        if provider_aliases.get("codes"):
            codes.append(provider_aliases["codes"][0])
    return codes


def competition_profile_for_name(name: str | None) -> dict:
    resolution = resolve_competition("storage", name=name)
    competition_class = resolution.competition_class if resolution else "experimental"
    profile = COMPETITION_CLASS_PROFILES.get(competition_class, COMPETITION_CLASS_PROFILES["experimental"]).copy()
    profile["competition_class"] = competition_class
    profile["display_name"] = resolution.display_name if resolution else str(name or "Unknown competition")
    profile["canonical_id"] = resolution.canonical_id if resolution else f"experimental::{normalize_competition_text(name)}"
    profile["source_confidence"] = resolution.source_confidence if resolution else 0.56
    profile["admission_score"] = resolution.admission_score if resolution else 0.58
    profile["experimental"] = resolution.experimental if resolution else True
    profile["country"] = resolution.country if resolution else "Unknown"
    profile["collection_tier"] = resolution.collection_tier if resolution else "experimental"
    return profile


def canonical_display_name(provider: str, *, code: str | None = None, name: str | None = None) -> str:
    resolution = resolve_competition(provider, code=code, name=name)
    return resolution.display_name if resolution else str(name or code or "Unknown")
