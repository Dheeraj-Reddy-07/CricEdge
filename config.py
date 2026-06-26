"""
config.py — CricEdge Central Configuration
All weights, adjustment ranges, modifier thresholds, and phase
boundaries are stored here. Tune without touching model code.
After 300+ predictions: migrate features to XGBoost using
each factor as a clean separate input (never blend).
"""

# ─────────────────────────────────────────────────────────────────
# PHASE BOUNDARIES (overs, inclusive)
# ─────────────────────────────────────────────────────────────────
# T20 format boundaries — death is last 4 overs (17–20)
PHASE_BOUNDARIES = {
    "powerplay": (0, 6),
    "middle":    (7, 16),
    "death":     (17, 20),
}

# ODI format boundaries (50 overs)
PHASE_BOUNDARIES_ODI = {
    "powerplay": (0, 10),    # overs 1-10
    "middle":    (11, 40),   # overs 11-40
    "death":     (41, 50),   # overs 41-50
}

# Map format string → which boundary set to use
FORMAT_IS_ODI = {
    "Women's T20I": False,
    "T20I":         False,
    "T20 Blast":    False,   # English domestic T20 — same phase boundaries as T20I
    "Men's ODI":    True,
    "Women's ODI":  True,
    "Men's List A": True,
}


# ─────────────────────────────────────────────────────────────────
# ADJUSTMENT RANGES PER PHASE
# Each factor: (min_adjustment, max_adjustment) in percentage points
# Positive = helps OVER, Negative = helps UNDER
# ─────────────────────────────────────────────────────────────────
ADJUSTMENT_RANGES = {
    "powerplay": {
        "momentum":            (-8.0, 8.0),   # live RR vs par RR
        "partnership_rate":    (0.0,  8.0),   # current partnership velocity
        "batter_bowler":       (-6.0, 6.0),   # matchup quality
        "available_resources": (-5.0, 5.0),   # wickets + batting depth COMBINED
        "pitch_weather":       (-8.0, 5.0),   # pitch type + dew + weather
        "team_strength":       (-5.0, 5.0),   # relative team quality
        "boundary_pct":        (-4.0, 4.0),   # boundary % last 3 overs
        "dot_ball_pct":        (-4.0, 4.0),   # dot ball % last 3 overs
        "toss":                (-2.0, 2.0),   # toss advantage
        "death_bowler_quota":  (0.0,  0.0),   # SKIP in powerplay
    },
    "middle": {
        "momentum":            (-10.0, 10.0),
        "available_resources": (-10.0, 10.0),
        "boundary_pct":        (-7.0,  7.0),
        "dot_ball_pct":        (-6.0,  6.0),
        "batter_bowler":       (-5.0,  5.0),
        "partnership_rate":    (-5.0,  5.0),
        "death_bowler_quota":  (-5.0,  5.0),
        "team_strength":       (-4.0,  4.0),
        "pitch_weather":       (-4.0,  4.0),
        "toss":                (0.0,   0.0),  # ignore in middle
    },
    "death": {
        "batter_bowler":       (-15.0, 15.0), # HIGHEST weight at death
        "death_bowler_quota":  (-12.0, 12.0),
        "dot_ball_pct":        (-10.0, 10.0),
        "boundary_pct":        (-10.0, 10.0),
        "available_resources": (-10.0, 10.0),
        "momentum":            (-8.0,  8.0),
        "pitch_weather":       (-5.0,  5.0),
        "team_strength":       (-2.0,  2.0),
        "toss":                (0.0,   0.0),  # pure noise by death
        "partnership_rate":    (0.0,   0.0),  # ignore in death
    },
}

# Death overs: base probability is capped at 35% influence
# Live signals dominate. Scale the base contribution accordingly.
DEATH_BASE_CAP_PCT = 35.0

