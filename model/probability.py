"""
model/probability.py — CricEdge Phase-Dynamic Probability Engine

Architecture: Baseline + Signed Adjustments
  Step 1: match_position_stats → base probability
  Step 2: phase-specific signed adjustments from each factor
  Step 3: conditional modifiers applied in order (see modifiers.py)
  Step 4: hard cap [5%, 95%]

All adjustment ranges come from config.py — never hardcoded here.
Features are kept as clean separate inputs for future XGBoost migration.
"""

import logging
import math
from typing import Optional

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data import database as db

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# PHASE DETECTION
# ─────────────────────────────────────────────────────────────────

def get_phase(over_number: float, format_: str = "") -> str:
    """Return 'powerplay', 'middle', or 'death' for a given over.
    ODI formats use overs 1-10 PP, 11-40 middle, 41-50 death.
    T20 formats use overs 1-6 PP, 7-16 middle, 17-20 death (last 4 overs).
    """
    over_int = int(over_number)
    is_odi = format_ in config.ODI_FORMATS if format_ else False
    bounds = config.PHASE_BOUNDARIES_ODI if is_odi else config.PHASE_BOUNDARIES
    pp_lo, pp_hi     = bounds["powerplay"]
    mid_lo, mid_hi   = bounds["middle"]
    if pp_lo <= over_int <= pp_hi:
        return "powerplay"
    elif mid_lo <= over_int <= mid_hi:
        return "middle"
    return "death"


def cricket_over_to_db_over(over_number: float) -> int:
    """
    Convert live cricket notation (e.g. 17.0 or 14.2) to the 0-indexed over key
    used in match_position_stats (Cricsheet: over 0 = first over).

    Live over N / N.x → snapshot at start of over N → db key N-1.
    """
    over_int = int(over_number)
    if over_int <= 0:
        return 0
    return over_int - 1


# Status emitted when the baseline is unusable (zero/invalid/out-of-range) so the
# engine refuses to fabricate a confident signal from broken math.
DEGRADED_STATUS = "🔄 INVALID MARKET LINE / DEGRADED MODE"
# Verdict forced whenever the base cannot be computed — never a confident number.
INSUFFICIENT_DATA = "⚠️ INSUFFICIENT DATA — cannot calculate"


# ─────────────────────────────────────────────────────────────────
# BASELINE LOOKUP (Step 1)
# ─────────────────────────────────────────────────────────────────

def get_base_probability(
    innings: int,
    format_: str,
    over_number: float,
    wickets_fallen: int,
    current_runs: int,
    line: float,
    db_path: Optional[str] = None,
    batting_team: str = "",
    bowling_team: str = "",
    venue: str = "",
    target: Optional[int] = None,
    market_type: str = "",
) -> tuple[float, str, dict]:
    """
    Query match_position_stats for historical % of innings that exceeded line.
    Returns (probability_pct, source_description, position_debug).
    """
    max_overs = config.MAX_OVERS.get(format_, 20)
    db_over = cricket_over_to_db_over(over_number)

    pre_match = over_number < 1.0 and current_runs == 0

    # ── SEGMENT MARKET HANDLING (powerplay) ──────────────────────
    # A T20 line well below the full-innings range is a 6-over powerplay market,
    # not a 20-over total. Price it off the score AT the 6-over mark — querying the
    # full-innings position table here would return "line_out_of_range" and let the
    # fallback compare a ~130-run innings projection against a ~45-run line.
    is_pp_segment = (
        max_overs == 20 and over_number <= 6 and 0 < line < db.SEGMENT_PP_MAX_LINE
    )
    if is_pp_segment:
        seg = db.query_powerplay_segment_detail(
            innings, format_, line, db_path,
            batting_team=batting_team, bowling_team=bowling_team,
            current_over=over_number, current_runs=current_runs,
            wickets_fallen=wickets_fallen,
        )
        seg_debug = {
            "db_over": db.POWERPLAY_DB_OVER,
            "line": line,
            "segment": "powerplay",
            "segment_over": 6,
            "sample_count": seg.get("sample_count", 0),
            "pct": seg.get("pct"),
            "source": seg.get("source", "none"),
            "avg_final_score": seg.get("avg_segment_score"),
            "projected_total": seg.get("avg_segment_score"),
            "avg_final_score_source": "powerplay 6-over historical",
        }
        if seg.get("pct") is not None and seg.get("sample_count", 0) >= config.MIN_POSITION_SAMPLES:
            seg_debug["conditional"] = seg.get("conditional", False)
            seg_debug["conditional_state"] = seg.get("conditional_state")
            kind = (
                f"state-conditional {seg['conditional_state']}"
                if seg.get("conditional") else "unconditional base rate"
            )
            src = f"Powerplay 6-over historical, {kind}"
            return seg["pct"], src, seg_debug, 0.0
        # No usable powerplay data → degraded mode (guardrail handled downstream)
        seg_debug["degraded_mode"] = True
        seg_debug["status"] = DEGRADED_STATUS
        return 50.0, DEGRADED_STATUS, seg_debug, 0.0

    position_debug = db.query_match_position_detail(
        innings, format_, db_over, wickets_fallen, current_runs, line, db_path,
        batting_team=batting_team, bowling_team=bowling_team
    )

    _POSITION_SOURCES = {
        "exact", "wicket_interpolation", "runs_interpolation", "combined_interpolation",
        "hierarchical_smoothing",
    }
    pos_source = position_debug.get("source", "none").replace("_low_sample", "")
    pos_source = pos_source.replace("mens_fallback_", "")

    if not pre_match:
        if position_debug.get("pct") is not None and (
            position_debug.get("sample_count", 0) >= config.MIN_POSITION_SAMPLES
            or pos_source in _POSITION_SOURCES
        ):
            n = position_debug.get("sample_count", 0)
            tier = position_debug.get("smoothing_tier", "")
            if pos_source == "hierarchical_smoothing":
                src = f"Hierarchical smoothing ({format_}, n={n}, blend 0.15/0.35/0.50)"
            elif n >= config.MIN_POSITION_SAMPLES:
                src = f"Historical data ({format_}, n={n})"
            else:
                src = (
                    f"Historical interpolation ({format_}, {pos_source.replace('_', ' ')}, n={n}"
                    f" < {config.MIN_POSITION_SAMPLES}; tier={tier})"
                )
            return position_debug["pct"], src, position_debug, 0.0  # std_dev N/A (historical data)

    # ── INSUFFICIENT-DATA GUARDRAIL: line outside the position table's range ──
    # The position table's columns are FULL-INNINGS totals (100–220). That range
    # check is only meaningful for a full-innings market. Window/session markets
    # (Next X Overs, Powerplay/Middle/Death Session) carry a sub-innings line that
    # is legitimately "out of range" here — they re-derive their own correctly
    # scaled base in route_market, so we must NOT degrade them. Only force
    # insufficient data when this IS the full-innings market.
    is_full_innings_market = market_type in ("", "Total Innings Score")
    if position_debug.get("source") == "line_out_of_range" and is_full_innings_market:
        position_debug["degraded_mode"] = True
        position_debug["status"] = DEGRADED_STATUS
        position_debug["projected_total"] = 0.0
        return 50.0, INSUFFICIENT_DATA, position_debug, 0.0

    projected = _estimate_projected_total(
        innings=innings,
        format_=format_,
        over_number=over_number,
        current_runs=current_runs,
        batting_team=batting_team,
        bowling_team=bowling_team,
        venue=venue,
        target=target,
        db_path=db_path,
    )

    # ── ZERO-BASELINE CRASH PROTECTION ───────────────────────────
    # If the Trajectory Base could not be formed (projection is zero/invalid),
    # halt rather than letting (0 - line)/sigma cascade into a fake signal.
    if not projected or projected <= 0:
        position_debug["degraded_mode"] = True
        position_debug["status"] = DEGRADED_STATUS
        position_debug["projected_total"] = 0.0
        return 50.0, DEGRADED_STATUS, position_debug, 0.0

    # Store team+venue-specific projected total so the insight generator can
    # display it instead of the generic elite-bucket avg_final_score (136.8).
    # For pre-match predictions this IS the relevant baseline; avg_final_score
    # is only the pooled all-nations average and is meaningless for NZ vs India.
    position_debug["projected_total"] = round(projected, 1)
    if pre_match:
        # Override avg_final_score so any downstream code (insight, UI) sees the
        # team-specific number rather than the generic elite-tier pool mean.
        position_debug["avg_final_score"] = round(projected, 1)
        position_debug["avg_final_score_source"] = "team/venue projection"

    std_dev = 18.0 if max_overs == 20 else 35.0
    if innings == 2 and target and line >= target - 5:
        std_dev = 12.0  # tighter distribution near chase target

    z = (projected - line) / std_dev
    pct_estimate = _sigmoid(z) * 100
    pct_estimate = max(config.PROBABILITY_MIN, min(config.PROBABILITY_MAX, pct_estimate))
    source = "Team/venue projection" if pre_match else "Estimated (blend)"
    if not pre_match and position_debug.get("sample_count", 0) < config.MIN_POSITION_SAMPLES:
        source = (
            f"Venue/team estimate (position n={position_debug.get('sample_count', 0)}"
            f" < {config.MIN_POSITION_SAMPLES})"
        )
    return pct_estimate, source, position_debug, std_dev


