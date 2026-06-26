"""
data/ingest.py — CricEdge Cricsheet Data Ingest

Downloads Cricsheet JSON zip files, parses ball-by-ball data, and
populates all stats tables in SQLite.

Usage:
    python -m data.ingest                          # All formats
    python -m data.ingest --format "Women's T20I"  # One format
    python -m data.ingest --quick                  # Last 2 seasons only
    python -m data.ingest --file path/to/zip       # Use local zip

IMPORTANT: Men's T20I data is ingested ONLY into match_position_stats
           and venue_stats. Player/team stats are strictly Women's only.
"""

import argparse
import json
import logging
import os
import sys
import zipfile
import io
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, date
from typing import Optional

import requests
from tqdm import tqdm

# Allow running as module or script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.database import (
    get_connection, init_db, set_meta, get_meta,
    mark_scorecard_ingested, get_unprocessed_scorecards,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────────────────────────

def download_zip(url: str, label: str) -> Optional[bytes]:
    """Download a Cricsheet zip file. Returns raw bytes or None on failure."""
    logger.info("Downloading %s from %s", label, url)
    try:
        headers = {"User-Agent": config.SCRAPER_USER_AGENT}
        resp = requests.get(url, headers=headers, timeout=120, stream=True)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        buf = io.BytesIO()
        with tqdm(total=total, unit="B", unit_scale=True, desc=label) as pbar:
            for chunk in resp.iter_content(chunk_size=65536):
                buf.write(chunk)
                pbar.update(len(chunk))
        logger.info("Downloaded %s (%.1f MB)", label, buf.tell() / 1_048_576)
        return buf.getvalue()
    except Exception as e:
        logger.error("Failed to download %s: %s", label, e)
        return None


def load_json_files_from_zip(zip_bytes: bytes) -> list[dict]:
    """Extract all .json match files from a zip archive."""
    matches = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        json_names = [n for n in zf.namelist() if n.endswith(".json")]
        logger.info("Found %d match files in zip", len(json_names))
        for name in tqdm(json_names, desc="Extracting", unit="match"):
            try:
                with zf.open(name) as f:
                    data = json.load(f)
                    matches.append(data)
            except Exception as e:
                logger.warning("Skipping %s: %s", name, e)
    return matches


# ─────────────────────────────────────────────────────────────────
# QUICK MODE FILTER
# ─────────────────────────────────────────────────────────────────

def filter_recent_seasons(matches: list[dict], n_seasons: int = 2) -> list[dict]:
    """Keep only matches from the last n_seasons calendar years."""
    current_year = datetime.now().year
    cutoff_year  = current_year - n_seasons
    filtered = []
    for m in matches:
        try:
            match_date = m.get("info", {}).get("dates", [None])[0]
            if match_date:
                year = int(str(match_date)[:4])
                if year >= cutoff_year:
                    filtered.append(m)
        except Exception:
            filtered.append(m)  # keep if we can't parse date
    logger.info(
        "Quick mode: kept %d / %d matches (from %d onwards)",
        len(filtered), len(matches), cutoff_year
    )
    return filtered


# ─────────────────────────────────────────────────────────────────
# CORE PARSER — Cricsheet JSON → Python dicts
# ─────────────────────────────────────────────────────────────────

class InningsData:
    """Parsed representation of one innings."""
    def __init__(self):
        self.batting_team: str = ""
        self.bowling_team: str = ""
        self.innings_num: int  = 1
        self.balls: list[dict] = []        # enriched ball records
        self.final_score: int  = 0
        self.wickets: int      = 0


def parse_match(match_data: dict) -> Optional[dict]:
    """
    Parse a Cricsheet JSON match into a structured dict.
    Returns None if the match data is malformed.
    """
    try:
        info      = match_data.get("info", {})
        innings_list = match_data.get("innings", [])
        if not info or not innings_list:
            return None

        teams   = info.get("teams", [])
        venue   = info.get("venue", "Unknown")
        dates   = info.get("dates", [])
        match_date = dates[0] if dates else None
        gender  = info.get("gender", "male")
        toss    = info.get("toss", {})
        outcome = info.get("outcome", {})

        parsed_innings = []
        for i, inn in enumerate(innings_list):
            team_key = "team" if "team" in inn else list(inn.keys())[0]
            batting_team = inn.get("team", "")
            overs_data   = inn.get("overs", [])

            idata = InningsData()
            idata.batting_team  = batting_team
            idata.bowling_team  = [t for t in teams if t != batting_team][0] if len(teams) == 2 else ""
            idata.innings_num   = i + 1

            cumulative_runs     = 0
            cumulative_wickets  = 0
            ball_number         = 0

            for over_data in overs_data:
                over_num = over_data.get("over", 0)
                deliveries = over_data.get("deliveries", [])
                for delivery in deliveries:
                    runs_obj  = delivery.get("runs", {})
                    batter_r  = runs_obj.get("batter", 0)
                    extras_r  = runs_obj.get("extras", 0)
                    total_r   = runs_obj.get("total", 0)
                    wickets   = delivery.get("wickets", [])
                    batter    = delivery.get("batter", "")
                    bowler    = delivery.get("bowler", "")
                    non_str   = delivery.get("non_striker", "")

                    is_wicket     = len(wickets) > 0
                    is_boundary   = batter_r in (4, 6)
                    is_six        = batter_r == 6
                    is_dot_ball   = (batter_r == 0 and extras_r == 0)
                    is_legal      = "wides" not in delivery.get("extras", {}) and \
                                    "noballs" not in delivery.get("extras", {})

                    cumulative_runs    += total_r
                    if is_wicket:
                        cumulative_wickets += len(wickets)

                    ball_number += 1

                    idata.balls.append({
                        "over":               over_num,
                        "ball_number":        ball_number,
                        "batter":             batter,
                        "bowler":             bowler,
                        "batter_runs":        batter_r,
                        "total_runs":         total_r,
                        "extras":             extras_r,
                        "is_wicket":          is_wicket,
                        "is_boundary":        is_boundary,
                        "is_six":             is_six,
                        "is_dot_ball":        is_dot_ball,
                        "is_legal":           is_legal,
                        "wicket_kinds":       [w.get("kind","") for w in wickets],
                        "cumulative_runs":    cumulative_runs,
                        "cumulative_wickets": cumulative_wickets,
                        "phase": _get_phase(over_num),
                    })

            idata.final_score  = cumulative_runs
            idata.wickets      = cumulative_wickets
            parsed_innings.append(idata)

        return {
            "venue":    venue,
            "date":     str(match_date) if match_date else None,
            "teams":    teams,
            "gender":   gender,
            "toss":     toss,
            "outcome":  outcome,
            "innings":  parsed_innings,
        }
    except Exception as e:
        logger.debug("parse_match error: %s", e)
        return None


