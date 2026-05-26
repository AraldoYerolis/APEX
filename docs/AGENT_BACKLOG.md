# APEX Agent Backlog

Implementation roadmap for the APEX agent system.
Each milestone builds on the previous. No milestone skips the safety progression.

---

## Milestone 11G — Candidate Gate Semantics Audit  ← COMPLETE

**Goal:** Disambiguate the misleading `candidate_gate_passed` flag uncovered in
the 11F research export. Some rows had `candidate_gate_passed=true` while
individual gates A/B/D were failing, because the flag was set to
`any(g.passed for g in gates)` despite the name reading as "the candidate
cleared the gate". 11G separates three orthogonal concerns and locks them with
explicit tests.

This is metadata/report/test-scoped only. No runtime behavior, no signal
generation, no alert send path, no Pushover, no `.env` flags, and no DB schema
were touched.

**What changed:**

- `src/apex/strategy/candidate_gates.py`
  - `GATE_VERSION` bumped from `11C_v1` to `11G_v1`.
  - `LEGACY_GATE_VERSION_ANY = "11C_v1"` added so readers can interpret old rows.
  - `candidate_gate_passed` now uses **strict ALL** semantics — True only when
    every gate (A AND B AND C AND D) passes.
  - `candidate_gate_any_passed` added to preserve the legacy 11C ANY value
    (True if at least one gate passes) for downstream readers.
- `src/apex/scheduler/tasks.py` `_capture_signal_features`
  - Adds `research_captured: true` when both 11C gates and 11F research tags
    were merged into `metadata_json` successfully.
  - Adds `alert_eligible: <bool>` computed locally from
    `(not candidate.suppressed) AND (alert_type in ALERT_TYPES_ENABLED)`.
  - `alert_eligible` is **metadata-only** — never read by `_send_alert`,
    `signal_engine`, or any runtime suppression path. Independent of
    `ALERTS_ENABLED` by design so flipping that flag does not retroactively
    rewrite history.
- `scripts/report_candidate_gates.py`
  - Filters rows by metadata-stored `candidate_gate_version`, accepting both
    `11C_v1` (ANY semantics) and `11G_v1` (ALL semantics).
  - Interpretation section now reports both ALL and ANY pass counts plus an
    `alert_eligible` cohort count for 11G rows that recorded the flag.
- Tests
  - `tests/test_candidate_gates.py` — updated ANY→ALL semantics assertions,
    added new tests locking `candidate_gate_any_passed`, gate version bump,
    `research_captured`, and four `alert_eligible` cases (engine-clean,
    type-disabled, suppressed, independent of ALERTS_ENABLED).
  - `tests/test_research_candidates.py:607` — gate version assertion bumped.
- `scripts/create_apex_snapshot.py` — section comment notes 11G; section
  title text kept stable to avoid churning the snapshot test.

**metadata_json merged shape (post-11G row):**
```json
{
  "candidate_gate_version":     "11G_v1",
  "candidate_gate_passed":      false,
  "candidate_gate_any_passed":  true,
  "gates": { ... },
  "research_candidate_version": "11F_v1",
  "research_candidate_names":   [...],
  "research_candidate_flags":   { ... },
  "research_candidate_positive": [...],
  "research_candidate_negative": [...],
  "research_candidate_notes":    [...],
  "research_captured":          true,
  "alert_eligible":             true
}
```

**Audit findings (why 11G):**
- 11C/11F gates were always **diagnostic only** — no runtime code in
  `src/apex/` reads `candidate_gate_passed`. The only consumers are the report
  script and tests. WOULD-BE ALERT volume is driven entirely by
  `signal_engine.evaluate_symbol` and `_send_alert`'s `ALERTS_ENABLED` branch,
  not by these gates. The 11F export confusion was a naming/semantics issue,
  not a runtime bug.
- `feature_version` (the SQL column) stays `11C_v1` because the row shape is
  unchanged. The new `candidate_gate_version=11G_v1` inside `metadata_json`
  is the version tag for the new semantics.

**Safety constraints (all maintained):**
- ALERTS_ENABLED=false, DRY_RUN_MODE=true — unchanged.
- No new alert types, Pushover calls, exchange keys, or open ports.
- No DB schema migration. No historical row rewrites.
- No change to `_send_alert`, `signal_engine.evaluate_symbol`, or scheduler
  behavior. `alert_eligible` is purely metadata.

**What not to do:**
- Do not promote `alert_eligible` from metadata to a runtime filter without a
  dedicated approval milestone.
- Do not treat 11C_v1 (ANY) rows and 11G_v1 (ALL) rows as a single cohort when
  reading `candidate_gate_passed` directly — branch on `candidate_gate_version`
  or use the report's `_all_gates_passed`/`_any_gate_passed` helpers.

---

## Milestone 11F — Prospective Research Candidate Tracker  ← COMPLETE

**Goal:** Tag future signal observations with 11F research cohort metadata so future
reports can answer: "Do the 11E promising/weak cohorts continue to validate prospectively
after they were identified?"

This is report-only / metadata-only / prospective-tracking-only.
No runtime filters are applied. No signals are suppressed. No alerts are changed.
No schema changes. No .env changes. No historical backfill.

**Strategic context:**
The 11E cohort analysis identified promising cohorts (US session, ATR 0.50–1.00, EMA>0.25,
WLD, DOGE, BTC) and weak cohorts (LATE_US, Hour 10, Hour 03, Hour 21) based on dry-run
historical data. 11F tags future observations with these cohort memberships so prospective
outcomes can be compared against the 11E baseline findings.

