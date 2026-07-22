# Audit Ledger — poly_drift.py (math · logic · reasoning · chart · stats)

Audited 2026-07-19. Audit-only: no production code changed (baseline hashes frozen in
`docs/.audit_baseline.json`). Every FINDING carries a runnable repro in `audit_repro/`
that prints `DEMONSTRATED <id>`. Severity: impact on P&L if traded live at size.

## Summary (ranked detail below)

| id | verdict | severity | file:line | repro | impact (P&L) |
|----|---------|----------|-----------|-------|--------------|
| A1 | FINDING | high | drift_math.py:95 | audit_repro/a1_zscore.py | biggest dislocations self-suppress ~32%: largest edges under-detected |
| A2 | FINDING | high | drift_math.py:59 | audit_repro/a2_xcorr_multiplicity.py | noise clears MIN_CORR most of the time: ref trust is largely unearned |
| A3 | FINDING | med | drift_math.py:76 | audit_repro/a3_beta_variance.py | beta sd ~0.3 at eff n~18: fair value wanders +-50% trade to trade |
| A4 | FINDING | med | drift_math.py:36 | audit_repro/a4_composite.py | venue drop in trend = fake bp-scale return; thin venue votes equally |
| A5 | FINDING | high | drift_math.py:103 | audit_repro/a5_fair_no_tau.py | tau-blind fair wrong by >=10c near expiry: worst trades look best |
| A6 | FINDING | low | poly_drift.py:312 | audit_repro/a6_roll_mask.py | ws-lag >3s puts cross-market jump outside mask: fake z spike |
| A7 | FINDING | med | poly_drift.py:327 | audit_repro/a7_volz.py | dead-market $500 print passes the volume gate at z~20 |
| B1 | FINDING | med | poly_drift.py:343 | audit_repro/b1_lag_zero.py | sub-second poly leadership invisible at 1s grid: lag=0 admits it |
| B2 | FINDING | low | poly_drift.py:343 | audit_repro/b2_ref_flapping.py | ref choice flaps between windows: beta/lag/fair swap mid-decision |
| B3 | FINDING | med | poly_drift.py:295 | audit_repro/b3_roll_stats.py | stats mix old+new market ~90s/cycle: beta attenuated ~50% post-roll |
| B4 | FINDING | low | poly_drift.py:360 | audit_repro/b4_cooldown.py | one oscillating episode fires 8+ correlated signals into the tally |
| B5 | FINDING | high | poly_drift.py:298 | audit_repro/b5_poly_staleness.py | dead poly ws + moving ref = phantom z>=2 vs frozen book |
| B6 | FINDING | med | poly_drift.py:295 | audit_repro/b6_clock_skew.py | measured lead = true lead + PC clock error: gate integrity depends on NTP |
| B7 | FINDING | high | poly_drift.py:343 | audit_repro/b7_tau_blind.py | no tau anywhere; near expiry cheap-entry gate selects near-certain losers |
| C1 | FINDING | low | poly_drift.py:479 | audit_repro/c1_minmax_illusion.py | minmax overlay fakes co-movement, erases amplitude |
| C2 | FINDING | low | poly_drift.py:498 | audit_repro/c2_markers.py | markers plot 1s late, re-normalize each frame, vanish off-window |
| C3 | FINDING | low | poly_drift.py:512 | audit_repro/c3_z_history_frames.py | z panel x-axis counts frames not seconds; curves misaligned |
| C4 | FINDING | low | poly_drift.py:466 | audit_repro/c4_missing_context.py | no spread band, fair line, tau clock, outcomes, PnL, feed health |
| C5 | FINDING | low | poly_drift.py:512 | audit_repro/c5_update_state.py | z history never resets: different markets spliced into one curve |
| D1 | FINDING | high | poly_drift.py:390 | audit_repro/d1_mark_to_mid.py | headline avg is mark-to-mid upper bound; exit fields not logged |
| D2 | FINDING | high | signals_outcomes.csv:1 | audit_repro/d2_sample_validity.py | n far below own 100-signal bar; cycle clustering shrinks eff n |
| D3 | FINDING | med | poly_drift.py:390 | audit_repro/d3_eval_truncation.py | roll-clamped evals are binary payouts pooled with drift marks |
| D4 | FINDING | med | poly_drift.py:339 | audit_repro/d4_construction_null.py | trigger earns on a null market: edge part-mechanical reversion |
| D5 | FINDING | med | execution.py:1 | audit_repro/d5_fee_sensitivity.py | break-even hit +2.5pts under 2c fee+slip; fee assumed not verified |
| D6 | FINDING | high | poly_drift.py:369 | audit_repro/d6_logging.py | no session/config provenance: pooled stats span different strategies |