def _get_phase(over: int, format_: str = "") -> str:
    """Return phase for a Cricsheet 0-indexed over number."""
    from model.probability import get_phase
    return get_phase(over + 1, format_)


# ─────────────────────────────────────────────────────────────────
# STATS ACCUMULATORS
# ─────────────────────────────────────────────────────────────────

class StatsAccumulator:
    """
    Accumulates raw match data in memory before bulk-writing to DB.
    Designed so Women's and Men's stats are completely separated.
    """

    def __init__(self, format_: str):
        self.format_    = format_
        self.is_womens  = (format_ == "Women's T20I")
        self.is_odi     = format_ in config.ODI_FORMATS
        self.is_t20_blast = (format_ == "T20 Blast")
        # For ODI formats and T20 Blast, we DO build player/team stats
        self.track_players = self.is_womens or self.is_odi or self.is_t20_blast

        # venue → {innings, runs_per_over: list[list], finals, etc.}
        max_overs = 50 if self.is_odi else 20
        self.venues: dict[str, dict] = defaultdict(lambda: {
            "finals_batting_first": [],
            "finals_chasing":       [],
            "runs_per_over": [[] for _ in range(max_overs)],
            "death_runs":    [],
            "death_balls":   0,
        })

        # batter_stats: player → {phase → SR data}
        self.batters: dict[str, dict] = defaultdict(lambda: {
            "pp_runs": 0, "pp_balls": 0, "pp_weight": 0,
            "mid_runs": 0, "mid_balls": 0, "mid_weight": 0,
            "death_runs": 0, "death_balls": 0, "death_weight": 0,
            "vs_spin_death_runs": 0, "vs_spin_death_balls": 0, "vs_spin_death_weight": 0,
            "vs_pace_death_runs": 0, "vs_pace_death_balls": 0, "vs_pace_death_weight": 0,
            "venue_stats": defaultdict(lambda: {"runs": 0, "balls": 0, "weight": 0}),
            "match_scores": [],         # (total_runs, total_balls, team_total) per match
        })

        # bowler_stats
        self.bowlers: dict[str, dict] = defaultdict(lambda: {
            "pp_runs": 0, "pp_balls": 0, "pp_weight": 0,
            "mid_runs": 0, "mid_balls": 0, "mid_weight": 0,
            "death_runs": 0, "death_balls": 0, "death_weight": 0,
            "venue_stats": defaultdict(lambda: {"runs": 0, "balls": 0, "weight": 0}),
            "match_economies": [],      # last 10 match economies
        })

        # h2h: (batter, bowler) → {balls, runs, dismissals, ...}
        self.h2h: dict[tuple, dict] = defaultdict(lambda: {
            "balls": 0, "runs": 0, "dismissals": 0, "weight": 0,
            "boundaries": 0,
            "spin_death_balls": 0, "spin_death_runs": 0, "spin_death_weight": 0,
            "pace_death_balls": 0, "pace_death_runs": 0, "pace_death_weight": 0,
        })

        # team batting/bowling
        self.team_batting: dict[str, dict]  = defaultdict(lambda: {
            "totals": [], "total_weights": [],
            "pp_scores": [], "pp_weights": [],
            "death_scores": [], "death_weights": [],
            "after_wicket_runs": [], "after_wicket_balls": [], "after_wicket_weights": [],
            "high_score_matches": [], "high_score_weights": [],   # matches where 190+ at over 18
        })
        self.team_bowling: dict[str, dict]  = defaultdict(lambda: {
            "pp_runs": 0, "pp_balls": 0, "pp_weight": 0,
            "mid_runs": 0, "mid_balls": 0, "mid_weight": 0,
            "death_runs": 0, "death_balls": 0, "death_weight": 0,
            "spinners_in_death": [],    # bowlers used in death
        })

        # match_position: (over, wickets_bucket, runs_bucket) → list of final scores
        self.positions: dict[tuple, list] = defaultdict(list)

        self.match_count = 0


    def process_match(self, parsed: dict) -> None:
        """Feed one parsed match into accumulators."""
        self.match_count += 1
        venue = parsed["venue"]
        match_date = parsed.get("date")
        weight = _recency_weight(match_date)

        for inn_idx, idata in enumerate(parsed["innings"]):
            innings_num = idata.innings_num
            final       = idata.final_score
            balls       = idata.balls

            if not balls:
                continue

            # ── venue stats ──
            v = self.venues[venue]
            if innings_num == 1:
                v["finals_batting_first"].append(final)
            else:
                v["finals_chasing"].append(final)

            over_totals: dict[int, int] = defaultdict(int)
            for ball in balls:
                # Re-compute phase using correct format (ODI vs T20)
                ball["phase"] = _get_phase(ball["over"], self.format_)
                over = ball["over"]
                max_overs = 50 if self.is_odi else 20
                death_start = 40 if self.is_odi else 16
                over_totals[over] += ball["total_runs"]
                if over >= death_start:
                    v["death_runs"].append(ball["total_runs"])
                    v["death_balls"] += 1
            for over, total in over_totals.items():
                if 0 <= over < max_overs and over < len(v["runs_per_over"]):
                    v["runs_per_over"][over].append(total)

            # ── match position states (snapshots every 6 balls) ──
            # Snapshot at start of each over
            state_at_over: dict[int, dict] = {}
            for ball in balls:
                ov = ball["over"]
                # Snapshot the cumulative state at the START of each over (just
                # before its first delivery). The old `ball_number % 6 == 1`
                # test misaligned with over boundaries as soon as any extra
                # (wide/no-ball) shifted the ball count — recording mid-over
                # state or dropping the over's snapshot entirely.
                if ov not in state_at_over:
                    state_at_over[ov] = {
                        "runs":    ball["cumulative_runs"] - ball["total_runs"],
                        "wickets": ball["cumulative_wickets"] - len(ball.get("wicket_kinds", [])),
                    }

            is_elite = False
            teams = idata.batting_team, idata.bowling_team
            match_date = parsed.get("date")
            if match_date and match_date >= "2022-01-01":
                from data.database import ELITE_TEAMS
                if teams[0] in ELITE_TEAMS and teams[1] in ELITE_TEAMS:
                    is_elite = True

            for over_num, state in state_at_over.items():
                runs_bucket    = round(state["runs"] / 10) * 10
                wickets_bucket = min(state["wickets"], 9)
                
                # Always accumulate to global 'all' tier
                key_all = (innings_num, over_num, wickets_bucket, runs_bucket, "all")
                self.positions[key_all].append(final)
                
                # Conditionally accumulate to 'elite' tier
                if is_elite:
                    key_elite = (innings_num, over_num, wickets_bucket, runs_bucket, "elite")
                    self.positions[key_elite].append(final)

            # ── batter/bowler/h2h stats (player-stat formats only) ──
            if not self.track_players:
                continue

            spinner_names: set[str] = set()
            per_bowler_delivery: dict[str, list] = defaultdict(list)
            per_batter_delivery: dict[str, list] = defaultdict(list)

            for ball in balls:
                batter = ball["batter"]
                bowler = ball["bowler"]
                phase  = ball["phase"]
                over   = ball["over"]
                b_runs = ball["batter_runs"]
                is_legal = ball["is_legal"]

                # Categorize bowler type (naive: spinners not easily identifiable
                # from Cricsheet without a separate lookup; use heuristic)
                is_spin = _is_spinner_heuristic(bowler)
                if is_spin and over >= 16:
                    spinner_names.add(bowler)

                per_bowler_delivery[bowler].append(ball)
                per_batter_delivery[batter].append(ball)

                # Batter accumulation
                if is_legal:
                    b = self.batters[batter]
                    if phase == "powerplay":
                        b["pp_runs"] += b_runs * weight; b["pp_balls"] += weight; b["pp_weight"] += weight
                    elif phase == "middle":
                        b["mid_runs"] += b_runs * weight; b["mid_balls"] += weight; b["mid_weight"] += weight
                    elif phase == "death":
                        b["death_runs"] += b_runs * weight; b["death_balls"] += weight; b["death_weight"] += weight
                        if is_spin:
                            b["vs_spin_death_runs"] += b_runs * weight; b["vs_spin_death_balls"] += weight; b["vs_spin_death_weight"] += weight
                        else:
                            b["vs_pace_death_runs"] += b_runs * weight; b["vs_pace_death_balls"] += weight; b["vs_pace_death_weight"] += weight
                    b["venue_stats"][venue]["runs"]  += b_runs * weight
                    b["venue_stats"][venue]["balls"] += weight
                    b["venue_stats"][venue]["weight"] += weight

                    # Bowler accumulation
                    bw = self.bowlers[bowler]
                    total_runs_given = ball["total_runs"]
                    if phase == "powerplay":
                        bw["pp_runs"] += total_runs_given * weight; bw["pp_balls"] += weight; bw["pp_weight"] += weight
                    elif phase == "middle":
                        bw["mid_runs"] += total_runs_given * weight; bw["mid_balls"] += weight; bw["mid_weight"] += weight
                    elif phase == "death":
                        bw["death_runs"] += total_runs_given * weight; bw["death_balls"] += weight; bw["death_weight"] += weight
                    bw["venue_stats"][venue]["runs"]  += total_runs_given * weight
                    bw["venue_stats"][venue]["balls"] += weight
                    bw["venue_stats"][venue]["weight"] += weight

                    # H2H
                    h = self.h2h[(batter, bowler)]
                    h["balls"] += weight; h["weight"] += weight
                    h["runs"]  += b_runs * weight
                    if ball["is_wicket"]:
                        h["dismissals"] += weight
                    if ball["is_boundary"]:
                        h["boundaries"] += weight
                    if over >= 16:
                        if is_spin:
                            h["spin_death_balls"] += weight; h["spin_death_runs"] += b_runs * weight; h["spin_death_weight"] += weight
                        else:
                            h["pace_death_balls"] += weight; h["pace_death_runs"] += b_runs * weight; h["pace_death_weight"] += weight

            # Team batting
            batting_team  = idata.batting_team
            bowling_team  = idata.bowling_team
            tb = self.team_batting[batting_team]
            tb["totals"].append(final)
            tb["total_weights"].append(weight)

            pp_score = sum(b["total_runs"] for b in balls if b["over"] < 6)
            death_score = sum(b["total_runs"] for b in balls if b["over"] >= 16)
            tb["pp_scores"].append(pp_score)
            tb["pp_weights"].append(weight)
            tb["death_scores"].append(death_score)
            tb["death_weights"].append(weight)

            # After-wicket runs (transition penalty)
            wicket_overs = [b["over"] for b in balls if b["is_wicket"]]
            for w_over in wicket_overs:
                post_balls = [b for b in balls if b["over"] == w_over or b["over"] == w_over + 1]
                if post_balls:
                    pr = sum(b["total_runs"] for b in post_balls[:6])
                    tb["after_wicket_runs"].append(pr)
                    tb["after_wicket_weights"].append(weight)

            # High score (psychological ceiling)
            at_over_18 = sum(b["total_runs"] for b in balls if b["over"] < 18)
            if at_over_18 >= 190:
                overs_18_20 = [b for b in balls if b["over"] >= 18]
                if overs_18_20:
                    late_runs = sum(b["total_runs"] for b in overs_18_20)
                    late_balls = len([b for b in overs_18_20 if b["is_legal"]])
                    sr = (late_runs / late_balls * 100) if late_balls > 0 else 0
                    tb["high_score_matches"].append(sr)
                    tb["high_score_weights"].append(weight)

            # Team bowling
            tbw = self.team_bowling[bowling_team]
            for ball in balls:
                if not ball["is_legal"]:
                    continue
                phase = ball["phase"]
                r = ball["total_runs"]
                if phase == "powerplay":
                    tbw["pp_runs"] += r * weight; tbw["pp_balls"] += weight; tbw["pp_weight"] += weight
                elif phase == "middle":
                    tbw["mid_runs"] += r * weight; tbw["mid_balls"] += weight; tbw["mid_weight"] += weight
                elif phase == "death":
                    tbw["death_runs"] += r * weight; tbw["death_balls"] += weight; tbw["death_weight"] += weight

            for sname in spinner_names:
                tbw["spinners_in_death"].append(sname)