**What was built (11F):**
- `src/apex/strategy/research_candidates.py` — pure evaluator module, no DB access
  - `RESEARCH_CANDIDATE_VERSION = "11F_v1"`
  - `evaluate_research_candidates(atr_pct, ema_spread_pct, observed_at, symbol, direction) -> dict`
  - Positive tags: SESSION_US, ATR_0_50_TO_1_00, EMA_GT_0_25, SYMBOL_WLD, SYMBOL_DOGE, SYMBOL_BTC
  - Negative tags: SESSION_LATE_US, HOUR_10_AVOID, HOUR_03_AVOID, HOUR_21_AVOID
  - Neutral tags: DIRECTION_LONG, DIRECTION_SHORT, SESSION_ASIA, SESSION_LONDON
  - All missing/invalid inputs fail safely with descriptive reasons, never crash
  - Direction-concentration notes for EMA_GT_0_25 and SYMBOL_WLD (were LONG-concentrated in 11E)
- `src/apex/scheduler/tasks.py` — modified `_capture_signal_features()` to call
  `evaluate_research_candidates()` and merge 11F results into metadata_json alongside
  existing 11C gate fields. Failure is isolated in try/except.
- `scripts/report_research_candidates.py` — read-only prospective tracking report
  - Filters to rows with `research_candidate_version = "11F_v1"` in metadata_json
  - CLI: `--since`, `--min-n`, `--signal-type`, `--research-version`, `--feature-version`
  - Shows baseline (all post-11F closed rows) and per-tag matched/not-matched breakdown
  - Summary tables for positive and negative tags
  - Direction-concentration warnings, INSUFFICIENT sample-size warnings
  - Safe no-data message if no 11F rows exist yet
- `scripts/create_apex_snapshot.py` — updated with "Research Candidate Report (11F)" section
- `tests/test_research_candidates.py` — 83 tests (unit + integration + report)

**How to run:**
```bash
# After deploying 11F, replace timestamp with actual deploy time:
PYTHONPATH=src python scripts/report_research_candidates.py \
    --since 2026-05-23T00:00:00Z

PYTHONPATH=src python scripts/report_research_candidates.py \
    --since 2026-05-23T00:00:00Z --min-n 5
```

**metadata_json merged shape (per row):**
```json
{
  "candidate_gate_version": "11C_v1",
  "candidate_gate_passed": true,
  "gates": { ... },
  "research_candidate_version": "11F_v1",
  "research_candidate_names": ["SESSION_US", "SYMBOL_BTC", ...],
  "research_candidate_flags": { "<tag>": { "matched": true, "reason": "..." }, ... },
  "research_candidate_positive": ["SESSION_US", ...],
  "research_candidate_negative": [],
  "research_candidate_notes": ["EMA_GT_0_25: Direction-concentrated LONG in 11E ..."]
}
```

**Safety constraints (all maintained):**
- Read-only report: never writes to any DB table beyond normal feature row insert
- feature_version remains "11C_v1" — not changed
- No schema changes (metadata_json TEXT column already existed)
- Does not change signal generation, alert eligibility, or scheduler behavior
- Does not change candidate gate logic from 11C
- Does not enable alerts, trading, or Pushover
- ALERTS_ENABLED=false, DRY_RUN_MODE=true are not touched
- No historical backfill — 11F starts prospectively at deploy time
- Old 11C rows are not rewritten

**What not to do:**
- Do not use research candidate tags to filter signals at runtime without an explicit
  approval milestone with dedicated safety review
- Do not treat direction-concentrated cohorts (EMA_GT_0_25, SYMBOL_WLD) as general signals
- Do not backfill existing 11C rows with 11F metadata

---

## Milestone 10B — Signal Feature Snapshot + Learning Memory

**Goal:** Capture a snapshot of indicator values (EMA, RSI, ATR, VWAP, trend bias, pullback state) at the exact moment each signal observation is recorded. This creates the foundational feature store for all downstream scoring agents.

**Implementation notes:**
- Add a `signal_features` DB table: `observation_uid` (FK), `ema_fast`, `ema_slow`, `rsi`, `atr`, `vwap`, `trend_bias`, `pullback_state`, `candle_high`, `candle_low`, `candle_close`, `captured_at`, `metadata_json`
- Capture features synchronously in `_record_observation()` immediately after inserting the observation row
- Write only when `dry_run_mode=True`
- Add a migration for the new table (same pattern as Milestone 10A — `CREATE TABLE IF NOT EXISTS`)
- Add a read-only `scripts/show_signal_features.py` dump script

**Safety constraints:**
- Must not change signal generation or alert behavior
- Must not write to `alerts`, `paper_trades`, `daily_risk`, `signal_observations`
- Feature capture failure must be swallowed (same pattern as observation insert failure)

**Success criteria:**
- Every new `signal_observations` row has a corresponding `signal_features` row
- Feature values match what was computed at observation time (not re-computed later)
- Existing observations without features remain valid (NULL features = pre-10B)

**Tests required:**
- Feature row is created when observation is inserted
- Feature row matches indicator values from the candle store at that moment
- Feature capture failure does not interrupt observation insert
- No feature rows for SETUP_FORMING (no risk_plan, skip feature capture)
- Feature table is not written in live mode (dry_run_mode=False)

**What not to do:**
- Do not backfill features for existing observations (candle state at that time is gone)
- Do not compute features asynchronously — capture inline while candles are fresh
- Do not add ML models or training loops yet

---

## Milestone 10C — Signal Quality Agent / Learning Report v2  ← COMPLETE
## Milestone 10C.1 — Fix Diagnostic Scoring, Labels, and Snapshot Git Metadata  ← COMPLETE

**Goal:** Deep read-only analysis of `signal_features` + `signal_observations` history.
Symbol/direction quality scores, macro alignment, feature buckets, expiry quality.

**What was built (10C):**
- `scripts/report_signal_learning.py` completely rewritten as v2
- Fixed root bug: `create_apex_snapshot.py` was hardcoding `--limit 500`, causing the
  learning report to only see ~210 of 288 confirmed observations. Removed the cap.
- `--limit` now defaults to `None` (unlimited); the SQL `--since` filter is applied
  in the WHERE clause (not in Python after a capped load)
- New sections: long vs short breakdown, symbol+direction breakdown with quality
  scores and recommendation labels, BTC/ETH macro alignment tables, expiry quality
  (after-TP1 vs without-TP1), feature bucket analysis (RSI, ATR%, VWAP%, EMA spread%)
