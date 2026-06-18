"""
app.py — CricEdge Main Streamlit Application

Flow (simplified):
  1. Type match (or click Demo) → teams auto-fill
  2. Load live data → everything auto-populates
  3. Set line + market
  4. Click ANALYSE → result

Pitch/weather/toss are auto-inferred from venue history. Advanced settings
available in a collapsed expander for edge cases.
"""

import streamlit as st
import os, sys
import joblib
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import config
from model.market import (
    MARKET_TYPES, route_market, market_description,
    MICRO_MARKETS, FULL_INNINGS_TARGET, available_micro_markets, calc_micro_market,
)
from model.checkpoint_models import detect_checkpoint, apply_checkpoint_model
from data.database import init_db, get_db_status, get_all_predictions, get_venue_stats, get_venues, get_batters, get_bowlers
from ui.components import (
    inject_css, render_header, render_live_score, render_recent_overs,
    render_probability_card, render_history_tab, render_analytics_tab,
    render_settings_tab, render_scorecard_entry_tab, render_copy_summary,
)

st.set_page_config(
    page_title="CricEdge — Live Cricket Score Forecasting",
    page_icon="🏏",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_css()

@st.cache_resource
def _init_db():
    init_db()
    return True
_init_db()

@st.cache_data(ttl=300)
def _load_venues(format_: str) -> list[str]:
    """Load venues from DB for the current format, cached 5 min."""
    try:
        venues = get_venues(format_)
        if not venues:  # fallback: all formats
            venues = get_venues()
        return venues
    except Exception:
        return []


@st.cache_data(ttl=600)
def _load_batters(format_: str) -> list[str]:
    """Load batter names from DB, cached 10 min."""
    try:
        p = get_batters(format_)
        if not p:
            p = get_batters()
        return p
    except Exception:
        return []


@st.cache_data(ttl=600)
def _load_bowlers(format_: str) -> list[str]:
    """Load bowler names from DB, cached 10 min."""
    try:
        p = get_bowlers(format_)
        if not p:
            p = get_bowlers()
        return p
    except Exception:
        return []


@st.cache_resource
def _load_live_calibrator():
    """Load the saved IsotonicRegression calibrator once per Streamlit session.

    Returns None (and the app continues WITHOUT the calibration overlay) if the
    file is missing or it can't be unpickled — e.g. scikit-learn not installed or
    a version mismatch. The calibration overlay is an optional enhancement; a
    failure here must never crash the whole app.
    """
    model_path = Path(__file__).resolve().parent / "live_calibrator.pkl"
    if not model_path.exists():
        return None
    try:
        return joblib.load(model_path)
    except Exception:
        return None


def _format_trade_signal(raw_probability_pct: float, calibrator) -> tuple[float | None, str]:
    """Convert raw model probability to calibrated probability and build the signal text."""
    raw_prob = float(raw_probability_pct) / 100.0
    
    # 1. Forecast direction mapping
    if raw_prob >= 0.50:
        side = "ABOVE"
        model_side_prob = raw_prob
    else:
        side = "BELOW"
        model_side_prob = 1.0 - raw_prob

    calibrated_win_prob = float(calibrator.predict([model_side_prob])[0])

    # 2. Minimum confidence margin (4% above the 50% no-separation point)
    edge = calibrated_win_prob - 0.50
    if edge < 0.04:
        return None, "⚠️ LOW CONFIDENCE — no clear separation"

    return calibrated_win_prob, f"⚡ SIGNAL: {side} TARGET (high confidence) | calibrated_prob={calibrated_win_prob:.4f}"


# ─────────────────────────────────────────────────────────────────
# TEAM NAME EXPANSION  (abbreviation / short → full name)
# ─────────────────────────────────────────────────────────────────
TEAM_EXPAND = {
    # Short country codes
    "ind": "India Women", "aus": "Australia Women", "eng": "England Women",
    "nz":  "New Zealand Women", "sa":  "South Africa Women",
    "wi":  "West Indies Women", "pak": "Pakistan Women",
    "sl":  "Sri Lanka Women", "ban": "Bangladesh Women",
    "zim": "Zimbabwe Women", "ire": "Ireland Women",
    "ned": "Netherlands Women", "uae": "UAE Women",
    "png": "Papua New Guinea Women", "sco": "Scotland Women",
    "usa": "USA Women", "can": "Canada Women",
    
    # Short country codes with 'w' suffix (crex.live style)
    "indw": "India Women", "ausw": "Australia Women", "engw": "England Women",
    "nzw":  "New Zealand Women", "saw":  "South Africa Women",
    "wiw":  "West Indies Women", "pakw": "Pakistan Women",
    "slw":  "Sri Lanka Women", "banw": "Bangladesh Women",
    "zimw": "Zimbabwe Women", "irew": "Ireland Women",
    "nedw": "Netherlands Women", "scow": "Scotland Women",

    # Common variants
    "india": "India Women", "australia": "Australia Women",
    "england": "England Women", "new zealand": "New Zealand Women",
    "south africa": "South Africa Women", "west indies": "West Indies Women",
    "pakistan": "Pakistan Women", "sri lanka": "Sri Lanka Women",
    "bangladesh": "Bangladesh Women", "zimbabwe": "Zimbabwe Women",
    "ireland": "Ireland Women", "netherlands": "Netherlands Women",
    # Already have "Women" - pass through
}

KNOWN_TEAMS_WOMENS = sorted([
    "India Women", "Australia Women", "England Women", "New Zealand Women",
    "South Africa Women", "West Indies Women", "Pakistan Women",
    "Sri Lanka Women", "Bangladesh Women", "Zimbabwe Women",
    "Ireland Women", "Netherlands Women", "Scotland Women",
    "UAE Women", "USA Women", "Thailand Women", "Japan Women",
    "Papua New Guinea Women", "Canada Women", "Kenya Women",
])

KNOWN_TEAMS_MENS_ODI = sorted([
    "India", "Australia", "England", "New Zealand", "South Africa",
    "West Indies", "Pakistan", "Sri Lanka", "Bangladesh", "Zimbabwe",
    "Afghanistan", "Ireland", "Netherlands", "Scotland", "UAE",
    "USA", "Canada", "Nepal", "Oman", "Namibia",
    # A-teams / Lions
    "India A", "Australia A", "England Lions", "Pakistan A",
    "Sri Lanka A", "South Africa A", "New Zealand A", "West Indies A",
    "Bangladesh A", "Zimbabwe A", "IND-A", "SL-A", "AUS-A",
])

KNOWN_TEAMS = sorted(set(KNOWN_TEAMS_WOMENS + KNOWN_TEAMS_MENS_ODI))

def _expand_team(raw: str) -> str:
    """Expand abbreviation → full team name."""
    if not raw:
        return raw
    s = raw.strip()
    lower = s.lower()

    # A-team notation expands first (before KNOWN_TEAMS check)
    odi_a_map = {
        "ind-a": "India A", "sl-a": "Sri Lanka A", "aus-a": "Australia A",
        "pak-a": "Pakistan A", "eng-a": "England Lions", "sa-a": "South Africa A",
        "wi-a": "West Indies A", "nz-a": "New Zealand A", "ban-a": "Bangladesh A",
        "zim-a": "Zimbabwe A",
    }
    if lower in odi_a_map:
        return odi_a_map[lower]

    # Expansion dict (country codes → full Women's names)
    if lower in TEAM_EXPAND:
        return TEAM_EXPAND[lower]

    # Slug forms like "sl-women", "nz women", "slw-women" → strip the Women
    # marker and separators to recover the country code, then expand.
    core = lower.replace("women", "").replace("woman", "").replace("-", " ")
    core = "".join(core.split())
    if core and core in TEAM_EXPAND:
        return TEAM_EXPAND[core]

    # Already a recognised full name — return as-is
    if s in KNOWN_TEAMS:
        return s

    # Title-case variant in Men's ODI list
    title_s = s.title()
    if title_s in KNOWN_TEAMS_MENS_ODI or s in KNOWN_TEAMS_MENS_ODI:
        return s

    # Default: assume Women's (append "Women" if not present)
    if "women" not in lower and len(s) > 2:
        candidate = title_s + " Women"
        if candidate in KNOWN_TEAMS_WOMENS:
            return candidate
    return s.title()


def _auto_detect_format(match_title: str, teams: list) -> str:
    """
    Auto-detect format from match title or team names.
    Returns a format string from config.SUPPORTED_FORMATS.
    """
    title_l = (match_title or "").lower()
    if "odi" in title_l or "one day" in title_l:
        # check if any team has "women" in name
        has_women = any("women" in str(t).lower() for t in teams)
        return "Women's ODI" if has_women else "Men's ODI"
    if "t20" in title_l or "twenty20" in title_l or "t-20" in title_l:
        has_women = any("women" in str(t).lower() for t in teams)
        return "Women's T20I" if has_women else "T20I"
    # Fallback: detect from team names
    has_women = any("women" in str(t).lower() for t in teams)
    if has_women:
        return "Women's T20I"  # default Women’s format
    return "Men's ODI"  # default Men’s format if no Women tag


def _team_abbr_matches(abbr: str, full_name: str) -> bool:
    """True if a scoreboard abbreviation (e.g. 'SLW', 'AUSW') refers to full_name."""
    if not abbr or not full_name:
        return False
    a = abbr.upper().rstrip("W")                       # drop the trailing Women marker
    words = full_name.upper().replace("WOMEN", "").split()
    if not words:
        return False
    initials = "".join(w[0] for w in words)
    if a == initials:                                  # SLW→SL, NZW→NZ, WIW→WI, SAW→SA
        return True
    if words[0].startswith(a):                         # AUSW→AUS, INDW→IND, ENGW→ENG
        return True
    return False


def _batting_team_index(raw: dict, teams: list) -> int:
    """Which of the two teams is currently batting, inferred from scraped data.

    Prefers the score-line abbreviation (e.g. 'SLW 150 - 6'); falls back to the
    innings number (the side batting second is on strike in the 2nd innings).
    """
    abbr = (raw.get("batting_abbr") or "").strip()
    if abbr and len(teams) >= 2:
        matches = [i for i, t in enumerate(teams) if _team_abbr_matches(abbr, t)]
        if len(matches) == 1:
            return matches[0]
    return 1 if raw.get("innings") == 2 else 0


def _parse_match_string(s: str) -> tuple[str, str]:
    """Parse 'India Women vs Pakistan Women' → (bat_team, bowl_team)."""
    s = s.strip()
    for sep in [" vs ", " v ", " VS ", " V "]:
        if sep in s:
            parts = s.split(sep, 1)
            return _expand_team(parts[0].strip()), _expand_team(parts[1].strip())
    return _expand_team(s), ""


def _infer_conditions(venue: str, fmt: str) -> dict:
    """
    Auto-infer pitch and weather from venue history in DB.
    Returns dict with pitch_type, weather, avg_score.
    """
    try:
        vs = get_venue_stats(venue, fmt)
        if vs:
            avg = vs.get("avg_total_batting_first", 130)
            death_eco = vs.get("avg_death_economy", 7.5)
            # Infer pitch from avg score
            if avg >= 155:
                pitch = "flat"
            elif avg >= 140:
                pitch = "flat"
            elif avg >= 120:
                pitch = "spin" if death_eco < 7.0 else "flat"
            else:
                pitch = "seam"
            return {"pitch_type": pitch, "weather": "day", "avg_score": round(avg, 0)}
    except Exception:
        pass
    return {"pitch_type": "flat", "weather": "day", "avg_score": 130}



# ─────────────────────────────────────────────────────────────────
# HISTORICAL SCENARIOS (real match snapshots for manual testing)
# Each can be loaded via the preset dropdown to pre-fill all fields.
# ─────────────────────────────────────────────────────────────────
SCENARIOS = {
    "-- Select a scenario --": None,

    "IND W vs AUS W — T20 WC 2023 Final (AUS bat, 14.2 ov, 118/3)": {
        "batting_team": "Australia Women", "bowling_team": "India Women",
        "format": "Women's T20I", "innings": 1, "venue": "Newlands, Cape Town",
        "runs": 118, "wickets": 3, "overs": 14.2,
        "batsmen": [
            {"name": "B Mooney", "runs": 34, "balls": 30, "on_strike": True},
            {"name": "A Gardner", "runs": 28, "balls": 19, "on_strike": False},
        ],
        "all_bowlers": [
            {"name": "D Hazra",    "overs_today": 3.0, "runs_today": 22, "wickets": 1, "economy": 7.3, "overs_remaining": 1},
            {"name": "R Ghosh",    "overs_today": 3.0, "runs_today": 26, "wickets": 1, "economy": 8.6, "overs_remaining": 1},
            {"name": "D Sharma",   "overs_today": 2.2, "runs_today": 14, "wickets": 1, "economy": 6.0, "overs_remaining": 2},
            {"name": "S Pandey",   "overs_today": 2.0, "runs_today": 18, "wickets": 0, "economy": 9.0, "overs_remaining": 2},
        ],
        "recent_overs": [["4","1","0","6","0","2"],["1","4","W","0","1","4"],["6","0","1","4","0","4"]],
        "line": 158.0, "market_type": "Total Innings Score",
    },

    "NZ W vs SA W — T20 WC 2023 SF (NZ bat, 8.0 ov, 52/1)": {
        "batting_team": "New Zealand Women", "bowling_team": "South Africa Women",
        "format": "Women's T20I", "innings": 1, "venue": "Newlands, Cape Town",
        "runs": 52, "wickets": 1, "overs": 8.0,
        "batsmen": [
            {"name": "S Devine",   "runs": 31, "balls": 28, "on_strike": True},
            {"name": "L Down",     "runs": 18, "balls": 14, "on_strike": False},
        ],
        "all_bowlers": [
            {"name": "S Ismail",   "overs_today": 2.0, "runs_today": 12, "wickets": 1, "economy": 6.0, "overs_remaining": 2},
            {"name": "N de Klerk", "overs_today": 2.0, "runs_today": 18, "wickets": 0, "economy": 9.0, "overs_remaining": 2},
            {"name": "A Khaka",    "overs_today": 2.0, "runs_today": 14, "wickets": 0, "economy": 7.0, "overs_remaining": 2},
            {"name": "C Tryon",    "overs_today": 2.0, "runs_today": 8,  "wickets": 0, "economy": 4.0, "overs_remaining": 2},
        ],
        "recent_overs": [["0","4","1","0","6","0"],["1","2","0","4","W","0"],["1","6","0","0","4","1"]],
        "line": 140.0, "market_type": "Total Innings Score",
    },

    "IND W vs ENG W — 2022 CWG Final (ENG chasing, 12.3 ov, 83/4, T:132)": {
        "batting_team": "England Women", "bowling_team": "India Women",
        "format": "Women's T20I", "innings": 2, "venue": "Edgbaston, Birmingham",
        "runs": 83, "wickets": 4, "overs": 12.3,
        "batsmen": [
            {"name": "N Sciver-Brunt", "runs": 32, "balls": 27, "on_strike": True},
            {"name": "A Jones",        "runs": 8,  "balls": 7,  "on_strike": False},
        ],
        "all_bowlers": [
            {"name": "D Sharma",      "overs_today": 3.0, "runs_today": 21, "wickets": 2, "economy": 7.0, "overs_remaining": 1},
            {"name": "P Vastrakar",   "overs_today": 3.0, "runs_today": 20, "wickets": 1, "economy": 6.7, "overs_remaining": 1},
            {"name": "R Ghosh",       "overs_today": 3.0, "runs_today": 25, "wickets": 1, "economy": 8.3, "overs_remaining": 1},
            {"name": "S Yadav",       "overs_today": 2.3, "runs_today": 12, "wickets": 0, "economy": 4.8, "overs_remaining": 2},
        ],
        "recent_overs": [["0","W","1","4","0","0"],["6","0","1","1","0","4"],["0","1","W","0","4","1"]],
        "target": 132, "line": 132.0, "market_type": "Total Innings Score",
    },

    "AUS W vs IND W — Death overs test (AUS 157/4, 17.0 ov)": {
        "batting_team": "Australia Women", "bowling_team": "India Women",
        "format": "Women's T20I", "innings": 1, "venue": "DY Patil, Mumbai",
        "runs": 157, "wickets": 4, "overs": 17.0,
        "batsmen": [
            {"name": "A Healy", "runs": 60, "balls": 38, "on_strike": True},
            {"name": "E Perry", "runs": 41, "balls": 30, "on_strike": False},
        ],
        "all_bowlers": [
            {"name": "D Sharma",   "overs_today": 4.0, "runs_today": 34, "wickets": 2, "economy": 8.5, "overs_remaining": 0},
            {"name": "R Ghosh",    "overs_today": 3.0, "runs_today": 30, "wickets": 2, "economy": 10.0, "overs_remaining": 1},
            {"name": "S Yadav",    "overs_today": 4.0, "runs_today": 28, "wickets": 0, "economy": 7.0, "overs_remaining": 0},
            {"name": "R Yadav",    "overs_today": 3.0, "runs_today": 38, "wickets": 0, "economy": 12.7, "overs_remaining": 1},
            {"name": "P Vastrakar","overs_today": 3.0, "runs_today": 27, "wickets": 0, "economy": 9.0, "overs_remaining": 1},
        ],
        "recent_overs": [["6","4","1","0","6","4"],["4","0","W","6","1","4"],["6","4","0","4","1","6"]],
        "line": 185.0, "market_type": "Total Innings Score",
    },

    "PAK W vs WI W — Next 2 overs market (PAK 78/2, over 9.0)": {
        "batting_team": "Pakistan Women", "bowling_team": "West Indies Women",
        "format": "Women's T20I", "innings": 1, "venue": "Gaddafi Stadium, Lahore",
        "runs": 78, "wickets": 2, "overs": 9.0,
        "batsmen": [
            {"name": "Muneeba Ali", "runs": 45, "balls": 35, "on_strike": True},
            {"name": "Nida Dar",    "runs": 12, "balls": 10, "on_strike": False},
        ],
        "all_bowlers": [
            {"name": "H Matthews", "overs_today": 2.0, "runs_today": 18, "wickets": 1, "economy": 9.0, "overs_remaining": 2},
            {"name": "S Henry",    "overs_today": 2.0, "runs_today": 14, "wickets": 1, "economy": 7.0, "overs_remaining": 2},
            {"name": "A Mohammed", "overs_today": 2.0, "runs_today": 20, "wickets": 0, "economy": 10.0, "overs_remaining": 2},
            {"name": "D Dottin",   "overs_today": 3.0, "runs_today": 26, "wickets": 0, "economy": 8.7, "overs_remaining": 1},
        ],
        "recent_overs": [["1","4","0","6","0","1"],["0","4","1","0","4","0"],["6","1","0","0","4","1"]],
        "line": 14.0, "market_type": "Next 2 Overs Runs",
    },
}



# ─────────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────────
def _init_session():
    defaults = {
        "mode":          "live",   # "live" or "replay"
        "format":        config.PRIMARY_FORMAT,
        "batting_team":  "",
        "bowling_team":  "",
        "innings":       1,
        "target":        None,
        "toss_choice":     "bat",
        "partnership_runs":  0,
        "partnership_balls": 0,
        # Live data
        "live_data":     None,
        "live_fetched":  False,
        "show_manual":   False,
        # Market
        "market_type":     MARKET_TYPES[0],
        "line":            160.0,
        "custom_from_over": 16,
        "custom_to_over":   20,
        # Result
        "last_result":     None,
        # Live match list
        "live_matches_cache":      None,
        "live_matches_fetched_at": None,
        "selected_match_idx":      0,
        # Quick match input state
        "match_input":    "",
        # Conditions / meta
        "venue":          "",
        "pitch_type":     "flat",
        "weather":        "day",
        "dew_factor":     False,
        "toss_winner":    "",
        # Replay scorecard state
        "n_batters":      2,
        "n_bowlers":      0,
        "pre_match":      False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_session()


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────
from model.probability import get_phase as _get_phase

def _parse_ball_token(ball_str: str) -> dict:
    """Parse a single ball token from recent-overs input."""
    token = ball_str.strip().upper()
    if token == "W":
        return dict(batter_runs=0, total_runs=0, is_wicket=True, is_boundary=False,
                    is_dot_ball=False, is_legal=True)
    if token in ("WD", "WIDE"):
        return dict(batter_runs=0, total_runs=1, is_wicket=False, is_boundary=False,
                    is_dot_ball=False, is_legal=False)
    if token in ("NB", "NOBALL"):
        return dict(batter_runs=0, total_runs=1, is_wicket=False, is_boundary=False,
                    is_dot_ball=False, is_legal=False)
    try:
        runs = int(token)
        return dict(batter_runs=runs, total_runs=runs, is_wicket=False,
                    is_boundary=runs in (4, 6), is_dot_ball=runs == 0, is_legal=True)
    except ValueError:
        return dict(batter_runs=0, total_runs=0, is_wicket=False, is_boundary=False,
                    is_dot_ball=True, is_legal=True)


def _trim_recent_overs(recent_overs: list, current_over: float) -> list:
    """Keep only completed overs relevant to current over (max 3)."""
    if current_over < 1.0:
        return []
    n = min(3, int(current_over))
    valid = [o for o in recent_overs if o]
    return valid[-n:] if valid else []


def _reconstruct_balls(recent_overs: list, current_over: float = 0.0) -> list:
    """Rebuild ball list with correct over numbers for modifier windows."""
    recent_overs = _trim_recent_overs(recent_overs, current_over)
    balls, ball_num = [], 0
    current_over_int = max(1, int(current_over)) if current_over > 0 else 0
    num_recent = len(recent_overs)
    first_over_num = max(1, current_over_int - num_recent + 1) if num_recent else 1
    for i, over in enumerate(recent_overs):
        over_num = first_over_num + i
        for ball_str in over:
            parsed = _parse_ball_token(ball_str)
            ball_num += 1
            balls.append({
                "over": over_num, "ball_number": ball_num,
                "batter_runs": parsed["batter_runs"],
                "total_runs": parsed["total_runs"],
                "is_wicket": parsed["is_wicket"],
                "is_boundary": parsed["is_boundary"],
                "is_dot_ball": parsed["is_dot_ball"],
                "is_legal": parsed["is_legal"],
            })
    return balls


def _build_remaining_batters(batsmen: list[dict], wickets_fallen: int) -> list[dict]:
    """
    Estimate batting depth: crease batters plus yet-to-bat placeholders.
    Placeholders use default quality in calc_available_resources.
    """
    on_crease = list(batsmen[:2])
    yet_to_bat = max(0, 11 - wickets_fallen - len(on_crease))
    return on_crease + [{"name": ""} for _ in range(yet_to_bat)]


def _market_line_bounds(market_type: str, innings: int, target: Optional[int]) -> tuple[float, float, float, str]:
    """Return (min, max, default, label) for the target-score input — no arbitrary low caps."""
    if "2 Overs" in market_type:
        return 1.0, 80.0, 13.0, "Target score (2-over runs)"
    if "4 Overs" in market_type:
        return 1.0, 120.0, 26.0, "Target score (4-over runs)"
    if "Session" in market_type:
        return 1.0, 300.0, 50.0, "Target score (full session total)"
    if "Custom" in market_type:
        return 1.0, 250.0, 25.0, "Target score (window runs)"
    # Total innings
    lmax = 350.0
    ldef = 160.0
    if innings == 2 and target and target > 0:
        lmax = float(target) + 1.0
        ldef = min(max(20.0, float(target) - 15.0), lmax)
    return 10.0, lmax, ldef, "Target score (innings total)"


def _build_all_scorers(batsmen: list[dict], live: dict) -> list[dict]:
    """Merge crease batters with dismissed scorers for concentration modifier."""
    dismissed = live.get("dismissed_batsmen", [])
    by_name: dict[str, dict] = {}
    for b in dismissed + batsmen:
        name = b.get("name", "").strip()
        if not name:
            continue
        by_name[name] = b
    return list(by_name.values())


# ─────────────────────────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────────────────────────
db_status = get_db_status()
render_header(db_status)

tab_analyse, tab_history, tab_analytics, tab_scorecard, tab_settings = st.tabs([
    "🏏 Analyse", "📜 History", "📊 Analytics", "📋 Scorecard Entry", "⚙️ Settings",
])


# ═════════════════════════════════════════════════════════════════
# TAB 1 — ANALYSE
# ═════════════════════════════════════════════════════════════════
with tab_analyse:

    if st.session_state.get("scorecard_loaded_msg"):
        st.success(
            f"📋 Scorecard loaded: **{st.session_state['scorecard_loaded_msg']}** — "
            "Replay mode is active below. Set scenario & target, then hit ⚡ ANALYSE."
        )

    # ── Use full width via two main columns: left (inputs) right (result) ──
    left_col, right_col = st.columns([5, 4], gap="large")

    with left_col:

        # ═══════════════════════════════════════════════════════════
        # MODE SELECTOR — the two big buttons at top
        # ═══════════════════════════════════════════════════════════
        mode = st.session_state.get("mode", "live")

        m1, m2 = st.columns(2)
        with m1:
            live_active = mode == "live"
            if st.button(
                "🔴  LIVE MATCH",
                use_container_width=True,
                key="mode_live_btn",
                type="primary" if live_active else "secondary",
                help="Fetch a live match — all data pulled automatically",
            ):
                if mode != "live":
                    st.session_state["mode"] = "live"
                    st.session_state["live_data"] = None
                    st.session_state["live_fetched"] = False
                    st.rerun()
        with m2:
            replay_active = mode == "replay"
            if st.button(
                "📺  REPLAY",
                use_container_width=True,
                key="mode_replay_btn",
                type="primary" if replay_active else "secondary",
                help="Fill in a past match yourself — verify your prediction after",
            ):
                if mode != "replay":
                    st.session_state["mode"] = "replay"
                    st.session_state["live_data"] = None
                    st.session_state["live_fetched"] = False
                    st.rerun()

        st.markdown("<hr style='border-color:rgba(255,255,255,0.06); margin:0.5rem 0 1rem;'>", unsafe_allow_html=True)

        # ─────────────────────────────────────────────────────────
        # ══ LIVE MODE ════════════════════════════════════════════
        # ─────────────────────────────────────────────────────────
        if mode == "live":

            # ╔══════════════════════════════════╗
            # ║  Step 1 — Fetch live matches     ║
            # ╚══════════════════════════════════╝
            st.markdown('<div class="ce-card"><div class="ce-card-title">📡 LIVE MATCHES</div>', unsafe_allow_html=True)

            fb1, fb2 = st.columns([3, 2])
            with fb1:
                if st.button("🔴 Fetch Live Matches", use_container_width=True, key="btn_fetch_list"):
                    with st.spinner("Checking live scores…"):
                        try:
                            from scraper.live_score import get_live_matches
                            matches = get_live_matches()
                            st.session_state["live_matches_cache"]      = matches
                            st.session_state["live_matches_fetched_at"] = datetime.now().isoformat()
                            if not matches:
                                st.warning("No live matches right now. Try again in a few minutes or use Replay mode.")
                        except Exception as e:
                            st.error(f"Scraper error: {e}")
                            st.session_state["live_matches_cache"] = []
            with fb2:
                fetch_time = st.session_state.get("live_matches_fetched_at")
                n_live = len(st.session_state.get("live_matches_cache") or [])
                if fetch_time:
                    st.markdown(
                        f'<div style="padding:0.5rem 0; color:#98a2b6; font-size:0.78rem;">'
                        f'⚡ {fetch_time[11:16]} &nbsp;·&nbsp; {n_live} match(es)</div>',
                        unsafe_allow_html=True,
                    )

            live_matches = st.session_state.get("live_matches_cache") or []
            if live_matches:
                match_labels = []
                for m in live_matches:
                    teams_raw = m.get("teams", [])
                    teams_exp = [_expand_team(t) for t in teams_raw]
                    label     = " vs ".join(teams_exp) if teams_exp else m["title"][:60]
                    score_raw = m.get("score", "")
                    match_labels.append(f"{score_raw}  —  {label}" if score_raw else label)

                sel_idx = st.selectbox(
                    "Select match",
                    range(len(match_labels)),
                    format_func=lambda i: match_labels[i],
                    index=min(st.session_state.get("selected_match_idx", 0), len(live_matches) - 1),
                    key="sel_match_idx_widget",
                )
                st.session_state["selected_match_idx"] = sel_idx
                selected_match = live_matches[sel_idx]

                if st.button("📥 Load & Auto-Fill Everything", use_container_width=True,
                             key="btn_fetch_match", type="primary"):
                    with st.spinner("Fetching full scorecard…"):
                        try:
                            from scraper.live_score import fetch_live_data, calculate_remaining_bowlers
                            raw = fetch_live_data(
                                selected_match["url"],
                                source=selected_match.get("source", "crex"),
                            )
                            raw["remaining_bowlers"] = calculate_remaining_bowlers(raw.get("all_bowlers", []))
                            teams = selected_match.get("teams", [])
                            expanded_teams = [_expand_team(t) for t in teams]
                            if len(expanded_teams) >= 2:
                                # Who is batting NOW, most-to-least reliable:
                                #  1. full name from the chase status line ("Sri Lanka
                                #     Women need 49 runs ...")
                                #  2. live score-line abbreviation ("SLW" → Sri Lanka Women)
                                #  3. innings/title-order heuristic
                                # All are independent of match-list ordering.
                                bat_full = (_expand_team(raw.get("batting_team_name") or "")
                                            or "")
                                if bat_full not in expanded_teams:
                                    bat_full = _expand_team(raw.get("batting_abbr") or "")
                                if bat_full in expanded_teams:
                                    bat_idx = expanded_teams.index(bat_full)
                                else:
                                    bat_idx = _batting_team_index(raw, expanded_teams)
                                st.session_state["batting_team"] = expanded_teams[bat_idx]
                                st.session_state["bowling_team"] = expanded_teams[1 - bat_idx]
                            # Innings + target straight from the scoreboard (no manual entry)
                            scraped_inn = raw.get("innings")
                            if scraped_inn in (1, 2):
                                st.session_state["innings"]      = scraped_inn
                                st.session_state["live_inn_sel"] = scraped_inn
                            if raw.get("target"):
                                st.session_state["target"]   = int(raw["target"])
                                st.session_state["live_tgt"] = int(raw["target"])
                            if raw.get("venue"):
                                st.session_state["venue"] = raw["venue"]
                            match_title = selected_match.get("title", "")
                            detected_fmt = _auto_detect_format(match_title, expanded_teams)
                            if detected_fmt in config.SUPPORTED_FORMATS:
                                st.session_state["format"] = detected_fmt
                            cond = _infer_conditions(st.session_state["venue"], st.session_state["format"])
                            st.session_state["pitch_type"] = cond["pitch_type"]
                            st.session_state["weather"]    = cond["weather"]
                            st.session_state["live_data"]    = raw
                            st.session_state["live_fetched"] = raw.get("fetch_success", False)
                            if raw.get("fetch_success"):
                                st.rerun()
                            else:
                                st.warning("Partial fetch — review the score display below.")
                                st.rerun()
                        except Exception as e:
                            st.error(f"Fetch error: {e}")

            st.markdown('</div>', unsafe_allow_html=True)

            # ╔══════════════════════════════════╗
            # ║  Step 2 — Live scorecard display ║
            # ╚══════════════════════════════════╝
            live = st.session_state.get("live_data") or {}
            if live and live.get("fetch_success"):
                st.markdown('<div class="ce-card"><div class="ce-card-title">📊 LIVE SCORECARD</div>', unsafe_allow_html=True)

                # Match / format / innings row
                t1, t_swap, t2, t3 = st.columns([2, 0.5, 2, 1.5])
                with t1:
                    st.markdown(f'<div style="color:#2ee6a6; font-size:0.9rem; font-weight:700;">'
                                f'{st.session_state.get("batting_team","—")}</div>'
                                f'<div style="color:#98a2b6; font-size:0.72rem;">BATTING</div>',
                                unsafe_allow_html=True)
                with t_swap:
                    # Give user a way to correct team assignment if the auto-fill guessed wrong from the title
                    if st.button("🔁", key="btn_swap_teams", help="Swap Batting/Bowling"):
                        bat = st.session_state.get("batting_team")
                        st.session_state["batting_team"] = st.session_state.get("bowling_team")
                        st.session_state["bowling_team"] = bat
                        st.rerun()
                with t2:
                    st.markdown(f'<div style="color:#f3f5fa; font-size:0.9rem; font-weight:700;">'
                                f'{st.session_state.get("bowling_team","—")}</div>'
                                f'<div style="color:#98a2b6; font-size:0.72rem;">BOWLING</div>',
                                unsafe_allow_html=True)
                with t3:
                    fmt_lbl = st.selectbox("Format", config.SUPPORTED_FORMATS,
                        index=config.SUPPORTED_FORMATS.index(st.session_state["format"])
                              if st.session_state["format"] in config.SUPPORTED_FORMATS else 0,
                        key="live_fmt_sel")
                    st.session_state["format"] = fmt_lbl

                inn_col, tgt_col, ven_col = st.columns([1, 2, 3])
                with inn_col:
                    # Seed from scraped innings; the key carries it across reruns.
                    st.session_state.setdefault("live_inn_sel", st.session_state.get("innings", 1))
                    inn_sel = st.selectbox("Innings", [1, 2], key="live_inn_sel")
                    st.session_state["innings"] = inn_sel
                with tgt_col:
                    if st.session_state["innings"] == 2:
                        # Auto-filled from the scoreboard target; editable as a fallback.
                        st.session_state.setdefault("live_tgt", int(st.session_state.get("target") or 160))
                        tgt_v = st.number_input("Target", 1, 300, key="live_tgt")
                        st.session_state["target"] = tgt_v
                with ven_col:
                    _live_venues = _load_venues(st.session_state["format"])
                    _cur_venue  = st.session_state.get("venue", "")
                    _venue_opts = ["-- Unknown venue --"] + _live_venues
                    _v_idx = (_venue_opts.index(_cur_venue)
                              if _cur_venue in _venue_opts else 0)
                    _sel_venue = st.selectbox("Venue", _venue_opts, index=_v_idx, key="live_venue_sel")
                    st.session_state["venue"] = "" if _sel_venue == "-- Unknown venue --" else _sel_venue

                render_live_score(live)
                recent = live.get("recent_overs", [])
                if recent:
                    render_recent_overs(recent)

                # Raw scrape values — verify what the feed actually returned.
                # If the batting team looks wrong, check `batting_abbr` here and
                # use the 🔁 swap button above to correct it.
                with st.expander("🔧 Scraper diagnostic", expanded=False):
                    st.caption(
                        f"batting_abbr = **{live.get('batting_abbr')}** &nbsp;·&nbsp; "
                        f"innings = **{live.get('innings')}** &nbsp;·&nbsp; "
                        f"target = **{live.get('target')}** &nbsp;·&nbsp; "
                        f"score = **{live.get('runs')}/{live.get('wickets')}** "
                        f"in **{live.get('overs')}** ov &nbsp;·&nbsp; "
                        f"batsmen = {len(live.get('batsmen', []))} &nbsp;·&nbsp; "
                        f"bowlers = {len(live.get('all_bowlers', []))} &nbsp;·&nbsp; "
                        f"source = {live.get('source')}"
                    )
                st.markdown('</div>', unsafe_allow_html=True)
            elif not live_matches:
                st.info("👆 Click **Fetch Live Matches** to load today's live games.")

        # ─────────────────────────────────────────────────────────
        # ══ REPLAY MODE ══════════════════════════════════════════
        # ─────────────────────────────────────────────────────────
        else:  # mode == "replay"

            # ╔══════════════════════════════════╗
            # ║  Match Details                   ║
            # ╚══════════════════════════════════╝
            st.markdown('<div class="ce-card"><div class="ce-card-title">🏏 MATCH DETAILS</div>', unsafe_allow_html=True)

            t1, t2 = st.columns(2)
            with t1:
                batting_team = st.selectbox("Batting team", [""] + KNOWN_TEAMS_WOMENS,
                    index=(KNOWN_TEAMS_WOMENS.index(st.session_state["batting_team"]) + 1)
                          if st.session_state["batting_team"] in KNOWN_TEAMS_WOMENS else 0,
                    key="sel_bat")
                if batting_team:
                    st.session_state["batting_team"] = batting_team
            with t2:
                bowling_team = st.selectbox("Bowling team", [""] + KNOWN_TEAMS_WOMENS,
                    index=(KNOWN_TEAMS_WOMENS.index(st.session_state["bowling_team"]) + 1)
                          if st.session_state["bowling_team"] in KNOWN_TEAMS_WOMENS else 0,
                    key="sel_bowl")
                if bowling_team:
                    st.session_state["bowling_team"] = bowling_team

            f1, f2, f3 = st.columns([2, 1, 2])
            with f1:
                fmt_sel = st.selectbox("Format", config.SUPPORTED_FORMATS,
                    index=config.SUPPORTED_FORMATS.index(st.session_state["format"])
                          if st.session_state["format"] in config.SUPPORTED_FORMATS else 0,
                    key="rep_fmt_sel")
                st.session_state["format"] = fmt_sel
            with f2:
                inn_sel = st.selectbox("Inn", [1, 2],
                    index=0 if st.session_state["innings"] == 1 else 1,
                    key="rep_inn_sel")
                st.session_state["innings"] = inn_sel
            with f3:
                _rep_venues = _load_venues(st.session_state["format"])
                _cur_venue  = st.session_state.get("venue", "")
                _venue_opts = ["-- Unknown venue --"] + _rep_venues
                _v_idx = (_venue_opts.index(_cur_venue) if _cur_venue in _venue_opts else 0)
                _sel_venue = st.selectbox("Venue", _venue_opts, index=_v_idx, key="rep_venue_sel")
                st.session_state["venue"] = "" if _sel_venue == "-- Unknown venue --" else _sel_venue

            if st.session_state["innings"] == 2:
                tgt_v = st.number_input("Target (1st innings total)", 1, 300,
                    int(st.session_state.get("target") or 160), key="rep_tgt")
                st.session_state["target"] = tgt_v

            st.markdown('</div>', unsafe_allow_html=True)

            # ── PRE-MATCH TOGGLE (outside form — triggers rerun to show/hide score fields) ──
            _is_prematch = st.session_state.get("pre_match", False)
            if st.toggle("⏳  Match has not started yet (pre-match)", value=_is_prematch, key="prematch_toggle"):
                st.session_state["pre_match"] = True
            else:
                st.session_state["pre_match"] = False
            _is_prematch = st.session_state["pre_match"]

            # ── SCORECARD ──────────────────────────────────────────────────────
            st.markdown('<div class="ce-card"><div class="ce-card-title">📊 SCORECARD</div>', unsafe_allow_html=True)

            if _is_prematch:
                st.info("🕐 Pre-match mode — score locked at 0/0 (0.0 ov). "
                        "Just set your scenario & target below, then hit ANALYSE.")
                if st.button("✅  Confirm Pre-match", key="apply_prematch", type="primary", use_container_width=True):
                    st.session_state["live_data"] = {
                        "fetch_success": True, "source": "replay", "pre_match": True,
                        "runs": 0, "wickets": 0, "overs": 0.0,
                        "batsmen": [], "all_bowlers": [],
                        "remaining_bowlers": [], "current_bowler": None,
                        "recent_overs": [],
                    }
                    st.session_state["live_fetched"] = True
                    st.success("Pre-match state set — choose your scenario & target below.")

            else:
                # ── Row count controls (outside form so + triggers a rerun to add rows) ──
                # Ensure we have the last saved live_data snapshot available
                _ld = st.session_state.get("live_data") or {}
                _fmt_now = st.session_state.get("format", config.PRIMARY_FORMAT)
                available_batters = [b for b in _load_batters(_fmt_now) if b]
                available_bowlers = [b for b in _load_bowlers(_fmt_now) if b]
                all_batters = ["-- Select batter --"] + available_batters
                all_bowlers_pool = ["-- Select bowler --"] + available_bowlers
                _bowlers = _ld.get("all_bowlers", [])
                _batsmen = _ld.get("batsmen", [])
                _recent  = _ld.get("recent_overs", [])

                n_bat = st.session_state.get("n_batters", 2)
                n_bwl = st.session_state.get("n_bowlers", 0)

                # Batter count row
                b_hdr, b_plus, b_minus = st.columns([7, 1, 1])
                with b_hdr:
                    st.markdown('<div style="font-weight:600; color:#f3f5fa; font-size:0.88rem; '
                                'padding-top:0.35rem;">🏏 Batters at crease</div>', unsafe_allow_html=True)
                with b_plus:
                    if st.button("＋", key="add_bat", help="Add batter", use_container_width=True):
                        st.session_state["n_batters"] = min(n_bat + 1, 11)
                        st.rerun()
                with b_minus:
                    if st.button("－", key="rem_bat", help="Remove batter", use_container_width=True):
                        st.session_state["n_batters"] = max(n_bat - 1, 1)
                        st.rerun()

                # Bowler count row
                bw_hdr, bw_plus, bw_minus = st.columns([7, 1, 1])
                with bw_hdr:
                    st.markdown('<div style="font-weight:600; color:#f3f5fa; font-size:0.88rem; '
                                'padding-top:0.35rem;">🎯 Bowlers</div>', unsafe_allow_html=True)
                with bw_plus:
                    if st.button("＋", key="add_bwl", help="Add bowler", use_container_width=True):
                        st.session_state["n_bowlers"] = min(n_bwl + 1, 11)
                        st.rerun()
                with bw_minus:
                    if st.button("－", key="rem_bwl", help="Remove bowler", use_container_width=True):
                        st.session_state["n_bowlers"] = max(n_bwl - 1, 0)
                        st.rerun()

                # ── TEAM SCORE — outside form, updates live in session_state ──
                st.markdown(
                    '<div style="background:rgba(0,212,212,0.07); border:1px solid rgba(0,212,212,0.2); '
                    'border-radius:10px; padding:0.6rem 0.8rem; margin-bottom:0.6rem;">'
                    '<span style="font-size:0.75rem; color:#2ee6a6; font-weight:700; letter-spacing:0.08em;">'
                    '📟 TEAM SCORE (fill this first)</span></div>',
                    unsafe_allow_html=True)
                sc1, sc2, sc3 = st.columns(3)
                with sc1:
                    _runs_val = st.number_input("Runs", 0, 500,
                        int(_ld.get("runs", 0)), key="sc_runs")
                    st.session_state.setdefault("_sc_runs", _runs_val)
                with sc2:
                    _wkts_val = st.number_input("Wickets", 0, 10,
                        int(_ld.get("wickets", 0)), key="sc_wkts")
                with sc3:
                    _ovrs_val = st.number_input("Overs", 0.0, 20.0,
                        float(_ld.get("overs", 0.0)), step=0.1, key="sc_overs")

                # ── FORM — no reruns on number +/- clicks (score already captured above) ──
                with st.form("scorecard_form", border=False):

                    # Batters (dynamic rows, from session n_batters)
                    n_bat_now = st.session_state.get("n_batters", 2)
                    batter_entries = []
                    use_batter_dropdown = len(all_batters) > 1
                    for i in range(n_bat_now):
                        _bd = _batsmen[i] if i < len(_batsmen) else {}
                        _cur_bn = _bd.get("name", "")
                        _b_idx  = all_batters.index(_cur_bn) if _cur_bn in all_batters else 0
                        st.markdown(
                            f'<div style="font-size:0.78rem; color:#98a2b6; margin-top:0.4rem;">Batter {i+1}</div>',
                            unsafe_allow_html=True)
                        if use_batter_dropdown:
                            bname = st.selectbox(
                                f"Batter {i+1} name",
                                all_batters,
                                index=_b_idx,
                                key=f"bf_name_{i}",
                                label_visibility="collapsed",
                            )
                        else:
                            bname = st.text_input(
                                f"Batter {i+1} name",
                                value=_cur_bn,
                                placeholder="Enter batter name",
                                key=f"bf_name_{i}",
                                label_visibility="collapsed",
                            )
                        rc1, rc2 = st.columns(2)
                        with rc1:
                            bruns  = st.number_input("Runs",  0, 300, int(_bd.get("runs",0)),
                                                     key=f"bf_runs_{i}")
                        with rc2:
                            bballs = st.number_input("Balls", 0, 200, int(_bd.get("balls",0)),
                                                     key=f"bf_balls_{i}")
                        batter_entries.append((bname, bruns, bballs))

                    strike_labels = [
                        f"{i+1}. {bname if bname else 'Batter ' + str(i+1)}"
                        for i, (bname, _, _) in enumerate(batter_entries)
                    ]
                    strike_idx = st.selectbox(
                        "Current batter on strike",
                        list(range(len(strike_labels))),
                        format_func=lambda idx: strike_labels[idx],
                        index=0,
                        key="manual_strike_idx",
                    )

                    st.divider()

                    # Bowlers (dynamic rows, from session n_bowlers)
                    n_bwl_now = st.session_state.get("n_bowlers", 0)
                    st.markdown(
                        '<div style="font-size:0.78rem; color:#98a2b6; '
                        'margin-bottom:0.2rem;">Name &nbsp;·&nbsp; Ov &nbsp;·&nbsp; R &nbsp;·&nbsp; W</div>',
                        unsafe_allow_html=True)
                    bowler_entries = []
                    for i in range(n_bwl_now):
                        _bw = _bowlers[i] if i < len(_bowlers) else {}
                        _cur_bwn = _bw.get("name", "")
                        _bw_idx  = all_bowlers_pool.index(_cur_bwn) if _cur_bwn in all_bowlers_pool else 0
                        wc1, wc2, wc3, wc4 = st.columns([3, 1, 1, 1])
                        with wc1:
                            bwname = st.selectbox(f"Bowler {i+1}", all_bowlers_pool,
                                                  index=_bw_idx, key=f"bwf_name_{i}",
                                                  label_visibility="collapsed")
                        with wc2:
                            bwov = st.number_input("Ov", 0.0, 4.0, float(_bw.get("overs_today",0)),
                                                   step=1.0, key=f"bwf_ov_{i}", label_visibility="collapsed")
                        with wc3:
                            bwr  = st.number_input("R", 0, 100, int(_bw.get("runs_today",0)),
                                                   key=f"bwf_r_{i}", label_visibility="collapsed")
                        with wc4:
                            bwwk = st.number_input("W", 0, 10, int(_bw.get("wickets",0)),
                                                   key=f"bwf_wk_{i}", label_visibility="collapsed")
                        bowler_entries.append((bwname, bwov, bwr, bwwk))

                    st.divider()

                    # Recent overs — only show fields for completed overs (max 3)
                    _ov_completed = max(0, int(float(st.session_state.get("sc_overs", 0.0))))
                    _n_recent = min(3, _ov_completed) if _ov_completed > 0 else 0
                    st.markdown('<div style="font-size:0.78rem; color:#98a2b6; margin-bottom:0.2rem;">'
                                '📋 Recent overs <span style="opacity:0.6;">'
                                '(one box per completed over · space-separated · W / WD / NB)</span>'
                                '</div>', unsafe_allow_html=True)
                    if _n_recent == 0:
                        st.caption("Fill in after the 1st over is complete.")
                    _r = [" ".join(o) if o else "" for o in _recent[-3:]]
                    while len(_r) < 3:
                        _r.append("")
                    manual_recent = []
                    if _n_recent >= 3:
                        rov1 = st.text_input("3 overs ago", value=_r[0], placeholder="4 1 0 6 0 2", label_visibility="collapsed")
                        manual_recent.append(rov1.strip().split() if rov1.strip() else [])
                    if _n_recent >= 2:
                        rov2 = st.text_input("2 overs ago", value=_r[1 if _n_recent >= 3 else 0], placeholder="0 0 W 1 4 0", label_visibility="collapsed")
                        manual_recent.append(rov2.strip().split() if rov2.strip() else [])
                    if _n_recent >= 1:
                        rov3 = st.text_input(
                            "Last over" if _n_recent > 1 else "Over 1",
                            value=_r[2 if _n_recent >= 3 else (_n_recent - 1)],
                            placeholder="4 1 1 1 0 WD 0",
                            label_visibility="collapsed",
                        )
                        manual_recent.append(rov3.strip().split() if rov3.strip() else [])

                    submitted = st.form_submit_button(
                        "✅  Set Scorecard", type="primary", use_container_width=True)

                    if submitted:
                        manual_batsmen = []
                        for i, (bname, bruns, bballs) in enumerate(batter_entries):
                            name_value = bname.strip() if bname.strip() else f"Batter {i+1}"
                            manual_batsmen.append({
                                "name": name_value,
                                "runs": bruns,
                                "balls": bballs,
                                "on_strike": (i == st.session_state.get("manual_strike_idx", 0)),
                            })
                        bowler_rows = []
                        for bwname, bwov, bwr, bwwk in bowler_entries:
                            if bwname.strip():
                                balls_t = int(bwov) * 6
                                bowler_rows.append({
                                    "name": bwname.strip(),
                                    "overs_today": bwov, "runs_today": bwr, "wickets": bwwk,
                                    "economy": round(bwr / balls_t * 6, 2) if balls_t > 0 else 0.0,
                                    "overs_remaining": max(0, 4 - int(bwov)),
                                })
                        manual_recent = [r for r in manual_recent if r]
                        # Use the score values from the prominent section above
                        st.session_state["live_data"] = {
                            "fetch_success": True, "source": "replay",
                            "runs":    st.session_state.get("sc_runs", 0),
                            "wickets": st.session_state.get("sc_wkts", 0),
                            "overs":   st.session_state.get("sc_overs", 0.0),
                            "batsmen": manual_batsmen, "all_bowlers": bowler_rows,
                            "remaining_bowlers": [b for b in bowler_rows if b["overs_remaining"] > 0],
                            "current_bowler": bowler_rows[-1] if bowler_rows else None,
                            "recent_overs": manual_recent,
                        }
                        st.session_state["live_fetched"] = True
                        st.success("✅ Scorecard saved — set scenario & target below, then hit ANALYSE.")


            st.markdown('</div>', unsafe_allow_html=True)


        # ═══════════════════════════════════════════════════════════
        # PREDICTION SCENARIO & TARGET  (same for both modes)
        # ═══════════════════════════════════════════════════════════

        live = st.session_state.get("live_data")

        # ╔══════════════════════════════════════╗
        # ║  SCENARIO + TARGET                   ║
        # ╚══════════════════════════════════════╝

        st.markdown('<div class="ce-card"><div class="ce-card-title">🎯 PREDICTION SCENARIO & TARGET</div>', unsafe_allow_html=True)

        # ── Target window (interval scenarios) ────────────────────────────
        # Additive control. "Full Innings (Default)" leaves the V1.0 path 100%
        # untouched. Interval options are exact-over gated: "Next 3 Overs"
        # only at 3.0 overs, "Next 4 Overs" only at 6.0 overs.
        _ld_tm   = st.session_state.get("live_data") or {}
        _cur_ov_tm = float(_ld_tm.get("overs", 0.0))
        _avail_micro = available_micro_markets(_cur_ov_tm)
        _target_options = [FULL_INNINGS_TARGET] + _avail_micro
        _stored_target = st.session_state.get("target_market", FULL_INNINGS_TARGET)
        if _stored_target not in _target_options:
            _stored_target = FULL_INNINGS_TARGET
        target_market = st.radio(
            "🎯 Target Market",
            _target_options,
            index=_target_options.index(_stored_target),
            horizontal=True,
        )
        st.session_state["target_market"] = target_market
        is_micro_market = target_market != FULL_INNINGS_TARGET

        if is_micro_market:
            _mspec = MICRO_MARKETS[target_market]
            _mprev = float(st.session_state.get("micro_line", 24.5))
            micro_line = st.number_input(
                f"Target score — runs in {_mspec['label']} (interval only)",
                min_value=0.5, max_value=150.0,
                value=max(0.5, min(150.0, _mprev)),
                step=0.5, format="%.1f", key="sel_micro_line",
            )
            st.session_state["micro_line"] = micro_line
            st.caption(
                f"Interval scenario: the target is the runs scored INSIDE {_mspec['label']} only. "
                "The full-innings scenario control below is ignored while this is selected."
            )
        elif not _avail_micro:
            st.caption(
                "Interval scenarios unlock at exactly 3.0 overs (Overs 4–6) and 6.0 overs (Overs 7–10)."
            )

        mkt_col, line_col = st.columns([3, 2])
        with mkt_col:
            market_type = st.selectbox(
                "Prediction scenario",
                MARKET_TYPES,
                index=MARKET_TYPES.index(st.session_state["market_type"])
                      if st.session_state["market_type"] in MARKET_TYPES else 0,
                key="sel_market_type",
            )
            st.session_state["market_type"] = market_type

        with line_col:
            _inn = st.session_state.get("innings", 1)
            _tgt = st.session_state.get("target") if _inn == 2 else None
            lmin, lmax, ldef, llabel = _market_line_bounds(market_type, _inn, _tgt)

            prev = float(st.session_state.get("line", ldef))
            line_val = st.number_input(
                llabel, min_value=lmin, max_value=lmax,
                value=max(lmin, min(lmax, prev)),
                step=0.5, format="%.1f", key="sel_line",
            )
            st.session_state["line"] = line_val
            if _inn == 2 and _tgt and market_type == "Total Innings Score":
                st.caption(f"Chasing {int(_tgt)} — target score cannot exceed {int(_tgt) + 1} (max possible score).")

        # Custom overs
        custom_from = custom_to = None
        if market_type == "Custom: Overs X to Y":
            cc1, cc2 = st.columns(2)
            with cc1:
                custom_from = st.number_input("From Over", 1, 19,
                    int(st.session_state.get("custom_from_over", 16)), key="sel_cust_from")
                st.session_state["custom_from_over"] = custom_from
            with cc2:
                custom_to = st.number_input("To Over", 2, 20,
                    int(st.session_state.get("custom_to_over", 20)), key="sel_cust_to")
                st.session_state["custom_to_over"] = custom_to

        # Market description line
        _ld2 = st.session_state.get("live_data") or {}
        _cur_ov = float(_ld2.get("overs", 0.0))
        _fmt_now = st.session_state.get("format", config.PRIMARY_FORMAT)
        _ph = _get_phase(_cur_ov, _fmt_now)
        mkt_desc = market_description(market_type, _cur_ov, _ph, custom_from, custom_to, format_=_fmt_now)
        st.markdown(
            f'<div style="color:#98a2b6; font-size:0.78rem; margin-top:0.2rem;">'
            f'Predicting: <strong style="color:#2ee6a6;">{mkt_desc}</strong></div>',
            unsafe_allow_html=True,
        )

        # ── Advanced settings (hidden by default) ──
        with st.expander("⚙️ Advanced settings", expanded=False):
            adv1, adv2, adv3 = st.columns(3)
            with adv1:
                pitch = st.selectbox("Pitch", ["flat","spin","seam","damp"],
                    index=["flat","spin","seam","damp"].index(st.session_state["pitch_type"]),
                    key="sel_pitch")
                st.session_state["pitch_type"] = pitch
            with adv2:
                weather = st.selectbox("Weather", ["day","evening","dew"],
                    index=["day","evening","dew"].index(st.session_state["weather"]),
                    key="sel_weather")
                st.session_state["weather"] = weather
            with adv3:
                dew = st.toggle("Dew", value=st.session_state["dew_factor"], key="sel_dew")
                st.session_state["dew_factor"] = dew

            adv4, adv5 = st.columns(2)
            with adv4:
                toss_w = st.text_input("Toss winner", value=st.session_state.get("toss_winner",""), key="sel_tw")
                st.session_state["toss_winner"] = toss_w
            with adv5:
                toss_c = st.radio("Choice", ["bat","field"],
                    index=0 if st.session_state.get("toss_choice","bat")=="bat" else 1,
                    horizontal=True, key="sel_tc")
                st.session_state["toss_choice"] = toss_c

            adv6, adv7 = st.columns(2)
            with adv6:
                pr = st.number_input("Partnership runs",  0, 300,
                    st.session_state.get("partnership_runs",0), key="p_r")
                st.session_state["partnership_runs"] = pr
            with adv7:
                pb = st.number_input("Partnership balls", 0, 120,
                    st.session_state.get("partnership_balls",0), key="p_b")
                st.session_state["partnership_balls"] = pb

        st.markdown('</div>', unsafe_allow_html=True)

        # ── ANALYSE BUTTON ──
        can_analyse = (
            st.session_state.get("live_fetched") and
            st.session_state.get("batting_team") and
            st.session_state.get("bowling_team")
        )

        if can_analyse:
            analyse_clicked = st.button(
                "⚡ ANALYSE", use_container_width=True,
                key="btn_analyse", type="primary",
            )
        else:
            st.markdown(
                '<div style="background:rgba(0,212,212,0.04); border:1px solid rgba(0,212,212,0.1); '
                'border-radius:12px; padding:0.9rem; text-align:center; color:#586172; font-size:0.85rem;">'
                '← Load a live match or click 🎮 Demo to begin</div>',
                unsafe_allow_html=True,
            )
            analyse_clicked = False

    # ─────────────────────────────────────────────────────────────
    # RIGHT COLUMN — RESULT (always visible, updates after analyse)
    # ─────────────────────────────────────────────────────────────
    with right_col:
        st.markdown('<div data-cricedge="result-anchor"></div>', unsafe_allow_html=True)

        if not st.session_state.get("last_result") and not analyse_clicked:
            # Empty state — instructions
            st.markdown("""
            <div style="
              height:420px; display:flex; flex-direction:column;
              align-items:center; justify-content:center;
              border:1px solid rgba(0,212,212,0.08);
              border-radius:18px;
              background:rgba(15,22,40,0.5);
              color:#586172; text-align:center; padding:2rem;
            ">
              <div style="font-size:3rem; margin-bottom:1rem;">🏏</div>
              <div style="font-size:1rem; color:#98a2b6; margin-bottom:0.5rem; font-weight:600;">
                Ready to analyse
              </div>
              <div style="font-size:0.82rem; line-height:1.9;">
                1. Click <strong style="color:#2ee6a6;">🎮 Demo</strong> to try instantly<br>
                or fetch a live match<br><br>
                2. Set your target score<br><br>
                3. Hit <strong style="color:#f5b53c;">⚡ ANALYSE</strong>
              </div>
            </div>
            """, unsafe_allow_html=True)

        # Run analysis
        if analyse_clicked:
            live = st.session_state.get("live_data") or {}
            with st.spinner("Calculating…"):
                try:
                    from model.innings_types import analyse
                    from model.probability import get_phase
                    from scraper.live_score import calculate_remaining_bowlers

                    batsmen = live.get("batsmen", [])

                    # FIX 4 — Scorer Concentration data feed:
                    # If live_data has no batsmen (user entered score directly
                    # without submitting the scorecard form), read individual
                    # batter name/runs/balls directly from current widget state.
                    _PLACEHOLDER = "-- Select batter --"
                    if not batsmen or all(b.get("runs", 0) == 0 for b in batsmen):
                        n_bat_w = st.session_state.get("n_batters", 2)
                        widget_batsmen = []
                        for _i in range(n_bat_w):
                            _bname = st.session_state.get(f"bf_name_{_i}", "")
                            _bruns = int(st.session_state.get(f"bf_runs_{_i}", 0))
                            _bballs = int(st.session_state.get(f"bf_balls_{_i}", 0))
                            if _bname and _bname != _PLACEHOLDER:
                                widget_batsmen.append({
                                    "name":      _bname,
                                    "runs":      _bruns,
                                    "balls":     _bballs,
                                    "on_strike": (_i == st.session_state.get("manual_strike_idx", 0)),
                                })
                        if widget_batsmen:
                            batsmen = widget_batsmen
                    # Strip any remaining placeholder entries
                    batsmen = [b for b in batsmen if b.get("name", "") not in ("", _PLACEHOLDER)]

                    all_bowlers       = live.get("all_bowlers", [])
                    remaining_bowlers = (
                        live.get("remaining_bowlers")
                        or calculate_remaining_bowlers(all_bowlers)
                    )
                    recent_overs      = _trim_recent_overs(
                        live.get("recent_overs", []),
                        float(live.get("overs", 0.0)),
                    )
                    balls_this_innings = _reconstruct_balls(recent_overs, float(live.get("overs", 0.0)))
                    current_over      = float(live.get("overs", 0.0))
                    fmt               = st.session_state["format"]
                    current_phase     = get_phase(current_over, fmt)

                    # ── Branch: Micro-Market (parallel path) vs Full Innings (V1.0) ──
                    # The Full Innings branch below is byte-for-byte the frozen V1.0
                    # pipeline; micro-markets never enter it.
                    _target_mkt = st.session_state.get("target_market", FULL_INNINGS_TARGET)
                    if _target_mkt != FULL_INNINGS_TARGET:
                        from datetime import datetime as _dt
                        _micro_ctx = {
                            "format":         fmt,
                            "batting_team":   st.session_state["batting_team"],
                            "bowling_team":   st.session_state["bowling_team"],
                            "venue":          st.session_state.get("venue") or "Unknown",
                            "innings":        st.session_state["innings"],
                            "over_number":    current_over,
                            "current_runs":   int(live.get("runs", 0)),
                            "wickets_fallen": int(live.get("wickets", 0)),
                            "match_label":    (
                                f"{st.session_state['batting_team']} vs "
                                f"{st.session_state['bowling_team']}"
                            ),
                            "timestamp":      _dt.now().isoformat(),
                        }
                        final_result = calc_micro_market(
                            _target_mkt,
                            float(st.session_state.get("micro_line", 0.0)),
                            _micro_ctx,
                            recent_overs=recent_overs,
                        )
                        # Micro-markets are not run through the live calibrator
                        # (calibrated on full-innings probabilities only).
                        final_result["calibrated_probability"] = None
                        final_result["trade_signal"] = ""
                        st.session_state["last_result"] = final_result
                        st.session_state["scroll_to_result"] = True
                    else:
                        full_result = analyse(
                            innings           = st.session_state["innings"],
                            format_           = st.session_state["format"],
                            over_number       = current_over,
                            current_runs      = int(live.get("runs", 0)),
                            wickets_fallen    = int(live.get("wickets", 0)),
                            line              = float(st.session_state["line"]),
                            batting_team      = st.session_state["batting_team"],
                            bowling_team      = st.session_state["bowling_team"],
                            venue             = st.session_state.get("venue") or "Unknown",
                            batsmen           = batsmen,
                            all_bowlers       = all_bowlers,
                            remaining_bowlers = remaining_bowlers,
                            remaining_batters = _build_remaining_batters(batsmen, int(live.get("wickets", 0))),
                            all_scorers       = _build_all_scorers(batsmen, live),
                            recent_overs      = recent_overs,
                            balls_this_innings = balls_this_innings,
                            pitch_type        = st.session_state["pitch_type"],
                            weather_condition = st.session_state["weather"],
                            dew_factor        = st.session_state["dew_factor"],
                            target            = st.session_state.get("target"),
                            toss_winner       = st.session_state.get("toss_winner", ""),
                            toss_choice       = st.session_state.get("toss_choice", "bat"),
                            partnership_runs  = st.session_state.get("partnership_runs", 0),
                            partnership_balls = st.session_state.get("partnership_balls", 0),
                            match_label       = (
                                f"{st.session_state['batting_team']} vs "
                                f"{st.session_state['bowling_team']}"
                            ),
                            market_type = st.session_state["market_type"],
                            save_to_db = (st.session_state["market_type"] == "Total Innings Score"),
                        )

                        calibrator = _load_live_calibrator()
                        if calibrator is not None:
                            calibrated_prob, trade_signal = _format_trade_signal(
                                full_result.get("final_probability", 0.0),
                                calibrator,
                            )
                            full_result["calibrated_probability"] = calibrated_prob
                            full_result["trade_signal"] = trade_signal
                        else:
                            # Calibrator unavailable (missing file / scikit-learn) —
                            # run uncalibrated rather than crashing the app.
                            full_result["calibrated_probability"] = None
                            full_result["trade_signal"] = ""

                        momentum_adj = (
                            full_result.get("adjustments", {})
                            .get("momentum", {}).get("adj", 0.0)
                        )

                        # Shared route_market args (re-used by the checkpoint model
                        # selector for the BASE/PRUNED window re-run). full_innings_result
                        # and momentum_adj_pct are supplied per-call so the selector can
                        # toggle the adjustment set without duplicating this list.
                        _market_type = st.session_state["market_type"]
                        route_market_kwargs = dict(
                            market_type       = _market_type,
                            line              = float(st.session_state["line"]),
                            current_over      = current_over,
                            innings           = st.session_state["innings"],
                            phase             = current_phase,
                            all_bowlers       = all_bowlers,
                            wickets_fallen    = int(live.get("wickets", 0)),
                            dew_factor        = st.session_state["dew_factor"],
                            remaining_bowlers = remaining_bowlers,
                            format_           = st.session_state["format"],
                            custom_from_over  = st.session_state.get("custom_from_over"),
                            custom_to_over    = st.session_state.get("custom_to_over"),
                            recent_overs      = recent_overs,
                            target            = st.session_state.get("target"),
                        )
                        final_result = route_market(
                            full_innings_result = full_result,
                            momentum_adj_pct    = momentum_adj,
                            **route_market_kwargs,
                        )

                        # ── Production model selection (single source of truth:
                        #    config.CHECKPOINT_MODELS). FULL / unmapped → pass-through. ──
                        _ckpt_key = detect_checkpoint(
                            current_over, _market_type,
                            route_market_kwargs["custom_from_over"],
                            route_market_kwargs["custom_to_over"],
                        )
                        final_result, _applied_model = apply_checkpoint_model(
                            final_result, full_result, _ckpt_key,
                            is_full_innings=(_market_type in ("", "Total Innings Score")),
                            route_market_kwargs=route_market_kwargs,
                        )
                        st.session_state["last_result"] = final_result
                        st.session_state["scroll_to_result"] = True

                except Exception as e:
                    st.error(f"Analysis error: {e}")
                    import traceback
                    st.exception(e)

        # Show result card
        if st.session_state.get("last_result"):
            result = st.session_state["last_result"]
            mkt = result.get("market_type", "")
            mkt_color = "#2ee6a6" if mkt == "Total Innings Score" else "#f5b53c"

            st.markdown(
                f'<div style="text-align:center; margin-bottom:0.5rem;">'
                f'<span style="background:rgba(0,212,212,0.07); '
                f'border:1px solid rgba(0,212,212,0.2); '
                f'border-radius:100px; padding:0.2rem 1rem; '
                f'font-size:0.72rem; color:{mkt_color}; '
                f'letter-spacing:0.1em; font-weight:700;">'
                f'MARKET: {mkt.upper()}</span></div>',
                unsafe_allow_html=True,
            )

            win = result.get("market_window")
            if win:
                st.markdown(
                    f'<div style="text-align:center; margin-bottom:0.6rem; '
                    f'color:#98a2b6; font-size:0.78rem;">'
                    f'Overs {int(win["from_over"])+1}–{int(win["to_over"])} · '
                    f'Expected ~{win["expected_runs"]} runs · '
                    f'σ ±{win["sigma"]}</div>',
                    unsafe_allow_html=True,
                )

            # Production-checkpoint indicator: which validated model produced this
            # number, plus the out-of-sample disclaimer. Shown only at the three
            # mapped production checkpoints (config.CHECKPOINT_MODELS).
            if result.get("checkpoint_key"):
                st.markdown(
                    f'<div style="text-align:center; margin-bottom:0.6rem; '
                    f'color:#8a93a6; font-size:0.72rem; line-height:1.4;">'
                    f'<b style="color:#f5b53c;">{result.get("checkpoint_model", "FULL")} model</b> · '
                    f'Tested on historical out-of-sample data (2024+ matches). '
                    f'Past performance does not guarantee future results.</div>',
                    unsafe_allow_html=True,
                )

            render_probability_card(result)

            # Condensed, copy-ready summary immediately below the card / Key Insight.
            render_copy_summary(result)

            st.markdown(
                '<div style="text-align:center; color:#586172; '
                'font-size:0.75rem; margin-top:0.5rem;">'
                'Saved to History tab. Mark result after match.</div>',
                unsafe_allow_html=True,
            )

        if st.session_state.pop("scroll_to_result", False):
            import streamlit.components.v1 as components
            components.html(
                """
                <script>
                (function () {
                    const doc = window.parent.document;
                    function scrollToResult() {
                        const anchor = doc.querySelector('[data-cricedge="result-anchor"]');
                        if (anchor) {
                            anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
                            return;
                        }
                        const main = doc.querySelector('section.main');
                        if (main) {
                            main.scrollTo({ top: main.scrollHeight, behavior: 'smooth' });
                        }
                    }
                    setTimeout(scrollToResult, 250);
                })();
                </script>
                """,
                height=0,
            )


# ═════════════════════════════════════════════════════════════════
# TAB 2 — HISTORY
# ═════════════════════════════════════════════════════════════════
with tab_history:
    st.markdown("### 📜 Prediction History")
    render_history_tab(get_all_predictions())


# ═════════════════════════════════════════════════════════════════
# TAB 3 — ANALYTICS
# ═════════════════════════════════════════════════════════════════
with tab_analytics:
    st.markdown("### 📊 Analytics & Accuracy Dashboard")
    render_analytics_tab(get_all_predictions())


# ═════════════════════════════════════════════════════════════════
# TAB 4 — SCORECARD ENTRY
# ═════════════════════════════════════════════════════════════════
with tab_scorecard:
    render_scorecard_entry_tab()


# ═════════════════════════════════════════════════════════════════
# TAB 5 — SETTINGS
# ═════════════════════════════════════════════════════════════════
with tab_settings:
    render_settings_tab()


# ─────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🗄️ Data Status")
    wc = db_status.get("womens_match_positions", 0)
    mc = db_status.get("mens_match_positions", 0)
    bc = db_status.get("batter_stats_count", 0)
    pc = db_status.get("predictions_count", 0)
    m_ing = db_status.get("manual_scorecards_ingested", 0)
    m_pend = db_status.get("manual_scorecards_pending", 0)

    if wc == 0:
        st.warning("Run: `python -m data.ingest --format \"Women's T20I\" --quick`")
    else:
        st.success(f"✅ {wc:,} Women's T20I states")

    st.markdown(f"""
    <div class="data-status">
      <div class="data-status-row"><span>Women's T20I</span><span class="data-status-val">{wc:,}</span></div>
      <div class="data-status-row"><span>Men's T20I</span><span class="data-status-val">{mc:,}</span></div>
      <div class="data-status-row"><span>Players</span><span class="data-status-val">{bc:,}</span></div>
      <div class="data-status-row"><span>Predictions</span><span class="data-status-val">{pc:,}</span></div>
      <div class="data-status-row"><span>Manual SC (in DB)</span><span class="data-status-val">{m_ing}</span></div>
      <div class="data-status-row"><span>Manual SC (pending)</span><span class="data-status-val">{m_pend}</span></div>
      <div class="data-status-row"><span>Last ingest</span>
        <span class="data-status-val" style="font-size:0.7rem;">{db_status.get('last_ingest','Never')[:16]}</span>
      </div>
    </div>
    """, unsafe_allow_html=True)
    st.markdown("---")
    st.caption("CricEdge v1.0 · Women's T20I · Baseline + Signed Adjustments")