def _recency_weight(match_date: Optional[str]) -> float:
    """
    Calculate recency weight for a match based on how old it is.
    - Within last 12 months: weight 1.0
    - 12-24 months ago: weight 0.6
    - 24+ months ago: weight 0.3
    """
    if not match_date:
        return 0.3  # Default to lowest weight if date unknown
    
    try:
        match_dt = datetime.strptime(match_date, "%Y-%m-%d")
        now = datetime.now()
        months_ago = (now.year - match_dt.year) * 12 + (now.month - match_dt.month)
        
        if months_ago <= 12:
            return 1.0
        elif months_ago <= 24:
            return 0.6
        else:
            return 0.3
    except (ValueError, TypeError):
        return 0.3  # Default to lowest weight if date parsing fails


def _is_spinner_heuristic(bowler_name: str) -> bool:
    """
    Very naive: flag as spinner if name appears in known spinner lists.
    In production, this should be a DB lookup. For now: always return
    False (we default to pace) unless we have a spinner list loaded.
    The head_to_head table stores spin vs pace stats from actuals.
    """
    # TODO: Replace with actual bowler type lookup from a reference file
    return False


def _eco(runs: int, balls: int) -> Optional[float]:
    if balls < 6:
        return None
    return round(runs / balls * 6, 2)