- MFE/MAE pulled via SQL LEFT JOIN with `signal_observations` (no schema change)
- 32 new tests in `tests/test_signal_learning_v2.py`

**What was fixed (10C.1):**
- **Score formula**: Rewrote `_quality_score` with 0.50 baseline and relative-to-overall
  adjustments (replaces old fixed-weight formula that produced Score=0.00 for high-expiry
  setups, making the score useless as a signal)
- **Label contradiction fixed**: Old formula let Score=0.00 produce PROMISING_OBSERVE.
  New label has hard constraints: `score <= 0.05` → WEAK; `expiry >= 80%` → never PROMISING
- **WATCHLIST_NEEDS_EXPIRY_FIX label added**: High expiry but above-avg TP1 or below-avg
  stop → separate from WEAK_OBSERVE_ONLY (setup quality present, expiry is the fixable issue)
- **Top actionable findings section**: Collects symbol/direction labels, RSI/ATR bucket
  anomalies, macro alignment anomalies. Report-only, no runtime changes
- **Snapshot git metadata fixed**: `git -C <repo_root>` so branch/commit populate correctly
  when running under systemd or from a non-repo CWD. Shows `unavailable (reason)` explicitly
  instead of silently blank
- 8 additional tests in `tests/test_signal_learning_v2.py` (40 total)

**How to run:**
```bash
# Full dataset (unlimited)
PYTHONPATH=src python scripts/report_signal_learning.py

# Filtered since a timestamp
PYTHONPATH=src python scripts/report_signal_learning.py --since 2026-05-20T00:00:00Z

# Export raw features to CSV
PYTHONPATH=src python scripts/report_signal_learning.py --csv /tmp/features.csv

# Manual row cap for debugging
PYTHONPATH=src python scripts/report_signal_learning.py --limit 200
```

**Safety constraints (all maintained):**
- Read-only: never writes to `alerts`, `paper_trades`, `daily_risk`, `signal_observations`
- Quality scores and recommendation labels are DIAGNOSTIC ONLY
- Labels are not used for signal generation, filtering, or alert gating
- No config values are changed at runtime
- `ALERTS_ENABLED=false`, `DRY_RUN_MODE=true` are not touched

**What the major sections mean:**
- **Outcome summary**: overall TP1/TP2/stop/expiry rates across all closed confirmed features
- **Long vs short**: compare outcomes by direction; warns when SHORT N < 10
- **Symbol + direction breakdown**: per-group metrics, diagnostic score (0–1), recommendation label
- **Macro alignment**: how BTC/ETH trend bias at capture time correlates with outcomes
- **Expiry quality**: separates "expired after TP1" (partial win) from "expired without TP1" (full miss)
- **Feature buckets**: RSI/ATR/VWAP/EMA spread bucket analysis — which indicator conditions correlate with better or worse outcomes
- **Diagnostic quality scores**: REPORT-ONLY, not stored in DB, not used for trading

**What not to do:**
- Do not use quality scores or recommendation labels to filter signals or change config
- Do not store diagnostic scores in DB until a dedicated `signal_quality_scores` table
  is designed (planned for a future 10D sub-milestone)
- Do not re-enable `--limit` cap in snapshot scripts

---

## Milestone 11A — Strategy Filter Simulator  ← COMPLETE
## Milestone 11A.1 — Usability Fix + Stricter Verdicts  ← COMPLETE

**Goal:** Retrospective simulation of candidate filter rules against historical dry-run
signal_features + signal_observations data. Answer: "If we had applied these rules,
what would historical outcomes have looked like?"

**What was built (11A):**
- `scripts/simulate_strategy_filters.py` — read-only report script
- 28 candidate filter rules across 8 groups (ATR, EMA spread, RSI, VWAP, direction,
  BTC macro alignment, ETH macro alignment, retrospective label-based, combined)
- Baseline performance section (all closed CONFIRMED_SETUP rows)
- Per-filter output: rows kept/removed, TP1/TP2/stop/expiry rates, deltas, verdict label
- Ranked "Top simulated filters" section
- "Filters that look dangerous" section
- Interpretation section, RETROSPECTIVE warnings
- Integrated into `create_apex_snapshot.py` as "Strategy Filter Simulation Report"

**What was fixed/added (11A.1):**
- **Direct script execution fixed**: Added `sys.path` bootstrap (same pattern as
  `create_apex_snapshot.py`). `PYTHONPATH=src python scripts/simulate_strategy_filters.py`
  now works correctly without requiring `PYTHONPATH=.:src`.
- **New verdict label `IMPROVES_BUT_EXPIRY_HIGH`**: A filter that improves TP1 and/or
  avgR but has expiry >= 80% now receives this label instead of `IMPROVES_SIGNAL_QUALITY`.
  Prevents misleadingly labeling extreme-expiry filters as "clean improvements."
- **Verdict order**: INSUFFICIENT_SAMPLE → REDUCES_SAMPLE_TOO_MUCH → WORSE_THAN_BASELINE
  → IMPROVES_BUT_EXPIRY_HIGH → IMPROVES_SIGNAL_QUALITY → MIXED_NEEDS_REVIEW
- **Warning flags per filter**: Each result includes a compact `Flags:` line with any
  applicable flags: HIGH_EXPIRY, THIN_SAMPLE, RETROSPECTIVE_LABEL, STOP_WORSE,
  TP2_WORSE, AVG_R_WORSE
- **Balanced candidates section**: New ranking section after "Top simulated filters."
  Scores = TP1Δ×0.40 + stopΔ×0.30 + avgRΔ×0.20 + exp-no-TP1Δ×0.10 with penalties
  for high expiry (-0.20), thin sample (-0.10), retrospective label (-0.05).
- **Interpretation enhanced**: Explicit language for IMPROVES_BUT_EXPIRY_HIGH: "not
  strategy-ready"; high expiry means timing/exit problem, not entry selection problem.
- **CSV updated**: Includes `flags` (pipe-separated) and `balanced_score` columns.
- 13 new tests (37 total in test_strategy_filter_simulator.py; 233 total in suite)

