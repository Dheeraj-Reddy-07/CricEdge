"""
model/market.py — CricEdge Market-Specific Probability Calculator

Markets:
  1. Total Innings Score          — full innings, routes to existing engine
  2. Next 2 Overs Runs            — runs in next 2 overs only
  3. Next 4 Overs Runs            — runs in next 4 overs only
  4. Powerplay / Middle / Death Session Runs — runs in that phase's overs
  5. Custom: Overs X to Y         — user-specified over window

Non-total-innings markets use a phase-weighted expected RPO + normal
distribution with phase-specific variance for the probability calculation.
"""

from __future__ import annotations
import logging
import math
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data import database as db

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────

MARKET_TYPES = [
    "Total Innings Score",
    "Next 2 Overs Runs",
    "Next 4 Overs Runs",
    "Powerplay Session Runs",
    "Middle Session Runs",
    "Death Session Runs",
    "Custom: Overs X to Y",
]

# Maps session market labels → phase key
SESSION_PHASE_MARKETS = {
    "Powerplay Session Runs": "powerplay",
    "Middle Session Runs":    "middle",
    "Death Session Runs":     "death",
}

# Legacy combined label (auto-detected phase) — still routed for old saved state
_LEGACY_SESSION_MARKET = "Session Runs (PP / Middle / Death)"

# Women's T20I typical RPO by phase (from 972-match ingest)
PHASE_TYPICAL_RPO = {
    "powerplay": 6.8,
    "middle":    6.2,
    "death":     8.5,
}

# Men's ODI typical RPO by phase
PHASE_TYPICAL_RPO_ODI = {
    "powerplay": 5.8,   # overs 1-10, restraint phase
    "middle":    4.8,   # overs 11-40, building phase
    "death":     8.2,   # overs 41-50, slog
}

# Std-dev of runs per over by phase (higher in death = more chaos)
PHASE_SIGMA_PER_OVER = {
    "powerplay": 2.3,
    "middle":    1.8,
    "death":     3.1,
}

# ODI — higher variance per over due to conditions/bowling changes
PHASE_SIGMA_PER_OVER_ODI = {
    "powerplay": 2.0,
    "middle":    1.6,
    "death":     3.3,
}

# Phase boundaries (1-indexed over numbers)
PHASE_END = {
    "powerplay": 6,
    "middle":   16,
    "death":    20,
}

PHASE_END_ODI = {
    "powerplay": 10,
    "middle":    40,
    "death":     50,
}


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

def _phase_for_over(over_1indexed: int, is_odi: bool = False) -> str:
    """Return phase for a 1-indexed over number (delegates to config boundaries)."""
    from model.probability import get_phase
    fmt = "Men's ODI" if is_odi else "Women's T20I"
    return get_phase(float(over_1indexed), fmt)


