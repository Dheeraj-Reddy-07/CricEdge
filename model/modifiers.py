"""
model/modifiers.py — CricEdge Conditional Modifiers

All 9 conditional modifiers applied AFTER the signed adjustments.
Applied sequentially in order. Each can be individually toggled
via config.MODIFIERS_ENABLED.

Each modifier returns:
  - fired: bool
  - adjustment: float (percentage points, direct)
  - reason: str (human-readable explanation)
"""

import logging
from typing import Optional

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data import database as db

logger = logging.getLogger(__name__)


def quota_scaling_factor(bowler_overs_remaining: float, total_overs_remaining: float) -> float:
    """Scale a bowler-centric modifier by the share of the remaining target window
    the bowler(s) actually control.

        scaling = 0.5 + 0.5 * (bowler_overs_remaining / total_overs_remaining)

    Floor of 0.5 (ratio clamped to [0, 1]) so a bowler with just one over left still
    has a meaningful impact — that over still caps real scoring — while the effect is
    reduced proportionally as fewer of their overs remain. Example: 1 of 4 → 0.625.
    """
    if total_overs_remaining <= 0:
        return 1.0
    ratio = max(0.0, min(1.0, bowler_overs_remaining / total_overs_remaining))
    return 0.5 + 0.5 * ratio


# ─────────────────────────────────────────────────────────────────
# MODIFIER 1: Wicket Clustering
# ─────────────────────────────────────────────────────────────────

def modifier_wicket_clustering(
    balls_this_innings: list[dict],
    current_over: float,
    wickets_fallen: int = 0,
    format_: str = "",
) -> dict:
    """
    Trigger: 2+ wickets in last 2 overs, OR 5+ wickets down in death overs.
    Logic: rebuild phase consistently kills scoring rate.
    """
    if not config.MODIFIERS_ENABLED.get("wicket_clustering", True):
        return _no_fire("wicket_clustering", "disabled in settings")

    params    = config.MODIFIER_PARAMS["wicket_clustering"]
    threshold = params["wickets_in_window"]
    window    = params["over_window"]
    adj       = params["adjustment"]
    collapse  = params.get("collapse_wickets", 5)
    death_start = params.get("death_over_start", 17)

    current_over_int = int(current_over)
    # Last `window` completed overs (overs are 1-indexed in the ball feed):
    # current over 10 with window 2 -> overs 9 and 10, not 8,9,10.
    min_over = max(1, current_over_int - window + 1)

    recent_wickets = sum(
        1 for b in balls_this_innings
        if b.get("is_wicket") and b.get("over", 0) >= min_over
    )

    if recent_wickets >= threshold:
        return {
            "name":       "wicket_clustering",
            "fired":      True,
            "adjustment": adj,
            "reason":     f"{recent_wickets} wickets in last {window} overs — rebuild phase penalty",
        }

    if wickets_fallen >= collapse and current_over_int >= death_start:
        return {
            "name":       "wicket_clustering",
            "fired":      True,
            "adjustment": adj,
            "reason":     f"{wickets_fallen} wickets down in death overs — collapse/rebuild risk",
        }

    ball_note = (
        f"{len(balls_this_innings)} balls in feed"
        if not balls_this_innings
        else f"{recent_wickets} wicket(s) in last {window} overs (overs {min_over}+)"
    )
    return _no_fire(
        "wicket_clustering",
        f"Not triggered: {ball_note}; {wickets_fallen} down (need {collapse}+ in death)",
    )


# ─────────────────────────────────────────────────────────────────
# MODIFIER 1b: Early-Innings Collapse  (powerplay-specific)
# ─────────────────────────────────────────────────────────────────