**How to run:**
```bash
# Direct execution (fixed in 11A.1):
PYTHONPATH=src python scripts/simulate_strategy_filters.py
PYTHONPATH=src python scripts/simulate_strategy_filters.py --since 2026-05-20T10:28:00Z
PYTHONPATH=src python scripts/simulate_strategy_filters.py --since 2026-05-20T10:28:00Z --min-n 10
PYTHONPATH=src python scripts/simulate_strategy_filters.py --since 2026-05-20T10:28:00Z --csv /tmp/sim.csv
```

**Safety constraints (all maintained):**
- Read-only: never writes to any DB table
- Does not change runtime signal generation or scheduler behavior
- Does not suppress or alter live/dry-run observations
- Does not enable alerts or trading
- Retrospective label-based filters use group labels from the same dataset —
  clearly flagged as RETROSPECTIVE in output and penalized in balanced ranking

**Important limitations / overfitting warnings:**
- All filters are retrospective. A filter that "improves" signal quality here may not
  generalise to future data.
- IMPROVES_BUT_EXPIRY_HIGH is NOT strategy-ready. High expiry at this level indicates
  a timing or exit problem, not an entry quality improvement.
- Label-based filters (G/H) are especially susceptible to overfitting.
- Sample sizes for SHORT, rare symbols, and narrow indicator buckets are often < 10.
- High expiry (~75%+) remains the dominant failure mode across most filter subsets.

**What this milestone does NOT do:**
- Does not activate any filter in production scanning
- Does not change ALERTS_ENABLED, DRY_RUN_MODE, or any config value
- Does not store simulation results in DB (stdout/CSV only)
- Does not promote candidate filters to runtime gates

---

## Milestone 11B — Out-of-Sample Filter Validation  ← COMPLETE

**Goal:** Chronological train/validation split validation of candidate strategy filters.
Answer: "Did a filter that improved metrics on historical (train) data also improve
metrics on a held-out chronological (validation) slice?"

**What was built (11B):**
- `scripts/validate_strategy_filters.py` — read-only out-of-sample validation report
- Imports 13 predicate functions from `simulate_strategy_filters` (no duplication)
- `_stability_label()` — 7-tier stability classification in checked order:
  INSUFFICIENT_VALIDATION_SAMPLE → HIGH_EXPIRY_RISK → FAILS_VALIDATION →
  TRAIN_ONLY_OVERFIT → VALIDATION_ONLY_REGIME_SHIFT → VALIDATED_CANDIDATE →
  PROMISING_NEEDS_MORE_DATA
- `_stability_score()` — numeric ranking score for validation-set quality
  (TP1Δ×0.40 + stopΔ×0.30 + avgRΔ×0.20 + expNoTP1Δ×0.10, penalties for high
  expiry, thin sample, and train-not-improving)
- 14 non-retrospective candidate filters evaluated (label-based filters excluded —
  they would leak train-set label assignments into validation)
- 7 report sections: header+disclaimer / baseline comparison / per-filter table /
  stability ranking / overfit warning / interpretation / optional CSV export
- Integrated into `create_apex_snapshot.py` as "Out-of-Sample Filter Validation Report"
- `tests/test_strategy_filter_validation.py` — 24 tests (unit + integration)

**How to run:**
```bash
PYTHONPATH=src python scripts/validate_strategy_filters.py
PYTHONPATH=src python scripts/validate_strategy_filters.py --since 2026-05-20T10:28:00Z
PYTHONPATH=src python scripts/validate_strategy_filters.py --split 0.70 --min-n 10
PYTHONPATH=src python scripts/validate_strategy_filters.py --csv /tmp/filter_validation.csv
```

**Safety constraints (all maintained):**
- Read-only: never writes to any DB table
- Does not change runtime signal generation, alerts, or scheduler behavior
- Validation results are not written into runtime config
- Does not enable live trading or alert delivery
- `ALERTS_ENABLED=false`, `DRY_RUN_MODE=true` are not touched

**Important limitations:**
- Validation splits are temporal but the overall sample may reflect a single market regime
- All observations are from dry-run mode — no live execution costs included
- A VALIDATED_CANDIDATE label is encouraging but NOT sufficient for a runtime gate
- A dedicated 11C milestone with explicit approval is required before any filter is
  applied to runtime scanning

**What this milestone does NOT do:**
- Does not activate any filter in production scanning
- Does not promote VALIDATED_CANDIDATE filters to runtime gates automatically
- Does not store validation results in DB (stdout/CSV only)
- Does not change ALERTS_ENABLED, DRY_RUN_MODE, or any config value

**Next step (11C):** Candidate runtime gate design — a separate milestone requiring
explicit approval before any filter is applied to live/dry-run scanning.

---

## Milestone 11D — Dry-Run Exit Policy Simulator  ← COMPLETE

**Goal:** Read-only simulator that compares alternate exit / accounting policies against
existing dry-run signal_features + signal_observations data. Answers: "Given observed
dry-run behavior, would different exit accounting produce better research outcomes?"

This is not a trading system, not a live exit engine, and not a runtime behavior change.
It is a report-only simulator.

**What was built (11D):**
- `scripts/simulate_exit_policies.py` — read-only exit policy simulation report
- Policies simulated:
  - **A  CURRENT_RECORDED_OUTCOME**: uses recorded outcome_r (baseline)
  - **B  TP1_SCALP_ACCOUNTING**: TP1 reached → +1R, stopped-no-TP1 → -1R, expired-no-TP1 → 0R
  - **C  TP1_THEN_BREAKEVEN_APPROX**: approximate "move stop to BE" after TP1
  - **D  STRICT_2R_ONLY**: HIT_2R → +2R, STOPPED → -1R, EXPIRED excluded or 0R (two variants)
  - **E  EXPIRE_PARTIAL_CREDIT**: expired-after-TP1 → +0.5R, expired-no-TP1 → -0.25R (named constants)
  - **F  EARLY_FAILURE_TIMEOUT_ANALYSIS**: failure timing by bucket (5m/10m/15m/30m)
  - **G  TP1_TIME_TO_HIT_ANALYSIS**: distribution of time_to_1r_seconds (avg/median/p25/p75/buckets)