# ─────────────────────────────────────────────────────────────────
# CHASING INNINGS — Required Run Rate Adjustments
# Applied at all phases for second innings only
# ─────────────────────────────────────────────────────────────────
# T20 RRR thresholds
CHASING_RRR_ADJUSTMENTS = {
    "rrr_easy":    {"max_rrr": 8.0,  "adjustment": +10.0},  # RRR < 8
    "rrr_neutral": {"max_rrr": 10.0, "adjustment":   0.0},  # RRR 8-10
    "rrr_hard":    {"max_rrr": 12.0, "adjustment":  -8.0},  # RRR 10-12
    "rrr_extreme": {"max_rrr": 99.0, "adjustment": -18.0},  # RRR > 12
}

# ODI RRR thresholds (lower threshold — more balls remaining)
CHASING_RRR_ADJUSTMENTS_ODI = {
    "rrr_easy":    {"max_rrr": 6.0,  "adjustment": +10.0},  # RRR < 6
    "rrr_neutral": {"max_rrr": 8.0,  "adjustment":   0.0},  # RRR 6-8
    "rrr_hard":    {"max_rrr": 10.0, "adjustment":  -8.0},  # RRR 8-10
    "rrr_extreme": {"max_rrr": 99.0, "adjustment": -18.0},  # RRR > 10
}

# ─────────────────────────────────────────────────────────────────
# CONDITIONAL MODIFIERS (applied last, in order)
# Each modifier can be toggled off without touching model code.
# ─────────────────────────────────────────────────────────────────
MODIFIERS_ENABLED = {
    "wicket_clustering":        True,
    "early_collapse":           True,
    "scorer_concentration":     True,
    "bowling_quota_trap":       True,
    "par_score_trap":           True,
    "pitch_deterioration":      True,
    "new_batter_transition":    True,
    "exceptional_bowler_today": True,   # HIGHEST PRIORITY — never disable
    "spin_death_mismatch":      True,
    "psychological_ceiling":    True,
}

MODIFIER_PARAMS = {
    "wicket_clustering": {
        "wickets_in_window":    2,      # 2+ wickets triggers
        "over_window":          2,      # in last N overs
        "collapse_wickets":     5,      # 5+ down in death also triggers
        "death_over_start":     17,     # T20 death start (1-indexed)
        "adjustment":           -8.0,
    },
    "early_collapse": {
        "max_over":             6,      # only within the first 6 overs (powerplay)
        "min_wickets":          2,      # 2+ wickets down triggers
        "adj_two_wickets":      -10.0,  # exactly 2 down early
        "adj_three_plus":       -15.0,  # 3+ down early (genuine collapse)
    },
    "scorer_concentration": {
        "high_concentration_threshold": 0.60,  # >60% of runs by one batter
        "high_concentration_adj":       -12.0,
        "spread_min_batters":           3,      # 3+ set batters = spread
        "spread_adj":                   +5.0,
    },
    "bowling_quota_trap": {
        "elite_death_economy_threshold": 8.5,   # career death eco < this = elite
        "elite_bowlers_for_trigger":     2,     # 2+ elite with full quota
        "elite_full_quota_adj":          -10.0,
        "part_timer_overs_threshold":    2,     # part timer must bowl 2+ death overs
        "part_timer_adj":                +12.0,
        # Workload exhaustion check (active, highest priority)
        # If the top-4 frontline bowlers have collectively bowled >=16 overs
        # entering the death, primaries are exhausted → force part-timer scenario.
        "workload_quota_overs":    16,    # top-4 overs sum that triggers exhaustion
        "workload_trigger_over":   16.0,  # only check at/after over 16 boundary
        "workload_adj":             7.0,  # run adjustment applied to death window
    },
    "par_score_trap": {
        "target_overs":   [13, 14, 15],         # overs 13-15 check
        "adjustment":     -6.0,
    },
    "pitch_deterioration": {
        "slowdown_threshold":  0.20,   # >20% slowdown overs 15-20 vs 1-10
        "trigger_after_over":  14,     # only after over 14
        "adjustment":          -8.0,
    },
    "new_batter_transition": {
        "ball_window":    2,           # wicket in last N balls
        "adjustment":     -5.0,
    },
    "exceptional_bowler_today": {
        "economy_threshold":      6.0,  # eco < this = exceptional
        "min_overs_bowled_today": 3,    # must have bowled 3+ overs today
        "adjustment":             -8.0, # on top of bowler quota factor
    },
    "spin_death_mismatch": {
        "spin_friendly_sr_threshold":    150.0, # batter SR>150 vs spin in death
        "spin_friendly_adj":             +10.0,
        "spin_struggles_adj":            -5.0,
    },
    "psychological_ceiling": {
        "score_threshold": 190,         # team above this at over 18 (full innings)
        "score_threshold_death": 170,   # lower bar in last 2 overs (overs 19–20)
        "trigger_over":    18,
        "default_adj":     -6.0,        # default when no historical data
    },
}

