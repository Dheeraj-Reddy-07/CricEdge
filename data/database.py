"""
data/database.py — CricEdge SQLite Schema + Query Helpers

All 8 tables defined here. Queries return typed dicts/DataFrames.
Format isolation enforced at query level (Women's stats never
mixed with Men's player/team stats).
"""

import sqlite3
import json
import math
import os
import logging
from typing import Optional, Any
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# METADATA & CONSTANTS
# ─────────────────────────────────────────────────────────────────

ELITE_TEAMS = [
    'Australia', 'India', 'England', 'New Zealand', 'South Africa', 
    'West Indies', 'Sri Lanka', 'Bangladesh', 'Pakistan', 'Ireland',
    'Australia Women', 'India Women', 'England Women', 'New Zealand Women', 
    'South Africa Women', 'West Indies Women', 'Sri Lanka Women', 
    'Bangladesh Women', 'Pakistan Women', 'Ireland Women'
]


# ─────────────────────────────────────────────────────────────────
# CONNECTION HELPER
# ─────────────────────────────────────────────────────────────────

def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Return a SQLite connection with row_factory = dict-like Row."""
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ─────────────────────────────────────────────────────────────────
# SCHEMA CREATION
# ─────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- ── 1. venue_stats ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS venue_stats (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    venue                           TEXT NOT NULL,
    format                          TEXT NOT NULL,           -- 'Women's T20I' | 'T20I'
    avg_total_batting_first         REAL,
    avg_total_chasing               REAL,
    avg_runs_per_over               TEXT,  -- JSON array[20]: avg runs in that over
    avg_death_economy               REAL,  -- overs 16-20 overall
    historical_slowdown_pct         REAL,  -- (overs15-20 avg) vs (overs1-10 avg), as %
    avg_economy_overs_13_15         REAL,
    avg_economy_overs_16_20         REAL,
    sample_matches                  INTEGER DEFAULT 0,
    last_updated                    TEXT,
    UNIQUE(venue, format)
);

-- ── 2. team_batting_stats ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS team_batting_stats (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    team                            TEXT NOT NULL,
    format                          TEXT NOT NULL,
    opposition                      TEXT DEFAULT NULL,       -- NULL = all opponents
    last_n_matches                  INTEGER DEFAULT 10,
    avg_total                       REAL,
    avg_pp_score                    REAL,
    avg_death_score                 REAL,                    -- runs in overs 16-20
    avg_runs_after_wicket           REAL,                    -- transition penalty
    avg_sr_when_190plus_at_over18   REAL,                    -- psychological ceiling
    sample_matches                  INTEGER DEFAULT 0,
    last_updated                    TEXT,
    UNIQUE(team, format, opposition)
);

-- ── 3. team_bowling_stats ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS team_bowling_stats (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    team                            TEXT NOT NULL,
    format                          TEXT NOT NULL,
    last_n_matches                  INTEGER DEFAULT 10,
    avg_pp_economy                  REAL,
    avg_mid_economy                 REAL,
    avg_death_economy               REAL,
    death_bowlers                   TEXT,  -- JSON list of bowler names
    uses_spinner_in_death           INTEGER DEFAULT 0,  -- bool
    sample_matches                  INTEGER DEFAULT 0,
    last_updated                    TEXT,
    UNIQUE(team, format)
);

-- ── 4. batter_stats ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS batter_stats (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    player_name                     TEXT NOT NULL,
    format                          TEXT NOT NULL,
    sr_powerplay                    REAL,
    sr_middle                       REAL,
    sr_death                        REAL,
    sr_vs_spin_death                REAL,
    sr_vs_pace_death                REAL,
    sr_by_venue                     TEXT,  -- JSON: {venue: SR}
    last_10_form_sr                 REAL,
    avg_pct_team_runs               REAL,  -- carrying factor
    total_balls_faced               INTEGER DEFAULT 0,
    last_updated                    TEXT,
    UNIQUE(player_name, format)
);

-- ── 5. bowler_stats ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bowler_stats (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    player_name                     TEXT NOT NULL,
    format                          TEXT NOT NULL,
    economy_powerplay               REAL,
    economy_middle                  REAL,
    economy_death                   REAL,
    economy_by_venue                TEXT,  -- JSON: {venue: economy}
    last_10_form_economy            REAL,
    is_elite_death_bowler           INTEGER DEFAULT 0,  -- bool: death eco < 8.5
    bowler_role                     TEXT DEFAULT 'unknown',
    total_balls_bowled              INTEGER DEFAULT 0,
    last_updated                    TEXT,
    UNIQUE(player_name, format)
);

-- ── 6. head_to_head ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS head_to_head (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    batter                          TEXT NOT NULL,
    bowler                          TEXT NOT NULL,
    format                          TEXT NOT NULL,
    balls_faced                     INTEGER DEFAULT 0,
    runs_scored                     INTEGER DEFAULT 0,
    dismissals                      INTEGER DEFAULT 0,
    sr                              REAL,
    boundary_pct                    REAL,
    sr_spin_death                   REAL,  -- SR vs this bowler in death if spinner
    sr_pace_death                   REAL,
    last_updated                    TEXT,
    UNIQUE(batter, bowler, format)
);

-- ── 7. match_position_stats ──────────────────────────────────────
-- MOST IMPORTANT TABLE. Built by replaying every ball of every innings.
-- Stores distribution of outcomes given (runs, wickets, over).
CREATE TABLE IF NOT EXISTS match_position_stats (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    innings                         INTEGER NOT NULL,        -- 1 or 2
    format                          TEXT NOT NULL,
    match_tier                      TEXT DEFAULT 'all',      -- 'elite' | 'all'
    over_number                     INTEGER NOT NULL,        -- 0-19
    wickets_fallen                  INTEGER NOT NULL,        -- 0-10
    current_runs                    INTEGER NOT NULL,        -- bucketed to nearest 10
    sample_count                    INTEGER DEFAULT 0,
    pct_exceeded_100                REAL,
    pct_exceeded_110                REAL,
    pct_exceeded_120                REAL,
    pct_exceeded_130                REAL,
    pct_exceeded_140                REAL,
    pct_exceeded_150                REAL,
    pct_exceeded_160                REAL,
    pct_exceeded_170                REAL,
    pct_exceeded_180                REAL,
    pct_exceeded_190                REAL,
    pct_exceeded_200                REAL,
    pct_exceeded_210                REAL,
    pct_exceeded_220                REAL,
    avg_final_score                 REAL,
    std_final_score                 REAL,
    last_updated                    TEXT,
    UNIQUE(innings, format, match_tier, over_number, wickets_fallen, current_runs)
);

-- ── 8. predictions ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS predictions (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp                       TEXT NOT NULL,
    match                           TEXT NOT NULL,           -- 'Team A vs Team B'
    venue                           TEXT,
    format                          TEXT NOT NULL,
    innings                         INTEGER NOT NULL,        -- 1 or 2
    over_at_prediction              REAL NOT NULL,           -- e.g. 14.2
    current_score_at_prediction     TEXT NOT NULL,           -- e.g. '118/3'
    line                            REAL NOT NULL,           -- target score
    predicted_probability           REAL NOT NULL,
    verdict                         TEXT NOT NULL,           -- 'HIGH CONFIDENCE'|'LIKELY'|'TOO CLOSE TO CALL'
    base_probability                REAL,
    modifiers_fired                 TEXT,                    -- comma-separated list
    each_adjustment                 TEXT,                    -- JSON
    actual_result                   TEXT DEFAULT NULL,       -- 'OVER'|'UNDER'
    was_correct                     INTEGER DEFAULT NULL,    -- 1=yes, 0=no
    notes                           TEXT DEFAULT '',
    created_at                      TEXT DEFAULT (datetime('now'))
);

-- ── 9. db_metadata ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS db_metadata (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- ── 10. manual_scorecards ───────────────────────────────────────
-- Stores manually pasted scorecards (e.g. WT20 WC 2026)
-- parsed and ingested to update stats
CREATE TABLE IF NOT EXISTS manual_scorecards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    TEXT UNIQUE NOT NULL,
    match_date  TEXT,
    team1       TEXT,
    team2       TEXT,
    venue       TEXT,
    format      TEXT,
    raw_text    TEXT,           -- original paste
    parsed_json TEXT,           -- parsed scorecard as JSON
    ingested    INTEGER DEFAULT 0,  -- 1 = already processed into stats
    created_at  TEXT DEFAULT (datetime('now'))
);

-- ── 11. training_manifest ───────────────────────────────────────
-- Records EXACTLY which matches contributed to the derived statistical
-- tables in a leak-free training build. The out-of-sample backtest reads
-- this to assert that no evaluated (test) match was part of training.
CREATE TABLE IF NOT EXISTS training_manifest (
    match_id    TEXT PRIMARY KEY,   -- Cricsheet match id (json filename stem)
    match_date  TEXT NOT NULL,      -- YYYY-MM-DD; guaranteed < SPLIT_DATE
    format      TEXT NOT NULL
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_mps_lookup
    ON match_position_stats(innings, format, match_tier, over_number, wickets_fallen, current_runs);
CREATE INDEX IF NOT EXISTS idx_predictions_timestamp
    ON predictions(timestamp);
CREATE INDEX IF NOT EXISTS idx_batter_format
    ON batter_stats(player_name, format);
CREATE INDEX IF NOT EXISTS idx_bowler_format
    ON bowler_stats(player_name, format);
CREATE INDEX IF NOT EXISTS idx_h2h_lookup
    ON head_to_head(batter, bowler, format);
"""