def _bowling_team_eco(bowl_stats: Optional[dict]) -> float:
    """Weighted average economy across phases."""
    if not bowl_stats:
        return 8.0
    pp  = bowl_stats.get("avg_pp_economy") or bowl_stats.get("avg_death_economy") or 8.0
    mid = bowl_stats.get("avg_mid_economy") or pp
    death = bowl_stats.get("avg_death_economy") or mid
    return (pp * 6 + mid * 10 + death * 4) / 20.0


# Format-aware league average totals (used when no team-specific data is available).
# Raised Women's T20I from 120→128 to reflect modern era (2022-2026 elite scoring ~130-150).
# Women's T20I: all-elite average ~136 (position table), all-nations ~120. Use ~128 as middle.
_FORMAT_LEAGUE_TOTAL: dict[str, float] = {
    "Women's T20I": 128.0,   # modern era all-nations avg; top teams 135-165
    "T20I":         155.0,   # Men's T20I average innings
    "Men's ODI":    270.0,
    "Women's ODI":  215.0,
}
_FORMAT_NEUTRAL_BOWL_ECO: dict[str, float] = {
    "Women's T20I": 6.30,
    "T20I":         7.50,
    "Men's ODI":    5.20,
    "Women's ODI":  4.80,
}


# Minimum batting average for elite teams — prevents stale all-time data from
# under-projecting teams that have improved significantly in the modern era.
# E.g. India Women all-time avg = 112 but 2022-2026 average = 145+.
_ELITE_BATTING_FLOOR: dict[str, float] = {
    "Women's T20I": 118.0,  # floor for ELITE_TEAMS — never project below this
    "T20I":         148.0,
    "Men's ODI":    230.0,
    "Women's ODI":  180.0,
}


def _estimate_projected_total(
    innings: int,
    format_: str,
    over_number: float,
    current_runs: int,
    batting_team: str,
    bowling_team: str,
    venue: str,
    target: Optional[int],
    db_path: Optional[str] = None,
) -> float:
    """Project final innings total from team, venue, opposition, and chase target.

    FIX (data quality): league_total and bowling baseline use format-specific values.
    FIX (venue reliability): venue average only used when venue has >= MIN_VENUE_SAMPLES_MODIFIER
      innings of data — prevents 1-3 sample venues (Edgbaston n=1 → 95 runs) from
      dragging projections far below the team-specific average.
    FIX (elite floor): elite teams (India, NZ, AUS…) have a modern-era minimum floor
      to prevent all-time averages (including pre-2018 low-scoring era) from
      underestimating current batting quality.
    """
    max_overs = config.MAX_OVERS.get(format_, 20)
    from model.market import PHASE_TYPICAL_RPO, PHASE_TYPICAL_RPO_ODI
    is_odi = format_ in config.ODI_FORMATS if format_ else False
    rpo_table = PHASE_TYPICAL_RPO_ODI if is_odi else PHASE_TYPICAL_RPO

    # Use format-specific league total as the fallback baseline
    league_total = _FORMAT_LEAGUE_TOTAL.get(format_, (
        rpo_table["powerplay"] * 6
        + rpo_table["middle"] * 10
        + rpo_table["death"] * 4
    ))
    neutral_bowl_eco = _FORMAT_NEUTRAL_BOWL_ECO.get(format_, 7.50)

    bat  = db.get_team_batting_stats(batting_team, format_, db_path) if batting_team else None
    bowl = db.get_team_bowling_stats(bowling_team, format_, db_path) if bowling_team else None
    vs   = db.get_venue_stats(venue, format_, db_path) if venue and venue != "Unknown" else None

    team_avg = float(bat["avg_total"]) if bat and bat.get("avg_total") else league_total

    # ── Elite team minimum floor ─────────────────────────────────
    # All-time DB averages include the pre-2020 low-scoring era.
    # Top teams have improved dramatically — never project below the floor.
    elite_floor = _ELITE_BATTING_FLOOR.get(format_, 0.0)
    if elite_floor > 0 and batting_team in db.ELITE_TEAMS:
        team_avg = max(team_avg, elite_floor)

    if innings == 1:
        # ── Venue reliability gate ───────────────────────────────
        # Only trust venue avg_total if the venue has enough matches.
        # A single old match (e.g. Edgbaston n=1, avg=95) should not
        # drag the projection 20+ runs below the team average.
        venue_ok = (
            vs is not None
            and db.venue_sample_count(vs) >= config.MIN_VENUE_SAMPLES_MODIFIER
            and vs.get("avg_total_batting_first") not in (None, 0)
        )
        venue_avg = float(vs["avg_total_batting_first"]) if venue_ok else team_avg
        projected = 0.45 * team_avg + 0.35 * venue_avg + 0.20 * league_total
    else:
        venue_ok = (
            vs is not None
            and db.venue_sample_count(vs) >= config.MIN_VENUE_SAMPLES_MODIFIER
            and vs.get("avg_total_chasing") not in (None, 0)
        )
        venue_avg = float(vs["avg_total_chasing"]) if venue_ok else team_avg
        projected = 0.40 * team_avg + 0.35 * venue_avg + 0.25 * league_total
        if target and target > 0:
            projected = 0.50 * projected + 0.50 * float(target)

    bowl_eco = _robust_bowling_eco(bowl, bowling_team, format_)
    # Apply bowling quality adjustment relative to format-specific neutral eco.
    # Higher eco than neutral = weaker bowling = more runs for batting team.
    # Uses the robust economy so a garbage low-sample figure (England Women eco
    # 10.4) can't inflate the projected total against an elite attack.
    projected += (bowl_eco - neutral_bowl_eco) * 2.5

    if over_number >= 1.0 and current_runs > 0:
        overs_done = over_number
        overs_left = max(0.0, max_overs - overs_done)
        phase = get_phase(over_number, format_)
        phase_rpo = rpo_table.get(phase, rpo_table["middle"])
        current_rr = current_runs / overs_done
        blended_rpo = (current_rr + phase_rpo) / 2.0
        live_projected = current_runs + blended_rpo * overs_left
        weight_live = min(1.0, overs_done / 8.0)
        projected = weight_live * live_projected + (1.0 - weight_live) * projected

    if innings == 2 and target and target > 0:
        projected = min(projected, float(target) + 1.0)

    return max(0.0, projected)


