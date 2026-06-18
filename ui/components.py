"""
ui/components.py — CricEdge Reusable Streamlit Components

All HTML-generating helpers and complex UI blocks.
Imports styles.css and injects via st.markdown.
"""

import os
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from typing import Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

MANUAL_TEAM_OPTIONS = sorted([
    "India Women", "Australia Women", "England Women", "New Zealand Women",
    "South Africa Women", "West Indies Women", "Pakistan Women", "Sri Lanka Women",
    "Bangladesh Women", "Zimbabwe Women", "Ireland Women", "Netherlands Women",
    "Scotland Women", "UAE Women", "USA Women", "Thailand Women",
    "Japan Women", "Papua New Guinea Women", "Canada Women", "Kenya Women",
])


@st.cache_data(ttl=300)
def _load_manual_venues(format_: str) -> list[str]:
    """Load venue options for manual scorecard entry from the DB."""
    from data.database import get_venues

    try:
        venues = get_venues(format_)
        if not venues:
            venues = get_venues()
        return venues
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────
# CSS INJECTION
# ─────────────────────────────────────────────────────────────────

def inject_css():
    """Load and inject the global CSS stylesheet."""
    css_path = os.path.join(os.path.dirname(__file__), "styles.css")
    if os.path.exists(css_path):
        with open(css_path, "r", encoding="utf-8") as f:
            css = f.read()
        st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────────────────────────

