"""
model/innings_types.py — CricEdge Innings Model Instances

Two separate model instances: INNINGS_1 and CHASING.
Never mix their weights or logic.
Each exposes a single .analyse() method that returns the full result dict.
"""

import logging
import re
from typing import Optional
from datetime import datetime

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from model.probability import calculate_probability, apply_final_cap
from model.modifiers import apply_all_modifiers
from data.database import save_prediction

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# SHARED ANALYSIS CORE
# ─────────────────────────────────────────────────────────────────

def _analyse(
    innings: int,
    format_: str,
    over_number: float,
    current_runs: int,
    wickets_fallen: int,
    line: float,
    batting_team: str,
    bowling_team: str,
    venue: str,
    batsmen: list[dict],
    all_bowlers: list[dict],
    remaining_bowlers: list[dict],
    remaining_batters: list[dict],
    recent_overs: list[list[str]],
    balls_this_innings: Optional[list[dict]] = None,
    pitch_type: str = "flat",
    weather_condition: str = "day",
    dew_factor: bool = False,
    target: Optional[int] = None,
    toss_winner: str = "",
    toss_choice: str = "",
    partnership_runs: int = 0,
    partnership_balls: int = 0,
    match_label: str = "",
    all_scorers: Optional[list[dict]] = None,
    market_type: str = "",
    save_to_db: bool = True,
    db_path: Optional[str] = None,
) -> dict:
    """Full analysis pipeline: probability → modifiers → verdict → save.

    Optional live inputs (conditions, toss, partnership, ball-by-ball) default to
    neutral values so callers — including pre-match predictions and tests — can
    omit them. Mirrors the defaults in calculate_probability.
    """

    balls_this_innings = balls_this_innings or []
    phase = _get_phase(over_number, format_)
    # FIX 1: At exact phase boundaries (6.0, 16.0), modifiers should check
    # the phase we're ENTERING not the one just completed. A market predicting
    # overs 7-10 at current_over=6.0 is a MIDDLE market, not powerplay.
    modifier_phase = _effective_modifier_phase(over_number, format_)
    total_balls = int(over_number) * 6 + round((over_number % 1) * 10)

    # Step 1 + 2: Baseline + signed adjustments
    result = calculate_probability(
        innings=innings,
        format_=format_,
        over_number=over_number,
        current_runs=current_runs,
        wickets_fallen=wickets_fallen,
        line=line,
        batting_team=batting_team,
        bowling_team=bowling_team,
        venue=venue,
        batsmen=batsmen,
        all_bowlers=all_bowlers,
        remaining_bowlers=remaining_bowlers,
        remaining_batters=remaining_batters,
        recent_overs=recent_overs,
        pitch_type=pitch_type,
        weather_condition=weather_condition,
        dew_factor=dew_factor,
        target=target,
        toss_winner=toss_winner,
        toss_choice=toss_choice,
        partnership_runs=partnership_runs,
        partnership_balls=partnership_balls,
        market_type=market_type,
        db_path=db_path,
    )
    # Extract target window if it's a window market (for accurate phase targeting)
    # The analyse function doesn't know the window directly, but for full innings
    # predictions it spans to the end. For UI/market routing, the window overrides 
    # the modifiers later, but let's default to the current effective phase.
    modifier_phase = _effective_modifier_phase(over_number, format_)

    # Step 3: Apply conditional modifiers (use effective boundary phase as default target)
    result = apply_all_modifiers(
        probability_result=result,
        innings=innings,
        batting_team=batting_team,
        bowling_team=bowling_team,
        current_runs=current_runs,
        current_over=over_number,
        wickets_fallen=wickets_fallen,
        total_balls_bowled=total_balls,
        format_=format_,
        target_phase=modifier_phase,  # Default to effective phase, route_market will override if needed
        venue=venue,
        batsmen=batsmen,
        all_bowlers=all_bowlers,
        remaining_bowlers=remaining_bowlers,
        remaining_batters=remaining_batters,
        balls_this_innings=balls_this_innings,
        all_scorers=all_scorers,
        db_path=db_path,
    )

    # Step 4: Hard cap
    result = apply_final_cap(result)

    # Store crease batsmen in result so window markets can apply
    # set-batter dot ball drag reduction (Fix 2)
    result["batsmen"] = batsmen

    # Enrich with display metadata
    result["innings"]         = innings
    result["format"]          = format_
    result["batting_team"]    = batting_team
    result["bowling_team"]    = bowling_team
    result["venue"]           = venue
    result["over_number"]     = over_number
    result["current_runs"]    = current_runs
    result["wickets_fallen"]  = wickets_fallen
    result["line"]            = line
    result["match_label"]     = match_label
    result["timestamp"]       = datetime.now().isoformat()

    # Generate key insight narrative
    result["key_insight"] = _generate_insight(result, batsmen, all_bowlers, venue)

    # Save to predictions table
    if save_to_db:
        pred_id = save_prediction({
            "timestamp":                  result["timestamp"],
            "match":                      match_label,
            "venue":                      venue,
            "format":                     format_,
            "innings":                    innings,
            "over_at_prediction":         over_number,
            "current_score_at_prediction": f"{current_runs}/{wickets_fallen}",
            "line":                       line,
            "predicted_probability":      result["final_probability"],
            "verdict":                    result["verdict"],
            "base_probability":           result["base_probability"],
            "modifiers_fired":            result.get("modifiers_fired", ""),
            "each_adjustment":            {
                k: v["adj"] for k, v in result.get("adjustments", {}).items()
            },
            "notes": "",
        }, db_path)
        result["prediction_id"] = pred_id

    return result