def _sigmoid(x: float) -> float:
    """Logistic sigmoid — used for probability estimation fallback."""
    return 1.0 / (1.0 + math.exp(-x))


# ─────────────────────────────────────────────────────────────────
# ADJUSTMENT SCALING HELPER
# ─────────────────────────────────────────────────────────────────

def scale_adjustment(signal: float, phase: str, factor_key: str) -> float:
    """
    Map a normalized signal [-1, +1] to an adjustment in percentage points
    using the configured range for this phase and factor.

    signal = -1 → min_adjustment (most negative)
    signal =  0 → 0 adjustment
    signal = +1 → max_adjustment (most positive)
    """
    ranges = config.ADJUSTMENT_RANGES.get(phase, {})
    lo, hi = ranges.get(factor_key, (0.0, 0.0))
    if lo == 0.0 and hi == 0.0:
        return 0.0
    signal = max(-1.0, min(1.0, signal))  # clamp
    if signal >= 0:
        return signal * hi
    else:
        return abs(signal) * lo  # lo is negative, so this is negative


# ─────────────────────────────────────────────────────────────────
# INDIVIDUAL FACTOR CALCULATORS
# Each returns (signal: float[-1,1], description: str)
# ─────────────────────────────────────────────────────────────────

def calc_momentum(
    current_rr: float,
    par_rr: float,
) -> tuple[float, str]:
    """
    Momentum = how far live RR is from par RR, normalized.
    Positive → batting ahead of par → OVER favored.
    """
    if par_rr <= 0:
        return 0.0, "No par data"
    raw = (current_rr - par_rr) / par_rr
    signal = max(-1.0, min(config.MOMENTUM_CLAMP, raw))
    desc = f"RR {current_rr:.1f} vs par {par_rr:.1f}"
    return signal, desc


def calc_partnership_rate(
    partnership_runs: int,
    partnership_balls: int,
) -> tuple[float, str]:
    """
    Partnership signal: high partnership rate → OVER favored.
    Normalized against typical partnership run rate (~1.1 per ball).
    """
    if partnership_balls < 3:
        return 0.0, "Insufficient partnership data"
    pr_per_ball = partnership_runs / partnership_balls
    typical = 1.1  # ~132 SR partnership
    signal = min(1.0, (pr_per_ball - typical) / typical)
    signal = max(-1.0, signal)
    desc = f"Partnership {partnership_runs}/{partnership_balls}b"
    return signal, desc


def calc_batter_bowler_matchup(
    batsmen: list[dict],
    remaining_bowlers: list[dict],
    format_: str,
    phase: str,
    db_path: Optional[str] = None,
) -> tuple[float, str]:
    """
    Evaluate all batsmen vs likely remaining bowlers using h2h and
    individual stats. Aggregate signal across current crease pair.
    Positive → batting advantage.

    FIX (Issue 2): When a batter has 0 balls faced (just arrived) their
    current-innings SR is undefined/zero.  We ALWAYS use career phase SR
    from the DB as the proxy — never derive SR from current innings stats.
    If career stats are found, a note 'using career SR (new batter)' is added.
    """
    if not batsmen or not remaining_bowlers:
        return 0.0, "No matchup data"

    signals = []
    notes   = []

    for batter_info in batsmen[:2]:
        batter_name = batter_info.get("name", "")
        if not batter_name:
            continue

        # Always use career stats regardless of balls faced this innings.
        # 0 balls faced = new batter; use career SR as historical proxy.
        batter_stats = db.get_batter_stats(batter_name, format_, db_path)
        batter_balls_this_innings = batter_info.get("balls", -1)
        is_new_batter = batter_balls_this_innings == 0

        for bowler_info in remaining_bowlers[:3]:
            bowler_name = bowler_info.get("name", "")
            if not bowler_name:
                continue

            # H2H lookup
            h2h = db.get_head_to_head(batter_name, bowler_name, format_, min_balls=5, db_path=db_path)
            bowler_stats = db.get_bowler_stats(bowler_name, format_, db_path)  # noqa: F841

            # Compute signal from available data
            h2h_signal = 0.0
            if h2h and h2h.get("sr"):
                # SR 100 = neutral; > 100 = batting advantage
                h2h_signal = (h2h["sr"] - 120.0) / 60.0  # normalize around 120 SR
                notes.append(f"{batter_name} vs {bowler_name}: SR {h2h['sr']:.0f}")

            # Phase-specific batter SR — pulled from career stats, NOT current innings.
            # This is the correct behaviour for a new batter with 0 balls faced.
            phase_sr_signal = 0.0
            if batter_stats:
                sr_key = {"powerplay": "sr_powerplay", "middle": "sr_middle", "death": "sr_death"}.get(phase)
                batter_phase_sr = batter_stats.get(sr_key) if sr_key else None
                if batter_phase_sr:
                    phase_sr_signal = (batter_phase_sr - 130.0) / 60.0
                    if is_new_batter and not h2h:
                        notes.append(
                            f"{batter_name} (new batter — 0 balls, using career {phase} SR {batter_phase_sr:.0f})"
                        )
            elif is_new_batter:
                # New batter, no career stats found — use neutral signal rather than 0 SR
                notes.append(f"{batter_name} (new batter, no career stats in DB — neutral signal)")

            combined = (h2h_signal * 0.6 + phase_sr_signal * 0.4) if h2h else phase_sr_signal
            signals.append(combined)

    if not signals:
        return 0.0, "No h2h/stats data"

    avg_signal = sum(signals) / len(signals)
    avg_signal = max(-1.0, min(1.0, avg_signal))
    return avg_signal, " | ".join(notes[:2]) if notes else "Stats-based matchup"