def init_db(db_path: Optional[str] = None) -> None:
    """Create all tables and indexes. Safe to call repeatedly."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(INDEXES_SQL)
        conn.commit()
        _set_meta(conn, "schema_version", "1")
        logger.info("Database initialized at %s", db_path or config.DB_PATH)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# TEMPORAL-SPLIT / LEAK-FREE TRAINING SUPPORT
# ─────────────────────────────────────────────────────────────────

# Every derived statistical table — i.e. everything the model reads at
# prediction time. Dropping all of these guarantees a from-scratch rebuild
# with no stale (and potentially post-split) rows surviving.
DERIVED_TABLES = [
    "venue_stats", "team_batting_stats", "team_bowling_stats",
    "batter_stats", "bowler_stats", "head_to_head",
    "match_position_stats", "predictions", "training_manifest",
]


def drop_derived_tables(db_path: Optional[str] = None) -> None:
    """Drop ALL derived statistical tables so the next build starts clean.

    Used by the leak-free training build. Raw inputs (manual_scorecards,
    db_metadata) are left intact; everything the model consumes is rebuilt.
    """
    conn = get_connection(db_path)
    try:
        for t in DERIVED_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
        logger.info("Dropped derived tables: %s", ", ".join(DERIVED_TABLES))
    finally:
        conn.close()


def save_training_manifest(rows: list[tuple], db_path: Optional[str] = None) -> int:
    """Persist the (match_id, match_date, format) of every match used in training.

    Returns the number of rows written. INSERT OR IGNORE so a match that appears
    in more than one format's pass is recorded once per id.
    """
    conn = get_connection(db_path)
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO training_manifest(match_id, match_date, format) "
            "VALUES (?,?,?)",
            rows,
        )
        conn.commit()
        n = conn.execute("SELECT COUNT(*) AS n FROM training_manifest").fetchone()["n"]
        return int(n)
    finally:
        conn.close()


def get_training_manifest_ids(db_path: Optional[str] = None) -> set:
    """Return the set of match_ids that contributed to the training tables.

    Returns an empty set if the manifest table is absent (e.g. a DB that was
    NOT built by the leak-free pipeline) so callers can fail loudly themselves.
    """
    conn = get_connection(db_path)
    try:
        try:
            rows = conn.execute("SELECT match_id FROM training_manifest").fetchall()
        except sqlite3.OperationalError:
            return set()
        return {r["match_id"] for r in rows}
    finally:
        conn.close()


def get_training_date_bounds(db_path: Optional[str] = None) -> dict:
    """Return {count, first_date, last_date} over the training manifest."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(match_date) AS first_date, "
            "MAX(match_date) AS last_date FROM training_manifest"
        ).fetchone()
        return {
            "count": int(row["n"] or 0),
            "first_date": row["first_date"],
            "last_date": row["last_date"],
        }
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# METADATA & CONSTANTS
# ─────────────────────────────────────────────────────────────────

ELITE_TEAMS = [
    'Australia', 'India', 'England', 'New Zealand', 'South Africa', 
    'West Indies', 'Sri Lanka', 'Bangladesh', 'Pakistan', 'Ireland',
    'Australia Women', 'India Women', 'England Women', 'New Zealand Women', 
    'South Africa Women', 'West Indies Women', 'Sri Lanka Women', 
    'Bangladesh Women', 'Pakistan Women', 'Ireland Women'
]

def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO db_metadata(key, value) VALUES (?, ?)",
        (key, value)
    )
    conn.commit()


def set_meta(key: str, value: str, db_path: Optional[str] = None) -> None:
    conn = get_connection(db_path)
    try:
        _set_meta(conn, key, value)
    finally:
        conn.close()


def get_meta(key: str, default: Any = None, db_path: Optional[str] = None) -> Any:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM db_metadata WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def get_db_status(db_path: Optional[str] = None) -> dict:
    """Return summary stats about what's loaded in the DB."""
    conn = get_connection(db_path)
    try:
        def count(table: str, where: str = "1=1") -> int:
            row = conn.execute(f"SELECT COUNT(*) as n FROM {table} WHERE {where}").fetchone()
            return row["n"] if row else 0

        return {
            "venue_stats_count":            count("venue_stats"),
            "batter_stats_count":           count("batter_stats"),
            "bowler_stats_count":           count("bowler_stats"),
            "match_position_count":         count("match_position_stats"),
            "predictions_count":            count("predictions"),
            "womens_match_positions":       count("match_position_stats", "format='Women''s T20I'"),
            "mens_match_positions":         count("match_position_stats", "format='T20I'"),
            "t20_blast_match_positions":    count("match_position_stats", "format='T20 Blast'"),
            "last_ingest":                  get_meta("last_ingest_time", "Never", db_path),
            "womens_matches_ingested":      get_meta("womens_matches_ingested", "0", db_path),
            "mens_matches_ingested":        get_meta("mens_matches_ingested", "0", db_path),
            "t20_blast_matches_ingested":   get_meta("t20_blast_matches_ingested", "0", db_path),
            "manual_scorecards":            count("manual_scorecards"),
            "manual_scorecards_ingested":   count("manual_scorecards", "ingested=1"),
            "manual_scorecards_pending":    count("manual_scorecards", "ingested=0"),
        }
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# MATCH POSITION QUERY — Core baseline lookup
# ─────────────────────────────────────────────────────────────────

def query_match_position(
    innings: int,
    format_: str,
    over_number: int,
    wickets_fallen: int,
    current_runs: int,
    line: float,
    db_path: Optional[str] = None,
) -> Optional[float]:
    """Look up historical % exceeding line. Returns None if insufficient data."""
    detail = query_match_position_detail(
        innings, format_, over_number, wickets_fallen, current_runs, line, db_path
    )
    if detail["pct"] is None:
        return None
    if detail["sample_count"] < config.MIN_POSITION_SAMPLES:
        return None
    return detail["pct"]


