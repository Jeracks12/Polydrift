# Engineering Loop — make poly_drift.py tradeable
Smart-Loop routed (see memory/SMART_LOOPS.md). Planner = frontier, one pass (this doc IS the plan artifact).
Driver = mid-tier model. Evaluator = near-free model reading gate output only. Valve = frontier, 1 turn per stall.

---
## 0. Script audit (why each step below exists)

### Math defects
- **M1 — Contaminated z baseline.** `z = (res[-1]−mean(res))/std(res)` computes mean/std over a window that
  includes the dislocation being tested; big events inflate their own std and self-suppress. Logit-space poly
  returns are also fat-tailed → Gaussian z mis-calibrated.
- **M2 — Lag-selection multiplicity.** `xcorr_best` maxes |corr| over 61 lags (31 in 5m preset) on ~90 samples.
  Best-of-61 |corr| ≥ 0.15 occurs by chance far too often; MIN_CORR=0.15 ≈ 1.4 SE at n=90. Uncorrected.
- **M3 — Beta on overlapping H-sums.** Rolling-H cumulative returns are autocorrelated (effective n ≈ w/H ≈ 18
  in 5m mode). No shrinkage, no sign clamp → beta noise passes straight into fair value.
- **M4 — No time-to-expiry structure.** For a 5m up/down binary, true beta = gamma of the digital and explodes
  as τ→0 while price pins to 0/1; logit returns blow up near expiry → spurious z. Script is τ-blind.
- **M5 — Fair value is regression-only.** `fair = expit(logit(mid) − dev)` trusts a noisy beta. No independent
  model check: P(up) = Φ(ln(S/K)/σ√τ), K = cycle open, σ = EWMA realized vol.
- **M6 — Outcome metric inflated.** Mark-to-mid at +30s: no spread on exit, no fee, no depth/queue, and
  mid-reversion toward "fair" is partially favored by construction. 79% / +15.2c is an upper bound, not P&L.

### Logic defects
- **L1 —** Composite is equal-weight de-meaned logs; a thin venue votes equally with Binance. (Volume-capped
  weights exist in `test/Triangular RankZ` `calcWeights` — port it.)
- **L2 —** vol_z gate uses raw std of H-sum $vol (heavy-tailed) — same Gaussian problem as M1.
- **L3 —** No chop filter: a large but inefficient (back-and-forth) ref move passes the vol gate.
  (`test/ER.mq5` Kaufman efficiency ratio = |Σr|/Σ|r| is the exact tool.)
- **L4 —** No entry cutoff near cycle end; no depth check that quoted BBO size covers intended size;
  no sizing, no risk caps, no order layer → not tradeable yet.
- **L5 —** Data only dumped on exit (`--dump`); no continuous recording → nothing to replay, nothing to gate on.

### Test-folder verdict: USE IT
- `Triangular RankZ Outlier Engine.pinescript` → port `rankZ` (probit of empirical rank — robust z), `calcWeights`
  (volume-capped composite weights), persistence gate (EMA of sign), cooldown/explosive-bar guard concepts.
- `ER.mq5` → port signed Kaufman ER as the chop filter (L3).

---
## 1. Goal conditions (planner-written; hand ONE phase to the driver at a time)

```
/goal P0: drift_math.py holds all pure functions (no I/O, no globals); poly_drift.py imports it and
  still runs; recorder writes ticks continuously (jsonl.gz, 1 file/session); replay.py re-emits identical
  signals from a recording (determinism assert); python scripts/gate.py exits 0 and prints "RECEIPT ...".
  Do not modify: FINDINGS.md, signals*.csv, test/, memory/, fixtures/. Stop after 12 turns.

/goal P1: rankZ replaces Gaussian z for drift + vol gates; beta = shrunk(λ=0.5 toward model gamma, sign-clamped
  by CORR_SIGN); lag search restricted to {0..5}s with Fisher-z sig test at α=0.01 (multiplicity-corrected);
  binary pricer fair value blended with regression fair (inverse-variance); ER gate ≥ 0.35 over DRIFT_H_S;
  no entries when τ_remaining < 40s; scripts/gate.py exits 0 AND replay hit-rate on fixtures/ ≥ recorded
  baseline in fixtures/baseline.json with avg net edge > 0 under spread+fee model. Same no-touch list.
  Stop after 15 turns.

/goal P2: outcomes scored 3 ways per signal (mid / exit-at-touch / resolution) in signals_outcomes.csv
  schema v2; exec price = walk-the-book for --size N; fee param (verify live Polymarket taker fee for 5m
  crypto markets before hardcoding — put it in CFG); latency ts logged (ref_move_ts, signal_ts, book_ts);
  gate.py exits 0 and net-of-cost replay edge > 0. Stop after 12 turns.

/goal P3: paper-trade mode (--paper) simulates fills vs live book; sizing = 0.25-Kelly from calibrated
  edge, hard caps (max position, max daily loss, kill switch, cancel-on-roll); order module isolated in
  execution.py using py-clob-client, OFF by default, never armed by the driver; gate.py exits 0.
  Stop after 12 turns. LIVE ARMING IS A HUMAN DECISION, never a loop step.
```

