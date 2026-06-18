# -*- coding: utf-8 -*-
"""
checkpoint_models.py — production model selection per checkpoint.

This is a THIN selection layer on top of the frozen prediction engine. It reads
the model assignment EXCLUSIVELY from `config.CHECKPOINT_MODELS` (the single
source of truth) and reproduces the BASE / FULL / PRUNED variants using the
exact decomposition validated in production_validation.py:

  * Full-innings market : additive recompose
        prob = base_eff + Σ(selected feature contributions) → apply_final_cap
  * Window markets       : re-run route_market with non-selected adjustments
        zeroed, then re-add only the selected fired modifiers (clamped [3,97]).

It does NOT modify core probability/market logic. FULL is a pass-through.
"""

from __future__ import annotations

import config
from model.probability import apply_final_cap, get_verdict_display

# Greedy-selected 8-feature subset (frozen — from the OOS ablation study).
PRUNED_ADJ = ["boundary_pct", "team_strength", "dot_ball_pct",
              "batter_bowler", "available_resources", "partnership_rate"]
PRUNED_MOD = ["exceptional_bowler_today", "wicket_clustering"]
PRUNED_FEATURES = set(PRUNED_ADJ + PRUNED_MOD)

_MAX_OVERS_T20 = 20


# ─────────────────────────────────────────────────────────────────
# CHECKPOINT DETECTION
# ─────────────────────────────────────────────────────────────────
def _resolve_window(market_type: str, current_over: float,
                    custom_from, custom_to):
    """Return (from_over, to_over) for a window market, mirroring route_market,
    or None for the full-innings market / unknown."""
    ov = float(current_over)
    if market_type == "Next 2 Overs Runs":
        return ov, min(ov + 2.0, float(_MAX_OVERS_T20))
    if market_type == "Next 4 Overs Runs":
        return ov, min(ov + 4.0, float(_MAX_OVERS_T20))
    if market_type == "Custom: Overs X to Y" and custom_from and custom_to:
        return max(float(custom_from) - 1.0, ov), min(float(custom_to), float(_MAX_OVERS_T20))
    return None


def detect_checkpoint(current_over: float, market_type: str,
                      custom_from=None, custom_to=None):
    """Map a live (over, market) to a production checkpoint key in
    config.CHECKPOINT_MODELS, or None if it is not one of the three."""
    mt = market_type or ""
    try:
        ov_int = int(round(float(current_over)))
    except (TypeError, ValueError):
        return None

    # CP3 — full innings total, end of over 15
    if mt in ("", "Total Innings Score") and ov_int == 15:
        return "OVER15_TOTAL"

    win = _resolve_window(mt, current_over, custom_from, custom_to)
    if win is None:
        return None
    frm, to = int(round(win[0])), int(round(win[1]))
    # CP1 — after over 3, window overs 4-6  → resolved [3, 6]
    if ov_int == 3 and frm == 3 and to == 6:
        return "OVER3_4_6"
    # CP2 — after over 6, window overs 7-10 → resolved [6, 10]
    if ov_int == 6 and frm == 6 and to == 10:
        return "OVER6_7_10"
    return None


# ─────────────────────────────────────────────────────────────────
# MODEL APPLICATION
# ─────────────────────────────────────────────────────────────────
def _clamp(p, lo=3.0, hi=97.0):
    return max(lo, min(hi, p))


def _recompose_full_innings(full_result: dict, model: str) -> float:
    """BASE/PRUNED probability for the full-innings market via the additive
    recompose, scored through the REAL apply_final_cap (frozen)."""
    selected = set() if model == "BASE" else PRUNED_FEATURES
    base_eff = float(full_result.get("base_probability_effective", 50.0))
    prob = base_eff
    for name, data in (full_result.get("adjustments", {}) or {}).items():
        if name in selected:
            prob += float(data.get("adj", 0.0) or 0.0)
    for m in (full_result.get("modifiers_applied", []) or []):
        if m.get("name") in selected:
            prob += float(m.get("adjustment", 0.0) or 0.0)
    clone = dict(full_result)          # shallow; apply_final_cap reads nested dicts read-only
    clone["final_probability"] = prob
    clone["pre_modifier_prob"] = prob
    clone = apply_final_cap(clone)
    return float(clone["final_probability"])


def _recompose_window(full_result: dict, model: str, route_market_kwargs: dict) -> float:
    """BASE/PRUNED probability for a window market by re-running the frozen
    route_market with non-selected adjustments zeroed, then re-adding only the
    selected fired modifiers. Imported locally to avoid any import cycle."""
    from model.market import route_market

    keep_adj = set() if model == "BASE" else set(PRUNED_ADJ)
    orig = full_result.get("adjustments", {}) or {}
    adj = {k: {**v, "adj": (v.get("adj", 0.0) if k in keep_adj else 0.0)}
           for k, v in orig.items()}
    fr = dict(full_result)
    fr["adjustments"] = adj
    momentum = adj.get("momentum", {}).get("adj", 0.0)

    routed = route_market(full_innings_result=fr, momentum_adj_pct=momentum,
                          **route_market_kwargs)
    pre_mod = float(routed["final_probability"]) - float(routed.get("modifier_total", 0.0))
    if model == "BASE":
        return round(_clamp(pre_mod), 1)
    sel = sum(float(m.get("adjustment", 0.0) or 0.0)
              for m in (routed.get("modifier_diagnostics", []) or [])
              if m.get("fired") and m.get("name") in PRUNED_MOD)
    return round(_clamp(pre_mod + sel), 1)


def apply_checkpoint_model(final_result: dict, full_result: dict, checkpoint_key,
                           *, is_full_innings: bool, route_market_kwargs: dict):
    """Transform `final_result` to the model assigned to `checkpoint_key` in
    config.CHECKPOINT_MODELS. Returns (result, applied_model_name).

    FULL (or an unmapped checkpoint) is a pass-through — the production engine
    already produces the FULL result.
    """
    if checkpoint_key is None:
        return final_result, "FULL"
    model = config.CHECKPOINT_MODELS.get(checkpoint_key, "FULL")
    if model == "FULL":
        out = dict(final_result)
        out["checkpoint_key"] = checkpoint_key
        out["checkpoint_model"] = "FULL"
        return out, "FULL"

    if is_full_innings:
        prob = _recompose_full_innings(full_result, model)
    else:
        prob = _recompose_window(full_result, model, route_market_kwargs)

    out = dict(final_result)
    out["final_probability"] = prob
    out["verdict"] = get_verdict_display(prob)
    out["checkpoint_key"] = checkpoint_key
    out["checkpoint_model"] = model
    return out, model