def modifier_early_collapse(
    wickets_fallen: int,
    current_over: float,
    format_: str = "",
) -> dict:
    """
    Trigger: 2+ wickets down within the first 6 overs (powerplay).
    Effect: 2 wickets → -10%, 3+ wickets → -15%.

    Distinct from wicket_clustering (which only fires on 2-in-2-overs clusters or
    5+ down in the death overs). An early top-order collapse devastates the run
    projection but accumulating runs partly mask it in the base, so it needs its
    own explicit penalty. Uses wickets_fallen + current_over directly (no reliance
    on ball-by-ball over tags), so it fires even from coarse scorecard input.
    """
    if not config.MODIFIERS_ENABLED.get("early_collapse", True):
        return _no_fire("early_collapse", "disabled in settings")

    params    = config.MODIFIER_PARAMS["early_collapse"]
    max_over  = params["max_over"]
    min_wkts  = params["min_wickets"]
    adj_two   = params["adj_two_wickets"]
    adj_three = params["adj_three_plus"]

    # Powerplay window only — once past over 6 the middle-overs dynamics differ
    # and wicket_clustering / available_resources take over.
    if current_over > max_over:
        return _no_fire(
            "early_collapse",
            f"over {current_over:.1f} past powerplay (max {max_over})",
        )

    if wickets_fallen >= 3:
        return {
            "name":       "early_collapse",
            "fired":      True,
            "adjustment": adj_three,
            "reason":     (
                f"Early collapse: {wickets_fallen} down inside {max_over} overs "
                f"(over {current_over:.1f}) — top-order gone, projection slashed"
            ),
        }
    if wickets_fallen >= min_wkts:
        return {
            "name":       "early_collapse",
            "fired":      True,
            "adjustment": adj_two,
            "reason":     (
                f"Early wickets: {wickets_fallen} down inside {max_over} overs "
                f"(over {current_over:.1f}) — powerplay pressure"
            ),
        }

    return _no_fire(
        "early_collapse",
        f"{wickets_fallen} down at over {current_over:.1f} (need {min_wkts}+ within {max_over})",
    )


# ─────────────────────────────────────────────────────────────────
# MODIFIER 2: Scorer Concentration Risk
# ─────────────────────────────────────────────────────────────────

def modifier_scorer_concentration(
    batsmen: list[dict],
    current_total: int,
    current_over: float,
    all_scorers: Optional[list[dict]] = None,
) -> dict:
    """
    Trigger: lead batter >60% of team total in middle/death, or >80% in PP.
    Counter: runs spread across 3+ set batters → positive.
    """
    if not config.MODIFIERS_ENABLED.get("scorer_concentration", True):
        return _no_fire("scorer_concentration", "disabled in settings")

    params        = config.MODIFIER_PARAMS["scorer_concentration"]
    hi_threshold  = params["high_concentration_threshold"]
    hi_adj        = params["high_concentration_adj"]
    spread_min    = params["spread_min_batters"]
    spread_adj    = params["spread_adj"]

    scorers = all_scorers if all_scorers else batsmen
    if current_total <= 0 or not scorers:
        return _no_fire("scorer_concentration", "no scorer data")

    # Find lead batter contribution across everyone who has batted
    lead_runs = max((b.get("runs", 0) for b in scorers), default=0)
    concentration = lead_runs / current_total

    is_high = False
    if current_over >= 6.0 and concentration > hi_threshold:
        is_high = True
    elif current_over < 6.0 and concentration > 0.80:
        is_high = True

    if is_high:
        lead_name = next((b["name"] for b in scorers if b.get("runs", 0) == lead_runs), "Lead batter")
        return {
            "name":       "scorer_concentration",
            "fired":      True,
            "adjustment": hi_adj,
            "reason":     f"{lead_name} carrying {concentration*100:.0f}% of {current_total} runs — single point of failure risk",
        }

    # Check if spread (3+ set batters with meaningful contributions)
    set_batters = [b for b in scorers if b.get("runs", 0) >= 15]
    if len(set_batters) >= spread_min:
        return {
            "name":       "scorer_concentration",
            "fired":      True,
            "adjustment": spread_adj,
            "reason":     f"Runs spread across {len(set_batters)} set batters — positive signal",
        }

    return _no_fire(
        "scorer_concentration",
        f"Lead batter {concentration*100:.0f}% (need >{hi_threshold*100:.0f}% if over>=6, >80% if over<6, or {spread_min}+ set batters)",
    )