# Below this matched-sample size we no longer trust a raw/interpolated bucket value;
# we blend it toward the phase-level global average (hierarchical smoothing).
SMOOTHING_SAMPLE_FLOOR = 10


def _phase_db_over_band(db_over: int, format_: str) -> tuple[int, int]:
    """Return the (lo, hi) db-over band for the phase containing `db_over`.
    db_over is 0-indexed (cricket over N → db key N-1)."""
    is_odi = format_ in config.ODI_FORMATS if format_ else False
    if is_odi:
        if db_over <= 9:
            return (0, 9)
        if db_over <= 39:
            return (10, 39)
        return (40, 49)
    if db_over <= 5:
        return (0, 5)
    if db_over <= 15:
        return (6, 15)
    return (16, 19)


def _phase_global_avg_pct(conn, innings, format_, tier, db_over, col) -> Optional[float]:
    """Sample-weighted average of `col` across every bucket in the same phase —
    the phase-level global base rate used as the smoothing anchor."""
    lo, hi = _phase_db_over_band(db_over, format_)
    rows = conn.execute(
        f"""SELECT {col} AS pct, sample_count
              FROM match_position_stats
             WHERE innings=? AND format=? AND match_tier=?
               AND over_number BETWEEN ? AND ?
               AND {col} IS NOT NULL AND sample_count >= 1""",
        (innings, format_, tier, lo, hi),
    ).fetchall()
    total_n = sum((r["sample_count"] or 0) for r in rows)
    if total_n <= 0:
        return None
    return sum((r["pct"] or 0) * (r["sample_count"] or 0) for r in rows) / total_n


def _blend_smoothed_base(exact_pct, nearby_pct, phase_pct) -> Optional[float]:
    """Hierarchical blend: 0.15*exact + 0.35*nearby + 0.50*phase-global.
    Missing components drop out and the remaining weights are renormalised."""
    parts = []
    if exact_pct is not None:
        parts.append((0.15, float(exact_pct)))
    if nearby_pct is not None:
        parts.append((0.35, float(nearby_pct)))
    if phase_pct is not None:
        parts.append((0.50, float(phase_pct)))
    if not parts:
        return None
    wsum = sum(w for w, _ in parts)
    return sum(w * v for w, v in parts) / wsum


def _finalize_smoothing(result, conn, innings, format_, tier, db_over, col,
                        exact_pct, nearby_pct):
    """Tag the reliability tier and, when the matched sample is < 10, replace the
    sparse value with a hierarchical blend toward the phase-level global average."""
    n = result.get("sample_count", 0) or 0
    if n >= config.MIN_POSITION_SAMPLES:
        result.setdefault("smoothing_tier", "reliable_n>=30")
        return result
    if n >= SMOOTHING_SAMPLE_FLOOR:
        result["smoothing_tier"] = "interpolated_low_sample(10-29)"
        return result
    # n < 10 → hierarchical smoothing
    phase_pct = _phase_global_avg_pct(conn, innings, format_, tier, db_over, col)
    blended = _blend_smoothed_base(exact_pct, nearby_pct, phase_pct)
    if blended is not None:
        result["pct"] = round(blended, 1)
        result["source"] = "hierarchical_smoothing"
        result["smoothing_tier"] = "hierarchical_smoothing(n<10)"
        result["smoothing_components"] = {
            "exact_bucket": round(exact_pct, 1) if exact_pct is not None else None,
            "nearby_bucket": round(nearby_pct, 1) if nearby_pct is not None else None,
            "phase_global": round(phase_pct, 1) if phase_pct is not None else None,
            "weights": "0.15 / 0.35 / 0.50",
        }
    else:
        result["smoothing_tier"] = "hierarchical_smoothing(no_anchor)"
    return result