def render_header(db_status: dict):
    """Render the CricEdge top header bar."""
    womens_count = db_status.get("womens_match_positions", 0)
    last_ingest  = db_status.get("last_ingest", "Never")

    status_html = "● Live" if womens_count > 0 else "○ No data"
    st.markdown(f"""
    <div class="cricedge-header">
      <div>
        <div class="cricedge-logo">Cric<span style="-webkit-text-fill-color:var(--gold);">Edge</span></div>
        <div style="font-size:0.72rem; color:#586172; letter-spacing:0.08em; margin-top:2px;">
          AI-POWERED LIVE CRICKET SCORE FORECASTING
        </div>
      </div>
      <div class="header-status">
        <div class="status-dot"></div>
        <div>
          <div style="color:#98a2b6;">{womens_count:,} match states</div>
          <div style="color:#586172; font-size:0.7rem;">Updated: {last_ingest[:16] if last_ingest != 'Never' else 'Never'}</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────
# LIVE SCORE DISPLAY
# ─────────────────────────────────────────────────────────────────

def render_live_score(live_data: dict):
    """Render the live score card with batsmen and bowler figures."""
    runs    = live_data.get("runs", 0)
    wickets = live_data.get("wickets", 0)
    overs   = live_data.get("overs", 0.0)
    batsmen = live_data.get("batsmen", [])
    bowlers = live_data.get("all_bowlers", [])

    st.markdown('<div class="ce-card">', unsafe_allow_html=True)
    st.markdown('<div class="ce-card-title">🏏 LIVE SCORE</div>', unsafe_allow_html=True)

    # Main score
    st.markdown(f"""
    <div class="score-main">
        {runs}/{wickets}
        <span class="score-overs">in {overs:.1f} overs</span>
    </div>
    """, unsafe_allow_html=True)

    # Batsmen
    if batsmen:
        for i, b in enumerate(batsmen[:2]):
            name = b.get('name') or f"Batter {i+1}"
            on_strike_badge = '<span class="on-strike">STRIKE</span>' if b.get("on_strike") else ""
            sr = round(b.get("runs", 0) / b.get("balls", 0) * 100, 0) if b.get("balls", 0) > 0 else 0
            st.markdown(f"""
            <div class="batter-row">
                <span class="batter-name">{name}</span>
                <span class="batter-score">{b.get('runs', 0)}</span>
                <span class="batter-balls">({b.get('balls', 0)}b)</span>
                <span style="color:#586172; font-size:0.78rem;">SR {sr:.0f}</span>
                {on_strike_badge}
            </div>
            """, unsafe_allow_html=True)

    # Current bowler
    if bowlers:
        # Flag exceptional bowlers
        for bwl in bowlers[-3:]:
            ov    = bwl.get("overs_today", 0)
            runs_g = bwl.get("runs_today", 0)
            balls_t = int(ov) * 6 + round((ov % 1) * 10)
            eco   = round(runs_g / balls_t * 6, 2) if balls_t > 0 else 0
            is_exceptional = ov >= 3 and eco < 6.0
            badge = '<span class="bowler-exceptional">⚡ EXCEPTIONAL TODAY</span>' if is_exceptional else ""
            st.markdown(f"""
            <div class="bowler-row">
                <span class="batter-name">{bwl.get('name','?')}</span>
                <span class="bowler-fig">{ov:.0f}-{bwl.get('runs_today',0)}-{bwl.get('wickets',0)}</span>
                {badge}
            </div>
            """, unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────
# RECENT OVERS DISPLAY
# ─────────────────────────────────────────────────────────────────

def render_recent_overs(recent_overs: list[list[str]]):
    """Render last 3 overs ball-by-ball as colored dots."""
    if not recent_overs:
        return

    st.markdown('<div class="ce-card">', unsafe_allow_html=True)
    st.markdown('<div class="ce-card-title">📊 RECENT OVERS</div>', unsafe_allow_html=True)

    for i, over in enumerate(recent_overs[-3:]):
        over_label = f"Over {i+1}"
        balls_html = ""
        for ball in over:
            css_class = f"ball-{ball}" if ball in ("0","1","2","4","6","W","wd","nb") else "ball-1"
            balls_html += f'<span class="over-ball {css_class}">{ball}</span>'
        st.markdown(f"""
        <div style="margin-bottom:0.5rem;">
          <span style="font-size:0.72rem; color:#586172; margin-right:0.5rem;">{over_label}</span>
          {balls_html}
        </div>
        """, unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────
# PROBABILITY OUTPUT CARD
# ─────────────────────────────────────────────────────────────────

def render_probability_card(result: dict):
    """Render the main probability output card."""
    # ── Insufficient-data / degraded guard ───────────────────────
    # A broken base must never render as a confident probability/verdict.
    if result.get("degraded_mode") or result.get("verdict") == "⚠️ INSUFFICIENT DATA — cannot calculate":
        status = result.get("status") or "⚠️ INSUFFICIENT DATA — cannot calculate"
        insight = result.get("key_insight", "")
        st.markdown(f"""
        <div class="prob-card" style="border-color:rgba(251,191,99,0.4);">
          <div class="prob-label">MARKET STATUS</div>
          <div style="font-family:var(--font-display); font-size:2.1rem; font-weight:700;
                      color:var(--amber); line-height:1.2; margin:0.5rem 0;">
            ⚠️ INSUFFICIENT DATA
          </div>
          <div style="color:var(--text-secondary); font-size:0.9rem; margin-bottom:0.5rem;">
            Base probability could not be computed — no forecast produced.
          </div>
          <div style="color:var(--text-muted); font-size:0.78rem;">{status}</div>
        </div>
        """, unsafe_allow_html=True)
        if insight:
            st.markdown(f"""
            <div class="ce-card">
              <div class="ce-card-title">💡 WHY</div>
              <div class="insight-box">{insight}</div>
            </div>
            """, unsafe_allow_html=True)
        return

    # ── Low-confidence caveat (format-average-only base) ─────────
    # Weaker than a venue/position-anchored prediction — flag it visibly but still
    # show the signal (unlike the hard INSUFFICIENT DATA case above).
    if result.get("low_confidence"):
        st.markdown("""
        <div style="background:rgba(251,191,99,0.10); border:1px solid rgba(251,191,99,0.4);
                    border-radius:var(--radius-md); padding:0.7rem 1rem; margin-bottom:0.75rem;
                    color:var(--amber); font-size:0.82rem; font-weight:600;">
          ⚠️ Low confidence — based on generic format average only (no venue / position-specific data).
          Treat this as a weaker signal.
        </div>
        """, unsafe_allow_html=True)

    prob    = result.get("final_probability", 0)
    phase   = result.get("phase", "middle").title()
    innings = "Innings 1" if result.get("innings", 1) == 1 else "Chasing"

    # Use unified verdict display function for consistency
    from model.probability import get_verdict_display
    verdict_display = get_verdict_display(prob)
    
    # Extract icon and text from unified display
    if "⚡" in verdict_display:
        verdict_icon = "⚡"
        verdict_text = verdict_display.replace("⚡ ", "")
    elif "📈" in verdict_display:
        verdict_icon = "📈"
        verdict_text = verdict_display.replace("📈 ", "")
    elif "📉" in verdict_display:
        verdict_icon = "📉"
        verdict_text = verdict_display.replace("📉 ", "")
    elif "🔄" in verdict_display:
        verdict_icon = "🔄"
        verdict_text = verdict_display.replace("🔄 ", "")
    else:
        verdict_icon = "⚠️"
        verdict_text = verdict_display

    # Color class based on verdict category
    from model.probability import get_verdict_category
    category = get_verdict_category(prob)
    if category in ("STRONG_OVER", "VALUE_OVER"):
        num_class = "over"
        verdict_class = "value-bet"
    elif category in ("STRONG_UNDER", "VALUE_UNDER"):
        num_class = "avoid"
        verdict_class = "avoid"
    elif category in ("LEAN_OVER", "LEAN_UNDER"):
        num_class = "skip"
        verdict_class = "skip"
    else:  # TOSS_UP
        num_class = "skip"
        verdict_class = "skip"

    phase_class = result.get("phase", "middle")

    st.markdown(f"""
    <div class="prob-card">
      <div class="prob-label">PROBABILITY ABOVE TARGET</div>
      <div class="prob-number {num_class}">{prob:.0f}%</div>
      <div style="margin:0.5rem 0 0.25rem;">
        <span class="phase-badge {phase_class}">{phase}</span>
        &nbsp;
        <span style="font-size:0.78rem; color:#586172;">{innings} · Line {result.get('line',0):.0f}</span>
      </div>
      <div>
        <span class="verdict-badge {verdict_class}">{verdict_icon} {verdict_text}</span>
      </div>
    </div>
    """, unsafe_allow_html=True)

    calibrated_prob = result.get("calibrated_probability")
    trade_signal = result.get("trade_signal")
    if calibrated_prob is not None and trade_signal:
        st.markdown(
            f"<div style='text-align:center; margin:0.5rem 0; font-size:0.85rem; color:#586172;'>{trade_signal}</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<div style='text-align:center; margin-bottom:0.75rem; font-size:0.78rem; color:#98a2b6;'>Calibrated win probability: {calibrated_prob:.1%}</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Factor Breakdown ──
    st.markdown('<div class="ce-card">', unsafe_allow_html=True)
    st.markdown('<div class="ce-card-title">📈 FACTOR BREAKDOWN</div>', unsafe_allow_html=True)

    # Base probability row (show death cap when applied)
    market_window = result.get("market_window", {}) or {}
    base_rpo = market_window.get("base_rpo")
    window_overs = market_window.get("window_overs")
    raw_base_runs = (base_rpo * window_overs) if base_rpo is not None and window_overs is not None else None

    base = result.get("base_probability", 0)
    base_eff = result.get("base_probability_effective")
    base_source = result.get("base_source", "")
    base_label = "Base (history)" if "Historical" in base_source or "Position" in base_source else "Base"
    if base_source:
        base_label = f"Base — {base_source}"
    if base_rpo is not None:
        base_label = f"{base_label} ~{base_rpo:.1f} RPO"

    base_display = base
    if result.get("phase") == "death" and base_eff is not None and base_eff != base:
        base_label = f"Base (death cap {config.DEATH_BASE_CAP_PCT:.0f}%)"
        base_display = base_eff

    base_display_label = f"{raw_base_runs:.1f}r" if raw_base_runs is not None else f"{base_display:.0f}%"
    st.markdown(f"""
    <div class="factor-row">
      <span class="factor-name" style="color:#6fbf9e;font-weight:600;">{base_label}</span>
      <div class="factor-bar-track">
        <div class="factor-bar-fill positive" style="width:{min(100,abs(base_display))}%;"></div>
      </div>
      <span class="factor-value neutral">{base_display_label}</span>
    </div>
    """, unsafe_allow_html=True)

    # Each adjustment
    adjustments = result.get("adjustments", {})
    FACTOR_LABELS = {
        "momentum":            "Momentum",
        "partnership_rate":    "Partnership",
        "batter_bowler":       "Bat/Bowl matchup",
        "available_resources": "Resources",
        "pitch_weather":       "Pitch + weather",
        "team_strength":       "Team strength",
        "boundary_pct":        "Boundary%",
        "dot_ball_pct":        "Dot ball%",
        "toss":                "Toss",
        "death_bowler_quota":  "Death bowler quota",
        "rrr_factor":          "Required RR",
        "recent_pace":         "Recent pace",
        "exceptional_bowler":  "Exceptional bowler",
        "wicket_pressure":     "Wicket pressure",
        "dew_factor":          "Dew factor",
    }

    max_adj = max((abs(v.get("adj", 0) if isinstance(v, dict) else 0) for v in adjustments.values()), default=1) or 1
    adj_unit = result.get("adjustment_unit", "pct")
    adj_suffix = "r" if adj_unit == "runs" else "%"

    for key, data in adjustments.items():
        adj_val = data.get("adj", 0) if isinstance(data, dict) else 0
        if abs(adj_val) < 0.1:
            continue  # skip near-zero adjustments for cleanliness
        label     = FACTOR_LABELS.get(key, key.replace("_", " ").title())
        bar_pct   = min(100, abs(adj_val) / max_adj * 100)
        val_class = "positive" if adj_val > 0 else "negative"
        sign      = "+" if adj_val > 0 else ""
        st.markdown(f"""
        <div class="factor-row">
          <span class="factor-name">{label}</span>
          <div class="factor-bar-track">
            <div class="factor-bar-fill {val_class}" style="width:{bar_pct:.0f}%;"></div>
          </div>
          <span class="factor-value {val_class}">{sign}{adj_val:.1f}{adj_suffix}</span>
        </div>
        """, unsafe_allow_html=True)

    # Modifiers
    modifiers = result.get("modifiers_applied", [])
    if modifiers:
        st.markdown('<div style="margin-top:0.75rem; border-top:1px solid rgba(255,255,255,0.05); padding-top:0.75rem;">', unsafe_allow_html=True)
        for mod in modifiers:
            adj_val   = mod.get("adjustment", 0)
            mod_class = "fired-pos" if adj_val > 0 else "fired-neg"
            sign      = "+" if adj_val > 0 else ""
            label     = mod["name"].replace("_", " ").title()
            st.markdown(f"""
            <div class="factor-row">
              <span class="factor-name" style="color:#6fbf9e;">◆ {label}</span>
              <div class="factor-bar-track">
                <div class="factor-bar-fill {'positive' if adj_val > 0 else 'negative'}"
                     style="width:{min(100, abs(adj_val)/2*100):.0f}%;"></div>
              </div>
              <span class="factor-value {'positive' if adj_val > 0 else 'negative'}">{sign}{adj_val:.0f}%</span>
            </div>
            """, unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)

    # ── Position lookup debug ──
    pos_debug = result.get("position_debug")
    if pos_debug:
        _render_position_debug(pos_debug)

    # ── Key Insight ──
    insight = result.get("key_insight", "")
    if insight:
        st.markdown(f"""
        <div class="ce-card">
          <div class="ce-card-title">💡 KEY INSIGHT</div>
          <div class="insight-box">{insight}</div>
        </div>
        """, unsafe_allow_html=True)

    # ── Modifiers Summary Tags ──
    _render_modifier_tags(result)


def _fmt_num(x, dec=None):
    """Compact numeric format: drop trailing zeros (.g) or fix decimals."""
    if isinstance(x, (int, float)):
        return f"{x:.{dec}f}" if dec is not None else f"{x:g}"
    return str(x)


def build_prediction_summary(result: dict) -> str:
    """Condensed, copy-ready plain-text summary of a single prediction.

    Pulls only from the result dict the engine already produces — Match, Phase,
    Line, Projection, Win Probability and Verdict — so it stays in lock-step with
    the on-screen card. Returns "" if the result is degraded/insufficient.
    """
    if not result or result.get("degraded_mode"):
        return ""

    match = result.get("match_label") or (
        f'{result.get("batting_team", "")} vs {result.get("bowling_team", "")}'
    ).strip(" vs").strip()
    date = result.get("match_date") or (result.get("timestamp", "") or "")[:10]
    over = result.get("over_number", result.get("checkpoint", ""))
    runs = result.get("current_runs", 0)
    wkts = result.get("wickets_fallen", 0)
    line = result.get("line", "")
    prob = result.get("final_probability", 0) or 0

    # Projected total (full-innings) → fall back to window expected runs.
    proj = (result.get("position_debug") or {}).get("projected_total")
    if proj is None:
        proj = (result.get("market_window") or {}).get("expected_runs")
    proj_str = f"{round(proj)} Runs" if isinstance(proj, (int, float)) else "—"

    from model.probability import get_verdict_display
    verdict = result.get("verdict") or get_verdict_display(prob)

    over_str = _fmt_num(over, 1) if isinstance(over, (int, float)) else str(over)
    date_str = f" ({date})" if date else ""
    return (
        f"🏏 MATCH: {match}{date_str}\n"
        f"📈 PHASE: End of Over {over_str} | Score: {runs}/{wkts}\n"
        f"🎯 TARGET SCORE: {_fmt_num(line)}\n"
        f"🔮 PROJECTION: {proj_str} | Win Prob: {round(prob)}%\n"
        f"⚡ VERDICT: {verdict}"
    )


def render_copy_summary(result: dict):
    """Render a condensed plain-text prediction summary with a one-click copy.

    Uses Streamlit's native st.code copy affordance (top-right copy icon) — no
    iframe / clipboard-permission pitfalls, copies the exact text on one click.
    """
    summary = build_prediction_summary(result)
    if not summary:
        return
    st.markdown(
        '<div class="ce-card-title" style="margin-top:0.75rem; margin-bottom:0.25rem;">'
        '📋 COPY SUMMARY</div>',
        unsafe_allow_html=True,
    )
    st.code(summary, language="markdown")
    st.caption("One-click copy → use the copy icon at the top-right of the box above.")


def _render_position_debug(debug: dict):
    """Show match_position_stats lookup rows and sample_count."""
    with st.expander("🔍 Position table lookup", expanded=False):
        st.markdown(
            f"**Query:** over={debug.get('db_over')} · wickets={debug.get('wickets_fallen')} · "
            f"run bucket={debug.get('run_bucket')} · line={debug.get('line')} · "
            f"column={debug.get('column')}"
        )
        st.markdown(
            f"**Result:** source=`{debug.get('source')}` · "
            f"**sample_count={debug.get('sample_count', 0)}** · "
            f"pct={debug.get('pct')}"
        )
        if debug.get("smoothing_tier"):
            st.markdown(f"**Smoothing tier:** `{debug.get('smoothing_tier')}`")
        if debug.get("smoothing_components"):
            sc = debug["smoothing_components"]
            st.markdown(
                f"Blend (w={sc.get('weights')}): exact=`{sc.get('exact_bucket')}` · "
                f"nearby=`{sc.get('nearby_bucket')}` · phase_global=`{sc.get('phase_global')}`"
            )
        if debug.get("avg_final_score") is not None:
            st.markdown(f"Avg final score from bucket: **{debug['avg_final_score']:.1f}**")
        if debug.get("venue_fallback_rpo"):
            fallback_kind = "Venue window RPO"
            if debug.get("source") == "venue_window_rpo":
                fallback_kind = "Venue avg for window overs"
            st.markdown(
                f"{fallback_kind}: **{debug['venue_fallback_rpo']:.1f} RPO** "
                f"→ μ≈{debug.get('venue_fallback_mu', 0):.1f} runs in window"
            )
        exact = debug.get("exact_row")
        if exact:
            st.markdown("**Exact row:**")
            st.json(exact)
        fuzzy = debug.get("fuzzy_rows") or []
        if fuzzy:
            st.markdown(f"**Fuzzy rows ({len(fuzzy)}):**")
            st.json(fuzzy[:10])


def _render_modifier_tags(result: dict):
    """Show all 9 modifiers with fired/not-fired status and skip reasons."""
    diagnostics = result.get("modifier_diagnostics")
    if diagnostics:
        all_modifier_names = [m["name"] for m in diagnostics]
    else:
        all_modifier_names = list(config.MODIFIERS_ENABLED.keys())
    fired_names = {m["name"] for m in result.get("modifiers_applied", [])}

    st.markdown('<div class="ce-card">', unsafe_allow_html=True)
    st.markdown('<div class="ce-card-title">🔘 MODIFIER STATUS</div>', unsafe_allow_html=True)

    tags_html = ""
    for name in all_modifier_names:
        label = name.replace("_", " ").title()
        if diagnostics:
            mod_data = next((m for m in diagnostics if m["name"] == name), {})
        elif name in fired_names:
            mod_data = next((m for m in result.get("modifiers_applied", []) if m["name"] == name), {})
        else:
            mod_data = {}
        if mod_data.get("fired") or name in fired_names:
            adj = mod_data.get("adjustment", 0)
            mod_class = "fired-pos" if adj > 0 else "fired-neg"
            sign = "+" if adj > 0 else ""
            tags_html += f'<span class="modifier-tag {mod_class}">◆ {label} {sign}{adj:.0f}%</span>'
        else:
            tags_html += f'<span class="modifier-tag not-fired">○ {label}</span>'

    st.markdown(tags_html, unsafe_allow_html=True)

    if diagnostics:
        with st.expander("Why each modifier did / didn't fire", expanded=False):
            for m in diagnostics:
                status = "✓ FIRED" if m.get("fired") else "○ inactive"
                reason = m.get("reason") or "no reason recorded"
                st.markdown(f"**{m['name'].replace('_', ' ').title()}** — {status}: {reason}")

    st.markdown('</div>', unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────
# HISTORY TAB
# ─────────────────────────────────────────────────────────────────

def render_history_tab(predictions: list[dict]):
    """Render the prediction history tab with result marking."""
    if not predictions:
        st.markdown("""
        <div style="text-align:center; padding:3rem; color:#586172;">
            <div style="font-size:2.5rem; margin-bottom:0.5rem;">🏏</div>
            <div>No predictions yet. Make your first prediction!</div>
        </div>
        """, unsafe_allow_html=True)
        return

    # Summary metrics
    total   = len(predictions)
    graded  = [p for p in predictions if p.get("was_correct") is not None]
    correct = sum(1 for p in graded if p.get("was_correct") == 1)
    pending = total - len(graded)
    acc_pct = round(correct / len(graded) * 100, 1) if graded else 0

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Total Predictions", total)
    with c2:
        st.metric("Correct", correct)
    with c3:
        st.metric("Accuracy", f"{acc_pct}%" if graded else "N/A")
    with c4:
        st.metric("Pending", pending)

    st.markdown("<br>", unsafe_allow_html=True)

    from data.database import update_prediction_result

    for pred in predictions:
        _render_history_row(pred)


def _render_history_row(pred: dict):
    """Render a single prediction history row."""
    match    = pred.get("match", "Unknown Match")
    over_f   = pred.get("over_at_prediction", 0)
    line     = pred.get("line", 0)
    prob     = pred.get("predicted_probability", 0)
    result   = pred.get("actual_result")
    correct  = pred.get("was_correct")
    pred_id  = pred.get("id")
    ts       = (pred.get("timestamp") or "")[:16].replace("T", " ")

    # Use unified verdict display function for consistency
    from model.probability import get_verdict_display, get_verdict_category
    verdict_display = get_verdict_display(prob)
    category = get_verdict_category(prob)

    # Result badge — accepts new (ABOVE/BELOW) and legacy (OVER/UNDER) tokens
    _res_label = {
        "OVER": "ABOVE TARGET", "ABOVE": "ABOVE TARGET",
        "UNDER": "BELOW TARGET", "BELOW": "BELOW TARGET",
    }.get((result or "").upper(), result or "")
    if result is not None and correct == 1:
        result_badge = f'<span class="result-badge result-correct">✓ {_res_label} (Correct)</span>'
    elif result is not None:
        result_badge = f'<span class="result-badge result-wrong">✗ {_res_label} (Wrong)</span>'
    else:
        result_badge = '<span class="result-badge result-pending">Pending</span>'

    # Color based on verdict category
    verdict_color = {
        "STRONG_OVER":   "#2ee6a6",
        "VALUE_OVER":    "#2ee6a6",
        "LEAN_OVER":     "#f5b53c",
        "TOSS_UP":       "#98a2b6",
        "LEAN_UNDER":    "#f5b53c",
        "VALUE_UNDER":   "#fb6f84",
        "STRONG_UNDER":  "#fb6f84",
    }.get(category, "#98a2b6")

    with st.expander(f"{match}  ·  Over {over_f:.1f}  ·  Target {line:.0f}  ·  {prob:.0f}% {verdict_display}", expanded=False):
        st.markdown(f"""
        <div style="font-size:0.8rem; color:#98a2b6; margin-bottom:0.75rem;">
            {ts} · {pred.get('venue','?')} · {pred.get('format','?')} · Innings {pred.get('innings','?')}
        </div>
        <div style="margin-bottom:0.5rem;">
            Score: <strong>{pred.get('current_score_at_prediction','?')}</strong>
            &nbsp;|&nbsp; Base: <strong>{pred.get('base_probability','?')}%</strong>
            &nbsp;|&nbsp; Final: <strong style="color:{verdict_color};">{prob:.0f}%</strong>
        </div>
        <div style="margin-bottom:0.75rem;">
            Modifiers fired: <span style="color:#f5b53c;">{pred.get('modifiers_fired','none') or 'none'}</span>
        </div>
        {result_badge}
        """, unsafe_allow_html=True)

        if not pred.get("actual_result"):
            c1, c2 = st.columns(2)
            with c1:
                if st.button(f"✓ Reached target", key=f"over_{pred_id}"):
                    from data.database import update_prediction_result
                    update_prediction_result(pred_id, "ABOVE")
                    st.rerun()
            with c2:
                if st.button(f"✗ Below target", key=f"under_{pred_id}"):
                    from data.database import update_prediction_result
                    update_prediction_result(pred_id, "BELOW")
                    st.rerun()

        notes = st.text_input("Notes", value=pred.get("notes",""), key=f"notes_{pred_id}",
                               placeholder="Add match notes...")
        if notes and notes != pred.get("notes", ""):
            from data.database import update_prediction_result
            update_prediction_result(pred_id, pred.get("actual_result") or "", notes=notes)


# ─────────────────────────────────────────────────────────────────
# ANALYTICS TAB
# ─────────────────────────────────────────────────────────────────

def render_analytics_tab(predictions: list[dict]):
    """Full analytics dashboard."""
    graded = [p for p in predictions if p.get("was_correct") is not None]

    if not graded:
        st.info("Mark at least a few prediction results to see analytics.")
        return

    total_graded = len(graded)
    total_correct = sum(1 for p in graded if p.get("was_correct") == 1)
    overall_acc = round(total_correct / total_graded * 100, 1) if total_graded else 0

    # ── Overall Metrics ──
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Overall Accuracy", f"{overall_acc}%",
                   help="Correct predictions / all graded predictions")
    with c2:
        st.metric("Graded Predictions", total_graded)
    with c3:
        st.metric("Correct", total_correct)

    st.markdown("---")

    # ── Accuracy by Confidence Band ──
    st.markdown("#### Accuracy by Confidence Band")
    bands = [
        (">70%",   70, 100),
        ("60-70%", 60,  70),
        ("52-60%", 52,  60),
    ]
    band_data = []
    for label, lo, hi in bands:
        subset = [p for p in graded if lo <= p.get("predicted_probability", 0) < hi]
        if subset:
            acc = round(sum(1 for p in subset if p["was_correct"] == 1) / len(subset) * 100, 1)
            band_data.append({"Band": label, "Accuracy%": acc, "Count": len(subset)})

    if band_data:
        df_bands = pd.DataFrame(band_data)
        fig = go.Figure()
        colors = ["#2ee6a6", "#f5b53c", "#98a2b6"]
        for i, row in df_bands.iterrows():
            fig.add_bar(
                x=[row["Band"]], y=[row["Accuracy%"]],
                name=row["Band"],
                marker_color=colors[i % len(colors)],
                text=[f"{row['Accuracy%']}%<br>n={row['Count']}"],
                textposition="auto",
            )
        _style_chart(fig, "Accuracy by Confidence Band", "Accuracy %")
        st.plotly_chart(fig, use_container_width=True)

    # ── Calibration Chart ──
    st.markdown("#### Calibration Chart")
    st.caption("A perfectly calibrated model follows the diagonal line — 60% predictions should win 60% of the time.")

    from data.database import get_calibration_data
    cal_data = get_calibration_data()

    if len(cal_data) >= 3:
        df_cal = pd.DataFrame(cal_data)
        fig = go.Figure()
        fig.add_scatter(
            x=[50, 95], y=[50, 95],
            mode="lines", name="Perfect calibration",
            line=dict(color="#586172", dash="dash", width=1.5)
        )
        fig.add_scatter(
            x=df_cal["bucket"], y=df_cal["actual_pct"],
            mode="lines+markers+text",
            name="Model",
            line=dict(color="#2ee6a6", width=2.5),
            marker=dict(color="#2ee6a6", size=10,
                        line=dict(color="#0a0c10", width=2)),
            text=df_cal["total"].apply(lambda n: f"n={n}"),
            textposition="top center",
            textfont=dict(color="#6fbf9e", size=10),
        )
        _style_chart(fig, "Model Calibration", "Actual Win Rate %")
        fig.update_xaxes(title_text="Predicted Probability %", range=[45, 100])
        fig.update_yaxes(range=[0, 100])

        # Warning if severely miscalibrated
        if len(df_cal) >= 3:
            high_pred = df_cal[df_cal["bucket"] >= 70]
            if not high_pred.empty and high_pred["actual_pct"].mean() < 55:
                st.warning("⚠️ Model appears overconfident — >70% predictions are winning <55% of the time. Consider reducing base adjustments in config.py.")

        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Need more graded predictions for calibration chart (minimum 3 per bucket).")

    # ── Accuracy by Phase ──
    st.markdown("#### Accuracy by Phase")
    phase_data = []
    for phase in ["powerplay", "middle", "death"]:
        subset = [p for p in graded
                  if _over_to_phase(p.get("over_at_prediction", 0)) == phase]
        if subset:
            acc = round(sum(1 for p in subset if p["was_correct"] == 1) / len(subset) * 100, 1)
            phase_data.append({"Phase": phase.title(), "Accuracy%": acc, "Count": len(subset)})

    if phase_data:
        df_phase = pd.DataFrame(phase_data)
        fig = px.bar(df_phase, x="Phase", y="Accuracy%",
                     text="Accuracy%", color="Phase",
                     color_discrete_map={
                         "Powerplay": "#2ee6a6",
                         "Middle":    "#f5b53c",
                         "Death":     "#fb6f84",
                     })
        _style_chart(fig, "Accuracy by Phase")
        st.plotly_chart(fig, use_container_width=True)

    # ── Modifier Accuracy ──
    from data.database import get_modifier_accuracy
    mod_acc = get_modifier_accuracy()
    if mod_acc:
        st.markdown("#### Modifier Accuracy — Which Modifiers Help?")
        st.caption("When a modifier fires, are the predictions more accurate?")
        df_mod = pd.DataFrame(mod_acc).sort_values("accuracy_pct", ascending=True)
        fig = px.bar(df_mod, y="modifier", x="accuracy_pct",
                     orientation="h", text="accuracy_pct",
                     color="accuracy_pct",
                     color_continuous_scale=["#fb6f84", "#f5b53c", "#34d399"],
                     range_color=[40, 80])
        _style_chart(fig, "Accuracy When Each Modifier Fires")
        fig.update_layout(coloraxis_showscale=False)
        st.plotly_chart(fig, use_container_width=True)

    # ── Where It Went Wrong ──
    st.markdown("#### Where It Went Wrong — Last 10 Misses")
    wrong = [p for p in graded if p.get("was_correct") == 0][-10:]
    if wrong:
        for p in reversed(wrong):
            prob     = p.get("predicted_probability", 0)
            actual   = p.get("actual_result", "?")
            mods_fired = p.get("modifiers_fired", "") or "none"
            match    = p.get("match", "?")
            over_f   = p.get("over_at_prediction", 0)
            miss_reason = _auto_miss_reason(p)
            
            # Use unified verdict display
            from model.probability import get_verdict_display
            verdict_display = get_verdict_display(prob)
            
            st.markdown(f"""
            <div class="ce-card" style="border-color:rgba(244,63,94,0.25);">
                <strong>{match}</strong> · Over {over_f:.1f} · Target {p.get('line',0):.0f}
                <br><span style="color:#fb6f84;">Predicted {prob:.0f}% ({verdict_display}) — Actual: {actual}</span>
                <br><span style="color:#98a2b6; font-size:0.8rem;">Modifiers: {mods_fired}</span>
                <br><span style="color:#f5b53c; font-size:0.8rem;">Possible miss reason: {miss_reason}</span>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.success("No missed predictions yet!")

    # ── Export ──
    st.markdown("---")
    if predictions:
        df_export = pd.DataFrame(predictions)
        csv = df_export.to_csv(index=False)
        st.download_button(
            "📥 Export Predictions CSV",
            data=csv,
            file_name="cricedge_predictions.csv",
            mime="text/csv",
        )


def _over_to_phase(over: float) -> str:
    ov = int(over)
    if ov <= 6:
        return "powerplay"
    elif ov <= 16:
        return "middle"
    return "death"


def _auto_miss_reason(pred: dict) -> str:
    """Auto-generate a possible reason for a missed prediction."""
    mods = (pred.get("modifiers_fired") or "").split(",")
    actual  = pred.get("actual_result", "")
    prob    = pred.get("predicted_probability", 50)

    # Use unified verdict category
    from model.probability import get_verdict_category
    category = get_verdict_category(prob)

    actual_u = (actual or "").upper()
    actual_above = actual_u in ("ABOVE", "OVER")
    actual_below = actual_u in ("BELOW", "UNDER")
    if category in ("STRONG_OVER", "VALUE_OVER", "LEAN_OVER") and actual_below:
        if "exceptional_bowler_today" not in mods:
            return "Exceptional bowler performance not captured — death bowling data may be stale"
        if "wicket_clustering" not in mods:
            return "Wicket cluster in final overs may have caused collapse not predicted"
        return "Batting team underperformed expected scoring rate in death"
    elif category in ("STRONG_UNDER", "VALUE_UNDER", "LEAN_UNDER") and actual_above:
        if "bowling_quota_trap" not in mods:
            return "Part-timer bowled death overs and conceded more than expected"
        return "Batting team outperformed expected scoring despite bowling dominance"
    elif category == "TOSS_UP":
        return "Projection was very close to the target — within the model's margin of error"
    return "Insufficient data to auto-diagnose miss"


# ─────────────────────────────────────────────────────────────────
# SETTINGS TAB
# ─────────────────────────────────────────────────────────────────

def render_settings_tab():
    """Render the settings tab for weight/threshold tuning."""
    st.markdown("### ⚙️ Probability Model Settings")
    st.caption("All values saved in session state. Restart app to apply config.py changes.")

    # ── Confidence Thresholds ──
    with st.expander("🎯 Confidence Thresholds", expanded=True):
        vb = st.slider("HIGH CONFIDENCE minimum %", 55, 85,
                       int(config.VERDICT_THRESHOLDS["value_bet_min"]), 1)
        sk = st.slider("LOW CONFIDENCE minimum %", 48, 65,
                       int(config.VERDICT_THRESHOLDS["skip_min"]), 1)
        if vb <= sk:
            st.warning("HIGH CONFIDENCE threshold must be above LOW CONFIDENCE threshold.")
        st.info(
            f"Confidence tiering (Z-gate primary): "
            f"|Z| ≥ 1.5σ → HIGH CONFIDENCE (ABOVE/BELOW target) | "
            f"1.0σ ≤ |Z| < 1.5σ → LOW CONFIDENCE | "
            f"|Z| < 1.0σ → TOO CLOSE TO CALL (prob clamped 30–70%). "
            f"Fallback prob thresholds: ≥{vb}% HIGH | {sk}–{vb}% LOW | <{sk}% LOW CONFIDENCE"
        )

    # ── Modifier Toggles ──
    with st.expander("🔘 Modifier Toggles", expanded=True):
        st.caption("Toggle individual modifiers on/off for this session.")
        col1, col2 = st.columns(2)
        mod_names = list(config.MODIFIERS_ENABLED.keys())
        for i, mod_name in enumerate(mod_names):
            col = col1 if i % 2 == 0 else col2
            label = mod_name.replace("_", " ").title()
            with col:
                enabled = st.toggle(label, value=config.MODIFIERS_ENABLED.get(mod_name, True),
                                     key=f"mod_toggle_{mod_name}")
                if enabled != config.MODIFIERS_ENABLED.get(mod_name):
                    st.session_state[f"mod_override_{mod_name}"] = enabled

    # ── Adjustment Range Preview ──
    with st.expander("📊 Adjustment Range Reference (read-only)", expanded=False):
        for phase in ["powerplay", "middle", "death"]:
            st.markdown(f"**{phase.title()} Phase:**")
            ranges = config.ADJUSTMENT_RANGES.get(phase, {})
            rows = []
            for factor, (lo, hi) in ranges.items():
                if lo == 0 and hi == 0:
                    continue
                rows.append({"Factor": factor.replace("_"," ").title(), "Min": f"{lo:.0f}%", "Max": f"+{hi:.0f}%"})
            if rows:
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ── DB Maintenance ──
    with st.expander("🗄️ Database Maintenance", expanded=False):
        from data.database import get_db_status
        status = get_db_status()
        st.markdown("**Current DB Status:**")
        for k, v in status.items():
            st.markdown(f"- **{k.replace('_',' ').title()}**: `{v}`")

        st.markdown("---")
        st.markdown("**Run Data Ingest** (will take several minutes):")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🔄 Ingest Women's T20I (Quick - Last 2 Seasons)"):
                st.info("To run ingest: `python -m data.ingest --format \"Women's T20I\" --quick`")
        with col2:
            if st.button("🔄 Ingest All Formats (Full)"):
                st.info("To run ingest: `python -m data.ingest`")


# ─────────────────────────────────────────────────────────────────
# MANUAL SCORECARD ENTRY
# ─────────────────────────────────────────────────────────────────

def _runs_to_ball_tokens(runs: int, wickets_in_over: int) -> list[str]:
    """Approximate ball-by-ball tokens from an over summary (for replay load)."""
    balls: list[str] = []
    rem = max(0, runs)
    wkts = min(6, max(0, wickets_in_over))
    # Spread the wickets across distinct balls (0..5). The old `(2,4)[:wkts]`
    # only had two slots, so 3+ wickets in an over silently lost the extras.
    wicket_idx = {(k * 6) // wkts for k in range(wkts)} if wkts else set()
    for i in range(6):
        if i in wicket_idx:
            balls.append("W")
            continue
        if rem >= 6:
            balls.append("6")
            rem -= 6
        elif rem >= 4:
            balls.append("4")
            rem -= 4
        elif rem > 0:
            balls.append(str(rem))
            rem = 0
        else:
            balls.append("0")
    return balls


def scorecard_snapshot_to_replay(
    parsed: dict,
    meta: dict,
    innings_num: int = 1,
    at_cricket_over: int | None = None,
) -> dict:
    """
    Build Replay-mode session payload from a saved manual scorecard.
    Returns dict with keys for st.session_state + live_data.
    """
    innings_list = parsed.get("innings", [])
    idx = max(0, min(innings_num - 1, len(innings_list) - 1))
    inn = innings_list[idx]
    overs_rows = inn.get("overs", [])
    if not overs_rows:
        raise ValueError("Scorecard has no over-by-over data")

    if at_cricket_over is None:
        at_cricket_over = max(r["over"] for r in overs_rows) + 1

    completed = [r for r in overs_rows if r["over"] < at_cricket_over]
    if not completed:
        completed = overs_rows[:1]
        at_cricket_over = completed[0]["over"] + 1

    runs = sum(r["runs"] for r in completed)
    wickets = sum(r["wickets"] for r in completed)

    recent_src = completed[-3:]
    recent_overs = [
        _runs_to_ball_tokens(r["runs"], r["wickets"]) for r in recent_src
    ]

    bowler_totals: dict[str, dict] = {}
    for r in completed:
        name = (r.get("bowler") or "Unknown").strip() or "Unknown"
        if name not in bowler_totals:
            bowler_totals[name] = {"overs": 0, "runs": 0, "wickets": 0}
        bowler_totals[name]["overs"] += 1
        bowler_totals[name]["runs"] += r["runs"]
        bowler_totals[name]["wickets"] += r["wickets"]

    all_bowlers = []
    for name, stats in bowler_totals.items():
        ov = float(stats["overs"])
        balls = int(ov) * 6
        all_bowlers.append({
            "name": name,
            "overs_today": ov,
            "runs_today": stats["runs"],
            "wickets": stats["wickets"],
            "economy": round(stats["runs"] / balls * 6, 2) if balls > 0 else 0.0,
            "overs_remaining": max(0.0, 4.0 - ov),
        })

    batting_team = inn.get("batting_team") or meta.get("team1", "")
    bowling_team = inn.get("bowling_team") or meta.get("team2", "")

    placeholder_batsmen = [
        {"name": "Batter 1", "runs": 0, "balls": 0, "on_strike": True},
        {"name": "Batter 2", "runs": 0, "balls": 0, "on_strike": False},
    ]

    return {
        "mode": "replay",
        "batting_team": batting_team,
        "bowling_team": bowling_team,
        "venue": meta.get("venue") or parsed.get("venue") or "",
        "format": meta.get("format") or config.PRIMARY_FORMAT,
        "innings": innings_num,
        "target": (
            parsed["innings"][0]["total_runs"]
            if innings_num == 2 and len(parsed.get("innings", [])) >= 1
            else None
        ),
        "live_data": {
            "fetch_success": True,
            "source": "manual_scorecard",
            "runs": runs,
            "wickets": wickets,
            "overs": float(at_cricket_over),
            "batsmen": placeholder_batsmen,
            "all_bowlers": all_bowlers,
            "remaining_bowlers": [b for b in all_bowlers if b["overs_remaining"] > 0],
            "current_bowler": all_bowlers[-1] if all_bowlers else None,
            "recent_overs": recent_overs,
        },
    }


def apply_scorecard_replay(payload: dict) -> None:
    """Write a scorecard replay payload into Streamlit session state."""
    import streamlit as st

    st.session_state["mode"] = payload["mode"]
    st.session_state["batting_team"] = payload["batting_team"]
    st.session_state["bowling_team"] = payload["bowling_team"]
    st.session_state["venue"] = payload["venue"]
    st.session_state["format"] = payload["format"]
    st.session_state["innings"] = payload["innings"]
    if payload.get("target"):
        st.session_state["target"] = payload["target"]
    st.session_state["live_data"] = payload["live_data"]
    st.session_state["live_fetched"] = True
    st.session_state["scorecard_loaded_msg"] = (
        f"{payload['batting_team']} vs {payload['bowling_team']} — "
        f"{payload['live_data']['runs']}/{payload['live_data']['wickets']} "
        f"({payload['live_data']['overs']:.1f} ov)"
    )


# ─────────────────────────────────────────────────────────────────
# MACRO MATCH-BLOCK PARSER  (single paste → full projection)
# ─────────────────────────────────────────────────────────────────

# Canonical paste format — shown as the in-app demo/template. Edit the values,
# keep the structure. Only the first line + (a scorecard OR live batters) are
# strictly required; everything else is optional and parsed if present.
MACRO_DEMO_BLOCK = """India Women vs England Women @ Lord's, London
Date: 2026-06-17 | ID: wt20wc2026_m10 | Line: 162.5 | Format: Women's T20I | Innings: 1

Innings Scorecard:
1|8|0|Sophie Ecclestone
2|6|1|Sophie Ecclestone
3|12|0|Lauren Bell
4|9|0|Charlie Dean
5|7|1|Sarah Glenn
6|11|0|Sophie Ecclestone
7|8|0|Charlie Dean
8|10|1|Sarah Glenn
9|6|0|Lauren Bell
10|9|0|Charlie Dean

Live Batters:
Smriti Mandhana: 48 (35b) *
Harmanpreet Kaur: 22 (18b)

Live Bowler:
Sophie Ecclestone | 3.0 ov | 18 runs | 2 wk | 1.0 rem"""


def parse_macro_match_block(text: str) -> dict:
    """Parse a single pasted match block into structured fields via smart regex.

    Returns a dict with: batting_team, bowling_team, venue, date, match_id, line,
    format, innings, target, innings_overs[], batsmen[], all_bowlers[], warnings[],
    and an optional ``error`` string when the block is unusable.
    """
    import re

    out = {
        "error": None, "warnings": [],
        "batting_team": "", "bowling_team": "", "venue": "",
        "date": "", "match_id": "", "line": None,
        "format": config.PRIMARY_FORMAT or "Women's T20I",
        "innings": 1, "target": None,
        "innings_overs": [], "batsmen": [], "all_bowlers": [],
    }

    if not text or not text.strip():
        out["error"] = "Paste a match block first."
        return out

    nonempty = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    header = nonempty[0]

    # ── Line 1: "Batting vs Bowling @ Venue" ──
    head_part, venue = header, ""
    if "@" in header:
        head_part, venue = header.split("@", 1)
        venue = venue.strip()
    teams = re.split(r"\s+(?:vs\.?|v)\s+", head_part.strip(), maxsplit=1, flags=re.I)
    out["batting_team"] = re.sub(r"\s+", " ", teams[0]).strip()
    out["bowling_team"] = re.sub(r"\s+", " ", teams[1]).strip() if len(teams) > 1 else ""
    out["venue"] = venue

    # ── Meta fields (searched across the whole block) ──
    def _find(pat):
        m = re.search(pat, text, re.I)
        return m.group(1).strip() if m else None

    out["date"] = _find(r"\bDate\s*:\s*([0-9]{4}-[0-9]{1,2}-[0-9]{1,2})") or ""
    out["match_id"] = _find(r"\bID\s*:\s*([^|\n]+)") or ""
    _line = _find(r"\bLine\s*:\s*([0-9]+(?:\.[0-9]+)?)")
    if _line:
        out["line"] = float(_line)
    _fmt = _find(r"\bFormat\s*:\s*([^|\n]+)")
    if _fmt:
        out["format"] = _fmt
    _inn = _find(r"\bInnings\s*:\s*([12])")
    if _inn:
        out["innings"] = int(_inn)
    # Lenient: "Target: 178", "Target 178", "Chasing 178", "Chase 178", "To win 178".
    _tgt = _find(r"(?:Target|Chasing|Chase|To\s*win)\b\s*:?\s*([0-9]+)")
    if _tgt:
        out["target"] = int(_tgt)

    # ── Body sections ──
    over_re = re.compile(r"^\s*\d+\s*\|")
    bat_re = re.compile(r"^\s*(.+?)\s*[:\-]\s*(\d+)\s*\(\s*(\d+)\s*b?\s*\)\s*(\*)?\s*$", re.I)
    section = None

    for ln in nonempty:
        low = ln.lower()

        # Over-by-over row — recognised regardless of any header
        if over_re.match(ln):
            parts = [p.strip() for p in ln.split("|")]
            try:
                ov = int(parts[0])
                out["innings_overs"].append({
                    "over": ov - 1,                       # store 0-based
                    "runs": int(parts[1]),
                    "wickets": int(parts[2]),
                    "bowler": parts[3] if len(parts) > 3 else "Unknown",
                })
            except (ValueError, IndexError):
                pass
            continue

        # Section headers
        if "scorecard" in low:
            section = "score"; continue
        if "batter" in low:
            section = "bat"; continue
        if "bowler" in low:
            section = "bowl"; continue

        # Skip the header line and any meta / non-data lines
        if ln == header:
            continue
        if re.search(r"\b(Date|ID|Line|Format|Innings|Target|Market|Venue|Toss)\s*:", ln, re.I):
            continue

        if section == "bat":
            m = bat_re.match(ln)
            if m:
                out["batsmen"].append({
                    "name": re.sub(r"\s+", " ", m.group(1)).strip(),
                    "runs": int(m.group(2)),
                    "balls": int(m.group(3)),
                    "on_strike": bool(m.group(4)),
                })
        elif section == "bowl":
            # Require real bowler evidence — a pipe-delimited stat line or an
            # over/runs/wkt/rem token. Stops stray lines (e.g. "Market: Total
            # Innings Score") from being ingested as phantom bowlers.
            has_stats = ("|" in ln) or re.search(r"\d+\s*(?:ov|runs?|wk|rem)\b", ln, re.I)
            if not has_stats:
                continue
            name = ln.split("|")[0].strip()
            if not name:
                continue
            ov_m = re.search(r"([\d.]+)\s*ov", ln, re.I)
            rn_m = re.search(r"(\d+)\s*runs?", ln, re.I)
            wk_m = re.search(r"(\d+)\s*wk", ln, re.I)
            rem_m = re.search(r"([\d.]+)\s*rem", ln, re.I)
            ov = float(ov_m.group(1)) if ov_m else 0.0
            rn = int(rn_m.group(1)) if rn_m else 0
            balls = int(ov) * 6 + round((ov % 1) * 10)
            out["all_bowlers"].append({
                "name": name,
                "overs_today": ov,
                "runs_today": rn,
                "wickets": int(wk_m.group(1)) if wk_m else 0,
                "economy": round(rn / balls * 6, 2) if balls > 0 else 0.0,
                "overs_remaining": float(rem_m.group(1)) if rem_m else max(0.0, 4.0 - ov),
            })

    if not out["batting_team"] or not out["bowling_team"]:
        out["warnings"].append(
            "Could not read both teams from line 1. Use: 'Team A vs Team B @ Venue'."
        )
    return out


def macro_to_payload(parsed: dict) -> dict:
    """Turn parsed macro fields into the live_data payload the pipeline expects."""
    overs_rows = sorted(parsed["innings_overs"], key=lambda o: o["over"])

    if overs_rows:
        runs = sum(o["runs"] for o in overs_rows)
        wkts = sum(o["wickets"] for o in overs_rows)
        overs_val = float(max(o["over"] for o in overs_rows) + 1)   # 0-based → cumulative
        recent = [_runs_to_ball_tokens(o["runs"], o["wickets"]) for o in overs_rows[-3:]]
    else:
        # Fallback: no over log — derive a coarse state from live players
        runs = sum(b["runs"] for b in parsed["batsmen"])
        wkts = 0
        overs_val = max((b.get("overs_today", 0.0) for b in parsed["all_bowlers"]), default=0.0)
        recent = []

    batsmen = list(parsed["batsmen"])
    if batsmen and not any(b.get("on_strike") for b in batsmen):
        batsmen[0]["on_strike"] = True
    if not batsmen:
        # Fallback safety: no live batter lines → run off the scorecard's last state
        batsmen = [
            {"name": "Batter 1", "runs": 0, "balls": 0, "on_strike": True},
            {"name": "Batter 2", "runs": 0, "balls": 0, "on_strike": False},
        ]

    all_bowlers = list(parsed["all_bowlers"])
    if not all_bowlers and overs_rows:
        # Derive bowlers from the scorecard's bowler column
        totals: dict[str, dict] = {}
        for o in overs_rows:
            nm = (o.get("bowler") or "Unknown").strip() or "Unknown"
            t = totals.setdefault(nm, {"overs": 0, "runs": 0, "wickets": 0})
            t["overs"] += 1
            t["runs"] += o["runs"]
            t["wickets"] += o["wickets"]
        for nm, t in totals.items():
            balls = t["overs"] * 6
            all_bowlers.append({
                "name": nm,
                "overs_today": float(t["overs"]),
                "runs_today": t["runs"],
                "wickets": t["wickets"],
                "economy": round(t["runs"] / balls * 6, 2) if balls else 0.0,
                "overs_remaining": max(0.0, 4.0 - t["overs"]),
            })

    return {
        "batting_team": parsed["batting_team"],
        "bowling_team": parsed["bowling_team"],
        "venue": parsed["venue"],
        "format": parsed["format"] or config.PRIMARY_FORMAT,
        "innings": parsed["innings"],
        "target": parsed["target"],
        "line": parsed["line"],
        "live_data": {
            "fetch_success": True,
            "source": "macro_paste",
            "runs": runs,
            "wickets": wkts,
            "overs": overs_val,
            "batsmen": batsmen,
            "all_bowlers": all_bowlers,
            "remaining_bowlers": [b for b in all_bowlers if b.get("overs_remaining", 0) > 0],
            "recent_overs": recent,
        },
    }


def _macro_synth_id(parsed: dict) -> str:
    """Build a stable match_id when the paste omits an explicit ID."""
    def _abbr(name):
        return "".join(w[0] for w in name.split() if w)[:3] or "tm"
    d = (parsed.get("date") or "match").replace("-", "")
    return f"macro_{d}_{_abbr(parsed['batting_team'])}{_abbr(parsed['bowling_team'])}".lower()


def _macro_parsed_for_db(parsed: dict, payload: dict) -> dict:
    """Shape parsed data to match _parse_scorecard_text() so saved lists render."""
    return {
        "venue": parsed["venue"],
        "date": parsed["date"],
        "team1": parsed["batting_team"],
        "team2": parsed["bowling_team"],
        "innings": [{
            "batting_team": parsed["batting_team"],
            "bowling_team": parsed["bowling_team"],
            "innings": 1,
            "total_runs": payload["live_data"]["runs"],
            "wickets": payload["live_data"]["wickets"],
            "overs": [
                {"over": o["over"], "runs": o["runs"], "wickets": o["wickets"], "bowler": o["bowler"]}
                for o in parsed["innings_overs"]
            ],
        }],
    }


def run_projection_from_payload(payload: dict) -> dict:
    """Run the full CricEdge projection pipeline from a parsed macro payload.

    Self-contained mirror of the ⚡ ANALYSE flow in app.py so the macro parser can
    project instantly without touching the Analyse tab's widget state.
    """
    from model.innings_types import analyse
    from model.probability import get_phase
    from model.market import route_market

    live = payload["live_data"]
    fmt = payload["format"]
    overs = float(live.get("overs", 0.0))
    runs = int(live.get("runs", 0))
    wkts = int(live.get("wickets", 0))
    line = float(payload["line"])
    innings = int(payload.get("innings", 1))
    phase = get_phase(overs, fmt)

    batsmen = [b for b in live.get("batsmen", []) if b.get("name")]
    all_bowlers = live.get("all_bowlers", [])
    recent = live.get("recent_overs", [])

    try:
        from scraper.live_score import calculate_remaining_bowlers
        remaining_bowlers = live.get("remaining_bowlers") or calculate_remaining_bowlers(all_bowlers)
    except Exception:
        remaining_bowlers = [b for b in all_bowlers if (b.get("overs_remaining") or 0) > 0]

    on_crease = list(batsmen[:2])
    yet_to_bat = max(0, 11 - wkts - len(on_crease))
    remaining_batters = on_crease + [{"name": ""} for _ in range(yet_to_bat)]

    full = analyse(
        innings=innings,
        format_=fmt,
        over_number=overs,
        current_runs=runs,
        wickets_fallen=wkts,
        line=line,
        batting_team=payload["batting_team"],
        bowling_team=payload["bowling_team"],
        venue=payload.get("venue") or "Unknown",
        batsmen=batsmen,
        all_bowlers=all_bowlers,
        remaining_bowlers=remaining_bowlers,
        remaining_batters=remaining_batters,
        recent_overs=recent,
        all_scorers=batsmen,
        target=payload.get("target"),
        match_label=f"{payload['batting_team']} vs {payload['bowling_team']}",
        market_type="Total Innings Score",
        save_to_db=True,
    )

    # Calibrated model signal (best-effort — mirrors _format_trade_signal in app.py)
    try:
        import joblib
        from pathlib import Path
        calib = joblib.load(Path(__file__).resolve().parent.parent / "live_calibrator.pkl")
        raw = float(full.get("final_probability", 0.0)) / 100.0
        side = "ABOVE" if raw >= 0.50 else "BELOW"
        model_side_prob = raw if raw >= 0.50 else 1.0 - raw
        cwp = float(calib.predict([model_side_prob])[0])
        if cwp - 0.50 >= 0.04:
            full["calibrated_probability"] = cwp
            full["trade_signal"] = f"⚡ SIGNAL: {side} TARGET (high confidence) | calibrated_prob={cwp:.4f}"
        else:
            full["calibrated_probability"] = None
            full["trade_signal"] = "⚠️ LOW CONFIDENCE — no clear separation"
    except Exception:
        pass

    momentum = full.get("adjustments", {}).get("momentum", {}).get("adj", 0.0)
    return route_market(
        market_type="Total Innings Score",
        line=line,
        current_over=overs,
        innings=innings,
        phase=phase,
        full_innings_result=full,
        momentum_adj_pct=momentum,
        all_bowlers=all_bowlers,
        wickets_fallen=wkts,
        dew_factor=False,
        remaining_bowlers=remaining_bowlers,
        format_=fmt,
        recent_overs=recent,
        target=payload.get("target"),
    )


def render_scorecard_entry_tab():
    """Manual scorecard entry for current tournaments (e.g., WT20 WC 2026)."""
    from data.database import (
        get_all_manual_scorecards,
        get_db_status,
        get_unprocessed_scorecards,
        save_manual_scorecard,
    )

    st.markdown("### 🚀 Live Match Macro-Parser")
    st.caption(
        "Paste one complete match block — CricEdge auto-extracts the teams, venue, target score, "
        "the over-by-over log, live batters (with strike) and the current bowler, then runs "
        "the full projection instantly. No dropdowns, no separate fields."
    )

    db = get_db_status()
    c1, c2, c3 = st.columns(3)
    c1.metric("Saved", db.get("manual_scorecards", 0))
    c2.metric("In stats DB", db.get("manual_scorecards_ingested", 0))
    c3.metric("Pending ingest", db.get("manual_scorecards_pending", 0))

    pending_n = db.get("manual_scorecards_pending", 0)
    if pending_n:
        if st.button(f"⚡ Process {pending_n} pending into stats DB", key="btn_ingest_manual"):
            from data.ingest import ingest_manual_scorecards
            with st.spinner("Updating team / venue / player stats…"):
                n = ingest_manual_scorecards()
            st.success(f"✅ Processed {n} scorecard(s).") if n else st.warning("Nothing to process.")
            st.rerun()

    # ── Format reference / demo ──
    with st.expander("📋 Paste format & live demo — copy this, edit the values", expanded=False):
        st.code(MACRO_DEMO_BLOCK, language="text")
        st.markdown(
            "**How to structure the block**\n"
            "- **Line 1:** `Batting Team vs Bowling Team @ Venue`\n"
            "- **Line 2 (meta):** `Date: … | ID: … | Line: … | Format: … | Innings: … | Target: …`\n"
            "- **`Innings Scorecard:`** one row per over → `Over|Runs|Wickets|Bowler` "
            "(the cumulative score is derived automatically)\n"
            "- **`Live Batters:`** `Name: runs (ballsb) *` — the `*` marks the batter on strike\n"
            "- **`Live Bowler:`** `Name | 3.0 ov | 18 runs | 2 wk | 1.0 rem`\n\n"
            "**Chasing (2nd innings):** add `Innings: 2` and a target — `Target: 178`, "
            "`Chasing 178`, or `Chase 178` all work — to activate the Required Run Rate factor.\n\n"
            "Only **line 1** plus **a scorecard *or* live batters** are required — everything else "
            "is optional. With no live-batter lines it falls back to the scorecard's last state."
        )

    if st.button("📥 Load demo block into the box", key="macro_load_demo"):
        st.session_state["macro_text"] = MACRO_DEMO_BLOCK
        st.rerun()

    st.text_area(
        "🚀 Paste Complete Live Match Block",
        height=340, key="macro_text",
        placeholder=MACRO_DEMO_BLOCK,
    )

    run = st.button(
        "⚡ Extract & Run Live Analysis",
        type="primary", use_container_width=True, key="macro_run",
    )

    if run:
        parsed = parse_macro_match_block(st.session_state.get("macro_text", ""))
        if parsed.get("error"):
            st.error(parsed["error"])
            return
        if not parsed["batting_team"] or not parsed["bowling_team"]:
            st.error("Need two teams on line 1 — e.g. `India Women vs England Women @ Lord's`.")
            return
        if not parsed["innings_overs"] and not parsed["batsmen"]:
            st.error("Need at least an `Innings Scorecard:` or `Live Batters:` section to project.")
            return

        warnings = list(parsed["warnings"])
        if parsed["line"] is None:
            parsed["line"] = 160.0
            warnings.append("No `Line:` found — defaulted to 160.0. Add `Line: <number>` for an accurate forecast.")
        if parsed["innings"] == 2 and not parsed["target"]:
            parsed["innings"] = 1
            warnings.append("Innings 2 needs a `Target:` — projected as Innings 1 instead.")

        # Chase line cap: the innings ends when the target is passed, so a Total
        # Innings Score line above target+1 is logically unreachable. Cap it to
        # target+1 — same rule _market_line_bounds enforces in the Analyse tab.
        if parsed["innings"] == 2 and parsed["target"] and parsed["target"] > 0:
            line_cap = float(parsed["target"]) + 1.0
            if parsed["line"] is not None and parsed["line"] > line_cap:
                warnings.append(
                    f"Line {parsed['line']:.1f} exceeds the chase target — unreachable; "
                    f"capped to target+1 = {line_cap:.1f}."
                )
                parsed["line"] = line_cap

        match_id = parsed["match_id"] or _macro_synth_id(parsed)
        payload = macro_to_payload(parsed)

        # Persist + bump DB metrics (Saved → In stats DB)
        try:
            parsed_db = _macro_parsed_for_db(parsed, payload)
            meta = {
                "date": parsed["date"], "team1": parsed["batting_team"],
                "team2": parsed["bowling_team"], "venue": parsed["venue"],
                "format": parsed["format"],
            }
            save_manual_scorecard(match_id, st.session_state.get("macro_text", ""), parsed_db, meta)
            try:
                from data.ingest import ingest_manual_scorecards
                ingest_manual_scorecards()
            except Exception as e:
                warnings.append(f"Saved, but stats-DB ingest was skipped: {e}")
        except Exception as e:
            if "UNIQUE" in str(e):
                warnings.append(f"`{match_id}` was already saved — re-ran the projection without duplicating it.")
            else:
                warnings.append(f"Could not save scorecard: {e}")

        # Run the full projection pipeline instantly
        try:
            with st.spinner("Extracting & projecting…"):
                result = run_projection_from_payload(payload)
        except Exception as e:
            st.error(f"Projection failed: {e}")
            import traceback
            st.exception(e)
            return

        ld = payload["live_data"]
        st.session_state["macro_result"] = result
        st.session_state["macro_warnings"] = warnings
        st.session_state["macro_summary"] = (
            f"{parsed['batting_team']} vs {parsed['bowling_team']} · "
            f"{ld['runs']}/{ld['wickets']} in {ld['overs']:.1f} ov · "
            f"line {parsed['line']:.1f} · {parsed['format']}"
        )
        st.rerun()

    # ── Projection result (persists across reruns) ──
    if st.session_state.get("macro_result"):
        for w in st.session_state.get("macro_warnings", []):
            st.warning(w)
        st.success(f"✅ Parsed & projected — {st.session_state.get('macro_summary', '')}")
        render_probability_card(st.session_state["macro_result"])
        if st.button("✖ Clear result", key="macro_clear"):
            for k in ("macro_result", "macro_warnings", "macro_summary"):
                st.session_state.pop(k, None)
            st.rerun()

    st.markdown("---")
    st.markdown("#### Saved scorecards")

    all_cards = get_all_manual_scorecards()
    if not all_cards:
        st.info("No scorecards saved yet. Paste one above.")
        return

    for sc in all_cards:
        ingested = bool(sc.get("ingested"))
        status = "✅ In stats DB" if ingested else "⏳ Pending ingest"
        parsed = sc.get("parsed_json") or {}
        inn1 = (parsed.get("innings") or [{}])[0]
        inn2 = (parsed.get("innings") or [{}, {}])[1] if len(parsed.get("innings", [])) > 1 else {}
        summary = (
            f"**`{sc['match_id']}`** — {sc.get('team1')} vs {sc.get('team2')} "
            f"({sc.get('match_date') or '—'}) · {status}"
        )
        if inn1:
            summary += (
                f"  \nInn1: {inn1.get('total_runs', '?')}/{inn1.get('wickets', '?')}"
            )
        if inn2 and inn2.get("total_runs") is not None:
            summary += f" · Inn2: {inn2.get('total_runs', '?')}/{inn2.get('wickets', '?')}"

        with st.expander(summary, expanded=not ingested):
            st.caption(f"Venue: {sc.get('venue') or '—'} · Format: {sc.get('format') or config.PRIMARY_FORMAT}")

            if not ingested:
                st.warning("Not in stats DB yet — click **Process into stats DB** at the top of this tab.")

            meta = {
                "date": sc.get("match_date"),
                "team1": sc.get("team1"),
                "team2": sc.get("team2"),
                "venue": sc.get("venue"),
                "format": sc.get("format"),
            }

            lc1, lc2, lc3 = st.columns([2, 2, 3])
            with lc1:
                load_inn = st.selectbox(
                    "Innings",
                    [1, 2],
                    key=f"load_inn_{sc['match_id']}",
                )
            with lc2:
                max_ov = 20
                if load_inn == 1 and inn1.get("overs"):
                    max_ov = max(r["over"] for r in inn1["overs"]) + 1
                elif load_inn == 2 and inn2.get("overs"):
                    max_ov = max(r["over"] for r in inn2["overs"]) + 1
                at_over = st.selectbox(
                    "Snapshot at end of over",
                    list(range(1, max_ov + 1)),
                    index=max_ov - 1,
                    key=f"load_ov_{sc['match_id']}",
                )
            with lc3:
                st.markdown("<div style='height:1.6rem'></div>", unsafe_allow_html=True)
                if st.button(
                    "▶ Load in Replay",
                    key=f"load_replay_{sc['match_id']}",
                    type="primary",
                    use_container_width=True,
                    disabled=not parsed.get("innings"),
                ):
                    try:
                        payload = scorecard_snapshot_to_replay(
                            parsed, meta, innings_num=load_inn, at_cricket_over=at_over,
                        )
                        apply_scorecard_replay(payload)
                        st.success(
                            "Loaded! Open the **🏏 Analyse** tab — Replay mode is ready. "
                            "Set your market & line, then hit ANALYSE."
                        )
                    except Exception as e:
                        st.error(f"Could not load: {e}")


def _parse_scorecard_text(inn1: str, inn2: str, team1: str, team2: str, venue: str, date: str) -> dict:
    """Parse pipe-separated over text into innings structure."""
    import re

    def parse_innings(text: str, batting_team: str, bowling_team: str, innings_num: int) -> dict:
        overs = []
        total_runs = 0
        total_wickets = 0
        for line in text.strip().split("\n"):
            if "|" not in line or line.strip().startswith("Over"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                try:
                    over_num = int(parts[0]) - 1
                    runs     = int(parts[1])
                    wkts     = int(parts[2])
                    bowler   = parts[3] if len(parts) > 3 else "Unknown"
                    overs.append({"over": over_num, "runs": runs, "wickets": wkts, "bowler": bowler})
                    total_runs += runs
                    total_wickets += wkts
                except ValueError:
                    continue
        return {
            "batting_team":  batting_team,
            "bowling_team":  bowling_team,
            "innings":       innings_num,
            "total_runs":    total_runs,
            "wickets":       total_wickets,
            "overs":         overs,
        }

    return {
        "venue": venue,
        "date":  date,
        "team1": team1,
        "team2": team2,
        "innings": [
            parse_innings(inn1, team1, team2, 1),
            parse_innings(inn2, team2, team1, 2),
        ],
    }


# ─────────────────────────────────────────────────────────────────
# CHART STYLING HELPER
# ─────────────────────────────────────────────────────────────────

def _style_chart(fig: go.Figure, title: str = "", ytitle: str = ""):
    """Apply CricEdge dark navy theme to a plotly figure."""
    fig.update_layout(
        title=dict(text=title, font=dict(color="#2ee6a6", size=14, family="Space Grotesk")),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#98a2b6", family="Inter"),
        showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10),
        xaxis=dict(
            gridcolor="rgba(0,212,212,0.06)",
            zerolinecolor="rgba(0,212,212,0.1)",
            tickfont=dict(color="#98a2b6"),
        ),
        yaxis=dict(
            title=ytitle,
            gridcolor="rgba(0,212,212,0.06)",
            zerolinecolor="rgba(0,212,212,0.1)",
            tickfont=dict(color="#98a2b6"),
        ),
    )
