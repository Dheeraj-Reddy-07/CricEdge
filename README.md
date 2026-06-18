# CricEdge 🏏 — Cricket Innings Score Projection & Probability Engine

**A machine-learning system that projects a batting team's innings total in
limited-overs cricket and estimates the probability of the team reaching a
target score, in real time, at any point during an innings.**

CricEdge combines a phase-aware statistical forecasting engine with a calibrated
machine-learning probability layer. Given the live match state — overs bowled,
runs, wickets, batters at the crease, bowler usage, venue, and recent scoring
pace — it projects the expected final (or interval) score and outputs a
**calibrated probability** that the team finishes above a chosen **target score**,
together with a confidence-tiered forecast and a transparent breakdown of the
factors driving the prediction.

> **Academic project.** Built as a B.Tech major project in applied machine
> learning and sports analytics. It is a forecasting and data-analysis system,
> intended for research and educational use.

---

## 1. Problem statement

Predicting the final score of a cricket innings is a hard sequential forecasting
problem: the scoring rate changes sharply across phases (powerplay, middle,
death), depends on the quality of the batting and bowling line-ups, the venue,
the match situation (wickets in hand, required rate when chasing), and live
momentum. A naïve "current run-rate × remaining overs" projection is badly
miscalibrated, especially early in an innings.

**Goal:** produce, at every stage of an innings, a *well-calibrated probability*
that the batting team's score will reach or exceed a given target — not just a
point estimate, but a trustworthy confidence number.

---

## 2. Key features

- **Real-time innings projection** — projects the expected final score (and the
  expected runs in any sub-interval, e.g. overs 4–6 or 7–10) from the live state.
- **Calibrated probability output** — a saved `IsotonicRegression` calibrator
  maps raw model output to a probability that is *calibrated* against historical
  outcomes, so "70%" really means ~70%.
- **Confidence-tiered forecast** — every prediction is labelled on a clear scale
  (HIGH CONFIDENCE · ABOVE / BELOW target, LIKELY ABOVE / BELOW, or TOO CLOSE TO
  CALL) with an auto-generated, plain-English explanation of *why*.
- **Phase-aware feature model** — a historical baseline derived from ball-by-ball
  "match-position" statistics, plus signed feature adjustments whose magnitudes
  differ by phase, refined by nine conditional context modifiers (wicket
  clustering, scorer concentration, bowling-resource depletion, pitch
  deterioration, spin-in-death mismatch, batting-depth/tail risk, and more).
- **Multiple prediction scenarios** — full-innings total, next-N-overs runs, and
  fixed session intervals, all priced off the same projection engine.
- **Live + manual data** — auto-fetches the live scorecard, with full manual
  entry as a fallback; the model never depends on the scraper.
- **Tracking & analytics** — every prediction is logged to SQLite; History and
  Analytics tabs show running accuracy, calibration (reliability) charts, and
  error analysis.

---

## 3. System architecture

```
                ┌─────────────────────────────────────────────┐
   Live state → │  Feature builder (phase, momentum, depth,    │
   (or manual)  │  matchup, venue, pitch, resources, …)        │
                └───────────────────┬─────────────────────────┘
                                    │  features
                ┌───────────────────▼─────────────────────────┐
                │  Phase-dynamic statistical engine            │
                │  baseline (historical position table)        │
                │  + signed adjustments + 9 context modifiers  │
                └───────────────────┬─────────────────────────┘
                                    │  projected score + raw prob.
                ┌───────────────────▼─────────────────────────┐
                │  Line-anchored probability                   │
                │  P(score ≥ target) via calibrated sigmoid    │
                └───────────────────┬─────────────────────────┘
                                    │  raw probability
                ┌───────────────────▼─────────────────────────┐
                │  ML calibration layer (IsotonicRegression)   │
                └───────────────────┬─────────────────────────┘
                                    │  calibrated probability
                            confidence-tiered forecast + insight
```

---

## 4. Methodology

1. **Feature engineering** — the live state is decomposed into clean, model-ready
   features: phase, current vs. par run-rate (momentum), batting resources and
   tail risk, batter–bowler matchups, team strength, venue scoring profile, pitch
   & conditions, and recent boundary/dot-ball rates.