# ─────────────────────────────────────────────────────────────────
# MODIFIER 3: Bowling Quota Trap
# ─────────────────────────────────────────────────────────────────

def modifier_bowling_quota_trap(
    remaining_bowlers: list[dict],
    overs_remaining: float,
    format_: str,
    all_bowlers: list[dict] | None = None,
    current_over: float = 0.0,
    db_path: Optional[str] = None,
) -> dict:
    """
    Trigger 0 (HIGHEST PRIORITY — Workload Exhaustion):
        Top-4 frontline bowlers have collectively bowled >=16 overs at/after
        over 16 → primaries are spent, part-timers MUST bowl the death.
        Overrides elite/part-timer checks. Format death avg baseline decayed.

    Trigger A: 2+ elite death bowlers (career death eco <8.5) with full quota in death → -10%
    Trigger B: part-timer must bowl 2+ death overs → +12%
    """
    if not config.MODIFIERS_ENABLED.get("bowling_quota_trap", True):
        return _no_fire("bowling_quota_trap", "disabled in settings")

    params           = config.MODIFIER_PARAMS["bowling_quota_trap"]
    elite_threshold  = params["elite_death_economy_threshold"]
    elite_trigger    = params["elite_bowlers_for_trigger"]
    elite_adj        = params["elite_full_quota_adj"]
    part_trigger     = params["part_timer_overs_threshold"]
    part_adj         = params["part_timer_adj"]
    wl_quota         = params.get("workload_quota_overs", 16)
    wl_over          = params.get("workload_trigger_over", 16.0)
    wl_adj           = params.get("workload_adj", 7.0)

    # ── Trigger 0: Workload Exhaustion ─────────────────────────────
    # Check BEFORE career-role classification — this is a live structural fact.
    if all_bowlers and current_over >= wl_over:
        sorted_by_overs = sorted(
            all_bowlers, key=lambda b: b.get("overs_today", 0), reverse=True
        )
        top4 = sorted_by_overs[:4]
        top4_total = sum(b.get("overs_today", 0) for b in top4)
        if top4_total >= wl_quota:
            names = ", ".join(
                f"{b.get('name','?')}({b.get('overs_today',0):.0f}ov)"
                for b in top4
            )
            # Window-wide structural fact: part-timers cover the entire remainder.
            scaling = quota_scaling_factor(overs_remaining, overs_remaining)
            return {
                "name":       "bowling_quota_trap",
                "fired":      True,
                "adjustment": round(wl_adj * scaling, 1),
                "reason":     (
                    f"Primaries exhausted: top-4 have bowled {top4_total:.0f}/{wl_quota} overs "
                    f"({names}). Format death avg invalidated — part-timers must bowl death. "
                    f"[scaled ×{scaling:.2f} over {overs_remaining:.0f} remaining over(s)]"
                ),
            }

    # ── Trigger A / B: Career-role classification (existing logic) ──
    elite_count      = 0
    elite_overs      = 0.0
    part_timer_overs = 0
    elite_names      = []
    part_names       = []
    unknown_names    = []

    for b in remaining_bowlers:
        bname  = b.get("name", "")
        quota  = b.get("overs_remaining", 0)
        if quota < 1:
            continue
        bstats = db.get_bowler_stats(bname, format_, db_path) if bname else None
        role   = db.classify_death_bowler_role(bstats, elite_threshold)

        if role == "elite_death_bowler":
            elite_count += 1
            elite_overs += quota
            elite_names.append(bname)
        elif role == "part_timer":
            part_timer_overs += quota
            part_names.append(f"{bname}({quota}ov)")
        else:
            unknown_names.append(bname)

    if elite_count >= elite_trigger:
        # Scale by the share of the remaining window the elite bowlers control.
        scaling = quota_scaling_factor(elite_overs, overs_remaining)
        return {
            "name":       "bowling_quota_trap",
            "fired":      True,
            "adjustment": round(elite_adj * scaling, 1),
            "reason":     (
                f"{elite_count} elite bowlers with overs remaining: {', '.join(elite_names)} "
                f"[scaled ×{scaling:.2f} — {elite_overs:.0f}/{overs_remaining:.0f} window overs]"
            ),
        }
    if part_timer_overs >= part_trigger:
        # Scale by the share of the remaining window the part-timers must bowl.
        scaling = quota_scaling_factor(part_timer_overs, overs_remaining)
        return {
            "name":       "bowling_quota_trap",
            "fired":      True,
            "adjustment": round(part_adj * scaling, 1),
            "reason":     (
                f"Part-timers must bowl {part_timer_overs} death over(s): {', '.join(part_names)} "
                f"[scaled ×{scaling:.2f} — {part_timer_overs}/{overs_remaining:.0f} window overs]"
            ),
        }

    rem_names = [b.get("name", "?") for b in remaining_bowlers if b.get("overs_remaining", 0) >= 1]
    unknown_note = f"; unclassified (no DB role): {', '.join(unknown_names)}" if unknown_names else ""
    return _no_fire(
        "bowling_quota_trap",
        f"Elite={elite_count} (need {elite_trigger}+); part-timer overs={part_timer_overs} "
        f"(need {part_trigger}+); remaining: {', '.join(rem_names) or 'none'}{unknown_note}",
    )