## Findings detail (high first)

### A1 — residual z contaminates its own baseline (high)
`drift_z` computes mean/std over a window that INCLUDES the dislocation being tested.
Repro plants a 5-sigma ramp: reported z=4.67 vs uncontaminated 6.88 (32% suppression) —
the alert threshold is hardest to reach exactly when the move is biggest. Informational:
same-window studentization roughly re-calibrates fat tails for the last point (t3 4.5%
vs gaussian 4.6% at |z|>=2), so the defect is the suppression, not tail inflation.
P&L: the largest, most profitable dislocations are systematically under-scored.

### A2 — lag-search multiplicity makes MIN_CORR nearly vacuous (high)
Best-of-31-lags |corr| on pure noise pairs (n=90) clears 0.15 in the majority of trials
(repro prints the measured rate; sign+lead gates roughly halve it). The ref-trust
gate therefore passes on noise regularly; real filtering is done downstream by z/vol
gates. P&L: fair value and beta are routinely computed off spurious relationships.

### A5 — fair value has no time-to-expiry term (high)
`expit(logit(mid) − dev)` is constant in tau. Against the binary-option reference
Phi(ln(S/K)/(sigma*sqrt(tau))), holding the same book state, the error exceeds 10c
inside the final minute. P&L: near expiry the system most confidently quotes the most
wrong fair — and B7 shows entries are not blocked there.

### B5 — no staleness guard on the poly feed (high)
Refs go NaN after 30s of silence; poly forward-fills forever (`stale=None if n=='poly'`,
poly_drift.py:298) and POLY_BOOK freezes at its last state. Repro: dead poly ws + a
40bp ref move = drift z −2.4 against a market that isn't there. P&L: live mode would
send FAK orders against a phantom book on every ws drop; paper mode books fictional fills.

### B7 — tau-blindness inverts the cheap-entry gate near expiry (high)
A 0.30 share needs a ~4x smaller underlying move to win at tau=240s than at 15s — no
gate distinguishes them. Worse: near expiry every losing side is "cheap" (<0.45), so
ENTRY_MAX_PX preferentially admits near-certain losers late in the cycle. Also
tau-blind: z threshold, EVAL_H_S, beta (true gamma explodes). P&L: adverse selection
concentrates in the final seconds — the exact regime the strategy's cheap entries favor.

### D1 — outcome headline is an unreconstructable upper bound (high)
Mark-to-mid at +30s books no exit spread or fee; a 1–2c exit penalty erases much of the
logged average (repro recomputes from signals_outcomes.csv). The log lacks bb/ba-at-eval
and resolution columns, so honest exit-at-touch / resolution marking cannot be computed
retroactively. P&L: reported edge cannot be trusted at face value; must be re-measured.