def query_match_position_detail(
    innings: int,
    format_: str,
    over_number: int,
    wickets_fallen: int,
    current_runs: int,
    line: float,
    db_path: Optional[str] = None,
    batting_team: str = "",
    bowling_team: str = "",
    match_date: Optional[str] = None,
) -> dict:
    """
    Full match_position_stats lookup for debugging and UI display.
    Returns rows found, sample_count, pct, and which match tier was used.

    When sample_count < MIN_POSITION_SAMPLES, interpolates from nearest states
    (±1 wicket, ±10 runs, then combined) rather than trusting a sparse exact row.
    """
    run_bucket = round(current_runs / 10) * 10
    col = _get_exceeded_col(line)
    min_samples = config.MIN_POSITION_SAMPLES

    result = {
        "db_over": over_number,
        "run_bucket": run_bucket,
        "wickets_fallen": wickets_fallen,
        "line": line,
        "column": col,
        "exact_row": None,
        "fuzzy_rows": [],
        "sample_count": 0,
        "pct": None,
        "source": "none",
        "avg_final_score": None,
    }

    if col is None:
        result["source"] = "line_out_of_range"
        return result

    conn = get_connection(db_path)
    try:
        is_elite_match = (
            batting_team in ELITE_TEAMS and 
            bowling_team in ELITE_TEAMS and 
            (not match_date or match_date >= "2022-01-01")
        )
        target_tier = "elite" if is_elite_match else "all"

        row = conn.execute(
            f"""SELECT innings, format, over_number, wickets_fallen, current_runs,
                       sample_count, {col}, avg_final_score
                FROM match_position_stats
                WHERE innings=? AND format=? AND match_tier=? AND over_number=?
                  AND wickets_fallen=? AND current_runs=?""",
            (innings, format_, target_tier, over_number, wickets_fallen, run_bucket),
        ).fetchone()

        if row:
            result["exact_row"] = dict(row)
            if (row["sample_count"] or 0) >= min_samples:
                result["sample_count"] = row["sample_count"] or 0
                result["pct"] = row[col]
                result["avg_final_score"] = row["avg_final_score"]
                result["source"] = "exact"
                # Try interpolating avg_final_score between nearest 10-run buckets
                try:
                    lb = int(current_runs // 10) * 10
                    ub = lb + 10
                    if lb != ub:
                        low_row = conn.execute(
                            "SELECT avg_final_score FROM match_position_stats "
                            "WHERE innings=? AND format=? AND match_tier=? AND over_number=? "
                            "AND wickets_fallen=? AND current_runs=?",
                            (innings, format_, target_tier, over_number, wickets_fallen, lb),
                        ).fetchone()
                        high_row = conn.execute(
                            "SELECT avg_final_score FROM match_position_stats "
                            "WHERE innings=? AND format=? AND match_tier=? AND over_number=? "
                            "AND wickets_fallen=? AND current_runs=?",
                            (innings, format_, target_tier, over_number, wickets_fallen, ub),
                        ).fetchone()
                        if low_row and high_row and low_row[0] is not None and high_row[0] is not None:
                            # linear interpolation by distance between buckets
                            t = (current_runs - lb) / 10.0
                            interp = (1.0 - t) * float(low_row[0]) + t * float(high_row[0])
                            result["avg_final_score"] = round(interp, 2)
                            result["avg_final_score_source"] = "bucket_interpolated"
                except Exception:
                    pass
                result["smoothing_tier"] = "reliable_n>=30"
                return result

        def _weighted_from_rows(rows: list, source: str) -> Optional[dict]:
            if not rows:
                return None
            total_n = sum(r["sample_count"] or 0 for r in rows)
            if total_n <= 0:
                return None
            weighted = sum(
                (r["pct"] or 0) * (r["sample_count"] or 0) for r in rows
            ) / total_n
            return {
                "sample_count": total_n,
                "pct": weighted,
                "avg_final_score": rows[0].get("avg_final_score"),
                "source": source,
                "rows": rows,
            }

        candidates: list[dict] = []

        wicket_rows = conn.execute(
            f"""SELECT innings, format, over_number, wickets_fallen, current_runs,
                       sample_count, {col} AS pct, avg_final_score
                FROM match_position_stats
                WHERE innings=? AND format=? AND match_tier=? AND over_number=?
                  AND ABS(wickets_fallen - ?) <= 1
                  AND current_runs=?
                  AND sample_count >= 1""",
            (innings, format_, target_tier, over_number, wickets_fallen, run_bucket),
        ).fetchall()
        wicket_rows = [dict(r) for r in wicket_rows]
        w = _weighted_from_rows(wicket_rows, "wicket_interpolation")
        if w:
            w["fuzzy_rows"] = wicket_rows
            candidates.append(w)

        runs_rows = conn.execute(
            f"""SELECT innings, format, over_number, wickets_fallen, current_runs,
                       sample_count, {col} AS pct, avg_final_score
                FROM match_position_stats
                WHERE innings=? AND format=? AND match_tier=? AND over_number=?
                  AND wickets_fallen=?
                  AND ABS(current_runs - ?) <= 10
                  AND sample_count >= 1""",
            (innings, format_, target_tier, over_number, wickets_fallen, run_bucket),
        ).fetchall()
        runs_rows = [dict(r) for r in runs_rows]
        r = _weighted_from_rows(runs_rows, "runs_interpolation")
        if r:
            r["fuzzy_rows"] = runs_rows
            candidates.append(r)

        combined_rows = conn.execute(
            f"""SELECT innings, format, over_number, wickets_fallen, current_runs,
                       sample_count, {col} AS pct, avg_final_score
                FROM match_position_stats
                WHERE innings=? AND format=? AND match_tier=? AND over_number=?
                  AND ABS(wickets_fallen - ?) <= 1
                  AND ABS(current_runs - ?) <= 10
                  AND sample_count >= 1
                ORDER BY sample_count DESC""",
            (innings, format_, target_tier, over_number, wickets_fallen, run_bucket),
        ).fetchall()
        combined_rows = [dict(r) for r in combined_rows]
        result["fuzzy_rows"] = combined_rows
        c = _weighted_from_rows(combined_rows, "combined_interpolation")
        if c:
            candidates.append(c)

        if candidates:
            best = max(candidates, key=lambda x: x["sample_count"])
            result["sample_count"] = best["sample_count"]
            result["pct"] = best["pct"]
            result["avg_final_score"] = best.get("avg_final_score")
            if best["sample_count"] >= min_samples:
                result["source"] = best["source"]
            else:
                result["source"] = f"{best['source']}_low_sample"
            if best.get("fuzzy_rows"):
                result["fuzzy_rows"] = best["fuzzy_rows"]
            # Attempt bucket interpolation to refine avg_final_score where possible
            try:
                lb = int(current_runs // 10) * 10
                ub = lb + 10
                if lb != ub:
                    low_row = conn.execute(
                        "SELECT avg_final_score FROM match_position_stats "
                        "WHERE innings=? AND format=? AND match_tier=? AND over_number=? "
                        "AND wickets_fallen=? AND current_runs=?",
                        (innings, format_, target_tier, over_number, wickets_fallen, lb),
                    ).fetchone()
                    high_row = conn.execute(
                        "SELECT avg_final_score FROM match_position_stats "
                        "WHERE innings=? AND format=? AND match_tier=? AND over_number=? "
                        "AND wickets_fallen=? AND current_runs=?",
                        (innings, format_, target_tier, over_number, wickets_fallen, ub),
                    ).fetchone()
                    if low_row and high_row and low_row[0] is not None and high_row[0] is not None:
                        t = (current_runs - lb) / 10.0
                        interp = (1.0 - t) * float(low_row[0]) + t * float(high_row[0])
                        result["avg_final_score"] = round(interp, 2)
                        result["avg_final_score_source"] = "bucket_interpolated"
            except Exception:
                pass
            _finalize_smoothing(
                result, conn, innings, format_, target_tier, over_number, col,
                exact_pct=(row[col] if row else None), nearby_pct=best["pct"],
            )
            return result

        if row:
            result["sample_count"] = row["sample_count"] or 0
            result["pct"] = row[col]
            result["avg_final_score"] = row["avg_final_score"]
            result["source"] = "exact_low_sample"
            # Try interpolating avg_final_score between buckets if nearby data exists
            try:
                lb = int(current_runs // 10) * 10
                ub = lb + 10
                if lb != ub:
                    low_row = conn.execute(
                        "SELECT avg_final_score FROM match_position_stats "
                        "WHERE innings=? AND format=? AND match_tier=? AND over_number=? "
                        "AND wickets_fallen=? AND current_runs=?",
                        (innings, format_, target_tier, over_number, wickets_fallen, lb),
                    ).fetchone()
                    high_row = conn.execute(
                        "SELECT avg_final_score FROM match_position_stats "
                        "WHERE innings=? AND format=? AND match_tier=? AND over_number=? "
                        "AND wickets_fallen=? AND current_runs=?",
                        (innings, format_, target_tier, over_number, wickets_fallen, ub),
                    ).fetchone()
                    if low_row and high_row and low_row[0] is not None and high_row[0] is not None:
                        t = (current_runs - lb) / 10.0
                        interp = (1.0 - t) * float(low_row[0]) + t * float(high_row[0])
                        result["avg_final_score"] = round(interp, 2)
                        result["avg_final_score_source"] = "bucket_interpolated"
            except Exception:
                pass
            _finalize_smoothing(
                result, conn, innings, format_, target_tier, over_number, col,
                exact_pct=row[col], nearby_pct=None,
            )
            return result

        if target_tier == "elite" and (result.get("sample_count", 0) < min_samples):
            # RECURSIVE FALLBACK: If elite tier has sparse data, route back to the global 'all' baseline
            all_result = query_match_position_detail(
                innings, format_, over_number, wickets_fallen, current_runs,
                line, db_path, "", "", match_date
            )
            if all_result.get("pct") is not None:
                return all_result

        if format_ == "Women's T20I":
            mens = query_match_position_detail(
                innings, "T20I", over_number, wickets_fallen, current_runs, line, db_path
            )
            if mens.get("pct") is not None:
                mens["source"] = f"mens_fallback_{mens['source']}"
            return mens

        return result
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# SEGMENT (POWERPLAY) MARKET LOOKUP
# ─────────────────────────────────────────────────────────────────
# A T20 target score well below the full-innings range (pct_exceeded_100..220)
# is NOT a 20-over total — it is a segment scenario such as the 6-over powerplay.
# Such lines must be priced off the score accumulated AT the segment boundary,
# never off full-innings statistics.
SEGMENT_PP_MAX_LINE = 70.0          # T20 line under this ⇒ treat as powerplay
POWERPLAY_DB_OVER   = 6             # 0-indexed snapshot = state after 6 completed overs


def _pp_unconditional_stats(conn, innings, format_, tier, line) -> Optional[dict]:
    """Unconditional powerplay (6-over) distribution for a tier.

    Reads the rows snapshotted at the powerplay boundary (db over = 6) and returns
    the sample-weighted mean/std of the powerplay score, the % exceeding `line`, and
    the mean full-innings total (used to ground the powerplay scoring rate).
    """
    rows = conn.execute(
        """SELECT current_runs, sample_count, avg_final_score
             FROM match_position_stats
            WHERE innings=? AND format=? AND match_tier=? AND over_number=?""",
        (innings, format_, tier, POWERPLAY_DB_OVER),
    ).fetchall()
    total_n = sum((r["sample_count"] or 0) for r in rows)
    if total_n <= 0:
        return None
    mean_pp = sum((r["current_runs"] or 0) * (r["sample_count"] or 0) for r in rows) / total_n
    var_pp = sum(((r["current_runs"] or 0) - mean_pp) ** 2 * (r["sample_count"] or 0) for r in rows) / total_n
    over_n = sum((r["sample_count"] or 0) for r in rows if (r["current_runs"] or 0) > line)
    fin_rows = [r for r in rows if r["avg_final_score"] is not None]
    fin_n = sum((r["sample_count"] or 0) for r in fin_rows)
    mean_final = (
        sum((r["avg_final_score"] or 0) * (r["sample_count"] or 0) for r in fin_rows) / fin_n
        if fin_n else 0.0
    )
    return {
        "total_n": total_n,
        "mean_pp": mean_pp,
        "std_pp": var_pp ** 0.5,
        "pct": 100.0 * over_n / total_n,
        "mean_final": mean_final,
    }


def _pp_state_conditional(
    conn, innings, format_, tier, line,
    current_over, current_runs, wickets_fallen, max_overs, base,
) -> Optional[dict]:
    """State-conditional powerplay projection.

    Filters the historical snapshots at the CURRENT over to matches that were in a
    similar state, derives their conditional full-innings projection, then projects
    the already-scored runs forward to the 6-over mark at a powerplay-grounded
    scoring rate and computes P(powerplay total > line).

    Wicket matching is tiered to stop stronger 0-wicket states bleeding into 1/2-
    wicket projections: it matches the EXACT wickets_fallen first and only relaxes to
    wickets ±1 if the exact-wicket sample is < MIN_POSITION_SAMPLES. Returns None (→
    caller uses the unconditional base) if even the relaxed sample is too thin.
    """
    db_over_now = max(0, int(current_over) - 1)        # live over N → db key N-1

    def _match(wk_lo, wk_hi):
        return conn.execute(
            """SELECT current_runs, sample_count, avg_final_score
                 FROM match_position_stats
                WHERE innings=? AND format=? AND match_tier=? AND over_number=?
                  AND current_runs BETWEEN ? AND ?
                  AND wickets_fallen BETWEEN ? AND ?
                  AND sample_count >= 1 AND avg_final_score IS NOT NULL""",
            (innings, format_, tier, db_over_now,
             current_runs - 5, current_runs + 5, wk_lo, wk_hi),
        ).fetchall()

    # 1. STRICT: exact wickets_fallen only.
    rows = _match(wickets_fallen, wickets_fallen)
    total_n = sum((r["sample_count"] or 0) for r in rows)
    wicket_match = "exact"

    # 2. RELAXED: widen to wickets ±1 only if the strict sample is too thin.
    if total_n < config.MIN_POSITION_SAMPLES:
        rows = _match(max(0, wickets_fallen - 1), wickets_fallen + 1)
        total_n = sum((r["sample_count"] or 0) for r in rows)
        wicket_match = "±1"

    if total_n < config.MIN_POSITION_SAMPLES:
        return None

    # Conditional full-innings projection from the matched early states.
    cond_final = sum((r["avg_final_score"] or 0) * (r["sample_count"] or 0) for r in rows) / total_n

    # Ground the forward powerplay rate in the unconditional data: how much faster
    # (or slower) the powerplay scores vs the innings average (≈1.0 in practice).
    innings_rate = (base["mean_final"] / max_overs) if base.get("mean_final") else (base["mean_pp"] / 6.0)
    pp_rate = base["mean_pp"] / 6.0
    uplift = (pp_rate / innings_rate) if innings_rate > 0 else 1.0
    proj_rate = (cond_final / max_overs) * uplift

    remaining_pp = max(0.0, 6.0 - current_over)
    expected_pp6 = current_runs + remaining_pp * proj_rate

    sigma = base["std_pp"] if base["std_pp"] > 1.0 else 12.0
    z = (expected_pp6 - line) / sigma
    pct = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))) * 100.0
    pct = max(1.0, min(99.0, pct))

    return {
        "sample_count": total_n,
        "pct": round(pct, 1),
        "avg_segment_score": round(expected_pp6, 1),
        "source": f"powerplay_state_conditional_{tier}",
        "conditional": True,
        "wicket_match": wicket_match,
        "conditional_state": (
            f"{current_runs}/{wickets_fallen} @ {current_over:.1f} ov "
            f"(wkt-match={wicket_match}, n={total_n})"
        ),
    }