# ─────────────────────────────────────────────────────────────────
# MODIFIER 4: Par Score Trap
# ─────────────────────────────────────────────────────────────────

def modifier_par_score_trap(
    current_over: float,
    venue: str,
    format_: str,
    db_path: Optional[str] = None,
) -> dict:
    """
    Trigger: overs 13-15 at venues that historically show scoring slowdown.
    Sites price linearly; scoring is non-linear.
    """
    if not config.MODIFIERS_ENABLED.get("par_score_trap", True):
        return _no_fire("par_score_trap", "disabled in settings")

    params       = config.MODIFIER_PARAMS["par_score_trap"]
    target_overs = params["target_overs"]
    adj          = params["adjustment"]

    current_over_int = int(current_over)
    if current_over_int not in target_overs:
        return _no_fire("par_score_trap", f"over {current_over_int} not in {target_overs}")

    venue_stats = db.get_venue_stats(venue, format_, db_path)
    if not venue_stats:
        return _no_fire("par_score_trap", f"no venue stats for {venue}")

    avg_per_over = venue_stats.get("avg_runs_per_over", [])
    if not avg_per_over or len(avg_per_over) < 16:
        return _no_fire("par_score_trap", "venue missing per-over run curve")

    # Compare avg runs in overs 13-15 vs overs 1-10 at this venue (0-indexed array)
    overs_1_10   = avg_per_over[:10]
    overs_13_15  = avg_per_over[12:15]
    avg_early    = sum(overs_1_10) / len(overs_1_10) if overs_1_10 else 0
    avg_par_trap = sum(overs_13_15) / len(overs_13_15) if overs_13_15 else 0

    if avg_early > 0 and (avg_early - avg_par_trap) / avg_early > 0.15:
        return {
            "name":       "par_score_trap",
            "fired":      True,
            "adjustment": adj,
            "reason":     f"Over {current_over_int}: {venue} historically slows in overs 13-15 ({avg_par_trap:.1f}/ov vs {avg_early:.1f}/ov early)",
        }
    return _no_fire(
        "par_score_trap",
        f"{venue} overs 13-15 not slow enough ({avg_par_trap:.1f} vs {avg_early:.1f} early)",
    )


# ─────────────────────────────────────────────────────────────────
# MODIFIER 5: Pitch Deterioration
# ─────────────────────────────────────────────────────────────────