def _get_phase(over: float, format_: str = "") -> str:
    """Return phase for a given over, respecting format boundaries."""
    ov = int(over)
    is_odi = format_ in config.ODI_FORMATS if format_ else False
    bounds = config.PHASE_BOUNDARIES_ODI if is_odi else config.PHASE_BOUNDARIES
    pp_lo, pp_hi = bounds["powerplay"]
    mid_lo, mid_hi = bounds["middle"]
    if pp_lo <= ov <= pp_hi:
        return "powerplay"
    elif mid_lo <= ov <= mid_hi:
        return "middle"
    return "death"


def _effective_modifier_phase(over_number: float, format_: str = "") -> str:
    """
    Boundary-aware phase for modifier evaluation.

    At EXACT phase boundaries (e.g. over 6.0 = end of PP, over 16.0 = end of middle),
    return the phase we are ENTERING rather than the one just completed.

    Why: modifiers evaluate the innings from now forward. At over 6.0 we are
    about to bowl overs 7-10 (middle overs). Modifier checks like spin-death
    mismatch that gate on phase='middle'/'death' would silently skip if we
    pass 'powerplay' just because int(6.0)==6 == pp_hi.

    Examples:
      6.0  → 'middle'   (entering middle overs 7-16)
      6.3  → 'powerplay' (still IN the powerplay)
      16.0 → 'death'    (entering death overs 17-20)
      16.3 → 'middle'   (still IN middle overs)
    """
    phase = _get_phase(over_number, format_)
    # Only apply look-ahead at exact integer boundaries (6.0, 16.0, not 6.3)
    if over_number == int(over_number) and over_number > 0:
        next_phase = _get_phase(over_number + 1, format_)  # +1: look at the NEXT full over
        if next_phase != phase:
            return next_phase  # we're AT the boundary — use the phase we're entering
    return phase


def get_target_phase(market_start: float, market_end: float) -> str:
    """Determine the phase of the target prediction window."""
    if market_end <= 6:
        return 'powerplay'
    elif market_start >= 16:
        return 'death'
    elif market_start >= 7 and market_end <= 15:
        return 'middle'
    else:
        return 'transition'  # spans multiple phases


# ─────────────────────────────────────────────────────────────────
# INSIGHT GENERATION
# ─────────────────────────────────────────────────────────────────