def query_powerplay_segment_detail(
    innings: int,
    format_: str,
    line: float,
    db_path: Optional[str] = None,
    batting_team: str = "",
    bowling_team: str = "",
    current_over: float = 0.0,
    current_runs: int = 0,
    wickets_fallen: int = 0,
) -> dict:
    """Historical % of innings whose powerplay (6-over) score exceeded `line`.

    Powerplay segment markets (e.g. line 44.5) are priced off the runs accumulated by
    the end of the powerplay — NOT the full-innings pct_exceeded columns.

    When a live state is supplied (0 < current_over <= 5), the estimate is made
    STATE-CONDITIONAL: it filters the historical snapshots to matches that were in a
    similar position (score ±5, wickets ±1) at the same over and projects forward to
    the 6-over mark. If that matched sample is < MIN_POSITION_SAMPLES it falls back to
    the unconditional 6-over base rate so the engine never prices off a thin sample.
    """
    result = {
        "segment": "powerplay",
        "segment_over": 6,
        "line": line,
        "sample_count": 0,
        "pct": None,
        "avg_segment_score": None,
        "source": "none",
        "conditional": False,
    }

    conn = get_connection(db_path)
    try:
        max_overs = config.MAX_OVERS.get(format_, 20)
        is_elite = batting_team in ELITE_TEAMS and bowling_team in ELITE_TEAMS
        tiers = (["elite"] if is_elite else []) + ["all"]
        state_conditional = 0.0 < current_over <= 5.0

        for tier in tiers:
            base = _pp_unconditional_stats(conn, innings, format_, tier, line)
            if not base or base["total_n"] < config.MIN_POSITION_SAMPLES:
                continue

            # Default: unconditional 6-over base rate for this tier.
            chosen = {
                "sample_count": base["total_n"],
                "pct": round(base["pct"], 1),
                "avg_segment_score": round(base["mean_pp"], 1),
                "source": f"powerplay_segment_{tier}",
                "conditional": False,
            }

            # Refine with the state-conditional projection when we have a live state
            # and enough matched samples; otherwise keep the unconditional base.
            if state_conditional:
                cond = _pp_state_conditional(
                    conn, innings, format_, tier, line,
                    current_over, current_runs, wickets_fallen, max_overs, base,
                )
                if cond:
                    chosen = cond

            result.update(chosen)
            return result

        # Women's T20I with no usable rows → fall back to the men's pool
        if format_ == "Women's T20I":
            mens = query_powerplay_segment_detail(
                innings, "T20I", line, db_path, batting_team, bowling_team,
                current_over, current_runs, wickets_fallen,
            )
            if mens.get("pct") is not None:
                mens["source"] = f"mens_fallback_{mens['source']}"
                return mens
        return result
    finally:
        conn.close()


def _get_exceeded_col(line: float) -> Optional[str]:
    """Map a target score to the nearest pct_exceeded_NNN column."""
    thresholds = [100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200, 210, 220]
    nearest = min(thresholds, key=lambda t: abs(t - line))
    if abs(nearest - line) > 15:
        return None  # too far from any precomputed threshold
    return f"pct_exceeded_{nearest}"


# ─────────────────────────────────────────────────────────────────
# VENUE STATS QUERIES
# ─────────────────────────────────────────────────────────────────