- All policy constants named and auditable at top of script
- CLI: `--since`, `--min-n`, `--feature-version`, `--signal-type`
- Report sections: header / baseline / policy assumptions / comparison table / timing analysis /
  direction breakdown / simulation limitations / interpretation
- LONG vs SHORT breakdown in all major sections
- Integrated into `create_apex_snapshot.py` as "Exit Policy Simulation Report (11D)"
- `tests/test_simulate_exit_policies.py` — 45 tests (24 unit + 21 integration)

**How to run:**
```bash
PYTHONPATH=src python scripts/simulate_exit_policies.py
PYTHONPATH=src python scripts/simulate_exit_policies.py --since 2026-05-21T19:47:48Z
PYTHONPATH=src python scripts/simulate_exit_policies.py --since 2026-05-21T19:47:48Z --min-n 10
PYTHONPATH=src python scripts/simulate_exit_policies.py --feature-version 11C_v1
```

**Safety constraints (all maintained):**
- Read-only: never writes to any DB table
- Does not change signal generation, alert eligibility, or scheduler behavior
- Does not change candidate gate logic or feature_version
- Does not enable alerts, trading, or Pushover
- `ALERTS_ENABLED=false`, `DRY_RUN_MODE=true` are not touched
- No schema changes

**Simulation limitations (explicit in report):**
- Uses recorded dry-run observations only — no candle-by-candle reconstruction
- Policies B/C assume TP1 hit = exit regardless of post-TP1 path
- Policy E constants (+0.5R / -0.25R) are accounting assumptions, not execution outcomes
- Timing fields reflect evaluation pass frequency, not exact tick-level events
- Results are research-only; cannot be used to enable any runtime behavior

**Strategic context:**
- 11C prospective data shows TP1 rate ~22% and expiry rate ~73.5%
- Candidate gates (A/B/C/D) do not improve TP1 rate prospectively
- 11D simulation targets: is the expiry rate the primary problem? Is TP1 scalp better?
- Next investigation: exit/timing behavior before considering entry filter changes

**What not to do:**
- Do not use simulation results to enable runtime gates without an explicit approval milestone
- Do not backfill or impute outcome_r values
- Do not promote any policy assumption to a live exit rule

---

## Milestone 11E — Exit / Timing Cohort Analysis  ← COMPLETE

**Goal:** Read-only cohort analysis report that answers: "Which cohorts produce fast TP1
and avoid expired-without-TP1?" Eight cohort dimensions provide a comprehensive breakdown
of the dry-run signal population.

This is a research report only. No runtime changes, no gate enablement, no schema changes.

**What was built (11E):**
- `scripts/analyze_exit_timing_cohorts.py` — read-only cohort analysis report
- Eight cohort sections:
  - **A  Overall baseline** — all closed confirmed rows
  - **B  Direction** — LONG / SHORT split
  - **C  Symbol** — top N symbols by count (configurable via `--top-symbols`)
  - **D  UTC hour** — grouped by `observed_at` hour (00–23)
  - **E  Session** — ASIA (00–07), LONDON (08–12), US (13–20), LATE_US (21–23)
  - **F  Candidate gate** — reads `metadata_json` from 11C feature version
  - **G  Indicator buckets** — RSI, ATR%, EMA spread%, price vs VWAP%
  - **H  Time-to-TP1** — ≤5m, 5–10m, 10–15m, >15m, NO_TP1
- Section I: best/worst ranking across all dimensions using named score formula
- `_cohort_metrics()` — pure function returning all key metrics per cohort
- Per-cohort metrics: TP1%, HIT_2R%, STOPPED%, EXPIRED%, exp-after-TP1%,
  exp-without-TP1%, stopped-no-TP1%, Policy B AvgR, Policy E AvgR, Avg MFE,
  Avg MAE, median/P25/P75 time-to-TP1, LONG/SHORT counts
- Direction-concentration warnings `[DIR-CONCENTRATED]` (≥90% one direction)
- Insufficient-N warnings `[INSUFFICIENT n=X < Y]`
- Ranking score formula (named constants): `policy_b_avg_r * 2.0 + (tp1_rate - baseline_tp1_rate) * 10.0 - exp_no_tp1_rate * 5.0`
- CLI: `--since`, `--min-n`, `--feature-version`, `--signal-type`, `--top-symbols`
- Imports pure policy functions from `simulate_exit_policies.py` (no duplication)
- Integrated into `create_apex_snapshot.py` as "Exit / Timing Cohort Analysis Report (11E)"
- `tests/test_analyze_exit_timing_cohorts.py` — 67 tests (unit + integration)

**How to run:**
```bash
PYTHONPATH=src python scripts/analyze_exit_timing_cohorts.py
PYTHONPATH=src python scripts/analyze_exit_timing_cohorts.py \
    --since 2026-05-21T19:47:48Z --feature-version 11C_v1
PYTHONPATH=src python scripts/analyze_exit_timing_cohorts.py --min-n 5 --top-symbols 10
```

**Safety constraints (all maintained):**
- Read-only: never writes to any DB table
- Does not change signal generation, alert eligibility, or scheduler behavior
- Does not change candidate gate logic or feature_version
- Does not enable alerts, trading, or Pushover
- `ALERTS_ENABLED=false`, `DRY_RUN_MODE=true` are not touched
- No schema changes

**What not to do:**
- Do not use cohort ranking to enable runtime filters without an explicit approval milestone
- Do not treat direction-concentrated cohorts as generally valid signals
- Do not promote ranking scores to live system configuration

---

## Milestone 11C.1 — Candidate Gate Report Clarity Fix  ← COMPLETE

**Goal:** Report-only. Prevent misreading of partial outcome_r coverage and
direction-concentrated cohorts in the Candidate Gate Prospective Tracking Report.