# Minimum historical samples before trusting match_position_stats baseline
MIN_POSITION_SAMPLES = 30

# Venue data quality gates
MIN_VENUE_SAMPLES_RPO = 5        # below this → use format-average RPO, never 0.0
MIN_VENUE_SAMPLES_MODIFIER = 10  # below this → pitch/par venue modifiers stay inactive

# ─────────────────────────────────────────────────────────────────
# AVAILABLE RESOURCES METRIC
# Combine wickets in hand + batting depth into ONE metric.
# Avoids double-counting. Weighted by remaining batters' death SR.
# ─────────────────────────────────────────────────────────────────
RESOURCES_WEIGHTS = {
    "wickets_weight":    0.4,   # 40% weight on raw wicket count
    "batting_quality":   0.6,   # 60% weight on quality of remaining batters
}

# Default death SR tiers for quality classification (used when no DB data)
BATTER_QUALITY_TIERS = {
    "elite":    {"min_sr": 160.0, "quality_score": 1.0},
    "good":     {"min_sr": 140.0, "quality_score": 0.75},
    "average":  {"min_sr": 120.0, "quality_score": 0.5},
    "lower":    {"min_sr": 0.0,   "quality_score": 0.25},
}

# ─────────────────────────────────────────────────────────────────
# PROBABILITY BOUNDS
# ─────────────────────────────────────────────────────────────────
PROBABILITY_MIN = 5.0   # never output below this
PROBABILITY_MAX = 95.0  # never output above this

# Minimum death-over SR below which an incoming batter counts as tail risk.
# At or above this threshold (or if DB stats are absent) no wicket penalty fires.
DEATH_DEPTH_SR_THRESHOLD = 115.0

# ─────────────────────────────────────────────────────────────────
# VERDICT THRESHOLDS
# ─────────────────────────────────────────────────────────────────
# Unified confidence ranges based on the probability of finishing ABOVE target.
# Used consistently across badge, insight, history, and analytics.
# (Category keys retain OVER/UNDER suffixes as internal identifiers only;
#  every user-facing label is neutral — see VERDICT_DISPLAY_LABELS.)
VERDICT_RANGES = {
    "STRONG_OVER":   (75, 100),   # >75% above target
    "VALUE_OVER":    (65, 75),    # 65-75% above target
    "LEAN_OVER":     (55, 65),    # 55-65% above target
    "TOSS_UP":       (45, 55),    # 45-55% — too close to call
    "LEAN_UNDER":    (35, 45),    # 35-45% above = 55-65% below target
    "VALUE_UNDER":   (25, 35),    # 25-35% above = 65-75% below target
    "STRONG_UNDER":  (0, 25),     # <25% above = >75% below target
}

# Display labels for each verdict category
VERDICT_DISPLAY_LABELS = {
    "STRONG_OVER":   "⚡ HIGH CONFIDENCE — ABOVE TARGET",
    "VALUE_OVER":    "⚡ HIGH CONFIDENCE — ABOVE TARGET",
    "LEAN_OVER":     "📈 LIKELY — ABOVE TARGET",
    "TOSS_UP":       "🔄 TOO CLOSE TO CALL",
    "LEAN_UNDER":    "📉 LIKELY — BELOW TARGET",
    "VALUE_UNDER":   "⚡ HIGH CONFIDENCE — BELOW TARGET",
    "STRONG_UNDER":  "⚡ HIGH CONFIDENCE — BELOW TARGET",
}