def _normalize_venue_rpo_array(arr: list) -> list:
    """
    Legacy ingest stored mean runs per BALL per over (< 3.5).
    Correct per-over totals are typically 4+ RPO. Scale legacy rows × 6.
    """
    if not arr:
        return arr
    sample = [x for x in arr[:10] if x]
    if sample and max(sample) < 3.5:
        return [round(x * 6, 2) for x in arr]
    return arr


def _venue_fuzzy_match(conn, venue: str, format_: str):
    """Resolve a venue name that doesn't match exactly. The same ground is stored
    inconsistently across formats (e.g. "Lord's, London" vs "Lord's"), so we match
    on a normalised ground name (the part before the first comma), case-insensitive,
    then fall back to a prefix match — always preferring the row with most matches.
    """
    if not venue:
        return None

    def _norm(s):
        return (s or "").strip().lower()

    def _ground(s):
        return _norm((s or "").split(",")[0])

    q_norm, q_ground = _norm(venue), _ground(venue)
    rows = conn.execute(
        "SELECT * FROM venue_stats WHERE format=?", (format_,)
    ).fetchall()

    # 1. case-insensitive full-name match
    full = [r for r in rows if _norm(r["venue"]) == q_norm]
    if full:
        return max(full, key=lambda r: r["sample_matches"] or 0)
    # 2. ground-name match (part before the comma)
    ground = [r for r in rows if _ground(r["venue"]) == q_ground]
    if ground:
        return max(ground, key=lambda r: r["sample_matches"] or 0)
    # 3. prefix match either direction (handles "Lord's" vs "Lord's, London")
    pref = [
        r for r in rows
        if _norm(r["venue"]).startswith(q_ground) or q_norm.startswith(_ground(r["venue"]))
    ]
    if pref:
        return max(pref, key=lambda r: r["sample_matches"] or 0)
    return None


def get_venue_stats(venue: str, format_: str, db_path: Optional[str] = None) -> Optional[dict]:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM venue_stats WHERE venue=? AND format=?",
            (venue, format_)
        ).fetchone()
        if not row:
            row = _venue_fuzzy_match(conn, venue, format_)
        if not row:
            return None
        d = dict(row)
        if d.get("avg_runs_per_over"):
            d["avg_runs_per_over"] = _normalize_venue_rpo_array(
                json.loads(d["avg_runs_per_over"])
            )
        return d
    finally:
        conn.close()


def venue_sample_count(venue_stats: Optional[dict]) -> int:
    """Number of innings used to build venue_stats (0 if missing)."""
    if not venue_stats:
        return 0
    return int(venue_stats.get("sample_matches") or 0)


def venue_has_reliable_rpo(venue_stats: Optional[dict]) -> bool:
    """True when venue has enough matches to trust per-over RPO curves."""
    return venue_sample_count(venue_stats) >= config.MIN_VENUE_SAMPLES_RPO


def venue_has_reliable_modifiers(venue_stats: Optional[dict]) -> bool:
    """True when venue has enough matches for slowdown / par modifiers."""
    return venue_sample_count(venue_stats) >= config.MIN_VENUE_SAMPLES_MODIFIER


def get_par_rr_for_over(venue: str, format_: str, over_number: int,
                         db_path: Optional[str] = None) -> Optional[float]:
    """Return historical avg runs in this specific over at this venue."""
    stats = get_venue_stats(venue, format_, db_path)
    if stats and stats.get("avg_runs_per_over"):
        arr = stats["avg_runs_per_over"]
        if 0 <= over_number < len(arr):
            return arr[over_number]
    return None