def modifier_pitch_deterioration(
    current_over: float,
    venue: str,
    format_: str,
    db_path: Optional[str] = None,
) -> dict:
    """
    Trigger: venue shows >20% historical slowdown in overs 15-20 vs 1-10,
             AND current over > 14.
    """
    if not config.MODIFIERS_ENABLED.get("pitch_deterioration", True):
        return _no_fire("pitch_deterioration", "disabled in settings")

    params           = config.MODIFIER_PARAMS["pitch_deterioration"]
    slowdown_thresh  = params["slowdown_threshold"]
    trigger_after    = params["trigger_after_over"]
    adj              = params["adjustment"]

    if int(current_over) <= trigger_after:
        return _no_fire("pitch_deterioration", f"over {int(current_over)} ≤ {trigger_after}")

    venue_stats = db.get_venue_stats(venue, format_, db_path)
    if not venue_stats:
        return _no_fire("pitch_deterioration", f"no venue stats for {venue}")

    if not db.venue_has_reliable_modifiers(venue_stats):
        return _no_fire("pitch_deterioration", "insufficient venue data")

    slowdown = venue_stats.get("historical_slowdown_pct", 0.0) or 0.0
    if slowdown > slowdown_thresh:
        return {
            "name":       "pitch_deterioration",
            "fired":      True,
            "adjustment": adj,
            "reason":     f"{venue} pitch deteriorates: {slowdown*100:.0f}% slowdown in overs 15-20 vs 1-10",
        }
    return _no_fire(
        "pitch_deterioration",
        f"{venue} slowdown {slowdown*100:.0f}% (need >{slowdown_thresh*100:.0f}%)",
    )


# ─────────────────────────────────────────────────────────────────
# MODIFIER 6: New Batter Transition Penalty
# ─────────────────────────────────────────────────────────────────

def modifier_new_batter_transition(
    balls_this_innings: list[dict],
    total_balls_bowled: int,
) -> dict:
    """
    Trigger: wicket fell in last 2 legal balls.
    Historical: avg runs/over immediately after wicket is lower.
    """
    if not config.MODIFIERS_ENABLED.get("new_batter_transition", True):
        return _no_fire("new_batter_transition", "disabled in settings")

    params     = config.MODIFIER_PARAMS["new_batter_transition"]
    ball_window = params["ball_window"]
    adj         = params["adjustment"]

    # Look at last N legal balls
    recent_legal = [b for b in balls_this_innings if b.get("is_legal", True)][-ball_window:]
    if any(b.get("is_wicket") for b in recent_legal):
        return {
            "name":       "new_batter_transition",
            "fired":      True,
            "adjustment": adj,
            "reason":     "Wicket in last 2 balls — new batter transition period penalty",
        }
    return _no_fire("new_batter_transition", "no wicket in last 2 legal balls")


# ─────────────────────────────────────────────────────────────────
# MODIFIER 7: Exceptional Bowler Today (HIGHEST PRIORITY)
# ─────────────────────────────────────────────────────────────────