def calc_available_resources(
    wickets_remaining: int,
    remaining_batters: list[dict],
    format_: str,
    phase: str,
    db_path: Optional[str] = None,
) -> tuple[float, str]:
    """
    COMBINED metric: wickets + batting depth quality.
    Never count separately to avoid double-counting.
    resources_pct = (wickets/10)*0.4 + quality_score*0.6
    signal = (resources_pct - 0.5) * 2  →  [-1, +1]
    """
    wicket_score = (wickets_remaining / 10.0) * config.RESOURCES_WEIGHTS["wickets_weight"]

    # Quality of remaining batters based on death SR
    quality_scores = []
    tiers = config.BATTER_QUALITY_TIERS
    for batter_info in remaining_batters:
        bname = batter_info.get("name", "")
        if not bname:
            continue
        bstats = db.get_batter_stats(bname, format_, db_path)
        death_sr = bstats.get("sr_death") if bstats else None
        if death_sr:
            for tier_name in ["elite", "good", "average", "lower"]:
                tier = tiers[tier_name]
                if death_sr >= tier["min_sr"]:
                    quality_scores.append(tier["quality_score"])
                    break
        else:
            quality_scores.append(0.4)  # unknown batter: slightly below average

    avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0.4
    quality_contribution = avg_quality * config.RESOURCES_WEIGHTS["batting_quality"]

    resources_pct = wicket_score + quality_contribution
    signal = (resources_pct - 0.5) * 2.0  # normalize to [-1, +1]
    signal = max(-1.0, min(1.0, signal))
    desc = f"{wickets_remaining} wkts, quality={avg_quality:.2f}"
    return signal, desc


def calc_pitch_weather(
    pitch_type: str,
    weather_condition: str,
    dew_factor: bool,
    venue_slowdown_pct: Optional[float],
) -> tuple[float, str]:
    """
    Pitch + weather combined signal.
    Flat pitch + dew = batting friendly = positive signal.
    Seam/damp + no dew = bowling friendly = negative signal.
    """
    pitch_scores = {
        "flat":  0.6,
        "spin":  0.1,
        "seam": -0.3,
        "damp": -0.5,
    }
    weather_scores = {
        "day":     0.0,
        "evening": 0.2,  # dew possibility
        "dew":     0.5,
    }

    pitch_score   = pitch_scores.get(pitch_type.lower(), 0.0)
    weather_score = weather_scores.get(weather_condition.lower(), 0.0)
    dew_bonus     = 0.3 if dew_factor else 0.0

    # If venue historically slows down a lot in late overs, negative
    venue_penalty = 0.0
    if venue_slowdown_pct and venue_slowdown_pct > 0.20:
        venue_penalty = -0.3

    signal = pitch_score + weather_score + dew_bonus + venue_penalty
    signal = max(-1.0, min(1.0, signal))
    desc = f"Pitch={pitch_type}, weather={weather_condition}, dew={dew_factor}"
    return signal, desc


# Format-specific baselines for team strength normalization.
# Each entry: (avg_innings_total, typical_weighted_economy)
# Weighted economy = (pp*6 + mid*10 + death*4) / 20  at format average.
# These anchor the signal at 0.0 for a typical team in that format.
_FORMAT_TEAM_STRENGTH_BASELINES: dict[str, tuple[float, float]] = {
    "Women's T20I": (120.0, 6.30),  # Women's T20I: avg ~118-123, eco ~6.2-6.5
    "T20I":         (155.0, 7.50),  # Men's T20I:   avg ~155,     eco ~7.5
    "Men's ODI":    (270.0, 5.20),  # Men's ODI:    avg ~270,     eco ~5.2
    "Women's ODI":  (225.0, 4.80),  # Women's ODI:  avg ~225,     eco ~4.8
}

# Spread constants: how many runs / eco units = a "1-sigma" move from baseline.
# Larger spread = smaller signal for the same absolute gap.
_FORMAT_TEAM_STRENGTH_SPREAD: dict[str, tuple[float, float]] = {
    "Women's T20I": (20.0, 0.60),  # 20 runs or 0.6 eco = strong differential
    "T20I":         (25.0, 1.00),
    "Men's ODI":    (35.0, 0.60),
    "Women's ODI":  (30.0, 0.55),
}

# Plausible weighted-economy band per format. A figure outside this band is
# almost always a low-sample / manual-scorecard artefact (e.g. England Women
# weighted eco 10.4 from a single hammered innings) and must NOT be trusted —
# letting it through makes an elite attack read as the weakest in the world.
_FORMAT_ECO_PLAUSIBLE: dict[str, tuple[float, float]] = {
    "Women's T20I": (4.5, 8.5),
    "T20I":         (5.5, 10.0),
    "Men's ODI":    (3.5, 7.5),
    "Women's ODI":  (3.0, 7.0),
}

# When a bowling row's economy is unreliable, an ELITE_TEAMS attack defaults to
# this strength (positive = tighter than baseline) instead of neutral, so a
# genuinely top-tier attack is never mistaken for a weak one on bad data.
_ELITE_BOWLING_POWER = 0.65

# Recency-trap discount. A weaker side's batting average is often inflated by
# runs piled up against weak attacks; that surplus does not transfer against a
# genuinely strong attack. We subtract this fraction of the opposing attack's
# strength from the batting surplus. Only ever tempers an unproven surplus —
# never inflates a real one and never penalises an already weak batting line.
_TEAM_STRENGTH_RECENCY_PENALTY = 1.0


def _bowling_power(
    bowl_eco: float,
    base_eco: float,
    spread_eco: float,
    bowling_team: str,
    format_: str,
) -> tuple[float, str]:
    """Strength of a bowling attack as a [-1, +1] signal.

    POSITIVE = tighter than baseline economy = strong attack that SUPPRESSES runs.
    NEGATIVE = leakier than baseline = weak attack that helps the batting side.

    Implausible economies (low-sample / manual-scorecard artefacts) are rejected
    so a garbage figure can never flip the matchup: an ELITE_TEAMS attack falls
    back to a strong default, everyone else to neutral.
    """
    lo, hi = _FORMAT_ECO_PLAUSIBLE.get(format_, (4.5, 11.0))
    if not (lo <= bowl_eco <= hi):
        if bowling_team in db.ELITE_TEAMS:
            return _ELITE_BOWLING_POWER, f"eco {bowl_eco:.1f} unreliable → elite attack assumed strong"
        return 0.0, f"eco {bowl_eco:.1f} unreliable → neutral"
    power = (base_eco - bowl_eco) / spread_eco  # POSITIVE = tighter than baseline
    power = max(-1.0, min(1.0, power))
    return power, f"eco {bowl_eco:.1f} (base {base_eco:.1f})"


def _robust_bowling_eco(bowl_stats: Optional[dict], bowling_team: str, format_: str) -> float:
    """Weighted economy with implausible (garbage) figures corrected.

    Used by the projection so a single hammered innings (England Women eco 10.4)
    can't inflate the projected total. Implausible elite-attack economies snap to
    a strong baseline; other implausible economies snap to the format-neutral eco.
    """
    eco = _bowling_team_eco(bowl_stats)
    lo, hi = _FORMAT_ECO_PLAUSIBLE.get(format_, (4.5, 11.0))
    if lo <= eco <= hi:
        return eco
    if bowling_team in db.ELITE_TEAMS:
        base_eco = _FORMAT_TEAM_STRENGTH_BASELINES.get(format_, (155.0, 7.50))[1]
        spread_eco = _FORMAT_TEAM_STRENGTH_SPREAD.get(format_, (25.0, 1.00))[1]
        return base_eco - _ELITE_BOWLING_POWER * spread_eco  # elite attack → tighter than neutral
    return _FORMAT_NEUTRAL_BOWL_ECO.get(format_, 7.50)