### D2 — sample far below the program's own evidence bar (high)
n scored signals << 100 (the program's stated bar), spread over few cycles; signals in
one cycle share a resolution, so effective n is the cycle count. Cluster-collapsed
binomial is far weaker than the naive test. P&L: current hit-rate is anecdote-grade.

### D6 — no provenance: pooled stats span different strategies (high)
Neither CSV logs session id or config hash; this session alone changed DRIFT_Z_ALERT,
ENTRY_MAX_PX, REF_DRIVEN, CORR_SIGN between logged signals. The running "record NW/NL"
therefore aggregates different strategies with no way to separate them. Also raw float
repr (e.g. 0.28000000000000004) in cells. P&L: every quoted record is unattributable.

### A3 — beta variance (med): sd~0.31 around true 0.6 at eff n ~ w/H=18 (overlapping
H-sums), 1.8% sign flips at realistic SNR; lag-shift alignment itself verified correct
(|beta| peaks exactly at the planted lag). Fair value inherits +-50% wander.
### A4 — composite artifacts (med): a venue dropping mid-trend injects a ~24x-drift
fake return at the drop second (de-mean windows differ per venue under drift); a thin
noisy venue gets equal weight, passing its noise through to the drift stats.
### A7 — volume gate miscalibrated (med): in a dead market a single $500 print scores
vol_z ~ 20 and passes the >=1.5 gate that exists to demand real flow; burst-flow
regimes shift the gate's effective meaning.
### B1 — lag=0 ambiguity (med): a market leading by 0.5s measures lag>=0 with corr
above MIN_CORR in most windows at the 1s grid: the lead gate cannot exclude sub-second
poly leadership (mitigated only partially by the ref-driven event gate).
### B3 — cross-roll stats contamination (med): the 90s windows mix a resolved market's
pinned prices with the new market for up to 90 of every 300 seconds; beta attenuates
~50% right after each roll — a third of every cycle runs on distorted estimates.
### B6 — clock-skew leaks into lag (med): venue rows carry exchange timestamps, poly
rows carry local time.time(); measured lead = true lead + PC clock offset. A 2s NTP
error doubles the believed latency edge or hides it entirely.
### D3 — truncated evals mix estimands (med): roll-clamped marks are near-binary
payouts (repro: mean |pnl| 50c vs 27c for full-horizon marks) pooled into one average;
mid-is-None eval skips are dropped with no counter.
### D4 — construction leak (med): on a null OU market with zero information the trigger
rule still books positive mark-to-mid P&L vs random-time entries — part of the
measured edge is mechanical reversion capture, currently unseparated from real drift.
### D5 — fee/size sensitivity (med): break-even hit-rate at the 0.30 entry band moves
+2.5pts under 2c fee + half-depth slippage; the zero-fee assumption is unverified.
### A6 — roll mask misses delayed jumps (low): first new-market quote arriving >3s
after the roll puts the cross-market logit jump outside the masked indices.
### B2 — ref flapping (low): winner-take-all |corr| selection flips between
near-equal composites across consecutive windows, swapping beta/lag/fair sources.
### B4 — per-side cooldown only (low): one 60s oscillating dislocation legally fires
8+ alternating signals; the outcome tally counts each as independent evidence.
### C1—C5 — chart layer (low): minmax overlay manufactures visual co-movement and
erases amplitude; markers plot one second late, re-scale every frame, and silently
scroll away; the z panel counts frames not seconds and misaligns feeds; no spread
band / fair line / tau clock / outcome markers / PnL / feed-health on screen; z
histories live on the update function object and are never reset across markets.

## Adversarial pass (frontier, post-gate)
Every cell was probed for a PASS verdict; none survived challenge — each cell has a
demonstrated defect (weakest: C-section, display-only). Two initial audit claims were
themselves corrected by their repros during the loop and are recorded narrowed:
A1's fat-tail-inflation claim (measured: self-normalization re-calibrates it away;
finding stands on contamination only) and A3's alignment probe (residual variance was
the wrong quantity — beta(k) is; alignment is in fact CORRECT and is recorded as a
positive result inside a FINDING row). No finding was removed or downgraded after
first being written. Highest-leverage fix order for ENGINEERING_LOOP phases:
B5 (staleness guard) -> D6/D1 (provenance + exit fields in logs) -> B7/A5 (tau layer)
-> A1/A2 (rankZ + lag significance, already planned P1) -> B3 (roll-aware windows).

---

# Audit 2026-07-22 — post-fix hidden-bug sweep

