# Audit Loop — poly_drift.py (math · logic · reasoning · chart · stats)
Audit-only: produces a findings ledger, changes NO production code. Fix work happens later via
ENGINEERING_LOOP.md. Smart-Loop routed (memory/SMART_LOOPS.md): this doc = plan artifact + finish line.

## Seats
Planner: frontier, done (this doc). Driver: mid-tier. Evaluator: near-free, reads gate RECEIPT only.
Valve: frontier, 1 diagnostic turn per stall (3 same-reason NOs), max 2 fires then re-plan.

## Goal condition (freeze at loop start)
```
/goal docs/audit.md contains one row per cell of the coverage matrix below, each marked PASS or
  FINDING; every FINDING has: id, severity(high|med|low), file:line, claim, evidence (a runnable
  python snippet in audit_repro/ whose output demonstrates the defect), and impact-on-P&L note;
  python scripts/audit_gate.py exits 0 and prints "RECEIPT audit checks=X/Y findings=N high=H";
  no file outside docs/, audit_repro/, scripts/audit_gate.py is created or modified;
  no finding is deleted or downgraded once written; stop after 14 turns.
```

## Coverage matrix (the audit's checklist — every cell gets a verdict)

### A. Math
| id | Cell | What to test (repro = synthetic data through drift_math functions) |
|---|---|---|
| A1 | Residual z-score | Baseline contamination: plant one dislocation; show window mean/std absorbs it and z self-suppresses. Fat tails: compare Gaussian z vs rank-probit z false-positive rate on logit-return noise. |
| A2 | xcorr lag search | Multiplicity: feed pure noise pairs; measure how often best-of-61-lag \|corr\| ≥ MIN_CORR(0.15) at n≈90. Report empirical false-positive %. |
| A3 | Beta estimator | Overlapping H-sums: effective sample size; beta variance on synthetic beta=0.6 data; sign-flip frequency. Verify the lag-shift alignment in compute() (indexing of `a`/`b` slices) is actually time-aligned — off-by-one check with planted lag. |
| A4 | composite_px | De-meaned-log averaging: venue join/drop mid-window — quantify level shift; NaN propagation; equal-weight vs volume-weight distortion when a thin venue diverges. |
| A5 | Fair value | `expit(logit(mid) − dev)`: behavior at mid near 0/1 (clip at 1e-4 — does dev subtraction still map sanely?); no τ term — show fair error vs a Φ(ln(S/K)/σ√τ) reference near expiry. |
| A6 | logit/rets | `prepend=x[0]` first return = 0 — window-edge effects on rolling sums; roll-boundary zeroing (±3s) — does it cover the actual jump indices? |
| A7 | vol z-gate | H-sum $vol distribution: tail heaviness; Gaussian z calibration; zeros-dominated windows. |

### B. Logic / reasoning
| id | Cell | What to test (repro = trace or minimal simulation) |
|---|---|---|
| B1 | Signal gate chain | Enumerate the 7 gates; for each, construct a case that passes all others and fails it — is any gate unreachable or redundant? Is `lag ≥ 0` sufficient to rule out poly-leads (lag=0 ambiguity)? |
| B2 | Ref selection | max \|corr\| picks the ref — noise winner-take-all across 2 composites; stability of chosen ref across consecutive seconds. |
| B3 | Roll handling | Token swap race: book cleared but stale quotes deque persists across roll — does _poly_mid_at read pre-roll mids for post-roll evals? 5s post-roll mute sufficiency. |
| B4 | Cooldown/dedup | Per-side cooldown only — same dislocation can fire BUY then SELL alternating? Repeated fires on one persistent dislocation. |
| B5 | Feed staleness | STALE_S=30 on refs, none on poly — stale poly mid can generate signals; bin-perp quote-mid mixing trades+quotes in one series (different noise processes) feeds xcorr. |
| B6 | Async/threading | Analytics reads FEEDS deques while ws threads append — snapshot consistency; time.time() vs exchange ts skew across venues (offset alignment claim). |
| B7 | τ awareness | No time-remaining logic anywhere: list every decision that should be τ-conditioned (entry, z-threshold, eval horizon) and isn't. |