**What was fixed (11C.1):**
- **Avg outcome R with n**: Each cohort now shows `Avg outcome R: X R (n=Y / N)` and
  `Outcome R cov: Y / N rows; Z excluded because outcome_r is null`.
  Expired rows typically have `outcome_r = NULL` and are excluded from the average —
  this is now explicit rather than silent.
- **PARTIAL COVERAGE warning**: When outcome_r coverage < 80%, a per-cohort inline
  warning tells the reader to compare TP1/STOP/EXPIRED and MFE/MAE alongside Avg outcome R.
- **Direction-concentration warnings**: If a cohort is >= 90% LONG or >= 90% SHORT, an
  inline `[DIR-CONCENTRATED]` flag appears in the cohort section and a named warning in
  the interpretation explains that results reflect a direction/regime effect, not a
  generally valid gate. Gate B and Gate D (currently 100% SHORT in the prospective window)
  would be clearly flagged.
- **Summary table improvements**: New `AvgOutR` column (was `AvgR`), new `R_n` coverage
  column (shows `r_n/N`), new `L/S` direction column. `[DC]` flag for concentrated cohorts.
- **Expired-after-TP1 explanation**: Interpretation section now explicitly explains that
  "expired after TP1" is a partial success (entry correct, exit timing issue) and
  "expired without TP1" is a full miss — they should not be collapsed.
- **Label rename**: "Avg R" → "Avg outcome R" throughout cohort sections.
- 17 new tests (63 total in test_candidate_gates.py, 319 total in suite)

**Safety**: Report-only. No schema changes. No runtime changes. No alerts/trading/env
changes. No candidate gate logic changes. No feature_version changes. No DB writes.

---

## Milestone 11C — Dry-Run Candidate Runtime Gate Simulation  ← COMPLETE

**Goal:** Prospectively tag new signal_features rows with candidate gate metadata so future
observations can be compared across gate cohorts. Answer: "If Gate A/B/C/D had been active,
which signals would have passed, and did those signals perform better over time?"

This is prospective dry-run tagging only. Gates are evaluated and stored as metadata.
They do NOT suppress signals, block alerts, or change any runtime behavior.

**What was built (11C):**
- `src/apex/strategy/candidate_gates.py` — pure gate evaluation module
  - `evaluate_candidate_gates(atr_pct, ema_spread_pct, rsi_val) -> dict`
  - Returns `candidate_gate_version`, `candidate_gate_passed`, and per-gate
    `{passed: bool, reason: str}` for all four gates
  - Missing/None features fail safely with descriptive reason strings
  - `GATE_VERSION = "11C_v1"`, `GATE_DEFINITIONS` dict for display
- `src/apex/db/models.py` — `feature_version` default changed from `"10B_v1"` to `"11C_v1"`
  so new captures are distinguishable from pre-11C rows. No schema change required
  (metadata_json and feature_version columns already existed).
- `src/apex/scheduler/tasks.py` — `_capture_signal_features()` calls
  `evaluate_candidate_gates()` and stores JSON result in `metadata_json`.
  Gate evaluation failure is isolated — a crash never prevents the feature row insert.
- `scripts/report_candidate_gates.py` — read-only prospective gate tracking report
  - Reads only `feature_version = '11C_v1'` rows (prospective only — no historical backfill)
  - 5 cohorts: BASELINE_ALL_WITH_GATE_VERSION, GATE_A, GATE_B, GATE_C, GATE_D
  - Full outcome stats per cohort (TP1, HIT_2R, STOPPED, EXPIRED, MFE/MAE/R, LONG/SHORT)
  - Summary comparison table (TP1Δ, stopΔ, avgR vs baseline)
  - Interpretation section with INSUFFICIENT SAMPLE warnings
- `scripts/create_apex_snapshot.py` — "Candidate Gate Report (11C)" section added
  after Out-of-Sample Filter Validation Report
- `tests/test_candidate_gates.py` — 46 new tests

**Gate definitions:**
```
Gate A  ATR_0_10_TO_1_00              0.10 <= atr_pct <= 1.00
Gate B  EMA_SPREAD_NEG_0_25_TO_0      -0.25 <= ema_spread_pct < 0
Gate C  RSI_50_TO_60                  50 <= rsi_val <= 60
Gate D  ATR_AND_EMA_COMBINED          Gate A AND Gate B
```

**How to run:**
```bash
PYTHONPATH=src python scripts/report_candidate_gates.py
PYTHONPATH=src python scripts/report_candidate_gates.py --since 2026-06-01T00:00:00Z
PYTHONPATH=src python scripts/report_candidate_gates.py --min-n 20
```

**Safety constraints (all maintained):**
- Read-only: report never writes to any DB table
- Does not change runtime signal generation, alert eligibility, or scheduler behavior
- `ALERTS_ENABLED=false`, `DRY_RUN_MODE=true` are not touched
- Gate evaluation failure is swallowed — never interrupts observation recording
- Existing observations (feature_version='10B_v1') are not modified
- No exchange keys, no Pushover, no port exposure changes

**When results become meaningful:**
- The report prints "No 11C candidate gate observations found yet" until new observations
  accumulate with `feature_version='11C_v1'`
- At least 20 closed rows per cohort are recommended before drawing conclusions
- A consistent improvement over 50+ rows per cohort is required before any gate is
  promoted to a runtime filter (requires a dedicated approval milestone)

**What this milestone does NOT do:**
- Does not activate any filter in production scanning
- Does not suppress or hide any signals
- Does not store gate results in a separate table (stored in metadata_json only)
- Does not promote gates to runtime filters automatically
- Does not change ALERTS_ENABLED, DRY_RUN_MODE, or any config value

**Next step (11D or 12A):** After sufficient prospective data accumulates (50+ rows per
gate cohort), a separate approval milestone can evaluate whether any gate warrants
promotion to a runtime filter. That requires explicit review and approval.

---

## Milestone 10D — Setup Scoring Agent

**Goal:** Combine available agent outputs (quality scores, plus placeholders for regime/MTF/SR) into a single composite score per open observation.