Nine defects found by reading the live decision path end to end. Each has a regression test in
`tests/test_audit_regressions.py`. Suite 87 pass, `gate.py` verified 5/5 (repro 22 sigs / 94%).

| # | Where | Sev | Defect |
|---|---|---|---|
| A1 | `drift_math.realized_sigma_per_s` | **Critical** | NaN-compaction booked a dead-feed gap as a 1s return; sigma ~8x true |
| A2 | `execution.Executor.on_roll` | **Critical** | Unknown close forced `final_mid=0.5`, and `>=0.5` made every unknown a YES win |
| A3 | `replay.reset_state` | **Critical** | `ROLL['end']/strike` and the loosened CFG leaked between replays |
| A4 | `poly_drift.compute` strike capture | **High** | Mid-cycle start invented a strike; whole cycle mispriced |
| A5 | `execution.on_signal` kill switch | **High** | Only realized P&L counted; open cost could exceed the whole daily cap |
| A6 | `poly_drift._eval_pending` | **High** | `continue` dropped pending signals; hit-rate biased |
| A7 | `poly_drift._paid_effective` | **High** | `--mode off` scored top-of-book while paper/live walked the book |
| A8 | `poly_drift._manage_exits` | Medium | Early exits still tallied at resolution |
| A9 | `poly_drift.compute` snap publish | Low | `dict.update` let the plot thread see `stats` without `drift` |

## A1 — the one that matters most
Sigma is the denominator of `ln(S/K)/(sigma*sqrt(tau))`, so it sets every `P_up`. Measured on a
synthetic GBM path, a 30s hole spanning a 1% move produced `sigma = 0.000995` against a true
`0.00012` — **8.3x**. That pins `P_up` to 0.5 and silently stops the strategy exactly after the
intermittent ISP venue outages this bot is built to survive. Only adjacent finite pairs now
contribute, and a dark feed returns `None` (-> gate `model_not_ready`) instead of a fabricated
number. Verified: the same path with and without a 60s hole now returns identical sigma.

## A2 — fake wins also disable risk
`final_mid = 0.5` then `yes_won = final_mid >= 0.5` resolved every unknown settlement as a BUY
win. Reproduced: an unknown close booked `daily_pnl +5.00` on a $5 position. This corrupts paper
P&L *and* blinds the kill switch, since fabricated profit keeps `daily_pnl` above the limit. Now
an indecisive close (`|mid-0.5| < settle_band`, default 0.10) books a **provisional loss** and is
corrected by `verify_settlement` against the official result.