def calc_team_strength(
    batting_team: str,
    bowling_team: str,
    format_: str,
    db_path: Optional[str] = None,
) -> tuple[float, str]:
    """
    Matchup Metric: compare BOTH teams against their format baseline,
    then take the net differential.

    Positive  → batting team stronger than typical / bowling team weaker → OVER
    Negative  → batting team weaker than typical  / bowling team stronger → UNDER

    FIX: previously used Men's T20I hardcoded constants (avg_total=140, eco=7.5)
    which made every Women's T20I batting team look weak (avg ~112 << 140) and
    every bowling team look elite (eco ~6.3 << 7.5).  The two errors nearly
    cancelled each other, producing near-zero signal for all matchups.

    Now uses format-specific baselines so the signal correctly differentiates:
      India Women (avg 112) → below Women's T20I baseline (120) → batting_signal < 0
      NZ Women   (avg 134) → above Women's T20I baseline (120) → batting_signal > 0
      Pakistan bowling (eco 6.20 < 6.30 baseline) → slightly tougher than avg
      Sri Lanka  bowling (eco 6.27 ≈ 6.30 baseline) → near-average
    """
    bat_stats = db.get_team_batting_stats(batting_team, format_, db_path=db_path)
    bow_stats  = db.get_team_bowling_stats(bowling_team, format_, db_path=db_path)

    if not bat_stats or not bow_stats:
        return 0.0, "No team strength data"

    avg_total = bat_stats.get("avg_total") or 0.0
    bowl_eco  = _bowling_team_eco(bow_stats)

    # Look up format-specific baselines (fall back to Men's T20I if unknown)
    base_total, base_eco = _FORMAT_TEAM_STRENGTH_BASELINES.get(
        format_, (155.0, 7.50)
    )
    spread_total, spread_eco = _FORMAT_TEAM_STRENGTH_SPREAD.get(
        format_, (25.0, 1.00)
    )

    # ── Batting power: above/below the format average (positive = strong) ──
    # Clamp INDIVIDUALLY so one component can never dominate the average below.
    batting_power = (avg_total - base_total) / spread_total if avg_total > 0 else 0.0
    batting_power = max(-1.0, min(1.0, batting_power))

    # ── Bowling power: POSITIVE = strong (tight) attack that SUPPRESSES runs ──
    # Rejects implausible economies (garbage low-sample rows) so an elite attack
    # is never read as weak. (Issue 1 root cause: England Women eco 10.4.)
    bowling_power, bowl_note = _bowling_power(
        bowl_eco, base_eco, spread_eco, bowling_team, format_
    )

    # ── Recency trap (Issue 2) ───────────────────────────────────────────
    # A weaker side's batting average is often inflated by runs piled up against
    # weak attacks. That surplus does not transfer against a genuinely strong
    # attack — discount the above-baseline batting surplus in proportion to how
    # strong the opposing attack is. Only tempers an unproven surplus.
    if batting_power > 0 and bowling_power > 0:
        batting_power = max(
            -1.0, batting_power - _TEAM_STRENGTH_RECENCY_PENALTY * bowling_power
        )

    # ── Net: batting power MINUS bowling power (Issue 1) ──────────────────
    # Weak batting (≤0) vs elite bowling (bowling_power > 0) → guaranteed negative.
    signal = (batting_power - bowling_power) / 2.0
    signal = max(-1.0, min(1.0, signal))
    return signal, (
        f"{batting_team} avg {avg_total:.0f} (base {base_total:.0f}, power {batting_power:+.2f}) "
        f"vs {bowling_team} {bowl_note} (power {bowling_power:+.2f})"
    )


def calc_boundary_pct(
    recent_overs: list[list[str]],
) -> tuple[float, str]:
    """
    Boundary % in last 3 overs.
    High boundary % → positive signal (batting in form).
    """
    all_balls = [b for over in recent_overs for b in over]
    if not all_balls:
        return 0.0, "No recent over data"

    _extras = {"WD", "WIDE", "NB", "NOBALL"}
    legal_balls = [b for b in all_balls if b.strip().upper() not in _extras]
    boundaries  = [b for b in legal_balls if b.strip() in ("4", "6")]
    if not legal_balls:
        return 0.0, "No legal balls in recent overs"

    boundary_pct = len(boundaries) / len(legal_balls)
    # Typical boundary rate ~25%, normalize around that
    signal = (boundary_pct - 0.25) / 0.25
    signal = max(-1.0, min(1.0, signal))
    return signal, f"Boundary% last 3 overs: {boundary_pct*100:.0f}%"


def calc_dot_ball_pct(
    recent_overs: list[list[str]],
) -> tuple[float, str]:
    """
    Dot ball % in last 3 overs.
    High dot % → NEGATIVE signal (batting under pressure).
    Returns an inverted signal (more dots = more negative).
    """
    all_balls = [b for over in recent_overs for b in over]
    if not all_balls:
        return 0.0, "No recent over data"

    legal_balls = [b for b in all_balls if b.strip().upper() not in {"WD", "WIDE", "NB", "NOBALL"}]
    dots = [b for b in legal_balls if b.strip() == "0"]
    if not legal_balls:
        return 0.0, "No legal balls"

    dot_pct = len(dots) / len(legal_balls)
    # Typical dot rate ~30%, normalize and invert
    signal = -((dot_pct - 0.30) / 0.30)  # more dots than normal = negative
    signal = max(-1.0, min(1.0, signal))
    return signal, f"Dot ball% last 3 overs: {dot_pct*100:.0f}%"


def calc_toss(
    batting_team: str,
    toss_winner: str,
    toss_choice: str,
    innings: int,
) -> tuple[float, str]:
    """
    Toss advantage: very small signal (max ±2% in PP, 0 in death).
    Positive if toss winner chose to bat and is batting, or chose field and chasing.
    """
    if not toss_winner or not toss_choice:
        return 0.0, "No toss data"
    batting_by_choice = (toss_winner == batting_team and toss_choice.lower() == "bat") or \
                         (toss_winner != batting_team and toss_choice.lower() == "field")
    signal = 0.5 if batting_by_choice else -0.3
    return signal, f"Toss: {toss_winner} chose {toss_choice}"