def _sr(runs: int, balls: int) -> Optional[float]:
    if balls < 6:
        return None
    return round(runs / balls * 100, 2)


def _weighted_avg(values: list, weights: list) -> Optional[float]:
    """Calculate weighted average."""
    if not values or not weights or len(values) != len(weights):
        return None
    total_weight = sum(weights)
    if total_weight <= 0:
        return None
    weighted_sum = sum(v * w for v, w in zip(values, weights))
    return round(weighted_sum / total_weight, 2)


# ─────────────────────────────────────────────────────────────────
# DB WRITE
# ─────────────────────────────────────────────────────────────────

def flush_to_db(acc: StatsAccumulator, conn: sqlite3.Connection) -> None:
    """Write all accumulated stats to SQLite."""
    now = datetime.now().isoformat()

    # ── venue_stats ──
    logger.info("Writing venue_stats (%d venues)...", len(acc.venues))
    for venue, v in acc.venues.items():
        avg_per_over = [
            round(sum(runs) / len(runs), 2) if runs else 0
            for runs in v["runs_per_over"]
        ]
        if acc.is_odi:
            # ODI: compare overs 1-25 (building) vs 41-50 (death) for slowdown
            early = avg_per_over[:25]
            death = avg_per_over[40:50] if len(avg_per_over) >= 50 else []
            avg_early = sum(early) / len(early) if early else 0
            avg_death = sum(death) / len(death) if death else 0
            slowdown = ((avg_early - avg_death) / avg_early) if avg_early > 0 else 0
        else:
            avg_1_10  = sum(avg_per_over[:10]) / min(10, len(avg_per_over)) if avg_per_over else 0
            avg_15_20 = sum(avg_per_over[15:]) / 5  if len(avg_per_over) >= 20 else 0
            slowdown  = ((avg_1_10 - avg_15_20) / avg_1_10) if avg_1_10 > 0 else 0
        death_eco = _eco(
            sum(v["death_runs"]),
            v["death_balls"]
        ) if v["death_balls"] else None

        conn.execute("""
            INSERT OR REPLACE INTO venue_stats
            (venue, format, avg_total_batting_first, avg_total_chasing,
             avg_runs_per_over, avg_death_economy, historical_slowdown_pct,
             avg_economy_overs_16_20, sample_matches, last_updated)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            venue, acc.format_,
            _avg(v["finals_batting_first"]),
            _avg(v["finals_chasing"]),
            json.dumps(avg_per_over),
            death_eco,
            slowdown,
            death_eco,
            len(v["finals_batting_first"]) + len(v["finals_chasing"]),
            now,
        ))

    # ── batter_stats (player-tracking formats only) ──
    if acc.track_players:
        logger.info("Writing batter_stats (%d batters)...", len(acc.batters))
        for player, b in acc.batters.items():
            venue_map = {
                vn: _sr(vs["runs"], vs["balls"])
                for vn, vs in b["venue_stats"].items()
                if vs["balls"] >= 10
            }
            conn.execute("""
                INSERT OR REPLACE INTO batter_stats
                (player_name, format, sr_powerplay, sr_middle, sr_death,
                 sr_vs_spin_death, sr_vs_pace_death, sr_by_venue,
                 total_balls_faced, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                player, acc.format_,
                _sr(b["pp_runs"], b["pp_balls"]) if b["pp_balls"] >= 6 else None,
                _sr(b["mid_runs"], b["mid_balls"]) if b["mid_balls"] >= 6 else None,
                _sr(b["death_runs"], b["death_balls"]) if b["death_balls"] >= 6 else None,
                _sr(b["vs_spin_death_runs"], b["vs_spin_death_balls"]) if b["vs_spin_death_balls"] >= 6 else None,
                _sr(b["vs_pace_death_runs"], b["vs_pace_death_balls"]) if b["vs_pace_death_balls"] >= 6 else None,
                json.dumps(venue_map),
                int(b["pp_balls"] + b["mid_balls"] + b["death_balls"]),
                now,
            ))

        # ── bowler_stats ──
        logger.info("Writing bowler_stats (%d bowlers)...", len(acc.bowlers))
        for player, bw in acc.bowlers.items():
            venue_map = {
                vn: _eco(vs["runs"], vs["balls"])
                for vn, vs in bw["venue_stats"].items()
                if vs["balls"] >= 6
            }
            death_eco = _eco(bw["death_runs"], bw["death_balls"]) if bw["death_balls"] >= 6 else None
            is_elite  = 1 if (death_eco is not None and death_eco < config.MODIFIER_PARAMS["bowling_quota_trap"]["elite_death_economy_threshold"]) else 0
            
            total_balls = int(bw["pp_balls"] + bw["mid_balls"] + bw["death_balls"])
            
            # User defined rule for bowler role
            role = "unclassified"
            if death_eco is not None and death_eco < 8.5 and bw["death_balls"] >= 60:  # 10+ death overs
                role = "elite_death_bowler"
            elif total_balls >= 300:  # 50+ overs total
                role = "primary_bowler"
            elif total_balls < 120:  # <20 overs total
                role = "part_timer"
            else:
                role = "primary_bowler" # Fallback per user instructions

            conn.execute("""
                INSERT OR REPLACE INTO bowler_stats
                (player_name, format, economy_powerplay, economy_middle, economy_death,
                 economy_by_venue, is_elite_death_bowler, bowler_role, total_balls_bowled, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                player, acc.format_,
                _eco(bw["pp_runs"], bw["pp_balls"]) if bw["pp_balls"] >= 6 else None,
                _eco(bw["mid_runs"], bw["mid_balls"]) if bw["mid_balls"] >= 6 else None,
                death_eco,
                json.dumps(venue_map),
                is_elite,
                role,
                total_balls,
                now,
            ))

        # ── head_to_head ──
        logger.info("Writing head_to_head (%d pairs)...", len(acc.h2h))
        for (batter, bowler), h in acc.h2h.items():
            if h["balls"] < 5:
                continue
            conn.execute("""
                INSERT OR REPLACE INTO head_to_head
                (batter, bowler, format, balls_faced, runs_scored, dismissals,
                 sr, boundary_pct, sr_spin_death, sr_pace_death, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                batter, bowler, acc.format_,
                int(h["balls"]), h["runs"], h["dismissals"],
                _sr(h["runs"], h["balls"]) if h["balls"] >= 6 else None,
                round(h["boundaries"] / h["balls"] * 100, 2) if h["balls"] > 0 else None,
                _sr(h["spin_death_runs"], h["spin_death_balls"]) if h["spin_death_balls"] >= 6 else None,
                _sr(h["pace_death_runs"], h["pace_death_balls"]) if h["pace_death_balls"] >= 6 else None,
                now,
            ))

        # ── team_batting_stats ──
        logger.info("Writing team_batting_stats (%d teams)...", len(acc.team_batting))
        for team, tb in acc.team_batting.items():
            # Use weighted averages for recency
            last10_vals = tb["totals"][-10:]
            last10_weights = tb["total_weights"][-10:]
            avg_total = _weighted_avg(last10_vals, last10_weights) if last10_vals else _avg(tb["totals"])
            
            pp_last10_vals = tb["pp_scores"][-10:]
            pp_last10_weights = tb["pp_weights"][-10:]
            avg_pp = _weighted_avg(pp_last10_vals, pp_last10_weights) if pp_last10_vals else _avg(tb["pp_scores"])
            
            death_last10_vals = tb["death_scores"][-10:]
            death_last10_weights = tb["death_weights"][-10:]
            avg_death = _weighted_avg(death_last10_vals, death_last10_weights) if death_last10_vals else _avg(tb["death_scores"])
            
            avg_after_wicket = _avg(tb["after_wicket_runs"])
            avg_high_score = _avg(tb["high_score_matches"])
            
            conn.execute("""
                INSERT OR REPLACE INTO team_batting_stats
                (team, format, avg_total, avg_pp_score, avg_death_score,
                 avg_runs_after_wicket, avg_sr_when_190plus_at_over18,
                 sample_matches, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                team, acc.format_,
                avg_total,
                avg_pp,
                avg_death,
                avg_after_wicket,
                avg_high_score,
                len(tb["totals"]),
                now,
            ))

        # ── team_bowling_stats ──
        logger.info("Writing team_bowling_stats (%d teams)...", len(acc.team_bowling))
        for team, tbw in acc.team_bowling.items():
            uses_spinner = 1 if len(set(tbw["spinners_in_death"])) > 0 else 0
            conn.execute("""
                INSERT OR REPLACE INTO team_bowling_stats
                (team, format, avg_pp_economy, avg_mid_economy, avg_death_economy,
                 uses_spinner_in_death, last_updated)
                VALUES (?,?,?,?,?,?,?)
            """, (
                team, acc.format_,
                _eco(tbw["pp_runs"], tbw["pp_balls"]) if tbw["pp_balls"] >= 6 else None,
                _eco(tbw["mid_runs"], tbw["mid_balls"]) if tbw["mid_balls"] >= 6 else None,
                _eco(tbw["death_runs"], tbw["death_balls"]) if tbw["death_balls"] >= 6 else None,
                uses_spinner,
                now,
            ))

    # ── match_position_stats ──
    logger.info("Writing match_position_stats (%d states)...", len(acc.positions))
    is_odi = acc.is_odi
    # ODI final scores range much higher — use ODI-appropriate thresholds
    if is_odi:
        thresholds = [150, 175, 200, 220, 240, 260, 280, 300, 320, 340, 360, 380, 400]
    else:
        thresholds = [100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200, 210, 220]

    batch = []
    for (innings, over_num, wickets, runs_bucket, match_tier), finals in acc.positions.items():
        if len(finals) < 3:
            continue
        pcts = {t: round(sum(1 for f in finals if f > t) / len(finals) * 100, 2) for t in thresholds}
        avg_f = round(sum(finals) / len(finals), 2)
        import statistics
        std_f = round(statistics.stdev(finals), 2) if len(finals) > 1 else 0.0
        batch.append((
            innings, acc.format_, match_tier, over_num, wickets, runs_bucket, len(finals),
            pcts[thresholds[0]],  pcts[thresholds[1]],  pcts[thresholds[2]],
            pcts[thresholds[3]],  pcts[thresholds[4]],  pcts[thresholds[5]],
            pcts[thresholds[6]],  pcts[thresholds[7]],  pcts[thresholds[8]],
            pcts[thresholds[9]],  pcts[thresholds[10]], pcts[thresholds[11]],
            pcts[thresholds[12]], avg_f, std_f, now,
        ))

    conn.executemany("""
        INSERT OR REPLACE INTO match_position_stats
        (innings, format, match_tier, over_number, wickets_fallen, current_runs, sample_count,
         pct_exceeded_100, pct_exceeded_110, pct_exceeded_120, pct_exceeded_130,
         pct_exceeded_140, pct_exceeded_150, pct_exceeded_160, pct_exceeded_170,
         pct_exceeded_180, pct_exceeded_190, pct_exceeded_200, pct_exceeded_210,
         pct_exceeded_220, avg_final_score, std_final_score, last_updated)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, batch)

    conn.commit()
    logger.info("Flush complete.")