## 2. Plan steps per phase (driver follows in order; one verification command per step)

**P0 — harness first (nothing else is verifiable without it)**
1. Extract pure math → `drift_math.py`: grid_series, grid_vol, composite_px, logit/expit, rets, xcorr,
   beta, residual/z, fair value. Verify: `python -c "import drift_math"` + poly_drift still imports.
2. `recorder.py`: append every tick (venue, ts, px, notional) + poly book events to `data/session_*.jsonl.gz`.
   Verify: file grows during 60s live run.
3. `replay.py`: stream a recording through Analytics on a virtual clock. Verify: two replays of the same
   file produce byte-identical signals.csv (determinism = the executable gate's foundation).
4. `tests/test_math.py`: synthetic series with planted lead=3s, beta=0.6 → estimators recover within
   tolerance; no-lookahead assert (signal at t uses data ≤ t only). Verify: `pytest -q`.
5. `scripts/gate.py`: runs pytest + replay-determinism + prints one line:
   `RECEIPT verified|failed checks=X/Y replay_sigs=N hit=H% netEdge=Ec`. Exit 0/1. This line is the ONLY
   thing the evaluator reads.
6. Record ≥ 2 live 5m sessions into `fixtures/` and freeze `fixtures/baseline.json` (current script's
   replay metrics). The ratchet measures against this.

**P1 — math (fixes M1–M5, L1–L3; port from test/)**
7. `rankZ(x, win)`: probit(empirical rank) — numpy port of the Pine function. Replace drift z + vol z.
8. Restrict lag scan to {0..5}; significance via Fisher z with Bonferroni over 6 lags.
9. Beta: OLS on non-overlapping H-blocks, shrink 50% toward model gamma (step 10's ∂P/∂lnS), clamp sign.
10. `binary_fair(S, K, sigma, tau)` = Φ(ln(S/K)/(σ√τ)); σ = EWMA realized vol of composite; K = cycle open
    captured at roll. Blend with regression fair by inverse variance. Track K per market from roll event.
11. ER gate: `abs(sum(r)) / sum(abs(r))` over DRIFT_H_S ≥ 0.35. Persistence gate optional (EMA of sign).
12. Volume-capped composite weights (port calcWeights, cap 50%) replacing equal-weight de-mean.
13. τ gates: no entry τ<40s; require |z| scaled ↑ as τ↓ (dislocations must be larger to matter late).

**P2 — execution realism (fixes M6, L4 half)**
14. Outcome v2: mid / exit-at-touch (cross spread at eval time) / resolution (cycle end up-down).
15. Book-walk exec price for size N; reject signal if depth at BBO < N.
16. Fee model in CFG (verify current fee on-chain/docs first — do not assume zero).
17. Latency instrumentation columns.

**P3 — trading shell (L4 rest)**
18. `--paper` fill simulator; risk module (caps, kill switch, cancel-on-roll); `execution.py` isolated,
    disarmed; calibration curve (predicted edge vs realized) feeding 0.25-Kelly.

## 3. Loop mechanics (token discipline)
- **Probe first:** run `scripts/gate.py` before any edit — if a step's check already passes, skip the step.
- **Driver context:** this file + the named files of the current step ONLY. Never re-read FINDINGS.md,
  fixtures, or transcripts. Stable prefix (this doc) leads every turn → cache hits.
- **Evaluator contract:** input = RECEIPT line + failing-test tail (≤2K tokens). Output = `YES` or
  `NO: <reason>`. Nothing else.
- **Ratchet:** accept a turn only if it fixes ≥1 gate check and regresses 0 (compare RECEIPT check counts).
- **Stall rule:** 3 same-reason NOs → ONE frontier valve turn (prompt: "Read the RECEIPT + failing check.
  Do not do the work. Name what the driver keeps missing and rewrite the next step for a mid-tier model.").
  Same stall trips valve twice → stop loop, re-plan.
- **Budgets:** turn budgets per condition above; hard stop. Log per loop: turns, tokens in/out, NO-rate,
  valve fires, $/accepted change → `loop_log.csv`.

## 4. Definition of tradeable (exit criterion for the whole program)
Replayed, net-of-spread-and-fee, depth-aware edge > 0 with ≥ ~100 signals across ≥ 3 recorded days;
calibration slope ≈ 1 between predicted and realized edge; paper-trade week matches replay within noise.
Until then it is a research monitor, not a strategy.