# Legacy thresholds (deprecated - use VERDICT_RANGES instead)
VERDICT_THRESHOLDS = {
    "value_bet_min":  65.0,   # >=65% -> HIGH CONFIDENCE (fallback when sigma=0)
    "skip_min":       52.0,   # 52–65% -> LOW CONFIDENCE
    "toss_up_min":    38.0,   # 38–52% -> TOO CLOSE TO CALL (clamp [30%,70%])
    # <38% -> LOW CONFIDENCE
    # Primary confidence tier for interval scenarios uses the Z-score gate.
}

# Z-score confidence gate threshold (primary tiering for interval scenarios).
# Z = (expected_runs - target_score) / sigma
# |Z| >= threshold -> high-confidence forecast (ABOVE or BELOW target)
# |Z| <  threshold -> low confidence (no statistical separation)
Z_SCORE_THRESHOLD = 0.75

# ─────────────────────────────────────────────────────────────────
# FORMATS
# ─────────────────────────────────────────────────────────────────
SUPPORTED_FORMATS = ["Women's T20I", "T20I", "T20 Blast"]
PRIMARY_FORMAT = "Women's T20I"

ODI_FORMATS   = set()   # ODI paused — T20I focus only
T20_FORMATS   = {"Women's T20I", "T20I", "T20 Blast"}
MAX_OVERS     = {"Women's T20I": 20, "T20I": 20, "T20 Blast": 20}

# Women's stats are NEVER mixed with Men's player/team stats.
# T20 Blast uses county team names — fully isolated from international formats.
FORMAT_ISOLATION = {
    "Women's T20I": {
        "use_for_player_stats":   True,
        "use_for_team_stats":     True,
        "use_for_match_position": True,
    },
    "T20I": {
        "use_for_player_stats":   False,
        "use_for_team_stats":     False,
        "use_for_match_position": True,
    },
    "T20 Blast": {
        # County domestic format — player and team stats tracked separately.
        # Teams are county sides (Yorkshire, Surrey, etc.) not national teams.
        "use_for_player_stats":   True,
        "use_for_team_stats":     True,
        "use_for_match_position": True,
    },
    "Men's ODI": {
        "use_for_player_stats":   True,
        "use_for_team_stats":     True,
        "use_for_match_position": True,
    },
    "Women's ODI": {
        "use_for_player_stats":   True,
        "use_for_team_stats":     True,
        "use_for_match_position": True,
    },
}

# ─────────────────────────────────────────────────────────────────
# CRICSHEET DATA SOURCES
# ─────────────────────────────────────────────────────────────────
CRICSHEET_BASE_URL = "https://cricsheet.org/downloads/"
CRICSHEET_FORMATS = {
    "Women's T20I": "f_t20s_male_json.zip",  # will be overridden below
    "T20I":         "t20s_male_json.zip",
}

# Actual Cricsheet zip filenames (YAML format, all matches)
CRICSHEET_DOWNLOADS = {
    "Women's T20I": {
        "url":   "https://cricsheet.org/downloads/t20s_female_json.zip",
        "label": "Women's T20I (JSON)",
    },
    "T20I": {
        "url":   "https://cricsheet.org/downloads/t20s_male_json.zip",
        "label": "Men's T20I (JSON)",
    },
    "T20 Blast": {
        "url":   "https://cricsheet.org/downloads/ntb_male_json.zip",
        "label": "T20 Blast (JSON)",
    },
    "Men's ODI": {
        "url":   "https://cricsheet.org/downloads/odis_male_json.zip",
        "label": "Men's ODI (JSON)",
    },
    "Women's ODI": {
        "url":   "https://cricsheet.org/downloads/odis_female_json.zip",
        "label": "Women's ODI (JSON)",
    },
}

# --quick flag: only process matches from the last N seasons
QUICK_INGEST_SEASONS = 2   # last 2 seasons

# ─────────────────────────────────────────────────────────────────
# DATABASE PATH
# ─────────────────────────────────────────────────────────────────
import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "cricedge.db")