2. **Phase-dynamic baseline** — a historical baseline probability is looked up
   from a ball-by-ball *match-position* table (probability of exceeding a score
   from a given over/wickets/runs state), with hierarchical smoothing for sparse
   states.
3. **Score projection** — the engine projects the expected final/interval score
   by blending team, venue, opposition, and live-pace signals.
4. **Probability anchoring** — `P(score ≥ target)` is computed from the projected
   score and target via a sigmoid, so the probability always moves consistently
   with the projection-vs-target gap.
5. **Calibration** — raw probabilities are passed through a trained isotonic
   regressor so the reported confidence matches empirical hit rates.
6. **Confidence tiering** — the calibrated probability is mapped to a forecast
   label and a natural-language rationale.

---

## 5. Dataset

The model is backed by historical **ball-by-ball** data (Cricsheet), parsed into:

- a **match-position table** (P(final ≥ X | over, wickets, runs)),
- **team batting / bowling** phase profiles,
- **venue** scoring profiles, and
- **player** (batter / bowler) phase statistics.

The repository ships with a pre-built SQLite database so the app runs
immediately; the ingestion pipeline can rebuild it from raw data.

---

## 6. Model validation

- **Leak-free out-of-sample evaluation** — a strict chronological train/test
  split prevents look-ahead leakage; performance is reported on unseen matches.
- **Probability calibration** — reliability is assessed with calibration curves;
  the isotonic layer is fit on training data only.
- **Ablation studies** — the contribution of each feature/modifier is measured so
  the model is not over-engineered.

> Honest finding: rigorous out-of-sample testing showed the context modifiers add
> only a small, marginal lift once leakage is removed — a result documented as
> part of the project's validation discipline rather than hidden.

---

## 7. Tech stack

Python · Streamlit (UI) · pandas · NumPy · scikit-learn (isotonic calibration,
via joblib) · SciPy · BeautifulSoup + requests (data fetch) · Plotly (charts) ·
SQLite (storage).

---

## 8. Project structure

```
CricEdge/
├── app.py                  # Streamlit entrypoint (UI + prediction flow)
├── config.py               # Weights, thresholds, phase ranges, modifier toggles
├── live_calibrator.pkl     # Saved IsotonicRegression calibrator
├── requirements.txt
├── data/
│   ├── database.py         # SQLite schema + query helpers
│   ├── ingest.py           # Ball-by-ball download + parse + populate DB
│   └── cricedge.db         # Pre-built database
├── model/
│   ├── probability.py      # Phase-dynamic baseline + adjustments + anchoring
│   ├── modifiers.py        # The 9 conditional context modifiers
│   ├── innings_types.py    # First-innings vs chasing model instances
│   └── market.py           # Prediction-scenario routing + interval engine
├── scraper/
│   └── live_score.py       # Live-score fetcher (with manual fallback)
├── ui/
│   ├── components.py        # Reusable Streamlit components
│   └── styles.css           # Dark theme
└── archive/                # Backtests, diagnostics, and validation scripts
```

---

## 9. Getting started

### Prerequisites
- Python 3.10+ (developed on 3.12)

### Install
```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate        # macOS / Linux

pip install -r requirements.txt
```

### Run
```bash
streamlit run app.py
```
The database initialises automatically on first run.

---

## 10. Usage

1. Type the match (or click **Demo**) — teams auto-fill.
2. **Load live data** — score, batters, bowlers, and recent overs auto-populate
   (or enter them manually).
3. Choose a **prediction scenario** and set the **target score**.
4. Click **ANALYSE** — get the calibrated probability and confidence-tiered
   forecast with a factor breakdown.

Pitch, weather, and toss are auto-inferred from venue history; advanced overrides
live in a collapsible expander.

---

## 11. Future work

- Replace the hand-tuned adjustment ranges with a gradient-boosted model
  (XGBoost) trained on the same feature set — the features are already structured
  for this migration.
- Per-format and per-tier calibration to address scoring-era and associate-nation
  distribution shift.
- Live probability time-series and uncertainty bands across the innings.

---

## 12. Disclaimer

CricEdge is a **statistical and educational** project for cricket score
forecasting and sports-analytics research. Probability estimates are model
outputs, not guarantees of real-world outcomes.