### C. Chart display
| id | Cell | What to test |
|---|---|---|
| C1 | minmax norm | Window-relative normalization: same price path renders differently as window slides; visual co-movement is an artifact — demonstrate two uncorrelated series that LOOK correlated after norm. |
| C2 | Signal markers | Markers pinned to normalized poly at searchsorted index — off-by-one vs actual signal ts; markers silently vanish when signal scrolls off window (record loss on screen). |
| C3 | Drift-z panel | `deque(maxlen=WINDOW_S)` appended per FRAME (1s animation tick) not per grid second — pauses/frame drops desync x-axis; two feeds' z histories can have different lengths. |
| C4 | Missing context | Chart shows no: bid/ask band, fair value line, τ-remaining, eval markers (outcomes) — auditor lists what a trading operator needs that is absent. |
| C5 | update() state | Attributes stored on function object (`update._z_*`) — reset behavior on reroll/reconnect; thread-safety of reading AN.snap mid-compute. |

### D. Stats / outcome methodology
| id | Cell | What to test (repro = signals.csv + signals_outcomes.csv analysis) |
|---|---|---|
| D1 | Mark-to-mid bias | Quantify: re-mark logged signals at exit-at-touch (cross spread) and at resolution; report edge deltas vs the +15.2c headline. |
| D2 | Sample validity | n=14, 7 cycles: cluster correlation (signals within a cycle share outcome); binomial CI on 11/14; is 79% distinguishable from 50%? |
| D3 | Selection effects | Eval-at-roll truncation (eval_ts pulled to roll−1s) — direction of bias; missing-mid skips (`mid is None: continue`) — survivorship in the tally. |
| D4 | Construction leak | Signal fires when fair beyond touch; eval marks vs mid 30s later — how much of measured "edge" is mechanical spread-capture vs real drift closure? Simulate null (random-time entries same book states). |
| D5 | Fee/size realism | Outcomes assume 1-unit fill at touch, zero fee — recompute break-even hit-rate under {0,1,2}c fee and half-BBO-depth fills. |
| D6 | Logging integrity | signals.csv float formatting (raw str() of floats), ts precision, schema drift risk between the two CSVs; no session id / config hash logged with signals (irreproducible params). |

## Gate — scripts/audit_gate.py (driver writes this in turn 1, spec is frozen)
Checks, in order; exit 0 only if all pass:
1. docs/audit.md parses; exactly one row per matrix id above; verdict ∈ {PASS, FINDING}.
2. Every FINDING row has all 6 required fields; severity vocabulary valid.
3. Every referenced audit_repro/*.py runs (`python <f>`) with exit 0 and prints `DEMONSTRATED <id>`.
4. Anti-gaming: git diff (or file mtime/hash list) shows no edits outside docs/, audit_repro/, the gate itself.
5. Monotonicity: findings count and severities never decrease vs previous ledger snapshot (stored hash).
Prints: `RECEIPT audit checks=X/Y findings=N high=H` — the ONLY evaluator input.

## Loop mechanics (token discipline)
- Turn 1 = gate + empty ledger skeleton (all 25 cells listed, verdict TODO). Cheap structural turn.
- One matrix SECTION (A–D) per turn maximum; driver context = this doc + poly_drift.py + the section's
  target lines only. Never load fixtures, FINDINGS.md, or prior transcripts.
- Repro snippets: synthetic/self-contained, <60 lines, numpy-only, no network. A finding without a
  running repro is an opinion — evaluator NOs it.
- Evaluator contract: RECEIPT line + gate stderr tail (≤1K tokens) → `YES` or `NO: <reason>`.
- Ratchet: a turn is accepted only if cells-resolved increases and no prior verdict/severity weakened.
- Stall rule: 3 same-reason NOs → 1 frontier valve turn ("Read RECEIPT + failing check. Don't audit.
  Name what the driver misses; rewrite the next cell's test so a mid-tier model can execute it.").
- After gate passes: ONE frontier adversarial pass (fixed cost) — challenges every PASS verdict,
  may add findings, may not remove any. Its output is final docs/audit.md.

## Exit artifact
docs/audit.md — the ranked ledger (high→low, P&L impact noted) that seeds the next ENGINEERING_LOOP.md
fix phases. Cost log row → loop_log.csv: turns, tokens, NO-rate, valve fires, $/finding.