def _generate_insight(result: dict, batsmen: list[dict], all_bowlers: list[dict], venue: str) -> str:
    """Auto-generate human-readable KEY INSIGHT narrative.

    FIX (Issue 1): venue_warning is prepended first so it is always visible.
    FIX (Issue 3): sanity_floor_applied note is appended when the easy-line
                   probability floor was triggered.
    FIX (136.8 bug): For pre-match predictions, show the team/venue-specific
                   projected total (from _estimate_projected_total) rather than
                   the generic elite-tier position bucket mean (always 136.8).
    """
    pos_debug   = result.get("position_debug", {})
    over_number = result.get("over_number", 0.0)
    is_prematch = float(over_number) < 1.0 and int(result.get("current_runs", 1)) == 0

    # Choose the right baseline figure and label
    if is_prematch and pos_debug.get("projected_total"):
        # Team+venue-weighted projection — team-specific, NOT the generic pool mean
        avg           = pos_debug["projected_total"]
        base_label    = "Team/venue projected total"
        base_src_note = pos_debug.get("avg_final_score_source", "team/venue projection")
    else:
        # Live or historical — use position bucket mean
        avg           = pos_debug.get("avg_final_score") or 0
        base_label    = "Historical position bucket mean"
        base_src_note = pos_debug.get("source", "")

    venue_adj = result.get("adjustments", {}).get("pitch_weather", {}).get("adj", 0)
    venue_sign = "+" if venue_adj > 0 else ""

    matchup_adj  = result.get("adjustments", {}).get("team_strength", {}).get("adj", 0)
    matchup_sign = "+" if matchup_adj > 0 else ""

    target_line  = result.get("line", 0)
    prob         = result.get("final_probability", 0)

    # Use unified verdict display function for consistency
    from model.probability import get_verdict_display
    verdict = get_verdict_display(prob)
    lean    = f"{verdict} at {prob:.0f}%"

    batting_team = result.get("batting_team", "")
    bowling_team = result.get("bowling_team", "")

    lines = [
        "📊 **Pre-Match Deep Audit:**<br>",
        f"• **Trajectory Base:** {base_label} is **{avg:.1f} runs** ({base_src_note}).",
        f"• **Venue Check:** {venue} conditions applied adjustment of {venue_sign}{venue_adj:.1f}% based on ground dimension profiles.",
        f"• **Matchup Metric:** {batting_team} batting strength vs {bowling_team} applied a {matchup_sign}{matchup_adj:.1f}% team strength adjustment.",
        f"• **Forecast:** Target score of {target_line} vs model projection of {avg:.0f}. Model forecast: {lean}."
    ]
    
    # ── Issue 1: Venue not found \u2014 show at top so it's impossible to miss ──
    venue_warn = result.get("venue_warning", "")
    if venue_warn:
        lines.insert(0, venue_warn + "<br>")

    # ── Issue 3: Easy-line floor note ──
    floor_note = result.get("sanity_floor_applied", "")
    if floor_note:
        lines.append(f"<br>*[Sanity check] {floor_note}*")

    insight = "<br>".join(lines)
    # Insight is rendered as raw HTML, so Markdown emphasis markers would show
    # as literal asterisks. Convert **bold** / *italic* to real HTML tags.
    insight = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", insight)
    insight = re.sub(r"\*(.+?)\*", r"<em>\1</em>", insight)
    return insight


# ─────────────────────────────────────────────────────────────────
# PUBLIC API — Two separate model instances
# ─────────────────────────────────────────────────────────────────

class Innings1Model:
    """
    Model for batting-first innings (setting a target).
    No RRR factor. Weights as per spec.
    """
    innings = 1

    def analyse(self, **kwargs) -> dict:
        kwargs.setdefault("innings", 1)
        kwargs.setdefault("target", None)
        return _analyse(**kwargs)


class ChasingModel:
    """
    Model for second innings run chase.
    Adds RRR factor at all phases (from config.CHASING_RRR_ADJUSTMENTS).
    """
    innings = 2

    def analyse(self, **kwargs) -> dict:
        kwargs.setdefault("innings", 2)
        if not kwargs.get("target"):
            raise ValueError("ChasingModel requires `target` (runs to chase)")
        return _analyse(**kwargs)


# Singleton instances — use these directly in app.py
INNINGS_1_MODEL = Innings1Model()
CHASING_MODEL   = ChasingModel()


# ─────────────────────────────────────────────────────────────────
# CONVENIENCE FUNCTION
# ─────────────────────────────────────────────────────────────────

def analyse(innings: int, **kwargs) -> dict:
    """
    Route to the correct model instance based on innings number.
    Called from app.py with all collected inputs.
    """
    if innings == 1:
        return INNINGS_1_MODEL.analyse(innings=1, **kwargs)
    elif innings == 2:
        return CHASING_MODEL.analyse(innings=2, **kwargs)
    else:
        raise ValueError(f"Invalid innings: {innings}. Must be 1 or 2.")