def calc_death_bowler_quota(
    remaining_bowlers: list[dict],
    overs_remaining: int,
    format_: str,
    db_path: Optional[str] = None,
) -> tuple[float, str]:
    """
    Death bowler quota analysis.
    Elite bowlers with full quota remaining = negative (harder to score).
    Part-timers forced to bowl death = positive.

    Positive signal → OVER favored (weak bowling).
    Negative signal → UNDER favored (elite bowling remaining).
    """
    elite_threshold = config.MODIFIER_PARAMS["bowling_quota_trap"]["elite_death_economy_threshold"]
    elite_with_quota = 0
    part_timers_needed = 0

    for b in remaining_bowlers:
        bname  = b.get("name", "")
        quota  = b.get("overs_remaining", 0)
        if quota < 1:
            continue
        bstats = db.get_bowler_stats(bname, format_, db_path) if bname else None
        role   = db.classify_death_bowler_role(bstats, elite_threshold)

        if role == "elite":
            elite_with_quota += 1
        elif role == "part_timer":
            part_timers_needed += 1

    # Signal: more elite overs remaining = negative, more part-timers = positive
    signal = (part_timers_needed * 0.3 - elite_with_quota * 0.4)
    signal = max(-1.0, min(1.0, signal))
    desc = f"Elite bowlers remaining: {elite_with_quota}, part-timers: {part_timers_needed}"
    return signal, desc


# ─────────────────────────────────────────────────────────────────
# CHASING FACTOR
# ─────────────────────────────────────────────────────────────────

def calc_rrr_adjustment(required_run_rate: float, format_: str = "") -> tuple[float, str]:
    """
    Required Run Rate adjustment for chasing innings.
    Returns a direct adjustment in percentage points (not a signal).
    Uses ODI thresholds for ODI formats, T20 thresholds otherwise.
    """
    is_odi = format_ in config.ODI_FORMATS if format_ else False
    cfg = config.CHASING_RRR_ADJUSTMENTS_ODI if is_odi else config.CHASING_RRR_ADJUSTMENTS
    
    if required_run_rate < cfg["rrr_easy"]["max_rrr"]:
        adj = cfg["rrr_easy"]["adjustment"]
        easy_threshold = cfg["rrr_easy"]["max_rrr"]
        label = f"RRR {required_run_rate:.1f} < {easy_threshold} (Easy chase)"
    elif required_run_rate < cfg["rrr_neutral"]["max_rrr"]:
        adj = cfg["rrr_neutral"]["adjustment"]
        label = f"RRR {required_run_rate:.1f} {cfg['rrr_easy']['max_rrr']}-{cfg['rrr_neutral']['max_rrr']} (Neutral)"
    elif required_run_rate < cfg["rrr_hard"]["max_rrr"]:
        adj = cfg["rrr_hard"]["adjustment"]
        label = f"RRR {required_run_rate:.1f} {cfg['rrr_neutral']['max_rrr']}-{cfg['rrr_hard']['max_rrr']} (Hard chase)"
    else:
        adj = cfg["rrr_extreme"]["adjustment"]
        label = f"RRR {required_run_rate:.1f} > {cfg['rrr_hard']['max_rrr']} (Extreme)"
    return adj, label


# ─────────────────────────────────────────────────────────────────
# MAIN CALCULATION
# ─────────────────────────────────────────────────────────────────