def get_venues(format_: Optional[str] = None, db_path: Optional[str] = None) -> list[str]:
    """Return all venues that have stats in the DB, sorted alphabetically.
    If format_ is given, only return venues that have data for that format."""
    conn = get_connection(db_path)
    try:
        if format_:
            rows = conn.execute(
                "SELECT DISTINCT venue FROM venue_stats WHERE format=? ORDER BY venue",
                (format_,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT venue FROM venue_stats ORDER BY venue"
            ).fetchall()
        return [r["venue"] for r in rows if r["venue"]]
    finally:
        conn.close()



def get_batters(format_: Optional[str] = None, db_path: Optional[str] = None) -> list[str]:
    """Return all batter names from batter_stats, sorted alphabetically."""
    conn = get_connection(db_path)
    try:
        if format_:
            rows = conn.execute(
                "SELECT DISTINCT player_name FROM batter_stats WHERE format=? ORDER BY player_name",
                (format_,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT player_name FROM batter_stats ORDER BY player_name"
            ).fetchall()
        return [r["player_name"] for r in rows if r["player_name"]]
    finally:
        conn.close()


def get_bowlers(format_: Optional[str] = None, db_path: Optional[str] = None) -> list[str]:
    """Return all bowler names from bowler_stats, sorted alphabetically."""
    conn = get_connection(db_path)
    try:
        if format_:
            rows = conn.execute(
                "SELECT DISTINCT player_name FROM bowler_stats WHERE format=? ORDER BY player_name",
                (format_,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT player_name FROM bowler_stats ORDER BY player_name"
            ).fetchall()
        return [r["player_name"] for r in rows if r["player_name"]]
    finally:
        conn.close()


def get_teams(format_: Optional[str] = None, db_path: Optional[str] = None) -> list[str]:
    """Return all team names from team_batting_stats for a given format, sorted alphabetically.

    For T20 Blast this returns actual county names as ingested from Cricsheet
    (e.g. 'Surrey', 'Yorkshire', 'Glamorgan') — always in sync with the DB.
    """
    conn = get_connection(db_path)
    try:
        if format_:
            rows = conn.execute(
                "SELECT DISTINCT team FROM team_batting_stats WHERE format=? ORDER BY team",
                (format_,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT team FROM team_batting_stats ORDER BY team"
            ).fetchall()
        return [r["team"] for r in rows if r["team"]]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# TEAM STATS QUERIES
# ─────────────────────────────────────────────────────────────────

def _team_name_variants(team: str) -> list[str]:
    """Cricsheet uses 'Pakistan'; UI often uses 'Pakistan Women'."""
    variants = [team]
    if team.endswith(" Women"):
        base = team[:-6].strip()
        if base and base not in variants:
            variants.append(base)
    else:
        w = f"{team} Women"
        if w not in variants:
            variants.append(w)
    return variants


def get_team_batting_stats(team: str, format_: str, opposition: Optional[str] = None,
                            db_path: Optional[str] = None) -> Optional[dict]:
    """
    Fetch batting stats, choosing the MOST REPRESENTATIVE row for pre-match projection.

    Multiple rows exist per team/format when:
     - ingest ran with different window sizes (last-N vs all-time)
     - opposition-specific rows (NULL = all-opponents aggregate)

    Strategy: among quality rows (n >= _MIN_SAMPLES, avg > 0, avg <= _AVG_CAP),
    prefer the one with the MOST SAMPLES — this is the broadest, most stable
    all-opponents view without being swamped by extreme outlier subsets.
    The _AVG_CAP prevents picking opposition-specific rows inflated by matches
    against very weak teams (e.g. PAK Women vs Associate nations avg=167).

    Historical note: the old ORDER BY sample_matches DESC was correct in principle
    but incorrect in practice because the largest-n row was often the all-time
    average including pre-2018 low-scoring era data (India Women n=167, avg=112).
    The cap ensures we skip all-time rows only when a better recent subset exists.
    """
    _MIN_SAMPLES  = 3
    _AVG_CAP: dict[str, float] = {   # realistic max for all-opponents rows
        "Women's T20I": 165.0,
        "T20I":         195.0,
        "Men's ODI":    340.0,
        "Women's ODI":  260.0,
    }
    cap = _AVG_CAP.get(format_, 999.0)

    conn = get_connection(db_path)
    try:
        first_sparse: Optional[dict] = None
        for team_name in _team_name_variants(team):
            if opposition:
                row = conn.execute(
                    "SELECT * FROM team_batting_stats WHERE team=? AND format=? AND opposition=?",
                    (team_name, format_, opposition)
                ).fetchone()
                if row:
                    return dict(row)
            # Fetch ALL opposition=NULL rows ordered by sample count desc.
            # Prefer the row with highest sample count that is within realistic range
            # (avoids opposition-specific outlier rows inflated by weak-team matches).
            rows = conn.execute(
                "SELECT * FROM team_batting_stats "
                "WHERE team=? AND format=? AND opposition IS NULL "
                "ORDER BY sample_matches DESC",
                (team_name, format_)
            ).fetchall()
            if not rows:
                continue
            # First pass: highest avg_total within the realistic cap.
            # Sorting by avg DESC ensures we pick the more-recent form row
            # (India Women n=43, avg=157.5) over the stale all-time row
            # (India Women n=167, avg=112.3), while the cap filters out
            # outlier opponent-specific rows (e.g. n=1, avg=204 manual scorecard).
            rows_sorted = sorted(
                [dict(r) for r in rows],
                key=lambda d: (d.get("avg_total") or 0.0),
                reverse=True
            )
            for d in rows_sorted:
                n     = d.get("sample_matches") or 0
                total = d.get("avg_total") or 0.0
                if n >= _MIN_SAMPLES and 0 < total <= cap:
                    return d
            # Second pass: any quality row regardless of cap
            for d in rows_sorted:
                n     = d.get("sample_matches") or 0
                total = d.get("avg_total") or 0.0
                if n >= _MIN_SAMPLES and total > 0:
                    return d
            # No quality row — save sparse fallback
            if first_sparse is None and rows:
                first_sparse = dict(rows[0])
        return first_sparse
    finally:
        conn.close()





def get_team_bowling_stats(team: str, format_: str, db_path: Optional[str] = None) -> Optional[dict]:
    """
    Fetch bowling stats for a team, preferring COMPLETE records (non-NULL mid + death economy)
    over partial records (PP-only rows created from limited data such as manual scorecards).

    Cricsheet stores team names without the 'Women' suffix (e.g. 'West Indies' not
    'West Indies Women'). The 'X Women' rows that exist are from manual scorecard ingestion
    and typically only have powerplay economy, leaving mid/death as NULL.
    The variant loop tries 'West Indies Women' first — if that record is PP-only,
    we fall through to 'West Indies' which has the complete phase breakdown.
    """
    conn = get_connection(db_path)
    try:
        first_partial: Optional[dict] = None  # best fallback if no complete record found
        for team_name in _team_name_variants(team):
            row = conn.execute(
                "SELECT * FROM team_bowling_stats WHERE team=? AND format=?",
                (team_name, format_)
            ).fetchone()
            if not row:
                continue
            d = dict(row)
            if d.get("death_bowlers"):
                d["death_bowlers"] = json.loads(d["death_bowlers"])
            # Prefer records that have at least mid OR death economy populated.
            # PP-only rows (mid=NULL and death=NULL) are incomplete fallbacks from
            # manual scorecard ingestion with insufficient ball-by-ball data.
            if d.get("avg_mid_economy") is not None or d.get("avg_death_economy") is not None:
                return d  # complete enough — use it
            if first_partial is None:
                first_partial = d  # save as last-resort fallback
        return first_partial  # PP-only or None if nothing found
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# BATTER / BOWLER STATS QUERIES
# ─────────────────────────────────────────────────────────────────

def _player_name_variants(name: str) -> list[str]:
    """
    Generate name-form candidates for fuzzy matching across Cricsheet/UI conventions.

    Cricsheet stores names in "First-initials Lastname" form, e.g. "SFM Devine" for
    Sophie Frances Moloney Devine.  The UI and scraper often pass shorter forms like
    "S Devine" or "Sophie Devine".  This helper generates several plausible variants
    so the DB query can try them in order.

    Strategy (in priority order):
      1. Exact original name
      2. Collapse multiple initials to single first initial  ("SFM Devine" → "S Devine")
      3. Expand single initial to first-initial of a full first name ("Sophie" → "S")
      4. Lastname, First (UI variant)
    """
    name = name.strip()
    if not name:
        return []

    variants: list[str] = [name]
    parts = name.split()
    if len(parts) < 2:
        return variants

    # Case 1: "SFM Devine" → "S Devine"  (collapse multiple initials to first only)
    first_part = parts[0]
    lastname   = " ".join(parts[1:])
    if len(first_part) > 1 and all(c.isupper() for c in first_part):
        # first_part is a sequence of uppercase initials
        single_initial = first_part[0] + " " + lastname
        if single_initial not in variants:
            variants.append(single_initial)

    # Case 2: "Sophie Devine" → "S Devine"  (first word is a full name, not initials)
    if not all(c.isupper() for c in first_part) and len(first_part) > 1:
        abbreviated = first_part[0].upper() + " " + lastname
        if abbreviated not in variants:
            variants.append(abbreviated)

    # Case 3: "S Devine" → try any name whose first initial matches  (done at query level)
    # We add a LIKE pattern form that callers can use as a fallback: e.g. "S% Devine"
    if len(first_part) == 1 and first_part.isupper():
        like_variant = first_part + "% " + lastname
        variants.append("__LIKE__" + like_variant)

    return variants


def get_batter_stats(player_name: str, format_: str, db_path: Optional[str] = None) -> Optional[dict]:
    conn = get_connection(db_path)
    try:
        for variant in _player_name_variants(player_name):
            if variant.startswith("__LIKE__"):
                # LIKE fallback — first-initial prefix match e.g. "S% Devine"
                pattern = variant[len("__LIKE__"):]
                row = conn.execute(
                    "SELECT * FROM batter_stats WHERE player_name LIKE ? AND format=? LIMIT 1",
                    (pattern, format_)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM batter_stats WHERE player_name=? AND format=?",
                    (variant, format_)
                ).fetchone()
            if row:
                d = dict(row)
                if d.get("sr_by_venue"):
                    d["sr_by_venue"] = json.loads(d["sr_by_venue"])
                return d
        return None
    finally:
        conn.close()


def get_bowler_stats(player_name: str, format_: str, db_path: Optional[str] = None) -> Optional[dict]:
    conn = get_connection(db_path)
    try:
        for variant in _player_name_variants(player_name):
            if variant.startswith("__LIKE__"):
                pattern = variant[len("__LIKE__"):]
                row = conn.execute(
                    "SELECT * FROM bowler_stats WHERE player_name LIKE ? AND format=? LIMIT 1",
                    (pattern, format_)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM bowler_stats WHERE player_name=? AND format=?",
                    (variant, format_)
                ).fetchone()
            if row:
                d = dict(row)
                if d.get("economy_by_venue"):
                    d["economy_by_venue"] = json.loads(d["economy_by_venue"])
                return d
        return None
    finally:
        conn.close()


def classify_death_bowler_role(
    bstats: Optional[dict],
    elite_threshold: float,
) -> str:
    """
    Classify a bowler's death-over role using DB stats only.

    Returns 'elite_death_bowler', 'primary_bowler', 'part_timer', or 'unknown'.
    """
    if not bstats:
        return "unknown"
    return bstats.get("bowler_role", "unknown")


def get_head_to_head(batter: str, bowler: str, format_: str, min_balls: int = 5,
                      db_path: Optional[str] = None) -> Optional[dict]:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """SELECT * FROM head_to_head
               WHERE batter=? AND bowler=? AND format=? AND balls_faced >= ?""",
            (batter, bowler, format_, min_balls)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# PREDICTIONS CRUD
# ─────────────────────────────────────────────────────────────────

def save_prediction(pred: dict, db_path: Optional[str] = None) -> int:
    """Insert a prediction record. Returns the new row id."""
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO predictions
               (timestamp, match, venue, format, innings, over_at_prediction,
                current_score_at_prediction, line, predicted_probability, verdict,
                base_probability, modifiers_fired, each_adjustment, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pred.get("timestamp"),
                pred.get("match"),
                pred.get("venue"),
                pred.get("format"),
                pred.get("innings"),
                pred.get("over_at_prediction"),
                pred.get("current_score_at_prediction"),
                pred.get("line"),
                pred.get("predicted_probability"),
                pred.get("verdict"),
                pred.get("base_probability"),
                pred.get("modifiers_fired"),
                json.dumps(pred.get("each_adjustment", {})),
                pred.get("notes", ""),
            )
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_prediction_result(pred_id: int, actual_result: str, notes: str = "",
                              db_path: Optional[str] = None) -> None:
    """Record the actual outcome (ABOVE / BELOW target) after the match.
    Legacy 'OVER'/'UNDER' tokens are still accepted for backward compatibility."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT verdict FROM predictions WHERE id=?", (pred_id,)
        ).fetchone()
        was_correct = None
        if row:
            verdict = (row["verdict"] or "").upper()
            # Forecasts are directional: "... ABOVE TARGET" predicts ABOVE,
            # "... BELOW TARGET" predicts BELOW. "TOO CLOSE TO CALL" / "LOW
            # CONFIDENCE" make no directional call and stay ungraded
            # (was_correct = None). Legacy stored verdicts ("OVER"/"UNDER"/
            # "AVOID") are still recognised for backward compatibility.
            predicts_over  = "ABOVE" in verdict or "OVER" in verdict
            predicts_under = "BELOW" in verdict or "UNDER" in verdict or "AVOID" in verdict
            actual = (actual_result or "").upper()
            actual_above = actual in ("ABOVE", "OVER")   # new + legacy outcome tokens
            actual_below = actual in ("BELOW", "UNDER")
            if predicts_over == predicts_under:
                was_correct = None        # no directional call (or ambiguous)
            elif actual_above:
                was_correct = 1 if predicts_over else 0
            elif actual_below:
                was_correct = 1 if predicts_under else 0

        conn.execute(
            """UPDATE predictions
               SET actual_result=?, was_correct=?, notes=COALESCE(NULLIF(?,''), notes)
               WHERE id=?""",
            (actual_result, was_correct, notes, pred_id)
        )
        conn.commit()
    finally:
        conn.close()


def clear_all_predictions(db_path: Optional[str] = None) -> int:
    """Delete every saved prediction and reset the id sequence. Returns rows removed.
    Use to start a fresh testing session — does not touch stats/position tables."""
    conn = get_connection(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.execute("DELETE FROM predictions")
        # Reset AUTOINCREMENT so new predictions start at id 1 again.
        try:
            conn.execute("DELETE FROM sqlite_sequence WHERE name='predictions'")
        except Exception:
            pass
        conn.commit()
        return n
    finally:
        conn.close()


def get_all_predictions(db_path: Optional[str] = None) -> list[dict]:
    """Return all predictions ordered by timestamp desc."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM predictions ORDER BY timestamp DESC"
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("each_adjustment"):
                try:
                    d["each_adjustment"] = json.loads(d["each_adjustment"])
                except Exception:
                    pass
            result.append(d)
        return result
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# MANUAL SCORECARD STORAGE
# ─────────────────────────────────────────────────────────────────

def save_manual_scorecard(match_id: str, raw_text: str, parsed: dict,
                           meta: dict, db_path: Optional[str] = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO manual_scorecards
               (match_id, match_date, team1, team2, venue, format, raw_text, parsed_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                match_id,
                meta.get("date"),
                meta.get("team1"),
                meta.get("team2"),
                meta.get("venue"),
                meta.get("format", config.MANUAL_SCORECARD_FORMAT_DEFAULT),
                raw_text,
                json.dumps(parsed),
            )
        )
        conn.commit()
    finally:
        conn.close()


def _parse_scorecard_rows(rows) -> list[dict]:
    result = []
    for row in rows:
        d = dict(row)
        if d.get("parsed_json"):
            try:
                d["parsed_json"] = json.loads(d["parsed_json"])
            except Exception:
                pass
        result.append(d)
    return result


def get_unprocessed_scorecards(db_path: Optional[str] = None) -> list[dict]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM manual_scorecards WHERE ingested=0 ORDER BY created_at DESC"
        ).fetchall()
        return _parse_scorecard_rows(rows)
    finally:
        conn.close()


def get_all_manual_scorecards(db_path: Optional[str] = None) -> list[dict]:
    """All saved manual scorecards, newest first."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM manual_scorecards ORDER BY created_at DESC"
        ).fetchall()
        return _parse_scorecard_rows(rows)
    finally:
        conn.close()


def mark_scorecard_ingested(match_id: str, db_path: Optional[str] = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE manual_scorecards SET ingested=1 WHERE match_id=?", (match_id,)
        )
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# ANALYTICS QUERIES
# ─────────────────────────────────────────────────────────────────

def get_accuracy_by_confidence_band(db_path: Optional[str] = None) -> list[dict]:
    """Accuracy breakdown by confidence band for analytics tab."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT
                 CASE
                   WHEN predicted_probability >= 70 THEN '>70%'
                   WHEN predicted_probability >= 60 THEN '60-70%'
                   WHEN predicted_probability >= 52 THEN '52-60%'
                   ELSE '<52%'
                 END as band,
                 COUNT(*) as total,
                 SUM(CASE WHEN was_correct=1 THEN 1 ELSE 0 END) as correct,
                 SUM(CASE WHEN was_correct IS NOT NULL THEN 1 ELSE 0 END) as graded
               FROM predictions
               WHERE actual_result IS NOT NULL
               GROUP BY band""",
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_calibration_data(db_path: Optional[str] = None) -> list[dict]:
    """
    Returns calibration chart data: predicted prob bucket vs actual win rate.
    Buckets are 5% wide (50-55, 55-60, …).
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT
                 CAST(ROUND((predicted_probability - 2.5) / 5) * 5 AS INTEGER) as bucket,
                 COUNT(*) as total,
                 AVG(CASE WHEN was_correct=1 THEN 100.0 ELSE 0.0 END) as actual_pct
               FROM predictions
               WHERE was_correct IS NOT NULL
               GROUP BY bucket
               HAVING total >= 3
               ORDER BY bucket""",
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_modifier_accuracy(db_path: Optional[str] = None) -> list[dict]:
    """Per-modifier accuracy: when modifier fired, what % were we correct?"""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT modifiers_fired, was_correct FROM predictions WHERE was_correct IS NOT NULL"
        ).fetchall()

        modifier_data: dict[str, dict] = {}
        for row in rows:
            fired = row["modifiers_fired"] or ""
            for mod in fired.split(","):
                mod = mod.strip()
                if not mod:
                    continue
                if mod not in modifier_data:
                    modifier_data[mod] = {"total": 0, "correct": 0}
                modifier_data[mod]["total"] += 1
                if row["was_correct"] == 1:
                    modifier_data[mod]["correct"] += 1

        return [
            {
                "modifier": k,
                "total": v["total"],
                "correct": v["correct"],
                "accuracy_pct": round(v["correct"] / v["total"] * 100, 1) if v["total"] else 0,
            }
            for k, v in modifier_data.items()
        ]
    finally:
        conn.close()


if __name__ == "__main__":
    # Quick test: initialize DB and print status
    init_db()
    status = get_db_status()
    print("DB Status:")
    for k, v in status.items():
        print(f"  {k}: {v}")