**Implementation notes:**
- Add a `setup_scores` table (see AGENT_ARCHITECTURE.md for full schema)
- Implement `run_setup_scoring_agent()` — runs after observation evaluation pass
- Score all OBSERVED CONFIRMED observations
- Weights are constants in config; default weights from AGENT_ARCHITECTURE.md
- Missing inputs (e.g., regime not yet implemented) use a neutral adjustment of 0

**Safety constraints:**
- Scores are informational only — no alert gating yet
- Read-only with respect to all input tables

**Success criteria:**
- Every open CONFIRMED observation has a `setup_scores` row within 2 evaluation passes
- Score updates on every evaluation pass
- Missing upstream inputs produce neutral (0) adjustments, not errors

**Tests required:**
- Composite score computed correctly with all inputs present
- Composite score computed with only quality_scores available (others NULL → 0 adjustment)
- Score bounded [0, 100]

**What not to do:**
- Do not gate signals on score threshold yet
- Do not hardcode symbol-specific weights

---

## Milestone 10E — Multi-Timeframe Confirmation Agent

**Goal:** Check 15m and 1h trend alignment for each signal. Add MTF alignment as an input to the Setup Scoring Agent.

**Implementation notes:**
- Add a `mtf_confirmations` table
- Compute 1h trend bias (EMA fast/slow) at observation time
- Alignment score: both aligned = +1.0, mixed = 0.0, opposed = -1.0
- Backfill not required — new observations only

**Safety constraints:**
- Must not require new API calls — use candle data already in the candle store
- If 1h candles are not in the store, alignment_score = 0.0 (neutral), not an error

**Success criteria:**
- MTF alignment computed for all new CONFIRMED observations
- 1h candle data is already being fetched by the WebSocket subscriptions if 1h is added to the timeframes list (may require adding 1h to backfill list)

**Tests required:**
- Alignment score = +1.0 when 15m and 1h both agree
- Alignment score = 0.0 when 1h data is missing
- MTF table not written in live mode

**What not to do:**
- Do not add 4h timeframe yet — complexity/data cost is not yet justified
- Do not add MTF as a hard gate on signal generation

---

## Milestone 10F — Support/Resistance Agent

**Goal:** Identify recent pivot highs/lows and compute how far the entry price is from the nearest adverse S/R level. Add SR score as an input to Setup Scoring.

**Implementation notes:**
- Compute pivots from 200-candle lookback on setup_timeframe
- Adverse level for LONG = nearest resistance above entry; for SHORT = nearest support below entry
- Distance in R units = distance_to_level / stop_distance
- Score = min(1.0, distance_r / 2.0) — higher = more room before obstruction

**Safety constraints:**
- Must not modify stop prices or targets
- Must not use levels to block signal generation

**Success criteria:**
- SR score computed for all new CONFIRMED observations with sufficient candle history
- Score of 0 (worst) when entry is directly at a resistance level
- Score of 1 (best) when nearest adverse level is ≥ 2R away

**Tests required:**
- Score = 0 when entry touches resistance
- Score = 1 when nearest adverse level is ≥ 2R away
- Graceful handling of insufficient candle history

**What not to do:**
- Do not implement volume-at-price or market profile yet
- Do not persist levels to DB — compute fresh each run

---

## Milestone 10G — Market Regime Agent

**Goal:** Classify the current market regime using BTC as proxy. Add regime adjustment to Setup Scoring.

**Implementation notes:**
- Regimes: TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOL
- Classification based on BTC 15m EMA alignment + ATR percentile
- Regime score: TRENDING_UP LONG = +1.0, TRENDING_DOWN SHORT = +1.0, opposed = -0.5, RANGING = 0.0, HIGH_VOL = -0.3
- Add `regime_snapshots` table

**Safety constraints:**
- Must not suppress signals in live mode
- BTC is a scan-enabled symbol already; use existing candle data

**Success criteria:**
- Regime classified every 15 minutes
- Regime score is applied as an adjustment to composite score in 10D

**Tests required:**
- TRENDING_UP regime when BTC EMA fast > slow and ATR is not elevated
- HIGH_VOL regime when ATR percentile > 80th

**What not to do:**
- Do not use on-chain data or funding rates yet
- Do not use regime to hard-filter signals

---

## Milestone 10H — Relative Strength Agent

**Goal:** Score each symbol's recent price performance relative to BTC. Symbols outperforming BTC in the signal direction score higher.

**Implementation notes:**
- Use 24h simple return: (close_now - close_24h_ago) / close_24h_ago
- RS score = symbol_return - btc_return, clamped to [-1, +1]
- Positive RS score for LONG = favorable; negative RS for SHORT = favorable
- Add `rel_strength_snapshots` table; update every 15 minutes

**Safety constraints:**
- BTC candles must already be in the candle store (guaranteed if BTC is priority symbol)

**Success criteria:**
- RS score computed for all scan-enabled symbols every 15 minutes

**Tests required:**
- RS score = +1 when symbol outperforms BTC by > 1 std dev
- RS score = 0 when symbol performance matches BTC

**What not to do:**
- Do not compute intra-candle RS; use close-to-close
- Do not add sector/basket comparisons yet

---

## Milestone 11A — Paper Execution Agent

**Goal:** Track virtual positions for open observations — entry, stop management, and exit — using candle data. Produces realistic simulated P&L in R units.

**Implementation notes:**
- Add `paper_positions` table
- For each open CONFIRMED observation, open a virtual position at entry_price
- Track: break-even stop move (after TP1), trailing stop (optional, configurable)
- Close virtual position when observation closes
- P&L tracked in R units

**Safety constraints:**
- Must not connect to exchange
- Must not read wallet/account state
- Runs in dry_run_mode only

**Success criteria:**
- Virtual P&L matches expected R outcome for known test cases
- Break-even stop logic correctly applied after TP1 milestone

**Tests required:**
- Virtual position opens when observation is OBSERVED
- Break-even stop applied after TP1
- Position closes with correct P&L when observation closes