def calculate_probability(
    # Match context
    innings: int,
    format_: str,
    over_number: float,
    current_runs: int,
    wickets_fallen: int,
    line: float,
    # Teams
    batting_team: str,
    bowling_team: str,
    venue: str,
    # Live figures
    batsmen: list[dict],          # [{"name": ..., "runs": ..., "balls": ...}]
    all_bowlers: list[dict],       # [{"name": ..., "overs": ..., "runs": ..., "wickets": ..., "today": True}]
    remaining_bowlers: list[dict], # bowlers with overs_remaining > 0
    remaining_batters: list[dict], # [{"name": ..., "position": ...}]
    recent_overs: list[list[str]], # last 3 overs ball-by-ball
    # Conditions
    pitch_type: str = "flat",
    weather_condition: str = "day",
    dew_factor: bool = False,
    # Chasing
    target: Optional[int] = None,
    # Toss
    toss_winner: str = "",
    toss_choice: str = "",
    # Partnership
    partnership_runs: int = 0,
    partnership_balls: int = 0,
    # Market (so the base guardrail can scale its range check to the market)
    market_type: str = "",
    # DB
    db_path: Optional[str] = None,
) -> dict:
    """
    Main probability calculation.
    Returns a rich dict with base_prob, adjustments, modifiers, final_prob, verdict.
    All features are separate clean inputs (XGBoost-ready).
    """
    phase = get_phase(over_number, format_)
    max_overs = config.MAX_OVERS.get(format_, 20)
    overs_remaining = max_overs - over_number
    wickets_remaining = 10 - wickets_fallen
    current_rr = (current_runs / over_number) if over_number > 0 else 0

    # Par RR default changes for ODI
    default_par_rr = 5.5 if format_ in config.ODI_FORMATS else 7.0

    # ── STEP 1: Baseline ──────────────────────────────────────────
    base_prob, base_source, position_debug, base_std_dev = get_base_probability(
        innings, format_, over_number, wickets_fallen, current_runs, line, db_path,
        batting_team=batting_team,
        bowling_team=bowling_team,
        venue=venue,
        target=target,
        market_type=market_type,
    )

    # In death overs, cap base contribution at DEATH_BASE_CAP_PCT (live signals dominate)
    base_prob_effective = base_prob
    if phase == "death":
        cap = config.DEATH_BASE_CAP_PCT / 100.0
        base_prob_effective = base_prob * cap

    # ── STEP 2: Compute venue par RR ─────────────────────────────
    # FIX (Issue 1): track whether venue was found so result can surface a clear message.
    par_rr = None
    venue_stats = db.get_venue_stats(venue, format_, db_path)
    venue_found_in_db: bool = venue_stats is not None if (venue and venue not in ("", "Unknown")) else True
    if venue_stats and venue_stats.get("avg_runs_per_over") and db.venue_has_reliable_rpo(venue_stats):
        over_idx = min(cricket_over_to_db_over(over_number), max_overs - 1)
        per_over = venue_stats["avg_runs_per_over"]
        if per_over and over_idx < len(per_over) and per_over[over_idx] > 0:
            par_runs_to_now = sum(x for x in per_over[:over_idx + 1] if x > 0)
            valid_overs = sum(1 for x in per_over[:over_idx + 1] if x > 0)
            if valid_overs > 0:
                par_rr = par_runs_to_now / valid_overs
    if par_rr is None:
        par_rr = default_par_rr  # 5.5 for ODI, 7.0 for T20

    venue_slowdown = None
    if venue_stats and db.venue_has_reliable_modifiers(venue_stats):
        venue_slowdown = venue_stats.get("historical_slowdown_pct")

    # ── STEP 3: Calculate all factor signals ─────────────────────
    adjustments = {}

    # 1. Momentum (skip at pre-match — no live run rate yet)
    if over_number < 1.0 and current_runs == 0:
        adjustments["momentum"] = {
            "signal": 0.0,
            "adj":    0.0,
            "desc":   "Pre-match — neutral momentum",
        }
    else:
        mom_signal, mom_desc = calc_momentum(current_rr, par_rr)
        adjustments["momentum"] = {
            "signal": mom_signal,
            "adj":    scale_adjustment(mom_signal, phase, "momentum"),
            "desc":   mom_desc,
        }

    # 2. Partnership rate
    part_signal, part_desc = calc_partnership_rate(partnership_runs, partnership_balls)
    adjustments["partnership_rate"] = {
        "signal": part_signal,
        "adj":    scale_adjustment(part_signal, phase, "partnership_rate"),
        "desc":   part_desc,
    }

    # 3. Batter vs bowler matchup
    bb_signal, bb_desc = calc_batter_bowler_matchup(batsmen, remaining_bowlers, format_, phase, db_path)
    adjustments["batter_bowler"] = {
        "signal": bb_signal,
        "adj":    scale_adjustment(bb_signal, phase, "batter_bowler"),
        "desc":   bb_desc,
    }

    # 4. Available resources (COMBINED wickets + batting depth)
    res_signal, res_desc = calc_available_resources(
        wickets_remaining, remaining_batters, format_, phase, db_path
    )
    adjustments["available_resources"] = {
        "signal": res_signal,
        "adj":    scale_adjustment(res_signal, phase, "available_resources"),
        "desc":   res_desc,
    }

    # 5. Pitch + weather
    pitch_signal, pitch_desc = calc_pitch_weather(
        pitch_type, weather_condition, dew_factor, venue_slowdown
    )
    adjustments["pitch_weather"] = {
        "signal": pitch_signal,
        "adj":    scale_adjustment(pitch_signal, phase, "pitch_weather"),
        "desc":   pitch_desc,
    }

    # 6. Team strength
    ts_signal, ts_desc = calc_team_strength(batting_team, bowling_team, format_, db_path)
    adjustments["team_strength"] = {
        "signal": ts_signal,
        "adj":    scale_adjustment(ts_signal, phase, "team_strength"),
        "desc":   ts_desc,
    }

    # 7. Boundary %
    bnd_signal, bnd_desc = calc_boundary_pct(recent_overs)
    adjustments["boundary_pct"] = {
        "signal": bnd_signal,
        "adj":    scale_adjustment(bnd_signal, phase, "boundary_pct"),
        "desc":   bnd_desc,
    }

    # 8. Dot ball %
    dot_signal, dot_desc = calc_dot_ball_pct(recent_overs)
    adjustments["dot_ball_pct"] = {
        "signal": dot_signal,
        "adj":    scale_adjustment(dot_signal, phase, "dot_ball_pct"),
        "desc":   dot_desc,
    }

    # 9. Toss
    toss_signal, toss_desc = calc_toss(batting_team, toss_winner, toss_choice, innings)
    adjustments["toss"] = {
        "signal": toss_signal,
        "adj":    scale_adjustment(toss_signal, phase, "toss"),
        "desc":   toss_desc,
    }

    # 10. Death bowler quota
    db_signal, db_desc = calc_death_bowler_quota(remaining_bowlers, int(overs_remaining), format_, db_path)
    adjustments["death_bowler_quota"] = {
        "signal": db_signal,
        "adj":    scale_adjustment(db_signal, phase, "death_bowler_quota"),
        "desc":   db_desc,
    }

    # Sum all adjustments
    total_adj = sum(a["adj"] for a in adjustments.values())

    # Chasing: add RRR factor
    rrr_adj = 0.0
    rrr_desc = ""
    if innings == 2 and target is not None and target > 0:
        runs_needed = target - current_runs
        rrr = (runs_needed / overs_remaining) if overs_remaining > 0 else 99.0
        rrr_adj, rrr_desc = calc_rrr_adjustment(rrr, format_)
        adjustments["rrr_factor"] = {
            "signal": rrr_adj / 18.0,  # normalized for display
            "adj":    rrr_adj,
            "desc":   rrr_desc,
        }
        total_adj += rrr_adj

    # Post-adjustment probability (before modifiers)
    pre_modifier_prob = base_prob_effective + total_adj

    # ── Venue-not-found warning (Issue 1) ────────────────────────
    venue_warning = ""
    if not venue_found_in_db:
        venue_warning = (
            f"⚠️ Venue '{venue}' not found in DB — using format average ({format_})"
        )

    return {
        "base_probability":   round(base_prob, 1),
        "base_probability_effective": round(base_prob_effective, 1),
        "base_source":        base_source,
        "position_debug":     position_debug,
        "phase":              phase,
        "adjustments":        adjustments,
        "total_adjustment":   round(total_adj, 1),
        "pre_modifier_prob":  round(pre_modifier_prob, 1),
        # modifiers will be applied by innings_types.py and filled in
        "modifiers_applied":  [],
        "modifier_total":     0.0,
        "final_probability":  round(pre_modifier_prob, 1),
        "verdict":            _get_verdict(pre_modifier_prob),
        "base_std_dev":       base_std_dev,  # std_dev used in fallback path (0.0 if historical)
        "venue_found":        venue_found_in_db,
        "venue_warning":      venue_warning,
        "degraded_mode":      position_debug.get("degraded_mode", False),
        "status":             position_debug.get("status", ""),
        # Clean feature inputs for future XGBoost migration
        "features": {
            "innings":           innings,
            "format":            format_,
            "over":              over_number,
            "phase":             phase,
            "runs":              current_runs,
            "wickets":           wickets_fallen,
            "line":              line,
            "current_rr":        round(current_rr, 2),
            "par_rr":            round(par_rr, 2),
            "momentum_signal":   adjustments["momentum"]["signal"],
            "resources_signal":  adjustments["available_resources"]["signal"],
            "matchup_signal":    adjustments["batter_bowler"]["signal"],
            "pitch_signal":      adjustments["pitch_weather"]["signal"],
            "boundary_signal":   adjustments["boundary_pct"]["signal"],
            "dot_signal":        adjustments["dot_ball_pct"]["signal"],
            "death_bowler_sig":  adjustments["death_bowler_quota"]["signal"],
            "team_strength_sig": adjustments["team_strength"]["signal"],
        },
    }


def get_verdict_category(prob: float) -> str:
    """
    Unified confidence categorization based on the probability of finishing
    ABOVE target. Returns the internal category key (e.g., "LEAN_OVER",
    "TOSS_UP") — user-facing labels are neutral (see get_verdict_display).
    This is the SINGLE SOURCE OF TRUTH for confidence-tier calculation.
    """
    ranges = config.VERDICT_RANGES
    if prob >= ranges["STRONG_OVER"][0]:
        return "STRONG_OVER"
    elif prob >= ranges["VALUE_OVER"][0]:
        return "VALUE_OVER"
    elif prob >= ranges["LEAN_OVER"][0]:
        return "LEAN_OVER"
    elif prob >= ranges["TOSS_UP"][0]:
        return "TOSS_UP"
    elif prob >= ranges["LEAN_UNDER"][0]:
        return "LEAN_UNDER"
    elif prob >= ranges["VALUE_UNDER"][0]:
        return "VALUE_UNDER"
    else:
        return "STRONG_UNDER"


def get_verdict_display(prob: float) -> str:
    """
    Unified forecast-label text based on the probability of finishing ABOVE target.
    Returns the human-readable label (e.g., "⚡ HIGH CONFIDENCE — ABOVE TARGET").
    This must be used consistently across badge, insight, history, and analytics.
    """
    category = get_verdict_category(prob)
    return config.VERDICT_DISPLAY_LABELS.get(category, "🔄 TOO CLOSE TO CALL")