def _avg(lst: list) -> Optional[float]:
    vals = [x for x in lst if x is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


# ─────────────────────────────────────────────────────────────────
# MANUAL SCORECARD INGEST
# ─────────────────────────────────────────────────────────────────

def ingest_manual_scorecards(db_path: Optional[str] = None) -> int:
    """
    Process any manually pasted scorecards that haven't been ingested yet.
    Parses the stored JSON and updates batter/bowler/venue stats.
    Returns number of scorecards processed.
    """
    scorecards = get_unprocessed_scorecards(db_path)
    if not scorecards:
        logger.info("No unprocessed manual scorecards found.")
        return 0

    conn = get_connection(db_path)
    acc  = StatsAccumulator("Women's T20I")

    processed = 0
    for sc in scorecards:
        try:
            parsed = sc.get("parsed_json", {})
            if not parsed:
                continue
            # Convert parsed scorecard format to match format
            match_data = _scorecard_to_match(parsed, sc)
            if match_data:
                acc.process_match(match_data)
                mark_scorecard_ingested(sc["match_id"], db_path)
                processed += 1
                logger.info("Ingested manual scorecard: %s", sc["match_id"])
        except Exception as e:
            logger.error("Failed to ingest scorecard %s: %s", sc.get("match_id"), e)

    if processed > 0:
        flush_to_db(acc, conn)
    conn.close()
    return processed


def _scorecard_to_match(parsed: dict, meta: dict) -> Optional[dict]:
    """
    Convert a parsed manual scorecard (from the UI parser) into
    the same format as parse_match() output so it feeds the same
    accumulator pipeline.
    """
    # The UI scorecard parser produces innings with ball-by-ball data.
    # If only summary data is available, create synthetic ball records.
    try:
        innings_list = parsed.get("innings", [])
        teams = [parsed.get("team1", ""), parsed.get("team2", "")]
        venue = parsed.get("venue", meta.get("venue", "Unknown"))
        date_str = parsed.get("date", meta.get("match_date"))

        synthetic_innings = []
        for i, inn in enumerate(innings_list):
            idata = InningsData()
            idata.batting_team = inn.get("batting_team", teams[i] if i < len(teams) else "")
            idata.bowling_team = inn.get("bowling_team", "")
            idata.innings_num  = i + 1
            idata.final_score  = inn.get("total_runs", 0)
            idata.wickets      = inn.get("wickets", 0)

            # Create synthetic balls from over-by-over summary if available
            idata.balls = _synthetic_balls(inn)
            synthetic_innings.append(idata)

        return {
            "venue":   venue,
            "date":    date_str,
            "teams":   teams,
            "gender":  "female",
            "toss":    {},
            "outcome": {},
            "innings": synthetic_innings,
        }
    except Exception as e:
        logger.debug("scorecard_to_match error: %s", e)
        return None


def _synthetic_balls(inn: dict) -> list[dict]:
    """Generate synthetic ball records from over summary data."""
    balls = []
    over_summaries = inn.get("overs", [])
    cum_runs = 0
    cum_wkts = 0
    ball_num = 0

    for over_data in over_summaries:
        over_num = over_data.get("over", 0)
        runs     = over_data.get("runs", 0)
        wickets  = over_data.get("wickets", 0)
        bowler   = over_data.get("bowler", "Unknown")

        # Spread runs across 6 balls (simplified — used only for team stats)
        for ball_in_over in range(6):
            ball_runs = runs // 6 + (1 if ball_in_over < runs % 6 else 0)
            is_wicket = (ball_in_over == 5 and wickets > 0)
            cum_runs += ball_runs
            if is_wicket:
                cum_wkts += 1
            ball_num += 1
            balls.append({
                "over":               over_num,
                "ball_number":        ball_num,
                "batter":             inn.get("batting_team", ""),
                "bowler":             bowler,
                "batter_runs":        ball_runs,
                "total_runs":         ball_runs,
                "extras":             0,
                "is_wicket":          is_wicket,
                "is_boundary":        ball_runs in (4, 6),
                "is_six":             ball_runs == 6,
                "is_dot_ball":        ball_runs == 0,
                "is_legal":           True,
                "wicket_kinds":       ["caught"] if is_wicket else [],
                "cumulative_runs":    cum_runs,
                "cumulative_wickets": cum_wkts,
                "phase":              _get_phase(over_num),
            })
    return balls


# ─────────────────────────────────────────────────────────────────
# MAIN INGEST PIPELINE
# ─────────────────────────────────────────────────────────────────

def ingest_format(
    format_: str,
    quick: bool = False,
    local_zip: Optional[str] = None,
    db_path: Optional[str] = None,
    progress_callback=None,
) -> int:
    """
    Download (or load local), parse, and store all matches for a format.
    Returns number of matches processed.
    """
    dl_config = config.CRICSHEET_DOWNLOADS.get(format_)
    if not dl_config and not local_zip:
        logger.error("No download config for format: %s", format_)
        return 0

    # Load zip
    if local_zip:
        logger.info("Using local zip: %s", local_zip)
        with open(local_zip, "rb") as f:
            zip_bytes = f.read()
    else:
        zip_bytes = download_zip(dl_config["url"], dl_config["label"])
        if not zip_bytes:
            return 0

    matches_raw = load_json_files_from_zip(zip_bytes)
    if quick:
        matches_raw = filter_recent_seasons(matches_raw, config.QUICK_INGEST_SEASONS)

    logger.info("Parsing %d matches for format: %s", len(matches_raw), format_)

    acc  = StatsAccumulator(format_)
    conn = get_connection(db_path)
    init_db(db_path)

    failed = 0
    for i, raw in enumerate(tqdm(matches_raw, desc=f"Parsing {format_}", unit="match")):
        parsed = parse_match(raw)
        if parsed:
            acc.process_match(parsed)
        else:
            failed += 1
        if progress_callback and i % 50 == 0:
            progress_callback(i / len(matches_raw))

    logger.info(
        "Parsed %d/%d matches (%d failed). Writing to DB...",
        len(matches_raw) - failed, len(matches_raw), failed
    )

    flush_to_db(acc, conn)
    conn.close()

    logger.info("Ingest complete: %d matches for %s", acc.match_count, format_)

    # Metadata
    meta_key_map = {
        "Women's T20I": "womens_matches_ingested",
        "T20I":         "mens_matches_ingested",
        "Men's ODI":    "mens_odi_matches_ingested",
        "Women's ODI":  "womens_odi_matches_ingested",
        "T20 Blast":    "t20_blast_matches_ingested",
    }
    if format_ in meta_key_map:
        set_meta(meta_key_map[format_], str(acc.match_count), db_path)

    return acc.match_count


# ─────────────────────────────────────────────────────────────────
# LEAK-FREE TRAINING BUILD (strict temporal split)
# ─────────────────────────────────────────────────────────────────

def _iter_zip_matches(zip_path: str):
    """Yield (match_id, raw_match_dict) preserving the Cricsheet filename stem
    as the match id — needed to record the training manifest."""
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".json"):
                continue
            match_id = os.path.splitext(os.path.basename(name))[0]
            try:
                with zf.open(name) as f:
                    yield match_id, json.load(f)
            except Exception as e:
                logger.warning("Skipping %s: %s", name, e)