def modifier_exceptional_bowler_today(
    all_bowlers: list[dict],
    remaining_bowlers: list[dict],
    current_over: float,
    overs_remaining: Optional[float] = None,
) -> dict:
    """
    HIGHEST PRIORITY live signal. Never skip this.
    Trigger: any bowler economy <6.0 in 3+ overs bowled TODAY,
             AND has overs remaining in death.
    Reason: exceptional day TODAY stronger signal than career stats.

    The effect is scaled by how many of the remaining target-window overs the
    exceptional bowler(s) still control (floor 0.5): a bowler with one over left
    still caps real scoring, but cannot bend the whole window like a full quota can.
    """
    if not config.MODIFIERS_ENABLED.get("exceptional_bowler_today", True):
        return _no_fire("exceptional_bowler_today", "disabled in settings")

    params        = config.MODIFIER_PARAMS["exceptional_bowler_today"]
    eco_threshold = params["economy_threshold"]
    min_overs     = params["min_overs_bowled_today"]
    adj           = params["adjustment"]

    rem_overs_by_name = {
        b.get("name", ""): b.get("overs_remaining", 0)
        for b in remaining_bowlers if b.get("overs_remaining", 0) >= 1
    }
    remaining_names = set(rem_overs_by_name)
    exceptional = []
    exceptional_overs = 0.0

    for bowler in all_bowlers:
        name   = bowler.get("name", "")
        overs  = bowler.get("overs_today", 0.0)
        runs   = bowler.get("runs_today", 0)

        if overs < min_overs:
            continue
        balls = int(overs) * 6 + round((overs % 1) * 10)
        eco = (runs / balls * 6) if balls > 0 else 99.0

        if eco < eco_threshold and name in remaining_names:
            exceptional.append(f"{name} ({overs}ov, {runs}r, eco={eco:.1f})")
            exceptional_overs += rem_overs_by_name.get(name, 0)

    if exceptional:
        scaling = (
            quota_scaling_factor(exceptional_overs, overs_remaining)
            if overs_remaining else 1.0
        )
        denom = f"{overs_remaining:.0f}" if overs_remaining else "?"
        return {
            "name":       "exceptional_bowler_today",
            "fired":      True,
            "adjustment": round(adj * scaling, 1),
            "reason":     (
                f"Exceptional day(s): {'; '.join(exceptional)} — strongest death signal "
                f"[scaled ×{scaling:.2f} — {exceptional_overs:.0f}/{denom} window overs]"
            ),
        }
    return _no_fire(
        "exceptional_bowler_today",
        f"no bowler eco<{eco_threshold} in {min_overs}+ overs with quota left "
        f"({len(remaining_names)} bowlers remaining)",
    )


# ─────────────────────────────────────────────────────────────────
# MODIFIER 8: Spin in Death Mismatch
# ─────────────────────────────────────────────────────────────────

def modifier_spin_death_mismatch(
    bowling_team: str,
    batsmen: list[dict],
    format_: str,
    target_phase: str,
    db_path: Optional[str] = None,
) -> dict:
    """
    Trigger: The target window is in the 'death' or 'middle' phase, and the bowling 
    team frequently bowls spinners here, but the current crease batters 
    crush spin in these phases.
    """
    if not config.MODIFIERS_ENABLED.get("spin_death_mismatch", True):
        return _no_fire("spin_death_mismatch", "disabled in settings")

    # This modifier only matters for the phase we are PREDICTING FOR.
    # We must check target_phase, NOT the current over's phase.
    if target_phase not in ("middle", "death"):
        return _no_fire("spin_death_mismatch", f"target window is {target_phase} (need middle/death)")

    params        = config.MODIFIER_PARAMS["spin_death_mismatch"]
    friendly_sr   = params["spin_friendly_sr_threshold"]
    friendly_adj  = params["spin_friendly_adj"]
    struggle_adj  = params["spin_struggles_adj"]

    bowl_stats = db.get_team_bowling_stats(bowling_team, format_, db_path)
    uses_spinner = bool(bowl_stats and bowl_stats.get("uses_spinner_in_death", 0))

    if not uses_spinner:
        return _no_fire("spin_death_mismatch", f"{bowling_team} does not use spin in death")

    friendly_batters = 0
    struggling_batters = 0
    missing_stats = 0

    for b_info in batsmen[:2]:
        bname  = b_info.get("name", "")
        bstats = db.get_batter_stats(bname, format_, db_path) if bname else None
        if not bstats:
            missing_stats += 1
            continue
        sr_vs_spin = bstats.get("sr_vs_spin_death")
        if sr_vs_spin is None:
            missing_stats += 1
            continue
        if sr_vs_spin > friendly_sr:
            friendly_batters += 1
        elif sr_vs_spin < 100:
            struggling_batters += 1

    if friendly_batters > 0:
        return {
            "name":       "spin_death_mismatch",
            "fired":      True,
            "adjustment": friendly_adj,
            "reason":     f"{friendly_batters} batter(s) have SR>{friendly_sr:.0f} vs spin in death — mismatch advantage",
        }
    if struggling_batters > 0:
        return {
            "name":       "spin_death_mismatch",
            "fired":      True,
            "adjustment": struggle_adj,
            "reason":     f"Batting lineup struggles vs spin in death ({struggling_batters} batter(s))",
        }

    return _no_fire(
        "spin_death_mismatch",
        f"spin in death but no clear mismatch (friendly={friendly_batters}, "
        f"struggling={struggling_batters}, no_data={missing_stats})",
    )