# ─────────────────────────────────────────────────────────────────
# OUT-OF-SAMPLE TEMPORAL VALIDATION
# ─────────────────────────────────────────────────────────────────
# Strict train/test boundary for leak-free model validation.
#   training  : match_date <  SPLIT_DATE
#   testing   : match_date >= SPLIT_DATE
# The training database is built ONLY from pre-split matches and is a separate
# artifact from the production DB so post-split data can never leak in.
SPLIT_DATE = "2024-01-01"
TRAIN_DB_PATH = os.path.join(BASE_DIR, "data", "cricedge_train.db")

# ─────────────────────────────────────────────────────────────────
# MOMENTUM CALCULATION
# Par RR = historical avg runs per over at this point in innings
# Momentum signal = (live RR - par RR) / par RR, clamped to [-1, 1]
# ─────────────────────────────────────────────────────────────────
MOMENTUM_CLAMP = 1.0  # raw signal clamped to [-1, +1] before scaling

# ─────────────────────────────────────────────────────────────────
# ANALYTICS ACCURACY BANDS
# ─────────────────────────────────────────────────────────────────
ACCURACY_CONFIDENCE_BANDS = [
    (70.0, 100.0, ">70%"),
    (60.0,  70.0, "60-70%"),
    (52.0,  60.0, "52-60%"),
]

# Calibration chart bucket width
CALIBRATION_BUCKET_WIDTH = 5.0  # 5% buckets (50-55, 55-60, ...)

# ─────────────────────────────────────────────────────────────────
# LIVE SCRAPER
# ─────────────────────────────────────────────────────────────────
SCRAPER_TIMEOUT_SECONDS = 15
SCRAPER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
CREX_LIVE_BASE_URL    = "https://crex.live"
CREX_LIVE_MATCHES_URL = "https://crex.live/fixtures/live"
CRICBUZZ_BASE_URL     = "https://www.cricbuzz.com"
CRICBUZZ_LIVE_URL     = "https://www.cricbuzz.com/cricket-match/live-scores"

# ─────────────────────────────────────────────────────────────────
# SCORECARD PARSER — Manual entry for current tournaments
# (e.g., Women's T20 World Cup 2026 — not yet on Cricsheet)
# ─────────────────────────────────────────────────────────────────
MANUAL_SCORECARD_FORMAT_DEFAULT = "Women's T20I"

# ─────────────────────────────────────────────────────────────────
# PRODUCTION MODEL ASSIGNMENT PER CHECKPOINT  (SINGLE SOURCE OF TRUTH)
# ─────────────────────────────────────────────────────────────────
# Set by the final leakage-free OOS production validation
# (see PRODUCTION_VALIDATION_REPORT.md). The prediction engine reads model
# selection EXCLUSIVELY from this dict — to re-assign a model later, change the
# value here and nothing else; core probability/market logic is untouched.
#
#   Values:  "BASE"   → historical position table only (no adjustments/modifiers)
#            "FULL"   → all 21 features (current production engine)
#            "PRUNED" → greedy-selected 8-feature subset
#
#   Keys (the three intended production checkpoints):
#     OVER3_4_6    : predict after over 3   → market = runs in overs 4-6
#     OVER6_7_10   : predict after over 6   → market = runs in overs 7-10 (Next 4 Overs)
#     OVER15_TOTAL : predict end of over 15 → market = full innings total
#
# NOTE: T20 Blast checkpoints carry over from Women's T20I validation as a starting
# point. They have NOT yet been independently validated for T20 Blast. T20 Blast-
# specific validation will be run once sufficient live match predictions are logged.
CHECKPOINT_MODELS = {
    # Women's T20I — validated production assignments
    "OVER3_4_6":    "PRUNED",
    "OVER6_7_10":   "FULL",
    "OVER15_TOTAL": "PRUNED",
}

# T20 Blast checkpoint model assignments (carried over from Women's T20I — NOT YET
# validated for T20 Blast. Run T20-Blast-specific validation once live data accrues.)
CHECKPOINT_MODELS_T20_BLAST = {
    "OVER3_4_6":    "PRUNED",   # carried over — not yet validated for T20 Blast
    "OVER6_7_10":   "FULL",     # carried over — not yet validated for T20 Blast
    "OVER15_TOTAL": "PRUNED",   # carried over — not yet validated for T20 Blast
}