def _get_verdict(prob: float) -> str:
    """Legacy verdict function - deprecated, use get_verdict_display instead."""
    return get_verdict_display(prob)


# Format-average middle-overs RPO by format (used for probability-floor sanity check).
_FORMAT_AVG_MIDDLE_RPO = {
    "Women's T20I": 6.2,
    "T20I":         7.0,
    "Men's ODI":    4.8,
    "Women's ODI":  4.5,
}
_FORMAT_AVG_RPO_BY_PHASE = {
    "Women's T20I": {"powerplay": 6.8, "middle": 6.2, "death": 8.5},
    "T20I":         {"powerplay": 7.5, "middle": 7.0, "death": 9.0},
    "Men's ODI":    {"powerplay": 5.8, "middle": 4.8, "death": 8.2},
    "Women's ODI":  {"powerplay": 5.5, "middle": 4.5, "death": 7.8},
}
# Probability floor applied when the target score implies a required RPO
# BELOW the format average for the current phase — i.e. an "easy" line.
_EASY_LINE_PROB_FLOOR = 35.0


def apply_final_cap(result: dict) -> dict:
    """Apply [5%, 95%] hard cap, Z-score confidence gate, and easy-target
    probability floor, then recompute the forecast label.

    For full-innings scenarios where the fallback estimation path was used
    (base_std_dev > 0), apply the same Z-score gate as interval scenarios:
      Z = (expected_runs - target) / sigma
      Z >= +0.75  -> HIGH CONFIDENCE (ABOVE target)
      Z <= -0.75  -> HIGH CONFIDENCE (BELOW target)
      |Z| < 0.75  -> LOW CONFIDENCE

    FIX (Issue 3): Sanity floor.
    If (target - current_runs) / remaining_overs < format_avg_phase_rpo,
    the target is BELOW average — it should be easy to reach — so probability
    must be >= 35%.  Prevents mathematically nonsensical sub-10% readings
    on easy targets caused by compounding negative modifiers.
    """
    # ── DEGRADED-MODE BACKSTOP ───────────────────────────────────
    # Baseline was invalid (zero / out-of-range / no segment data). Refuse to emit
    # a confident number: force a neutral readout, zero the edge, and surface the
    # explicit status flag so broken math can never masquerade as a 95–100% signal.
    if result.get("degraded_mode"):
        result["final_probability"] = 50.0
        result["pre_modifier_prob"] = 50.0
        result["total_adjustment"]  = 0.0
        result["modifier_total"]    = 0.0
        result["edge_pct"]          = 0.0
        result["status"]            = result.get("status") or DEGRADED_STATUS
        result["verdict"]           = INSUFFICIENT_DATA
        return result

    prob = result["final_probability"]
    prob = max(config.PROBABILITY_MIN, min(config.PROBABILITY_MAX, prob))

    # ── Sanity floor: easy-line protection (Issue 3) ──────────────
    features   = result.get("features", {})
    format_    = features.get("format", "")
    phase      = features.get("phase", "middle")
    line_val   = features.get("line", 0.0)
    current_runs = features.get("runs", 0)
    over_number  = features.get("over", 0.0)
    max_overs_fmt = config.MAX_OVERS.get(format_, 20)
    remaining_overs = max(0.0, max_overs_fmt - over_number)

    # Segment (powerplay) markets are scored against the 6-over total, so the
    # full-innings easy-line floor (which divides by ~18 remaining overs) does
    # not apply — the historical segment base rate already prices the line.
    is_segment = result.get("position_debug", {}).get("segment") == "powerplay"

    if not is_segment and remaining_overs > 0 and line_val > 0:
        runs_still_needed = max(0.0, line_val - current_runs)
        implied_rpo = runs_still_needed / remaining_overs
        phase_avg_rpo = _FORMAT_AVG_RPO_BY_PHASE.get(format_, {}).get(phase, 6.2)
        if implied_rpo < phase_avg_rpo:
            # Easy line — probability floor
            if prob < _EASY_LINE_PROB_FLOOR:
                prob = _EASY_LINE_PROB_FLOOR
                result["sanity_floor_applied"] = (
                    f"Easy-line floor {_EASY_LINE_PROB_FLOOR:.0f}%: "
                    f"implied RPO {implied_rpo:.1f} < format avg {phase_avg_rpo:.1f} ({phase})"
                )

    std_dev = result.get("base_std_dev", 0.0)
    projected_total = result.get("position_debug", {}).get("projected_total")
    if std_dev > 0 and projected_total is not None:
        # mu must be the projected RUN TOTAL — same units as line_val and
        # std_dev (runs). Previously this used pre_modifier_prob, which is a
        # probability %, so z = (prob - target)/sigma was meaningless (always
        # hugely negative for full-innings run targets) and the low-confidence /
        # too-close branches below never executed.
        # ── TARGET-ANCHORED PROBABILITY (catastrophic-decoupling fix) ────────
        # The displayed above-target probability MUST sit on the correct side of
        # 50% relative to the projection-vs-target delta:
        #     projection == target  ->  50%
        #     projection <  target  ->  above-target prob < 50%  (below-target forecast)
        #     projection >  target  ->  above-target prob > 50%
        #
        # The calibrated sigmoid of z = (projection - target) / sigma is the
        # SOURCE OF TRUTH for both direction and base magnitude. This is exactly
        # the mapping the out-of-sample calibration tuned — sigma (18 / 35 / 12)
        # is left untouched, so calibration is preserved; we only stop downstream
        # signals from flipping the side of the target.
        #
        # Live adjustments + modifiers (already folded into `prob`) may only ADD
        # conviction in the projection's direction; they can NEVER push the
        # forecast to the wrong side of the target. Previously this gate read
        # `prob` straight through (and the |z| < 1 branch clamped symmetrically to
        # [35, 65]), so a bullish-modifier prob (e.g. 78%) survived as an
        # above-target forecast even when the projection sat well BELOW the
        # target — the bug QA reported.
        mu = projected_total
        z = (mu - line_val) / std_dev
        line_prob = _sigmoid(z) * 100.0   # calibrated, strictly correct side of 50%

        if z > 0:
            prob = max(line_prob, prob)   # above-target: never below the calibrated prob
        elif z < 0:
            prob = min(line_prob, prob)   # below-target: never above the calibrated prob
        else:
            prob = 50.0                   # projection == target -> exactly 50%

        # Low-conviction damping: within 1 sigma of the target the separation is
        # weak, so pull the modifier-inflated magnitude back toward the calibrated
        # probability — while always remaining strictly on the correct side of 50%.
        Z_DAMP = 1.0
        if 0 < abs(z) < Z_DAMP:
            prob = (prob + line_prob) / 2.0

        prob = max(config.PROBABILITY_MIN, min(config.PROBABILITY_MAX, prob))
        result["final_probability"] = round(prob, 1)
        result["verdict"] = get_verdict_display(prob)
        result["line_anchor"] = {
            "z": round(z, 3),
            "line_prob": round(line_prob, 1),
            "projection": round(mu, 1),
            "line": line_val,
        }
        return result

    result["final_probability"] = round(prob, 1)
    result["verdict"] = get_verdict_display(prob)
    return result