# ─────────────────────────────────────────────────────────────────
# MODIFIER 9: Psychological Ceiling
# ─────────────────────────────────────────────────────────────────

def modifier_psychological_ceiling(
    batting_team: str,
    current_runs: int,
    current_over: float,
    format_: str,
    db_path: Optional[str] = None,
    overs_remaining: Optional[float] = None,
) -> dict:
    """
    Trigger: team above score threshold at over 18+.
    Uses lower threshold (170) when ≤2 overs remain (last 2 overs context).
    """
    if not config.MODIFIERS_ENABLED.get("psychological_ceiling", True):
        return _no_fire("psychological_ceiling", "disabled in settings")

    params         = config.MODIFIER_PARAMS["psychological_ceiling"]
    score_thresh   = params["score_threshold"]
    death_thresh   = params.get("score_threshold_death", 170)
    trigger_over   = params["trigger_over"]
    default_adj    = params["default_adj"]

    max_overs = config.MAX_OVERS.get(format_, 20)
    if overs_remaining is None:
        overs_remaining = max(0.0, max_overs - current_over)

    in_last_two = overs_remaining <= 2.0 and int(current_over) >= trigger_over - 2
    effective_thresh = death_thresh if in_last_two else score_thresh

    if current_runs < effective_thresh or int(current_over) < trigger_over:
        ctx = "last 2 overs" if in_last_two else "full innings"
        return _no_fire(
            "psychological_ceiling",
            f"{current_runs} < {effective_thresh} ({ctx} threshold) or over {current_over:.1f} < {trigger_over}",
        )

    bat_stats = db.get_team_batting_stats(batting_team, format_, db_path=db_path)
    historical_sr = bat_stats.get("avg_sr_when_190plus_at_over18") if bat_stats else None

    adj = default_adj
    reason_detail = f"default {default_adj}%"
    if historical_sr is not None:
        # Historical SR when 190+ at over 18, normalized
        # SR 100 = 1 run/ball = 6/over = typical ceiling; lower than normal
        adj = max(-8.0, min(-4.0, -(120.0 - historical_sr) / 5.0))
        reason_detail = f"historical SR {historical_sr:.0f} in overs 18-20 when 190+"

    return {
        "name":       "psychological_ceiling",
        "fired":      True,
        "adjustment": adj,
        "reason":     f"{batting_team} at {current_runs}+ (over {current_over:.1f}): psychological ceiling — {reason_detail}",
    }


# ─────────────────────────────────────────────────────────────────
# APPLY ALL MODIFIERS
# ─────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────
# EVALUATE ALL MODIFIERS (diagnostics + apply)
# ─────────────────────────────────────────────────────────────────