**What not to do:**
- Do not simulate slippage or funding costs yet — simple R-unit tracking only
- Do not write real order records

---

## Milestone 11B — Trade Review Agent

**Goal:** Auto-generate a structured post-trade review for every closed CONFIRMED observation. Compare predicted score vs actual outcome.

**Implementation notes:**
- Add `trade_reviews` table
- Review triggered by observation close (EXPIRED, STOPPED, HIT_2R)
- Compare `setup_scores.composite_score` (if available) to actual `outcome_r`
- Generate structured notes JSON: entry quality, exit quality, what the setup scored vs what happened

**Safety constraints:**
- Read-only with respect to all tables except `trade_reviews`
- Must not re-open closed observations

**Success criteria:**
- Every newly closed CONFIRMED observation gets a review within one evaluation pass
- Reviews include setup score comparison when 10D data is available

**Tests required:**
- Review created for newly closed observation
- Review gracefully handles missing setup_scores (pre-10D observations)

**What not to do:**
- Do not use LLM for review generation yet
- Do not email/push reviews

---

## Milestone 12A — Risk / Position Sizing Agent

**Goal:** Given a high-scoring observation and current drawdown state, suggest an optimal position size. Output is a recommendation only — no execution.

**Implementation notes:**
- Add `sizing_recommendations` table
- Base risk = `settings.risk_per_trade_pct`
- Drawdown adjustment: reduce size by 25% per 1% drawdown beyond 1%
- Score adjustment: scale size by composite_score / 100 (e.g., score 80 → 80% of base risk)
- Optional Kelly fraction using historical tp1_rate and win_size/loss_size from quality scores

**Safety constraints:**
- Must not modify `daily_risk`
- Must not place orders
- Sizing is a suggestion only

**Success criteria:**
- Sizing recommendation produced for all open CONFIRMED observations with composite scores
- Size is reduced correctly at elevated drawdown levels

**Tests required:**
- Size reduced at 2% drawdown
- Size increased (up to 1.5x max) at high composite score
- Size never exceeds 2x base risk regardless of score

**What not to do:**
- Do not implement Kelly sizing by default — opt-in only
- Do not expose sizing via public API endpoint

---

## Milestone 13A — Approval-Required Alert Flow

**Goal:** When a human explicitly enables alerts, high-scoring observations are promoted to real alerts with human approval in the loop.

**Implementation notes:**
- Requires ALERTS_ENABLED=true set explicitly by user
- Minimum composite score threshold (configurable, default 75)
- Alert is staged for approval — written to `staged_alerts` table first
- Human reviews via a new API endpoint or CLI command
- Only after explicit approval does the alert go to Pushover

**Safety constraints:**
- Must not activate while DRY_RUN_MODE=true
- Must not auto-approve — human confirmation always required
- ALERTS_ENABLED gate must not be bypassed

**Success criteria:**
- No alerts fire without human approval
- Staged alerts expire if not approved within a configurable window
- Approval action is logged to `app_events`

**Tests required:**
- Alert staged but not sent without approval
- Alert sent after approval
- Alert expired when approval window lapses

**What not to do:**
- Do not auto-approve based on score alone
- Do not add webhook-based auto-approval

---

## Milestone 14+ — Live Execution

**Goal:** Enable real order placement on Hyperliquid after all prior milestones have been reviewed, validated, and explicitly approved.

**Prerequisites (all must be met before implementation):**
- Milestones 10A–13A complete and reviewed
- Signal quality scores show positive expected value (tp2_rate > stop_rate over N>=100 observations)
- Paper execution agent P&L is positive over N>=50 paper positions
- User has explicitly approved moving to live mode in writing
- LIVE_TRADING_ENABLED=true set manually in production .env by user

**Implementation notes:**
- Adds a `HyperliquidExecutor` class — isolated in its own module
- Uses Hyperliquid API private key (never stored in DB, read from env only)
- Order sizes from `sizing_recommendations`
- Full audit trail: every order attempt logged to `order_log` table

**Safety constraints:**
- Private key must never be logged, written to DB, or transmitted outside the exchange API
- Position size hard cap: 2x `max_risk_usd` regardless of signals
- Automatic kill switch: if daily loss > `max_daily_loss_usd`, halt all trading immediately
- No leverage changes without explicit config update

**Success criteria:**
- Live orders placed only after human approval chain from 13A
- Every order has a corresponding `order_log` entry
- Kill switch tested in staging before production use

**What not to do:**
- Do not implement until all prior milestones are reviewed
- Do not add market orders without slippage consideration
- Do not auto-leverage

---

## Milestone ordering summary

```
10A   TP1 non-terminal milestone tracking                   ← COMPLETE
10B   Signal feature snapshot                               ← COMPLETE
10C   Signal quality agent / learning report v2             ← COMPLETE
10C.1 Fix diagnostic scoring, labels, git meta              ← COMPLETE
11A   Strategy filter simulator (report-only)               ← COMPLETE
11A.1 Usability fix + stricter verdicts                    ← COMPLETE
10D   Setup scoring agent
10E   Multi-TF confirmation
10F   Support/resistance agent
10G   Market regime agent
10H   Relative strength agent
11B   Candidate runtime gate design (explicit approval req)
12A   Paper execution agent
12B   Trade review agent
13A   Risk/position sizing agent
14A   Human-approved alert flow
15+   Live execution (explicit approval only)
```

**Milestone intent summary:**
- 10A–10C.1: Learning/reporting foundation — read-only, no live side effects
- 11A: Report-only strategy filter simulation — retrospective, no runtime changes
- 11B: Candidate runtime gate — design and review only; requires explicit approval before implementation
- 10D–10H: Scoring agents — informational scoring, no alert gating yet
- 12A: Paper execution agent — virtual P&L simulation only
- 12B: Trade review agent — post-trade review records
- 13A: Risk/position sizing — recommendations only, no execution
- 14A: Human-approved alert flow — alerts require human approval; ALERTS_ENABLED=true set manually
- 15+: Live execution — only after all prior milestones reviewed and explicitly approved