def build_training_db(
    cutoff_date: str,
    db_path: str,
    format_zips: dict,
) -> dict:
    """Build a DERIVED-STATS database from scratch using ONLY matches strictly
    before `cutoff_date` (match_date < cutoff_date).

    This is the leak-free training step for out-of-sample validation:
      1. Create schema + DROP every derived table (clean slate).
      2. For each (format -> zip), parse every match, KEEP only those with a
         valid date < cutoff_date, accumulate stats, flush to DB.
      3. Record a manifest of exactly which match ids/dates were used.
      4. ABORT (raise) if any manifested match is dated on/after the cutoff.

    `format_zips` maps a format string (e.g. "Women's T20I") to a local zip path.
    Returns a stats dict and prints the required verification (count, first/last).
    """
    from data.database import (
        init_db, drop_derived_tables, save_training_manifest,
        get_training_date_bounds, get_connection,
    )

    logger.info("=== LEAK-FREE TRAINING BUILD (cutoff=%s) ===", cutoff_date)
    init_db(db_path)
    drop_derived_tables(db_path)
    init_db(db_path)  # recreate the dropped derived tables empty

    manifest: list[tuple] = []
    per_format_counts: dict[str, int] = {}
    skipped_post = skipped_undated = parse_failed = 0

    conn = get_connection(db_path)
    try:
        for format_, zip_path in format_zips.items():
            if not zip_path or not os.path.exists(zip_path):
                logger.warning("No zip for %s (%s) — skipping format", format_, zip_path)
                continue
            acc = StatsAccumulator(format_)
            kept = 0
            logger.info("Building %s from %s ...", format_, zip_path)
            for match_id, raw in tqdm(_iter_zip_matches(zip_path),
                                      desc=f"Train {format_}", unit="match"):
                parsed = parse_match(raw)
                if not parsed:
                    parse_failed += 1
                    continue
                date = parsed.get("date")
                if not date:
                    skipped_undated += 1
                    continue
                # STRICT split: anything on/after the cutoff is invisible to training.
                if str(date) >= cutoff_date:
                    skipped_post += 1
                    continue
                acc.process_match(parsed)
                manifest.append((match_id, str(date), format_))
                kept += 1
            flush_to_db(acc, conn)
            per_format_counts[format_] = kept
            logger.info("  %s: %d pre-split matches ingested", format_, kept)
    finally:
        conn.close()

    # ── Persist manifest + metadata ──────────────────────────────
    total_manifest = save_training_manifest(manifest, db_path)
    set_meta("split_date", cutoff_date, db_path)
    set_meta("train_match_count", str(len(manifest)), db_path)

    bounds = get_training_date_bounds(db_path)

    # ── HARD LEAKAGE GUARD ───────────────────────────────────────
    # Every manifested match MUST be strictly before the cutoff. If not, the
    # build is corrupt — abort rather than ship a leaky training DB.
    violations = [m for m in manifest if not (str(m[1]) < cutoff_date)]
    if violations:
        raise SystemExit(
            f"TEMPORAL LEAKAGE: {len(violations)} training match(es) dated on/after "
            f"{cutoff_date}, e.g. {violations[:3]}. Aborting."
        )
    if bounds["last_date"] and bounds["last_date"] >= cutoff_date:
        raise SystemExit(
            f"TEMPORAL LEAKAGE: manifest last_date {bounds['last_date']} >= "
            f"{cutoff_date}. Aborting."
        )

    # ── Required verification output ─────────────────────────────
    print("\n" + "=" * 64)
    print("  LEAK-FREE TRAINING BUILD — VERIFICATION")
    print("=" * 64)
    print(f"  Split date            : {cutoff_date}  (train = date < split)")
    print(f"  Training match count  : {bounds['count']}")
    print(f"  First training date   : {bounds['first_date']}")
    print(f"  Last training date    : {bounds['last_date']}")
    for fmt, n in per_format_counts.items():
        print(f"    - {fmt:<14}: {n}")
    print(f"  Skipped (>= split)    : {skipped_post}")
    print(f"  Skipped (no date)     : {skipped_undated}")
    print(f"  Parse failures        : {parse_failed}")
    print(f"  Manifest rows written : {total_manifest}")
    print(f"  Training DB           : {db_path}")
    print("  [OK] No record violates the temporal split.")
    print("=" * 64 + "\n")

    return {
        "db_path": db_path,
        "cutoff_date": cutoff_date,
        "train_count": bounds["count"],
        "first_date": bounds["first_date"],
        "last_date": bounds["last_date"],
        "per_format": per_format_counts,
        "skipped_post": skipped_post,
        "skipped_undated": skipped_undated,
    }