## A4 — never invent a strike
`ROLL['open']` (= the previous cycle's close) is now tracked. A strike is captured only within
`CFG['STRIKE_MAX_LAG_S']` (10s) of that open; an unknown open counts as infinite lag. Starting
the bot at 15:03 inside a 15:00-15:05 cycle used to set the strike to the 15:03 price, pricing a
deep in-the-money cycle as at-the-money for its whole remaining life. The first cycle after
startup is now sat out. `CFG['STRIKE_MAX_LAG_S']=0` disables the check for replaying recordings
made before `open` existed (the loosened research profile does this).

## Still open (not defects, but they bound what the numbers mean)
- **`up_won = mid >= 0.5` is a proxy for resolution**, not the Chainlink result. `verify_settlement`
  corrects the executor's P&L; `Analytics.tally` has no such correction and remains a proxy.
- **Strike is our composite price at cycle open**, not Polymarket's oracle print. Systematic basis.
- **`beta_residual` covariance** mixes sample sets (`nanmean(a*b)` pairs vs `nanmean(a)` singles)
  when a and b have NaNs in different places. Small bias; only bites on ragged feeds.

---

# Audit 2026-07-22 (second pass) — post-strategy-rewrite

Covers the code written after the first audit: the exchange-implied fair value path, the scenario
gates, the telemetry, and long-run performance. 102 tests, `gate.py` verified 5/5.

| # | Where | Sev | Defect |
|---|---|---|---|
| B1 | `drift_math.fair_from_mid` via `_fair_value` | **Critical** | Assumes 100% reversion; measured 3-15%. Edge inflated ~7-30x |
| B2 | `Feed.quotes` / `Analytics.compute` | **Critical** | Per-tick cost grows with uptime; 1696 ms/tick at 400k quotes, on the asyncio thread |
| B3 | `Analytics._signal` | Medium | `sources_disagree` overwritten by a generic `no_fair` before it was logged |
| B4 | `Analytics._write_state` | Medium | Stale `fair`/`dev`/`scenario` stamped with a fresh timestamp on early-return ticks |
| B5 | `Analytics._write_state` | Low | Recomputed the strike model and both book walks that `_fair_value` had just done |

## B1 — the reversion assumption is the big one
`fair = fair_from_mid(mid, dev)` undoes the **entire** unjustified displacement. Regressing the
realised move on the predicted gap (OLS through the origin) over 1018 candidates:

| horizon | n | reversion slope | 95% CI | R^2 |
|---|---|---|---|---|
| 10s | 719 | 0.030 | [-0.05, +0.11] | 0.001 |
| 40s | 724 | 0.086 | [-0.06, +0.23] | 0.001 |
| 60s | 679 | 0.131 | [-0.04, +0.30] | 0.001 |

Predicted 5.7c, realised 0.34c at 20s (**6%**). Two consequences:
1. Logged `edge` values are raw displacements, **not expected profit**. "edge 21.8c" means an
   expected ~2c.
2. **`RT_GATE` compares this inflated edge against a REAL cost** (spread + fee), so it is roughly
   10x too lenient. Same for any economic reading of `MIN_EDGE` / `MIN_EDGE_ROI`.

`CFG['REVERT_FRAC']` now scales `dev` before `fair_from_mid`. Default **1.0** (raw), because every
threshold in the file was tuned on the raw scale and silently rescaling would invalidate them.
Scaling is monotonic, so it never reorders signals — set it to ~0.10 and recalibrate `MIN_EDGE`
if you want the gates to be economically meaningful.

**R^2 ~ 0.001 means the predicted gap explains essentially none of the realised move.** The
fair-value magnitude is decoration. What earned in the study was the model-agreement filter and
the take-profit, not the point estimate.

## B2 — per-tick cost grew with uptime
`compute()` runs `np.asarray` over every feed deque each tick, so cost is O(len). `maxlen=400_000`
bounds memory but not time. Measured, at a realistic 2 trades/s/venue:

| uptime | before | after |
|---|---|---|
| 4h | 154 ms/tick | 16 ms |
| 12h | 420 ms | 16 ms |
| 24h | 798 ms | 16 ms |
| 48h | **1511 ms — misses the 1s loop** | 18 ms |

`compute()` runs **on the asyncio thread**, so overrunning also stalls the WebSocket readers and
the feeds fall further behind — on a strategy whose entire edge is latency. `Feed.trim()` now
bounds history by TIME (`CFG['FEED_KEEP_S']=900`). Verified: every statistic bit-identical
(`corr`, `beta`, `lag`, `vol_z`, `ref_move`, `poly_move`, `dev`, `fair`) trimmed vs untrimmed.
Real venue rates measured from the recordings are 0.2-1.9 trades/s, so 900s retains <2k quotes.

## Verified sound (checked, no change needed)
- `fair_from_mid` sign convention and dimensionality: `dev` is a logit-space H-second
  displacement, `logit(mid) - dev` is dimensionally correct; dev>0 -> poly rich -> fair<mid.
- `_walk_price` NO-side ordering (bids descending = cheapest NO first) and dollar accounting.
- `edge_up + edge_dn == bb - ba` invariant, so at most one side is ever positive.
- P&L signs at resolution for BUY (YES) and SELL (NO held at 1-exec_px).
- `beta_residual` lag alignment: `a[i]=ref[t]`, `b[i]=poly[t+k]`, k>0 = ref leads.