def evaluate_all_modifiers(
    innings: int,
    batting_team: str,
    bowling_team: str,
    current_runs: int,
    current_over: float,
    wickets_fallen: int,
    total_balls_bowled: int,
    format_: str,
    target_phase: str,
    venue: str,
    batsmen: list[dict],
    all_bowlers: list[dict],
    remaining_bowlers: list[dict],
    remaining_batters: list[dict],
    balls_this_innings: list[dict],
    all_scorers: Optional[list[dict]] = None,
    db_path: Optional[str] = None,
) -> list[dict]:
    """Run all 9 modifiers and return full status (fired or not) with reasons."""
    max_overs = config.MAX_OVERS.get(format_, 20)
    overs_remaining = max(0.0, max_overs - current_over)

    return [
        modifier_wicket_clustering(
            balls_this_innings, current_over, wickets_fallen, format_,
        ),
        modifier_early_collapse(wickets_fallen, current_over, format_),
        modifier_scorer_concentration(batsmen, current_runs, current_over, all_scorers),
        modifier_bowling_quota_trap(
            remaining_bowlers, overs_remaining, format_,
            all_bowlers=all_bowlers,
            current_over=current_over,
            db_path=db_path,
        ),
        modifier_par_score_trap(current_over, venue, format_, db_path),
        modifier_pitch_deterioration(current_over, venue, format_, db_path),
        modifier_new_batter_transition(balls_this_innings, total_balls_bowled),
        modifier_exceptional_bowler_today(
            all_bowlers, remaining_bowlers, current_over, overs_remaining=overs_remaining,
        ),
        modifier_spin_death_mismatch(bowling_team, batsmen, format_, target_phase, db_path),
        modifier_psychological_ceiling(
            batting_team, current_runs, current_over, format_, db_path,
            overs_remaining=overs_remaining,
        ),
    ]


def apply_all_modifiers(
    probability_result: dict,
    # Match state
    innings: int,
    batting_team: str,
    bowling_team: str,
    current_runs: int,
    current_over: float,
    wickets_fallen: int,
    total_balls_bowled: int,
    format_: str,
    target_phase: str,
    venue: str,
    # Live data
    batsmen: list[dict],
    all_bowlers: list[dict],
    remaining_bowlers: list[dict],
    remaining_batters: list[dict],
    balls_this_innings: list[dict],
    all_scorers: Optional[list[dict]] = None,
    # Optional
    db_path: Optional[str] = None,
) -> dict:
    """
    Apply all 9 conditional modifiers sequentially to probability_result.
    Returns enriched result with modifiers_applied list and final_probability.
    """
    prob = probability_result["pre_modifier_prob"]
    modifiers_applied = []

    all_mods = evaluate_all_modifiers(
        innings=innings,
        batting_team=batting_team,
        bowling_team=bowling_team,
        current_runs=current_runs,
        current_over=current_over,
        wickets_fallen=wickets_fallen,
        total_balls_bowled=total_balls_bowled,
        format_=format_,
        target_phase=target_phase,
        venue=venue,
        batsmen=batsmen,
        all_bowlers=all_bowlers,
        remaining_bowlers=remaining_bowlers,
        remaining_batters=remaining_batters,
        balls_this_innings=balls_this_innings,
        all_scorers=all_scorers,
        db_path=db_path,
    )

    for m in all_mods:
        if m["fired"]:
            prob += m["adjustment"]
            modifiers_applied.append(m)

    # Apply hard cap
    prob = max(config.PROBABILITY_MIN, min(config.PROBABILITY_MAX, prob))

    # Update result
    modifier_total = sum(m["adjustment"] for m in modifiers_applied)
    probability_result["modifiers_applied"]   = modifiers_applied
    probability_result["modifier_diagnostics"] = all_mods
    probability_result["modifier_total"]      = round(modifier_total, 1)
    probability_result["final_probability"]   = round(prob, 1)
    probability_result["modifiers_fired"]     = ",".join(m["name"] for m in modifiers_applied)

    # Recompute verdict with final prob
    from model.probability import _get_verdict
    probability_result["verdict"] = _get_verdict(prob)

    return probability_result


# ─────────────────────────────────────────────────────────────────
# HELPER
# ─────────────────────────────────────────────────────────────────

def _no_fire(name: str, reason: str = "") -> dict:
    return {"name": name, "fired": False, "adjustment": 0.0, "reason": reason}