# ─────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CricEdge — Cricsheet data ingest"
    )
    parser.add_argument(
        "--format",
        choices=["Women's T20I", "T20I", "Men's ODI", "Women's ODI", "T20 Blast", "all"],
        default="all",
        help="Which format to ingest (default: all)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help=f"Only process last {config.QUICK_INGEST_SEASONS} seasons",
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="Path to a local Cricsheet zip file (skips download)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite DB (default from config.py)",
    )
    parser.add_argument(
        "--manual-only",
        action="store_true",
        help="Only process unprocessed manual scorecards, skip Cricsheet",
    )
    args = parser.parse_args()

    init_db(args.db)

    if args.manual_only:
        n = ingest_manual_scorecards(args.db)
        print(f"Processed {n} manual scorecards.")
        return

    formats_to_run = (
        ["Women's T20I", "T20I", "Men's ODI", "T20 Blast"] if args.format == "all"
        else [args.format]
    )

    total = 0
    for fmt in formats_to_run:
        n = ingest_format(fmt, quick=args.quick, local_zip=args.file, db_path=args.db)
        total += n
        print(f"  {fmt}: {n} matches processed")

    print(f"\nTotal matches processed: {total}")
    print(f"DB location: {args.db or config.DB_PATH}")


if __name__ == "__main__":
    main()