def _normal_cdf(x: float) -> float:
    """Standard normal CDF using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _prob_exceed(mu: float, sigma: float, line: float) -> float:
    """P(X > line) where X ~ N(mu, sigma). Returns % clamped to [3, 97]."""
    if sigma <= 0:
        return 97.0 if mu > line else 3.0
    z = (line - mu) / sigma
    p = (1.0 - _normal_cdf(z)) * 100.0
    return round(min(97.0, max(3.0, p)), 1)


def _weighted_phase_stat(from_over: float, to_over: float, stat_dict: dict, is_odi: bool = False) -> float:
    """Average a per-phase stat across a (possibly multi-phase) over window."""
    overs = []
    start = int(from_over) + 1
    end   = int(to_over)
    for ov in range(start, end + 1):
        overs.append(stat_dict[_phase_for_over(ov, is_odi)])
    return sum(overs) / len(overs) if overs else stat_dict["middle"]


def _get_verdict(prob: float) -> str:
    """Unified forecast label — delegates to probability.get_verdict_display for
    consistency, so interval scenarios share the same confidence-tier labels used
    by the full-innings total.
    """
    from model.probability import get_verdict_display
    return get_verdict_display(prob)


def _zscore_verdict(mu: float, line: float, sigma: float, final_prob: float, high_conviction: bool = False) -> tuple:
    """
    Z-score confidence gate — authoritative forecast tier for interval scenarios.

    Z = (expected_runs - target_score) / sigma

    Three-tier confidence system:
      |Z| >= 1.5  Clear separation → HIGH CONFIDENCE (ABOVE / BELOW target)
      |Z| 1.0–1.5 Mild separation  → LOW CONFIDENCE
      |Z| < 1.0   No separation    → TOO CLOSE TO CALL (clamp prob [30%, 70%])

    The high_conviction flag (quota trap fired + pace > 10 RPO) lowers
    the effective thresholds by 0.15 when live context dominates.

    Validation:
      mu=80.9, line=75, sigma=5.7 → Z=+1.03 — between 1.0 and 1.5 → LOW CONFIDENCE
      mu=84.5, line=75, sigma=5.7 → Z=+1.66 — >=1.5 → HIGH CONFIDENCE
    """
    # Thresholds: 1.5σ for high confidence, 1.0σ for the low/too-close boundary
    Z_BET  = 1.5   # needs clear separation to call HIGH CONFIDENCE
    Z_SKIP = 1.0   # below this = no separation → TOO CLOSE TO CALL

    if high_conviction:
        # Live dominance (quota trap + hot pace) lowers both thresholds slightly
        Z_BET  = max(1.2, Z_BET  - 0.15)
        Z_SKIP = max(0.8, Z_SKIP - 0.15)

    if sigma > 0:
        z = (mu - line) / sigma
        if z >= Z_BET:
            return "HIGH CONFIDENCE — ABOVE TARGET", final_prob   # clear ABOVE separation
        elif z <= -Z_BET:
            return "HIGH CONFIDENCE — BELOW TARGET", final_prob   # clear BELOW separation
        elif abs(z) >= Z_SKIP:
            return "LOW CONFIDENCE", max(final_prob, 30.0)        # some separation, not decisive
        else:
            # No separation — within 1σ of target
            # Clamp probability to [35%, 65%] — tighter band prevents extreme swings
            clamped = min(65.0, max(35.0, final_prob))
            return "TOO CLOSE TO CALL", clamped

    # No sigma available: fall back to probability thresholds
    return _get_verdict(final_prob), final_prob


def _session_phase_for_window(from_over: float, to_over: float, is_odi: bool) -> str:
    """Dominant phase for a window (used for team/venue RPO lookup)."""
    start = int(from_over) + 1
    end   = int(to_over)
    counts = {"powerplay": 0, "middle": 0, "death": 0}
    for ov in range(start, end + 1):
        counts[_phase_for_over(ov, is_odi)] += 1
    return max(counts, key=counts.get)


def _phase_overs(phase: str, is_odi: bool) -> int:
    """Number of overs in a full phase."""
    if is_odi:
        return {"powerplay": 10, "middle": 30, "death": 10}[phase]
    return {"powerplay": 6, "middle": 10, "death": 4}[phase]


def _team_batting_phase_rpo(
    bat: dict | None,
    phase: str,
    is_odi: bool,
    batting_team: str,
) -> tuple[float | None, str | None]:
    """Per-over batting rate for a phase (death = last 4 overs in T20)."""
    if not bat:
        return None, None
    if phase == "powerplay" and bat.get("avg_pp_score"):
        rpo = bat["avg_pp_score"] / 6.0
        return rpo, f"{batting_team} PP {rpo:.1f} RPO"
    if phase == "death" and bat.get("avg_death_score"):
        n = _phase_overs("death", is_odi)
        rpo = bat["avg_death_score"] / n
        return rpo, f"{batting_team} death {rpo:.1f} RPO"
    if phase == "middle" and bat.get("avg_total"):
        pp = bat.get("avg_pp_score") or 0
        death = bat.get("avg_death_score") or 0
        mid_total = max(0, bat["avg_total"] - pp - death)
        n = _phase_overs("middle", is_odi)
        rpo = mid_total / n
        return rpo, f"{batting_team} middle {rpo:.1f} RPO"
    return None, None


def _resolved_team_phase_rpo(
    batting_team: str,
    phase: str,
    is_odi: bool,
    format_: str,
) -> tuple[float, str]:
    """
    Resolve a reliable team-phase RPO with step-down fallbacks:
      1) team phase RPO from DB when sample_matches > 0
      2) team global estimation (middle estimate from avg_total)
      3) format typical phase RPO (hard baseline)
    Guarantees never to return 0.0.
    """
    # Level 0: try explicit team phase stats (requires at least 1 historical match)
    bat = db.get_team_batting_stats(batting_team, format_) if batting_team else None
    if bat and (bat.get("sample_matches") or 0) > 0:
        r, note = _team_batting_phase_rpo(bat, phase, is_odi, batting_team)
        if r is not None and r > 0:
            return r, f"team ({note})"

    # Level 1: team-level global estimation (derive middle from avg_total when available)
    if bat and (bat.get("avg_total") or 0) > 0:
        try:
            if phase == "middle":
                pp = float(bat.get("avg_pp_score") or 0.0)
                death = float(bat.get("avg_death_score") or 0.0)
                mid_total = max(0.0, float(bat["avg_total"]) - pp - death)
                n = _phase_overs("middle", is_odi)
                if mid_total > 0:
                    return mid_total / n, f"team (middle est from avg_total {bat['avg_total']:.1f})"
            elif phase == "powerplay":
                # If explicit PP missing but avg_total exists, estimate PP as 30% of avg_total
                est_pp_total = float(bat.get("avg_pp_score") or (float(bat["avg_total"]) * 0.30))
                return est_pp_total / 6.0, f"team (PP est from avg_total {bat['avg_total']:.1f})"
            else:
                # death: prefer explicit, otherwise estimate small share (20% of avg_total)
                est_death_total = float(bat.get("avg_death_score") or (float(bat["avg_total"]) * 0.20))
                n = _phase_overs("death", is_odi)
                return est_death_total / n, f"team (death est from avg_total {bat['avg_total']:.1f})"
        except Exception:
            pass

    # Level 2: format typical baseline
    rpo_table = PHASE_TYPICAL_RPO_ODI if is_odi else PHASE_TYPICAL_RPO
    fallback = rpo_table.get(phase, rpo_table["middle"])
    return fallback, "format_avg"


def _team_bowling_phase_rpo(
    bowl: dict | None,
    phase: str,
    bowling_team: str,
) -> tuple[float | None, str | None]:
    """Phase economy ≈ runs conceded per over by the bowling team."""
    if not bowl:
        return None, None
    if phase == "powerplay":
        eco = bowl.get("avg_pp_economy")
    elif phase == "middle":
        eco = bowl.get("avg_mid_economy")
    else:
        eco = bowl.get("avg_death_economy")
    if eco:
        return float(eco), f"{bowling_team} {phase} eco {eco:.1f}"
    return None, None


def _venue_phase_rpo(venue_stats: dict | None, phase: str, is_odi: bool) -> float | None:
    """Average RPO across a full phase at a venue. None if data missing or unreliable."""
    if not venue_stats or not venue_stats.get("avg_runs_per_over"):
        return None
    if not db.venue_has_reliable_rpo(venue_stats):
        return None
    arr = venue_stats["avg_runs_per_over"]
    if is_odi:
        seg = arr[:10] if phase == "powerplay" else (arr[10:40] if phase == "middle" else arr[40:50])
    elif phase == "powerplay":
        seg = arr[:6]
    elif phase == "middle":
        seg = arr[6:16]
    else:
        seg = arr[16:20]
    if not seg:
        return None
    rpo = sum(seg) / len(seg)
    return rpo if rpo > 0 else None


def _venue_window_rpo(
    venue_stats: dict | None,
    from_over: float,
    to_over: float,
) -> float | None:
    """Average RPO for the exact overs in a prediction window. None if unreliable."""
    if not venue_stats or not venue_stats.get("avg_runs_per_over"):
        return None
    if not db.venue_has_reliable_rpo(venue_stats):
        return None
    arr = venue_stats["avg_runs_per_over"]
    first_ov = int(from_over) + 1
    last_ov = int(to_over)
    seg = arr[first_ov - 1:last_ov]
    if not seg:
        return None
    rpo = sum(seg) / len(seg)
    return rpo if rpo > 0 else None


def _resolved_venue_window_rpo(
    venue_stats: dict | None,
    from_over: float,
    to_over: float,
    is_odi: bool,
    live_powerplay_rpo: float | None = None,
) -> tuple[float, str]:
    """
    Reliable venue RPO for an over window, or format phase average.
    Never returns 0.0.
    """
    rpo_table = PHASE_TYPICAL_RPO_ODI if is_odi else PHASE_TYPICAL_RPO
    fallback = _weighted_phase_stat(from_over, to_over, rpo_table, is_odi)
    raw = _venue_window_rpo(venue_stats, from_over, to_over)
    if raw is not None:
        return raw, "venue"

    # Elevate the hard fallback for the Women’s T20I 7–10 block when venue data
    # is insufficient. Avoid aggressive down-weighting from the old 6.2 RPO.
    if not is_odi and int(from_over) == 6 and int(to_over) == 10:
        fallback = 7.1

    if live_powerplay_rpo is not None and live_powerplay_rpo > 0:
        fallback = max(fallback, live_powerplay_rpo * 0.95)

    return fallback, "format_avg"


def _resolved_venue_phase_rpo(
    venue_stats: dict | None,
    phase: str,
    is_odi: bool,
) -> tuple[float, str]:
    """Reliable venue phase RPO, or format phase average. Never returns 0.0."""
    rpo_table = PHASE_TYPICAL_RPO_ODI if is_odi else PHASE_TYPICAL_RPO
    fallback = rpo_table.get(phase, rpo_table["middle"])
    raw = _venue_phase_rpo(venue_stats, phase, is_odi)
    if raw is not None:
        return raw, "venue"
    return fallback, "format_avg"


def _bowling_eco_adj(bowl: dict | None, session_phase: str) -> tuple[float, str | None]:
    """Opposition economy adjustment for a phase."""
    if not bowl:
        return 0.0, None
    if session_phase == "powerplay":
        eco, neutral = bowl.get("avg_pp_economy"), 6.8
    elif session_phase == "middle":
        eco, neutral = bowl.get("avg_mid_economy"), 6.2
    else:
        eco, neutral = bowl.get("avg_death_economy"), 9.0
    if not eco:
        return 0.0, None
    return (eco - neutral) * 0.30, f"{session_phase} eco {eco:.1f}"


def _blended_window_rpo(
    from_over: float,
    to_over: float,
    window_overs: float,
    batting_team: str,
    bowling_team: str,
    venue: str,
    format_: str,
    is_odi: bool,
    win_phase: str,
    recent_live_rpo: float | None = None,
    recent_lookback: int = 0,
) -> tuple[float, list[str]]:
    """
    RPO baseline for a short over-window (Next 2/4, Custom).
    Live recent-over pace plus batting team, bowling team, and venue for that window.

    FIX (Issue 1): When venue is specified but absent from DB, a clear
    '⚠️ Venue not found' note is appended so it surfaces in the UI output.
    """
    rpo_table = PHASE_TYPICAL_RPO_ODI if is_odi else PHASE_TYPICAL_RPO
    generic = _weighted_phase_stat(from_over, to_over, rpo_table, is_odi)
    notes: list[str] = []
    bat = db.get_team_batting_stats(batting_team, format_) if batting_team else None
    bowl = db.get_team_bowling_stats(bowling_team, format_) if bowling_team else None
    team_bat_rpo, bat_note = _resolved_team_phase_rpo(batting_team, win_phase, is_odi, format_)
    team_bowl_rpo, bowl_note = _team_bowling_phase_rpo(bowl, win_phase, bowling_team)

    venue_stats = db.get_venue_stats(venue, format_) if venue and venue != "Unknown" else None
    # FIX (Issue 1): surface venue-not-found clearly
    if venue and venue not in ("", "Unknown") and venue_stats is None:
        notes.append(f"⚠️ Venue '{venue}' not found in DB — using format average")
    raw_venue_rpo = _venue_window_rpo(venue_stats, from_over, to_over)
    venue_reliable = raw_venue_rpo is not None
    window_label = f"overs {int(from_over) + 1}–{int(to_over)}"

    if recent_live_rpo is not None and recent_live_rpo > 0:
        if win_phase == "death" and not venue_reliable:
            parts: list[tuple[float, float]] = [(recent_live_rpo, 0.55)]
            w_bat, w_bowl, w_venue, w_gen = 0.20, 0.15, 0.0, 0.10
            notes.append(
                f"Last {recent_lookback} overs: {recent_live_rpo:.1f} RPO "
                f"(death anchor — venue data insufficient)"
            )
        else:
            parts = [(recent_live_rpo, 0.40)]
            w_bat, w_bowl, w_venue, w_gen = 0.22, 0.18, 0.12, 0.08
            notes.append(f"Last {recent_lookback} overs: {recent_live_rpo:.1f} RPO")
        if team_bat_rpo is not None:
            parts.append((team_bat_rpo, w_bat))
            notes.append(bat_note)
        if team_bowl_rpo is not None:
            parts.append((team_bowl_rpo, w_bowl))
            notes.append(bowl_note)
        if venue_reliable and w_venue > 0:
            parts.append((raw_venue_rpo, w_venue))
            notes.append(f"{venue} {window_label} ~{raw_venue_rpo:.1f} RPO")
        parts.append((generic, w_gen))
        total_w = sum(w for _, w in parts)
        rpo = sum(v * w for v, w in parts) / total_w
        return max(4.0, min(13.0, rpo)), notes

    bowl_adj, bowl_adj_note = _bowling_eco_adj(bowl, win_phase)
    if bowl_adj_note and bowling_team and not bowl_note:
        notes.append(f"{bowling_team} {bowl_adj_note}")

    venue_rpo, venue_src = _resolved_venue_window_rpo(
        venue_stats, from_over, to_over, is_odi, recent_live_rpo
    )
    parts = [("league", generic, 0.25)]
    if venue_src == "venue":
        parts.append(("venue", venue_rpo, 0.30))
        notes.append(f"{venue} {window_label} ~{venue_rpo:.1f} RPO")
    else:
        parts.append(("format avg", venue_rpo, 0.30))
        notes.append(
            f"Format avg {window_label} ~{venue_rpo:.1f} RPO "
            f"(venue n<{config.MIN_VENUE_SAMPLES_RPO})"
        )
    if team_bat_rpo is not None:
        parts.append(("batting", team_bat_rpo, 0.25))
        notes.append(bat_note)
    if team_bowl_rpo is not None:
        parts.append(("bowling", team_bowl_rpo, 0.20))
        notes.append(bowl_note)

    total_w = sum(w for _, _, w in parts)
    rpo = sum(v * w for _, v, w in parts) / total_w + bowl_adj
    return max(4.0, min(13.0, rpo)), notes


def _blended_phase_rpo(
    session_phase: str,
    from_over: float,
    to_over: float,
    batting_team: str,
    bowling_team: str,
    venue: str,
    format_: str,
    is_odi: bool,
) -> tuple[float, list[str]]:
    """
    Blend generic phase RPO with team batting, opposition bowling, and venue data.
    Returns (rpo, human-readable source notes).

    FIX (Issue 1): When venue is specified but absent from DB, a clear
    '⚠️ Venue not found' note is appended so it surfaces in the UI output.
    """
    rpo_table = PHASE_TYPICAL_RPO_ODI if is_odi else PHASE_TYPICAL_RPO
    generic   = _weighted_phase_stat(from_over, to_over, rpo_table, is_odi)
    parts: list[tuple[str, float, float]] = [("league avg", generic, 0.20)]
    notes: list[str] = []

    # Resolve team phase RPO using step-down fallbacks
    team_rpo, team_note = _resolved_team_phase_rpo(batting_team, session_phase, is_odi, format_)
    if team_note and team_rpo is not None:
        label = (
            "batting PP avg" if session_phase == "powerplay" else
            "batting death avg" if session_phase == "death" else
            "batting middle est"
        )
        weight = 0.40 if session_phase in ("powerplay", "death") else 0.35
        parts.append((label, team_rpo, weight))
        notes.append(f"{batting_team} {team_note}")

    bowl = db.get_team_bowling_stats(bowling_team, format_) if bowling_team else None
    bowl_adj, bowl_note = _bowling_eco_adj(bowl, session_phase)
    if bowl_note and bowling_team:
        notes.append(f"{bowling_team} {bowl_note}")

    venue_stats = db.get_venue_stats(venue, format_) if venue and venue != "Unknown" else None
    # FIX (Issue 1): surface venue-not-found clearly
    if venue and venue not in ("", "Unknown") and venue_stats is None:
        notes.append(f"⚠️ Venue '{venue}' not found in DB — using format average")
    venue_rpo, venue_src = _resolved_venue_phase_rpo(venue_stats, session_phase, is_odi)
    parts.append(("venue" if venue_src == "venue" else "format avg", venue_rpo, 0.25))
    if venue_src == "venue":
        notes.append(f"{venue} ~{venue_rpo:.1f} RPO")
    else:
        notes.append(
            f"Format {session_phase} avg ~{venue_rpo:.1f} RPO "
            f"(venue n<{config.MIN_VENUE_SAMPLES_RPO})"
        )

    total_w = sum(w for _, _, w in parts)
    rpo = sum(v * w for _, v, w in parts) / total_w + bowl_adj
    return max(4.0, min(13.0, rpo)), notes

def _team_depth_penalty(
    mu: float,
    wickets_fallen: int,
    remaining_batters: list[dict],
    format_: str,
    db_path: str | None = None,
) -> tuple[float, str]:
    """
    Dynamic Team Depth Index: wicket pressure ONLY fires if the next
    incoming batter has a death-over SR below DEATH_DEPTH_SR_THRESHOLD.

    Returns (run_adjustment, reason_string).
    Adjustment is always <= 0 (a reduction to mu).

    Design:
      • If wickets_fallen < 5         → no penalty (plenty of batting)
      • If incoming batter SR >= 115
        or SR is unknown (no DB data)  → no penalty (elite/unknown = trust them)
      • If SR < 115                   → scaled penalty proportional to how far
                                         below 115 the batter is, capped at
                                         -12% mu (tail) or -6% mu (under pressure)
    """
    sr_threshold = config.DEATH_DEPTH_SR_THRESHOLD

    if wickets_fallen < 5:
        return 0.0, ""

    # Find the next non-crease incoming batter (first in remaining_batters list)
    incoming = next(
        (b for b in (remaining_batters or []) if b.get("name")),
        None,
    )

    if incoming:
        bname  = incoming.get("name", "")
        bstats = db.get_batter_stats(bname, format_, db_path) if bname else None
        death_sr = bstats.get("sr_death") if bstats else None

        if death_sr is None or death_sr >= sr_threshold:
            # No data or elite finisher — trust batting depth, zero penalty
            sr_note = f"SR {death_sr:.0f}" if death_sr is not None else "no death SR data"
            return 0.0, f"{bname} {sr_note} >= {sr_threshold:.0f} threshold — depth penalty waived"

        # Tail-risk batter: scale penalty by how far below threshold
        frac = (sr_threshold - death_sr) / sr_threshold   # 0.0 → 1.0
        if wickets_fallen >= 7:
            penalty = -mu * min(0.12, frac * 0.15)
            return penalty, (
                f"{bname} death SR {death_sr:.0f} < {sr_threshold:.0f} with {wickets_fallen} wkts — "
                f"tail risk penalty ({penalty:.1f} runs)"
            )
        else:
            penalty = -mu * min(0.06, frac * 0.08)
            return penalty, (
                f"{bname} death SR {death_sr:.0f} < {sr_threshold:.0f} with {wickets_fallen} wkts — "
                f"depth index penalty ({penalty:.1f} runs)"
            )

    # No remaining batters info — fall back to static penalty (conservative)
    if wickets_fallen >= 7:
        penalty = -mu * 0.08   # reduced from 0.12 when batter info unknown
        return penalty, f"{wickets_fallen} wkts, no remaining batter data — static tail penalty"
    penalty = -mu * 0.04
    return penalty, f"{wickets_fallen} wkts, no remaining batter data — static depth penalty"



def _ball_runs(ball_str: str) -> int:
    """Runs scored on a single ball token from recent-overs input."""
    token = ball_str.strip().upper()
    if token == "W":
        return 0
    if token in ("WD", "WIDE", "NB", "NOBALL"):
        return 1
    try:
        return int(token)
    except ValueError:
        return 0


def _recent_overs_rpo(recent_overs: list[list[str]], num_overs: int) -> float | None:
    """Average RPO across the last `num_overs` completed overs (ball-by-ball input)."""
    completed = [o for o in (recent_overs or []) if o]
    if not completed or num_overs <= 0:
        return None
    use = completed[-num_overs:]
    total_runs = sum(sum(_ball_runs(b) for b in over) for over in use)
    return total_runs / len(use)


def _recent_pace_run_adjustment(
    recent_overs: list[list[str]] | None,
    remaining_overs: float,
    baseline_rpo: float,
    win_phase: str,
    venue: str,
    format_: str,
    proj_from: float,
    proj_to: float,
    current_over: float,
    current_phase: str,
) -> tuple[float, float | None, int, str | None]:
    """
    Explicit run delta from live recent-over pace vs static baseline RPO.
    Anchors heavily to last 3 overs in death when venue data is insufficient.
    Returns (run_adjustment, recent_live_rpo, lookback, note).
    """
    completed = [o for o in (recent_overs or []) if o]
    if not completed or remaining_overs <= 0 or baseline_rpo <= 0:
        return 0.0, None, 0, None

    lookback = min(len(completed), 3)
    recent_live_rpo = _recent_overs_rpo(recent_overs, lookback)
    if recent_live_rpo is None or recent_live_rpo <= 0:
        return 0.0, None, 0, None

    venue_stats = db.get_venue_stats(venue, format_) if venue and venue != "Unknown" else None
    venue_reliable = _venue_window_rpo(venue_stats, proj_from, proj_to) is not None

    if win_phase == "death" and not venue_reliable:
        w_recent = 0.85
    elif win_phase == "death":
        w_recent = 0.55
    else:
        w_recent = 0.35

    effective_rpo = w_recent * recent_live_rpo + (1.0 - w_recent) * baseline_rpo
    run_adj = (effective_rpo - baseline_rpo) * remaining_overs

    # --- RECENT PACE SAMPLE SIZE AWARE CALIBRATION ---
    if current_phase == 'powerplay':
        overs_observed = current_over
        if overs_observed <= 2.0:
            confidence_multiplier = 0.60
            cap = 3.0          # ≤2 overs: tiny sample, hard cap ±3
        elif overs_observed <= 4.0:
            confidence_multiplier = 0.85
            cap = 4.5          # 3–4 overs: growing confidence, cap ±4.5
        else:
            confidence_multiplier = 1.00
            cap = 5.5          # 5–6 overs: full confidence, cap ±5.5

        run_adj = run_adj * confidence_multiplier
        run_adj = max(min(run_adj, cap), -cap)

    elif current_phase == 'death':
        # Death overs (16-20) remain fully uncapped regardless of sample size 
        # as tactical death acceleration is inherently high-signal.
        pass
    note = (
        f"Last {lookback} overs: {recent_live_rpo:.1f} RPO"
        + (" (death anchor — venue data insufficient)" if win_phase == "death" and not venue_reliable else "")
    )
    return run_adj, recent_live_rpo, lookback, note


def _factor_adjustments_as_runs(
    full_innings_result: dict | None,
    window_overs: float,
    session_phase: str,
    base_projection: float = 0.0,
) -> tuple[float, dict]:
    """
    Convert signed % adjustments from the full engine into run deltas.

    Fix 2 (Dot Ball Drag Reduction):
    In middle overs, if a crease batter has faced 15+ balls with SR > 100,
    reduce the dot_ball_pct negative penalty by 40%.  A set anchor batter
    naturally rotates strike in middle overs — powerplay dot-ball patterns
    do not apply to a settled batter finding gaps.
    """
    if not full_innings_result or window_overs <= 0:
        return 0.0, {}

    skip = {"death_bowler_quota"} if session_phase != "death" else set()
    factor_labels = {
        "momentum":            "Momentum",
        "partnership_rate":    "Partnership",
        "batter_bowler":       "Matchup",
        "available_resources": "Resources",
        "pitch_weather":       "Pitch/weather",
        "team_strength":       "Team strength",
        "boundary_pct":        "Boundary%",
        "dot_ball_pct":        "Dot%",
        "toss":                "Toss",
        "death_bowler_quota":  "Bowler quota",
        "rrr_factor":          "Required RR",
    }

    # FIX 2: Check for a set batter (15+ balls, SR > 100) in middle overs
    # If found, reduce the dot_ball_pct drag penalty by 40%
    batsmen = (full_innings_result or {}).get("batsmen", [])
    has_set_batter_middle = False
    if session_phase == "middle" and batsmen:
        for b in batsmen[:2]:
            balls = b.get("balls", 0) or 0
            runs  = b.get("runs", 0)  or 0
            sr    = (runs / balls * 100) if balls > 0 else 0
            if balls >= 15 and sr > 100:
                has_set_batter_middle = True
                break

    # Per-factor cap: ±40% of the base projection so no single factor can
    # dominate the forecast. If base_projection is zero, use an absolute
    # minimum cap to allow small adjustments.
    per_factor_cap = abs(base_projection) * 0.4
    if per_factor_cap < 1.0:
        per_factor_cap = 1.0

    total = 0.0
    breakdown: dict = {}
    for key, data in full_innings_result.get("adjustments", {}).items():
        if key in skip:
            continue
        adj_pct = data.get("adj", 0) or 0
        if abs(adj_pct) < 0.01:
            continue

        # Dot ball drag reduction: set batter middle overs → 40% off penalty
        if key == "dot_ball_pct" and has_set_batter_middle and adj_pct < 0:
            adj_pct = adj_pct * 0.6  # 40% reduction

        run_delta = (adj_pct / 8.0) * window_overs
        # Cap this single factor's run delta to ±per_factor_cap
        run_delta = max(min(run_delta, per_factor_cap), -per_factor_cap)
        label = factor_labels.get(key, key)
        breakdown[label] = {"adj": round(run_delta, 2), "pct": adj_pct}
        total += run_delta
    return total, breakdown


def _window_historical_base(
    innings: int,
    format_: str,
    from_over: float,
    window_overs: float,
    current_runs: int,
    wickets_fallen: int,
    line: float,
    venue: str,
    is_odi: bool,
) -> tuple[float, str, dict]:
    """
    Base probability for a short run window.
    Uses match_position_stats on implied total (current + line) when n≥MIN_POSITION_SAMPLES,
    or weighted interpolation from nearest states when n is lower.
    Falls back to venue average RPO for the window overs when position data is absent.
    """
    from model.probability import cricket_over_to_db_over

    _POSITION_SOURCE_LABELS = {
        "exact": "Position table",
        "wicket_interpolation": "Position table (±1 wicket fallback)",
        "runs_interpolation": "Position table (±10 runs fallback)",
        "combined_interpolation": "Position table (±1 wicket, ±10 runs fallback)",
    }

    implied_total = current_runs + line
    db_over = cricket_over_to_db_over(from_over)
    detail = db.query_match_position_detail(
        innings, format_, db_over, wickets_fallen, current_runs, implied_total,
    )

    source = detail.get("source", "none")
    base_key = source.replace("_low_sample", "").replace("mens_fallback_", "")
    if (
        detail.get("pct") is not None
        and base_key in _POSITION_SOURCE_LABELS
    ):
        label = _POSITION_SOURCE_LABELS[base_key]
        if source.startswith("mens_fallback_"):
            label = f"Men's T20I {label.lower()}"
        n = detail.get("sample_count", 0)
        low_note = (
            f", interpolated n={n} < {config.MIN_POSITION_SAMPLES}"
            if n < config.MIN_POSITION_SAMPLES else f", n={n}"
        )
        return (
            detail["pct"],
            f"{label} (+{line:.0f} runs → {implied_total:.0f}{low_note})",
            detail,
        )

    venue_stats = db.get_venue_stats(venue, format_) if venue and venue != "Unknown" else None
    to_over = from_over + window_overs
    venue_rpo, venue_src = _resolved_venue_window_rpo(venue_stats, from_over, to_over, is_odi)

    sigma_table = PHASE_SIGMA_PER_OVER_ODI if is_odi else PHASE_SIGMA_PER_OVER
    base_sigma = _weighted_phase_stat(from_over, to_over, sigma_table, is_odi)
    mu = venue_rpo * window_overs
    sigma = base_sigma * math.sqrt(window_overs) if window_overs > 0 else base_sigma
    pct = _prob_exceed(mu, sigma, line)
    n = detail.get("sample_count", 0)
    window_label = f"overs {int(from_over) + 1}–{int(to_over)}"
    sparse_note = ""
    if detail.get("source") == "exact_low_sample":
        sparse_note = f"; exact match n={n} too sparse — "
    rpo_label = "Venue avg" if venue_src == "venue" else "Format avg"
    src = (
        f"{sparse_note}{rpo_label} {window_label} ~{venue_rpo:.1f} RPO "
        f"(no position interpolation, n={n})"
    )
    detail["venue_fallback_rpo"] = venue_rpo
    detail["venue_fallback_mu"] = mu
    detail["source"] = "venue_window_rpo"

    # Low-confidence flag: position lookup failed (we're in this fallback) AND the
    # RPO is a pure format average (no usable venue data). A weaker signal than a
    # venue/position-anchored prediction — caller surfaces a caveat to the user.
    venue_n = db.venue_sample_count(venue_stats)
    if venue_src == "format_avg" and (venue_stats is None or venue_n < 5):
        detail["low_confidence"] = True
        detail["low_confidence_reason"] = (
            "⚠️ Low confidence: based on generic format average only — "
            "no venue or position-specific data available."
        )
    return pct, src, detail


# ─────────────────────────────────────────────────────────────────
# OVER-WINDOW CALCULATOR
# ─────────────────────────────────────────────────────────────────

def _calc_window(
    from_over: float,
    to_over:   float,
    line:      float,
    momentum_adj_pct: float,
    all_bowlers:      list[dict],
    wickets_fallen:   int,
    innings:          int,
    dew_factor:       bool,
    phase:            str,
    format_:          str = "",
    full_innings_result: dict | None = None,
    batting_team:     str = "",
    bowling_team:     str = "",
    venue:            str = "",
    session_phase:    str | None = None,
    line_mode:        str = "window_only",  # "session_total" | "window_only"
    runs_in_session:  int = 0,
    current_runs:     int = 0,
    current_over:     float = 0.0,
    recent_overs:     list[list[str]] | None = None,
    remaining_batters: list[dict] | None = None,
    target:           int | None = None,
    db_path:          str | None = None,
) -> dict:
    """
    Core calculation for any fixed over-window.
    Returns a result dict compatible with render_probability_card.

    Args:
        from_over:        start of window (current over, fractional OK)
        to_over:          end of window (e.g. 18.0 = end of over 18)
        line:             runs line to beat
        momentum_adj_pct: signed % from probability engine (-8 to +8)
        all_bowlers:      list of bowler dicts with overs_today/runs_today/wickets
        wickets_fallen:   wickets lost so far
        innings:          1 or 2
        dew_factor:       True if dew expected
        phase:            current phase (for display)
    """
    window_overs = to_over - from_over
    if window_overs <= 0:
        return _empty_result(line, phase)

    # Only project overs not yet bowled — never re-count completed session overs
    remaining_overs = max(0.0, to_over - current_over) if current_over > 0 else window_overs
    if remaining_overs <= 0:
        return _empty_result(line, phase)

    proj_from = current_over if current_over > 0 else from_over
    proj_to = to_over

    is_odi = format_ in config.ODI_FORMATS if format_ else False
    sigma_table = PHASE_SIGMA_PER_OVER_ODI if is_odi else PHASE_SIGMA_PER_OVER
    max_overs   = 50 if is_odi else 20

    win_phase = session_phase or _session_phase_for_window(proj_from, proj_to, is_odi)

    # ── 1. Static baseline RPO (no recent pace — applied as explicit run factor below) ──
    if line_mode == "window_only":
        baseline_rpo, rpo_notes = _blended_window_rpo(
            proj_from, proj_to, remaining_overs,
            batting_team, bowling_team, venue, format_, is_odi, win_phase,
            recent_live_rpo=None,
            recent_lookback=0,
        )
    else:
        baseline_rpo, rpo_notes = _blended_phase_rpo(
            win_phase, proj_from, proj_to, batting_team, bowling_team, venue, format_, is_odi,
        )
    base_sigma = _weighted_phase_stat(proj_from, proj_to, sigma_table, is_odi)
    generic_rpo = _weighted_phase_stat(
        proj_from, proj_to,
        PHASE_TYPICAL_RPO_ODI if is_odi else PHASE_TYPICAL_RPO,
        is_odi,
    )

    eco_threshold = 4.8 if is_odi else 5.8

    recent_pace_adj, recent_live_rpo, recent_lookback, pace_note = _recent_pace_run_adjustment(
        recent_overs, remaining_overs, baseline_rpo, win_phase,
        venue, format_, proj_from, proj_to, current_over, phase,
    )
    if pace_note:
        rpo_notes.append(pace_note)

    # Base projection (runs expected from static baseline RPO over remaining overs)
    base_projection = baseline_rpo * remaining_overs

    # Cap any single non-modifier adjustment (e.g., recent pace) to ±40% of base projection
    cap_value = abs(base_projection) * 0.4
    if cap_value < 1.0:
        cap_value = 1.0
    if recent_pace_adj is not None:
        recent_pace_adj = max(min(recent_pace_adj, cap_value), -cap_value)

    mu = base_projection + recent_pace_adj

    # ── 2. All signed factor adjustments from full innings engine ──
    factor_runs, factor_breakdown = _factor_adjustments_as_runs(
        full_innings_result, remaining_overs, win_phase, base_projection,
    )
    mu += factor_runs

    # ── 3. Exceptional bowler reduction (live only) ──
    bowler_adj_runs = 0.0
    exceptional_names = []
    for b in (all_bowlers or []):
        ov_bowled = b.get("overs_today", 0)
        if ov_bowled < 2:
            continue
        balls = int(ov_bowled) * 6 + round((ov_bowled % 1) * 10)
        eco   = (b.get("runs_today", 0) / balls * 6) if balls > 0 else 99.0
        ov_rem = b.get("overs_remaining", 0)
        if ov_rem > 0 and eco < eco_threshold:
            reduction = min(ov_rem, remaining_overs) * 0.85
            bowler_adj_runs -= reduction
            exceptional_names.append(b["name"])
    mu += bowler_adj_runs

    # ── 4. Dynamic Team Depth Index (replaces hardcoded wicket penalties) ──
    # Penalty only fires if the next incoming batter has death SR < 115.
    # Elite or unknown batters: zero penalty regardless of wicket count.
    wicket_adj_runs, wicket_note = _team_depth_penalty(
        mu, wickets_fallen, remaining_batters or [], format_, db_path
    )
    if wicket_adj_runs != 0.0:
        mu += wicket_adj_runs

    # ── 5. High-conviction override detection ──────────────────────
    # When: Bowling Quota Trap fired AND recent 3-over pace > 10 RPO
    # Effect: relax the sigma-based low-confidence constraint so live
    # momentum/matchup factors can push a 66%+ probability to high confidence.
    quota_trap_fired = "bowling_quota_trap" in (
        full_innings_result.get("modifiers_fired", "") if full_innings_result else ""
    )
    high_conviction = quota_trap_fired and (recent_live_rpo or 0.0) > 10.0

    # ── 5. Dew factor bonus (2nd innings death) ──
    dew_adj_runs = 0.0
    dew_trigger  = 40.0 if is_odi else 17.0  # T20 death = overs 17–20
    if dew_factor and innings == 2 and proj_from >= dew_trigger - 1:
        dew_adj_runs = mu * 0.05
        mu += dew_adj_runs

    # ── 5b. Required Run Rate factor (chasing window markets) ──────
    # A chasing side scores toward the rate the chase demands, so the expected
    # window runs shift partway toward the required rate (capped). This wires the
    # RRR signal into window markets (Next X Overs), not just Total Innings Score.
    rrr_adj_runs = 0.0
    rrr_note = ""
    if innings == 2 and target and target > 0 and remaining_overs > 0:
        overs_left_innings = max(0.0, max_overs - current_over)
        runs_needed = max(0, target - current_runs)
        if overs_left_innings > 0:
            rrr = runs_needed / overs_left_innings
            window_rpo = mu / remaining_overs if remaining_overs > 0 else baseline_rpo
            _RRR_PULL = 0.40   # batting side closes ~40% of the rate gap
            rrr_adj_runs = (rrr - window_rpo) * _RRR_PULL * remaining_overs
            rcap = max(1.0, abs(base_projection) * 0.35)
            rrr_adj_runs = max(min(rrr_adj_runs, rcap), -rcap)
            mu += rrr_adj_runs
            rrr_note = (
                f"Required RR {rrr:.1f} vs window {window_rpo:.1f} RPO "
                f"({rrr_adj_runs:+.1f} runs, chasing {target})"
            )

    mu = max(0, mu)
    mu_remaining = mu

    # Session markets: line is FULL phase total; mu_remaining is future overs only
    if line_mode == "session_total":
        mu_compare = runs_in_session + mu_remaining
        line_compare = line
    else:
        mu_compare = mu_remaining
        line_compare = line

    # ── 6. Sigma (total std dev for remaining window) ──
    sigma = base_sigma * math.sqrt(remaining_overs)

    # Insufficient-data guard: a non-positive expected projection or sigma means the
    # base could not be computed — never emit a confident verdict from broken math.
    if mu_remaining <= 0 or sigma <= 0:
        return _insufficient_result(
            line, win_phase,
            f"projected window runs={mu_remaining:.1f}, sigma={sigma:.1f}.",
        )

    # ── 7. Probability ──
    if line_mode == "session_total":
        base_mu_compare = runs_in_session + generic_rpo * remaining_overs
    else:
        base_mu_compare = generic_rpo * remaining_overs

    window_position_debug = None
    base_source = "generic RPO"
    if current_runs > 0:
        lookup_line = line
        if line_mode == "session_total":
            lookup_line = max(0.0, line - runs_in_session)
        if lookup_line > 0:
            base_prob, base_source, window_position_debug = _window_historical_base(
                innings, format_, proj_from, remaining_overs,
                current_runs, wickets_fallen, lookup_line, venue, is_odi,
            )
        else:
            base_prob = _prob_exceed(base_mu_compare, sigma, line_compare)
    else:
        base_prob = _prob_exceed(base_mu_compare, sigma, line_compare)

    # ── 7. Z-score execution gate ──────────────────────────────────
    # Z = (expected_runs - market_line) / sigma
    # Replaces all internal modifier/interpolation checks as the
    # single authoritative execution decision.
    final_prob = _prob_exceed(mu_compare, sigma, line_compare)

    verdict, final_prob = _zscore_verdict(
        mu_compare, line_compare, sigma, final_prob,
        high_conviction=high_conviction,
    )

    # ── 8. Sanity floor: easy-line protection (Issue 3) ───────────
    # If the line implies a required RPO below the format average for this
    # phase, it is an 'easy' line and probability must be >= 35%.
    _FORMAT_AVG_PHASE_RPO = {
        "Women's T20I": {"powerplay": 6.8, "middle": 6.2, "death": 8.5},
        "T20I":         {"powerplay": 7.5, "middle": 7.0, "death": 9.0},
        "Men's ODI":    {"powerplay": 5.8, "middle": 4.8, "death": 8.2},
        "Women's ODI":  {"powerplay": 5.5, "middle": 4.5, "death": 7.8},
    }
    _EASY_LINE_PROB_FLOOR = 35.0
    sanity_floor_note = None
    if remaining_overs > 0 and line_compare > 0:
        implied_rpo = line_compare / remaining_overs
        phase_avg_rpo = _FORMAT_AVG_PHASE_RPO.get(format_, {}).get(win_phase, 6.2)
        if implied_rpo < phase_avg_rpo and final_prob < _EASY_LINE_PROB_FLOOR:
            final_prob = _EASY_LINE_PROB_FLOOR
            sanity_floor_note = (
                f"Easy-line floor {_EASY_LINE_PROB_FLOOR:.0f}%: "
                f"line RPO {implied_rpo:.1f} < format avg {phase_avg_rpo:.1f} ({win_phase})"
            )
            verdict = _get_verdict(final_prob)

    # ── Low-confidence detection (format-average-only base) ──────
    low_confidence = bool(window_position_debug and window_position_debug.get("low_confidence"))
    low_conf_note = (
        window_position_debug.get("low_confidence_reason") if window_position_debug else None
    )
    # Also flag when there is no live-runs anchor and the base stayed generic.
    if not low_confidence and base_source == "generic RPO":
        _vn = db.venue_sample_count(db.get_venue_stats(venue, format_)) if venue and venue != "Unknown" else 0
        if _vn < 5:
            low_confidence = True
            low_conf_note = (
                "⚠️ Low confidence: based on generic format average only — "
                "no venue or position-specific data available."
            )

    # ── 9. Insight ──
    window_label = f"{int(proj_from) + 1}–{int(to_over)}"
    if line_mode == "session_total":
        insight_parts = [
            f"{win_phase.title()} session: {runs_in_session} runs so far + "
            f"~{mu_remaining:.1f} projected ({remaining_overs:.0f} overs left, {window_label}) "
            f"= ~{mu_compare:.1f} total vs line {line:.0f}.",
        ]
    else:
        effective_rpo = mu_remaining / remaining_overs if remaining_overs > 0 else baseline_rpo
        insight_parts = [
            f"Overs {window_label}: expecting ~{mu_remaining:.1f} runs "
            f"({effective_rpo:.1f} RPO × {remaining_overs:.0f} overs) vs line {line:.0f}.",
        ]
    if rpo_notes:
        insight_parts.append("Sources: " + "; ".join(rpo_notes[:3]) + ".")
    if factor_runs != 0:
        insight_parts.append(f"Model factors adjusted expected runs by {factor_runs:+.1f}.")
    if recent_pace_adj != 0 and recent_live_rpo is not None and abs(recent_pace_adj) >= 0.5:
        insight_parts.append(
            f"Live {recent_lookback}-over pace ({recent_live_rpo:.1f} RPO) "
            f"vs generic window baseline ({recent_pace_adj:+.1f} runs)."
        )
    if exceptional_names:
        insight_parts.append(
            f"{', '.join(exceptional_names[:2])} bowling exceptionally — "
            f"expected output reduced by {abs(bowler_adj_runs):.1f} runs."
        )
    if wickets_fallen >= 5:
        insight_parts.append(
            f"{wickets_fallen} wickets down — tail risk applied ({abs(wicket_adj_runs):.1f} runs)."
        )
    if dew_adj_runs > 0:
        insight_parts.append("Dew factor added +{:.1f} expected runs.".format(dew_adj_runs))
    if rrr_note:
        insight_parts.append(rrr_note + ".")
    if sanity_floor_note:
        insight_parts.append(sanity_floor_note)
    if low_conf_note:
        insight_parts.insert(0, low_conf_note)   # surface the caveat first

    return {
        "final_probability": final_prob,
        "base_probability":  base_prob,
        "base_source":       base_source,
        "position_debug":    window_position_debug,
        "total_adjustment":  round(mu_compare - base_mu_compare, 1),
        "modifier_total":    0.0,
        "modifiers_applied": [],
        "modifiers_fired":   "",
        "adjustments": {
            **{k: v for k, v in factor_breakdown.items()},
            "exceptional_bowler": {"adj": round(bowler_adj_runs, 1)},
            "wicket_pressure":  {"adj": round(wicket_adj_runs, 1)},
            "dew_factor":       {"adj": round(dew_adj_runs, 1)},
            "recent_pace":      {"adj": round(recent_pace_adj, 1)},
            **(
                {"rrr_factor": {"adj": round(rrr_adj_runs, 1), "desc": rrr_note}}
                if (innings == 2 and target and target > 0) else {}
            ),
        },
        "verdict":       verdict,
        "low_confidence": low_confidence,
        "phase":         win_phase,   # FIX Issue 5: target window phase, not current_over phase
        "key_insight":   " ".join(insight_parts),
        "adjustment_unit": "runs",
        "market_window": {
            "from_over":       proj_from,
            "to_over":         to_over,
            "window_overs":    remaining_overs,
            "expected_runs":   round(mu_remaining, 1),
            "session_total":   round(mu_compare, 1) if line_mode == "session_total" else None,
            "runs_in_session": runs_in_session if line_mode == "session_total" else None,
            "base_rpo":        round(
                (baseline_rpo * remaining_overs + recent_pace_adj) / remaining_overs
                if remaining_overs > 0 else baseline_rpo,
                2,
            ),
            "sigma":           round(sigma, 1),
            "line_mode":       line_mode,
        },
    }


def _empty_result(line: float, phase: str = "middle") -> dict:
    return {
        "final_probability": 50.0,
        "base_probability":  50.0,
        "total_adjustment":  0,
        "modifier_total":    0,
        "modifiers_applied": [],
        "modifiers_fired":   "",
        "adjustments":       {},
        "verdict":           "LOW CONFIDENCE",
        "phase":             phase,
        "key_insight":       "Cannot calculate — invalid over window.",
        "market_window":     None,
    }


# Forced whenever a window base cannot be computed — never a confident number.
INSUFFICIENT_DATA = "⚠️ INSUFFICIENT DATA — cannot calculate"


def _insufficient_result(line: float, phase: str = "middle", reason: str = "") -> dict:
    """Returned when the window base is broken (zero/invalid expected runs).
    Explicitly neutral with an insufficient-data verdict so a calculation failure
    can never surface as a confident forecast."""
    return {
        "final_probability": 50.0,
        "base_probability":  50.0,
        "total_adjustment":  0,
        "modifier_total":    0,
        "modifiers_applied": [],
        "modifiers_fired":   "",
        "adjustments":       {},
        "verdict":           INSUFFICIENT_DATA,
        "status":            INSUFFICIENT_DATA,
        "degraded_mode":     True,
        "phase":             phase,
        "key_insight":       f"⚠️ Insufficient data — base could not be computed. {reason}".strip(),
        "market_window":     None,
    }


def _session_bounds(session_phase: str, is_odi: bool) -> tuple[int, int]:
    """1-indexed inclusive over numbers for each phase."""
    if is_odi:
        bounds = {"powerplay": (1, 10), "middle": (11, 40), "death": (41, 50)}
    else:
        bounds = {"powerplay": (1, 6), "middle": (7, 16), "death": (17, 20)}
    return bounds[session_phase]


def _estimate_phase_total_runs(
    phase: str,
    batting_team: str,
    format_: str,
    is_odi: bool,
) -> float:
    """Historical average runs scored in a full phase."""
    bat = db.get_team_batting_stats(batting_team, format_) if batting_team else None
    if phase == "powerplay":
        if bat and bat.get("avg_pp_score"):
            return float(bat["avg_pp_score"])
        return (6.8 * 6) if not is_odi else (5.8 * 10)
    if phase == "death":
        if bat and bat.get("avg_death_score"):
            return float(bat["avg_death_score"])
        return (8.5 * 4) if not is_odi else (8.2 * 10)
    # middle
    if bat and bat.get("avg_total"):
        pp = float(bat.get("avg_pp_score") or _estimate_phase_total_runs("powerplay", batting_team, format_, is_odi))
        death = float(bat.get("avg_death_score") or _estimate_phase_total_runs("death", batting_team, format_, is_odi))
        return max(0.0, float(bat["avg_total"]) - pp - death)
    return (6.2 * 10) if not is_odi else (4.8 * 30)


def _session_runs_so_far(
    session_phase: str,
    current_over: float,
    current_runs: int,
    batting_team: str,
    format_: str,
    is_odi: bool,
) -> int:
    """
    Runs already scored in this session phase.
    PP: all innings runs while still inside overs 1–6.
    Middle/death: innings total minus estimated prior-phase runs.
    """
    lo, hi = _session_bounds(session_phase, is_odi)
    cur = current_over

    if session_phase == "powerplay":
        if cur <= hi + 0.001:
            return current_runs
        return int(_estimate_phase_total_runs("powerplay", batting_team, format_, is_odi))

    if cur < lo - 0.001:
        return 0

    pp_runs = _estimate_phase_total_runs("powerplay", batting_team, format_, is_odi)
    if session_phase == "middle":
        if cur <= hi + 0.001:
            return max(0, current_runs - int(pp_runs))
        return int(_estimate_phase_total_runs("middle", batting_team, format_, is_odi))

    mid_runs = _estimate_phase_total_runs("middle", batting_team, format_, is_odi)
    if cur <= hi + 0.001:
        return max(0, current_runs - int(pp_runs) - int(mid_runs))
    return int(_estimate_phase_total_runs("death", batting_team, format_, is_odi))


def _session_phase_window(
    target_phase: str,
    current_over: float,
    is_odi: bool,
    max_overs: int,
) -> tuple[float, float]:
    """
    Over-window boundaries for a named session (fractional end-of-over notation).
    T20: PP 1–6, middle 7–16, death 17–20 (last 4 overs).
    """
    if is_odi:
        bounds = {"powerplay": (0.0, 10.0), "middle": (10.0, 40.0), "death": (40.0, 50.0)}
    else:
        bounds = {"powerplay": (0.0, 6.0), "middle": (6.0, 16.0), "death": (16.0, 20.0)}
    start, end = bounds.get(target_phase, (current_over, float(max_overs)))
    from_ov = max(current_over, start)
    to_ov   = min(end, float(max_overs))
    return from_ov, to_ov


# ─────────────────────────────────────────────────────────────────
# PUBLIC ROUTER
# ─────────────────────────────────────────────────────────────────

def route_market(
    market_type:       str,
    line:              float,
    current_over:      float,
    innings:           int,
    phase:             str,
    full_innings_result: dict,
    momentum_adj_pct:  float,
    all_bowlers:       list[dict],
    wickets_fallen:    int,
    dew_factor:        bool,
    remaining_bowlers: list[dict],
    format_:           str = "",
    custom_from_over:  int | None = None,
    custom_to_over:    int | None = None,
    recent_overs:      list[list[str]] | None = None,
    remaining_batters: list[dict] | None = None,
    target:            int | None = None,
    db_path:           str | None = None,
) -> dict:
    """
    Route the probability calculation to the correct market engine.

    For "Total Innings Score" → returns the full innings result unchanged.
    For all other markets    → calls the over-window calculator.

    The returned dict is always compatible with render_probability_card.
    """
    # Build shared context
    is_odi   = format_ in config.ODI_FORMATS if format_ else False
    max_overs = 50 if is_odi else 20
    ctx = {
        "market_type":  market_type,
        "innings":      full_innings_result.get("innings", innings),
        "line":         line,
        "over_number":  current_over,
        "batting_team": full_innings_result.get("batting_team", ""),
        "bowling_team": full_innings_result.get("bowling_team", ""),
        "venue":        full_innings_result.get("venue", ""),
        "format":       format_ or full_innings_result.get("format", config.PRIMARY_FORMAT),
        "match_label":  full_innings_result.get("match_label", ""),
        "timestamp":    full_innings_result.get("timestamp", ""),
        "current_runs": full_innings_result.get("current_runs", 0),
        "wickets_fallen": wickets_fallen,
    }

    # ── 1. Total Innings Score — use existing full engine result ──
    if market_type == "Total Innings Score":
        result = dict(full_innings_result)
        result["market_type"] = "Total Innings Score"
        return result

    # If the full-innings base was flagged insufficient/degraded, a Total-Innings
    # line is already handled above. Window markets re-derive their own base below,
    # so we only proceed when that base is computable.

    # ── Resolve over window for each market ──
    if market_type == "Next 2 Overs Runs":
        from_ov = current_over
        to_ov   = min(current_over + 2.0, float(max_overs))

    elif market_type == "Next 4 Overs Runs":
        from_ov = current_over
        to_ov   = min(current_over + 4.0, float(max_overs))

    elif market_type in SESSION_PHASE_MARKETS:
        session_phase_key = SESSION_PHASE_MARKETS[market_type]
        _, to_ov = _session_phase_window(session_phase_key, current_over, is_odi, max_overs)
        from_ov = current_over

    elif market_type == _LEGACY_SESSION_MARKET:
        _, to_ov = _session_phase_window(phase, current_over, is_odi, max_overs)
        from_ov = current_over

    elif market_type == "Custom: Overs X to Y":
        # User enters 1-indexed overs inclusive (e.g. 17–20 = four overs).
        # Window math uses fractional end-of-over boundaries: [from-1, to].
        raw_from = float(custom_from_over or int(current_over) + 1)
        raw_to   = float(custom_to_over   or min(int(current_over) + 5, max_overs))
        from_ov = max(raw_from - 1, current_over)
        to_ov   = min(raw_to, float(max_overs))

    else:
        result = dict(full_innings_result)
        result["market_type"] = market_type
        return result

    # ── Call window calculator ──
    win_phase = (
        SESSION_PHASE_MARKETS.get(market_type)
        or phase
    )
    is_session_line = market_type in SESSION_PHASE_MARKETS or market_type == _LEGACY_SESSION_MARKET
    runs_so_far = 0
    if is_session_line:
        runs_so_far = _session_runs_so_far(
            win_phase,
            current_over,
            int(ctx.get("current_runs", 0)),
            ctx.get("batting_team", ""),
            format_ or ctx.get("format", config.PRIMARY_FORMAT),
            is_odi,
        )
    result = _calc_window(
        from_over        = from_ov,
        to_over          = to_ov,
        line             = line,
        momentum_adj_pct = momentum_adj_pct,
        all_bowlers      = all_bowlers,
        wickets_fallen   = wickets_fallen,
        innings          = innings,
        dew_factor       = dew_factor,
        phase            = phase,
        format_          = format_,
        full_innings_result = full_innings_result,
        batting_team     = ctx.get("batting_team", ""),
        bowling_team     = ctx.get("bowling_team", ""),
        venue            = ctx.get("venue", ""),
        session_phase    = win_phase if is_session_line else None,
        line_mode        = "session_total" if is_session_line else "window_only",
        runs_in_session  = runs_so_far,
        current_runs     = int(ctx.get("current_runs", 0)),
        current_over     = current_over,
        recent_overs     = recent_overs,
        remaining_batters = remaining_batters,
        target           = target,
        db_path          = db_path,
    )

    # ── Merge context ──
    result.update(ctx)

    # ── Re-evaluate ALL 9 modifiers using the target window's phase ──────────
    # FIX 1: Previously only spin_death_mismatch was patched — and even that
    # was silently failing because get_target_phase was undefined (NameError
    # swallowed by bare except). All 9 modifiers now receive:
    #   current_over  = from_ov   → phase-gated checks see the WINDOW's phase
    #   target_phase  = _session_phase_for_window(from_ov, to_ov)  → explicit
    # FIX 4: verdict is set via get_verdict_display() — unified emoji labels.
    if full_innings_result:
        try:
            from model.modifiers import evaluate_all_modifiers
            from model.probability import get_verdict_display as _gvd

            # Dominant phase of the PREDICTION window, not current over's phase
            target_phase = _session_phase_for_window(from_ov, to_ov, is_odi)

            # Use window's start over so all phase-gated modifiers check the
            # target window phase (e.g. death modifiers only fire for death windows)
            effective_over = from_ov

            batsmen          = full_innings_result.get("batsmen", [])
            all_scorers      = full_innings_result.get("all_scorers")
            balls_this_inn   = full_innings_result.get("balls_this_innings", [])
            total_balls      = int(effective_over) * 6
            batting_team_ctx = ctx.get("batting_team", "")
            bowling_team_ctx = ctx.get("bowling_team", "")
            venue_ctx        = ctx.get("venue", "")
            current_runs_ctx = int(ctx.get("current_runs", 0))

            all_mods = evaluate_all_modifiers(
                innings            = innings,
                batting_team       = batting_team_ctx,
                bowling_team       = bowling_team_ctx,
                current_runs       = current_runs_ctx,
                current_over       = effective_over,
                wickets_fallen     = wickets_fallen,
                total_balls_bowled = total_balls,
                format_            = format_,
                target_phase       = target_phase,
                venue              = venue_ctx,
                batsmen            = batsmen,
                all_bowlers        = all_bowlers,
                remaining_bowlers  = remaining_bowlers,
                remaining_batters  = remaining_batters or [],
                balls_this_innings = balls_this_inn,
                all_scorers        = all_scorers,
                db_path            = db_path,
            )

            modifiers_applied = [m for m in all_mods if m["fired"]]
            mod_total = sum(m.get("adjustment", 0) for m in modifiers_applied)

            # Apply modifier total to window probability (Z-score gate already applied
            # inside _calc_window; modifiers are an additive layer on top)
            prob = round(max(3.0, min(97.0, result["final_probability"] + mod_total)), 1)

            result["modifiers_applied"]    = modifiers_applied
            result["modifier_diagnostics"] = all_mods
            result["modifier_total"]       = round(mod_total, 1)
            result["final_probability"]    = prob
            result["modifiers_fired"]      = ",".join(m["name"] for m in modifiers_applied)
            result["verdict"]              = _gvd(prob)  # unified emoji verdict

        except Exception as _mod_exc:
            logger.warning("Window modifier re-evaluation failed: %s", _mod_exc)
            # Fallback: inherit full-innings modifiers unchanged
            result["modifiers_applied"]    = full_innings_result.get("modifiers_applied", [])
            result["modifier_diagnostics"] = full_innings_result.get("modifier_diagnostics", [])
            result["modifiers_fired"]      = full_innings_result.get("modifiers_fired", "")
            result["modifier_total"]       = full_innings_result.get("modifier_total", 0)

        if not result.get("position_debug"):
            result["position_debug"] = full_innings_result.get("position_debug")

    return result


# ─────────────────────────────────────────────────────────────────
# MARKET DESCRIPTION HELPER (for UI display)
# ─────────────────────────────────────────────────────────────────

def market_description(
    market_type:    str,
    current_over:   float,
    phase:          str,
    custom_from:    int | None = None,
    custom_to:      int | None = None,
    format_:        str = "",
) -> str:
    """Return a short human-readable description of the market being analysed."""
    cur      = int(current_over)
    is_odi   = format_ in config.ODI_FORMATS if format_ else False
    max_overs = 50 if is_odi else 20
    if market_type == "Total Innings Score":
        return f"Full innings total ({max_overs} overs)"
    elif market_type == "Next 2 Overs Runs":
        return f"Runs in overs {cur+1}–{min(cur+2, max_overs)}"
    elif market_type == "Next 4 Overs Runs":
        return f"Runs in overs {cur+1}–{min(cur+4, max_overs)}"
    elif market_type in SESSION_PHASE_MARKETS:
        target = SESSION_PHASE_MARKETS[market_type]
        if is_odi:
            spans = {"powerplay": (1, 10), "middle": (11, 40), "death": (41, 50)}
            labels = {"powerplay": "Powerplay", "middle": "Middle overs", "death": "Death overs"}
        else:
            spans = {"powerplay": (1, 6), "middle": (7, 16), "death": (17, 20)}
            labels = {"powerplay": "Powerplay", "middle": "Middle overs", "death": "Death overs"}
        lo_base, hi = spans.get(target, (cur + 1, max_overs))
        lbl = labels.get(target, target)
        if cur >= hi:
            return f"{lbl} session total (phase complete)"
        # Use remaining overs from current point to the session end so the
        # UI and factor calculations are consistent. E.g. current_over=14.5
        # for a death session → show overs 15–20.
        lo = int(cur) + 1
        if lo < 1:
            lo = 1
        return f"{lbl} session total (overs {lo}–{hi})"
    elif market_type == _LEGACY_SESSION_MARKET:
        legacy = {
            "powerplay": "Powerplay Session Runs",
            "middle":    "Middle Session Runs",
            "death":     "Death Session Runs",
        }
        return market_description(
            legacy.get(phase, "Middle Session Runs"),
            current_over, phase, custom_from, custom_to, format_,
        )
    elif market_type == "Custom: Overs X to Y":
        return f"Runs in overs {custom_from}–{custom_to}"
    return market_type


# ─────────────────────────────────────────────────────────────────
# INTERVAL SCENARIOS  (session-interval runs forecast off the engine trajectory)
# ─────────────────────────────────────────────────────────────────
#
# An interval scenario forecasts the runs scored INSIDE a fixed short interval.
# Each is only offered when the live scorecard sits EXACTLY at its entry over
# (3.0 → overs 4–6, 6.0 → overs 7–10). The target score for an interval scenario
# is the interval runs only (e.g. 24.5).
#
# This is a PARALLEL path: the full-innings engine (analyse → route_market →
# checkpoint models) is never routed here, so V1.0 stays byte-for-byte identical.

FULL_INNINGS_TARGET = "Full Innings (Default)"

MICRO_MARKETS = {
    "Next 3 Overs (Overs 4-6)":  {
        "requires_over": 3.0, "from_over": 3.0, "to_over": 6.0,  "label": "Overs 4–6",
    },
    "Next 4 Overs (Overs 7-10)": {
        "requires_over": 6.0, "from_over": 6.0, "to_over": 10.0, "label": "Overs 7–10",
    },
}


def available_micro_markets(current_over: float, tol: float = 1e-6) -> list[str]:
    """Micro-markets offered at the current over (exact-over gating).

    Returns only those whose entry over matches `current_over` exactly (within a
    float tolerance) — e.g. at 3.0 → ['Next 3 Overs (Overs 4-6)'], at 6.0 →
    ['Next 4 Overs (Overs 7-10)'], otherwise [].
    """
    return [
        name for name, spec in MICRO_MARKETS.items()
        if abs(float(current_over) - spec["requires_over"]) <= tol
    ]


def _line_anchored_probability(
    projection: float,
    line: float,
    sigma: float,
    incoming_prob: float | None = None,
) -> tuple[float, str, dict]:
    """Probability of finishing ABOVE target, anchored to (projection − target).

    Mirrors EXACTLY the locked V1.0 logic in probability.apply_final_cap:
        projection == target  ->  50%
        projection <  target  ->  above-target prob < 50%   (below-target forecast)
        projection >  target  ->  above-target prob > 50%

    The calibrated sigmoid of z = (projection − target) / sigma is the source of
    truth for direction and base magnitude. An optional `incoming_prob` (e.g.
    from live signals) may only ADD conviction in the projection's direction and
    can never flip the side. With no incoming_prob, the probability IS the
    calibrated value — a pure Expected-Interval-Runs-vs-target comparison.
    """
    from model.probability import _sigmoid, get_verdict_display

    if sigma <= 0:
        z = 0.0
        line_prob = 50.0
        prob = 50.0
    else:
        z = (projection - line) / sigma
        line_prob = _sigmoid(z) * 100.0
        if incoming_prob is None:
            prob = line_prob
        elif z > 0:
            prob = max(line_prob, incoming_prob)   # above-target: never below calibrated prob
        elif z < 0:
            prob = min(line_prob, incoming_prob)   # below-target: never above calibrated prob
        else:
            prob = 50.0

    prob = max(config.PROBABILITY_MIN, min(config.PROBABILITY_MAX, prob))
    verdict = get_verdict_display(prob)
    anchor = {
        "z":          round(z, 3),
        "line_prob":  round(line_prob, 1),
        "projection": round(projection, 1),
        "line":       line,
    }
    return round(prob, 1), verdict, anchor


def calc_micro_market(
    market_label: str,
    line: float,
    context: dict,
    recent_overs: list[list[str]] | None = None,
    db_path: str | None = None,
) -> dict:
    """Forecast a session-interval scenario off the engine's trajectory.

    Methodology (faithful to the locked V1.0 anchoring logic):
      Step A — project the cumulative score at the END of the interval using the
               engine's blended window RPO (batting team + venue + opposition +
               live recent pace), i.e. current_runs + projected interval runs.
      Step B — subtract the current live runs → "Expected Interval Runs".
      Compare Expected Interval Runs vs the interval target using the SAME
      line-anchoring logic as the full innings (_line_anchored_probability): the
      above-target probability sits strictly on the correct side of 50% of
      (projection − target).

    `context` carries the match state (format, teams, venue, innings, over_number,
    current_runs, wickets_fallen, match_label, timestamp) — typically a thin dict
    built from live data. The full-innings probability is NOT required or used.
    """
    if market_label not in MICRO_MARKETS:
        raise ValueError(f"Unknown interval scenario: {market_label!r}")
    spec = MICRO_MARKETS[market_label]

    fmt          = context.get("format", "") or config.PRIMARY_FORMAT
    is_odi       = fmt in config.ODI_FORMATS if fmt else False
    batting_team = context.get("batting_team", "")
    bowling_team = context.get("bowling_team", "")
    venue        = context.get("venue", "")
    innings      = context.get("innings", 1)
    current_over = float(context.get("over_number", spec["requires_over"]))
    current_runs = int(context.get("current_runs", 0))
    wickets      = int(context.get("wickets_fallen", 0))

    from_over      = spec["from_over"]
    to_over        = spec["to_over"]
    interval_overs = to_over - from_over
    win_phase      = _session_phase_for_window(from_over, to_over, is_odi)

    # ── Step A: engine projection of runs scored inside the interval ─────────
    completed_overs = [o for o in (recent_overs or []) if o]
    lookback        = min(len(completed_overs), 3)
    recent_rpo      = _recent_overs_rpo(recent_overs, lookback) if lookback else None
    blended_rpo, rpo_notes = _blended_window_rpo(
        from_over, to_over, interval_overs,
        batting_team, bowling_team, venue, fmt, is_odi, win_phase,
        recent_live_rpo=recent_rpo, recent_lookback=lookback,
    )
    expected_interval_runs           = blended_rpo * interval_overs
    projected_score_at_interval_end  = current_runs + expected_interval_runs

    # ── Step B: Expected Interval Runs = cumulative projection − live runs ───
    expected_interval_runs = projected_score_at_interval_end - current_runs

    # ── Interval sigma (same per-phase variance the window engine uses) ──────
    sigma_table    = PHASE_SIGMA_PER_OVER_ODI if is_odi else PHASE_SIGMA_PER_OVER
    base_sigma     = _weighted_phase_stat(from_over, to_over, sigma_table, is_odi)
    interval_sigma = base_sigma * math.sqrt(interval_overs) if interval_overs > 0 else base_sigma

    # ── Probability + verdict via the locked line-anchoring logic ────────────
    prob, verdict, anchor = _line_anchored_probability(
        expected_interval_runs, line, interval_sigma,
    )

    side = (
        ">" if expected_interval_runs > line else
        "<" if expected_interval_runs < line else "="
    )
    insight = (
        f"{spec['label']} interval forecast: engine projects ~{expected_interval_runs:.1f} runs "
        f"in the interval ({blended_rpo:.1f} RPO × {int(interval_overs)} overs; "
        f"score at over {int(to_over)} ≈ {projected_score_at_interval_end:.0f}) "
        f"vs target {line:.1f} (σ ±{interval_sigma:.1f}). "
        f"Projection {side} target → {verdict}."
    )
    if rpo_notes:
        insight += " Sources: " + "; ".join(rpo_notes[:3]) + "."

    return {
        "market_type":       market_label,
        "is_micro_market":   True,
        "final_probability": prob,
        "verdict":           verdict,
        "base_probability":  prob,
        "base_source":       "Interval scenario: engine interval projection vs target (anchored)",
        "line_anchor":       anchor,
        "phase":             win_phase,
        "innings":           innings,
        "adjustments":       {},
        "adjustment_unit":   "runs",
        "modifiers_applied": [],
        "modifiers_fired":   "",
        "modifier_total":    0.0,
        "total_adjustment":  0.0,
        "key_insight":       insight,
        "market_window": {
            "from_over":                       from_over,
            "to_over":                         to_over,
            "window_overs":                    interval_overs,
            "expected_runs":                   round(expected_interval_runs, 1),
            "projected_score_at_interval_end": round(projected_score_at_interval_end, 1),
            "base_rpo":                        round(blended_rpo, 2),
            "sigma":                           round(interval_sigma, 1),
            "line_mode":                       "window_only",
        },
        # ── display metadata (mirrors the keys analyse() enriches) ──
        "batting_team":   batting_team,
        "bowling_team":   bowling_team,
        "venue":          venue,
        "format":         fmt,
        "over_number":    current_over,
        "current_runs":   current_runs,
        "wickets_fallen": wickets,
        "line":           line,
        "match_label":    context.get("match_label", ""),
        "timestamp":      context.get("timestamp", ""),
    }
