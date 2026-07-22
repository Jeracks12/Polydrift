# Polymarket Drift — Findings, Data & Logic (canonical)

Last updated: 2026-07-21. This is the single source of truth for another agent picking up the
project. Companion docs: `docs/audit.md` (25 ranked defects), `memory/EXECUTION_UI_NOTES.md`
(execution/chart notes), `ENGINEERING_LOOP.md` / `AUDIT_LOOP.md` (the loop protocols).

## 0. TL;DR for a new agent

`poly_drift.py` monitors Polymarket 5-minute BTC (or any coin) up/down markets and runs ONE
strategy: **drift, reworked to MODEL-AS-SIGNAL** (2026-07-21; the old favorite strategy was removed):
- **Fair value = the calibrated binary model**, not the lead/lag residual:
  `edge = calibrate(Phi(ln(S/K)/(sigma*sqrt(tau))), CALIB_SLOPE=0.9) - real_executable_price`, per
  side, required to clear `max(MIN_EDGE, live round-trip cost)`.
- **Lead/lag is only a TIMING trigger**: trade when a ref leads poly (lag>=0, |corr|>=MIN_CORR,
  CORR_SIGN), the move is exchange-driven (REF_DRIVEN), and the trigger is strong (robust median/MAD
  z >= `DRIFT_Z_ALERT`, at [poly_drift.py:33] CFG — no CLI flag yet, edit CFG to tune frequency).
- Live: the model almost always sees some edge, so the timing gate is the binding constraint
  (`[skip] DRIFT ...: no leading ref`). Prices are book-walked for the real stake, so gates test
  the price you'd ACTUALLY pay (a $1 order slipped 0.43->0.52 live).

Execution (`execution.py`, uses **py-clob-client-v2**) supports `off` / `paper` / `live`; **LIVE
WORKS** (a real $1 order filled + settled on-chain). Exit management (take-profit +
thesis-invalidation) is OFF by default (`--exit`). **Not proven to make money yet** — read §4;
run with `python poly_drift.py` (interactive menu). Logs split to `logs/YYYY-MM-DD/*.log`.

## 1. Repo map

| File | Role |
|---|---|
| `poly_drift.py` | live monitor: feeds, analytics, drift strategy, chart, logging, CLI/menu |
| `drift_math.py` | ALL pure math (no I/O/globals): grids, composite, xcorr, beta/residual, **robust z**, **binary pricer** (fair value) + **calibration** (`binary_p_up`, `realized_sigma_per_s`) |
| `execution.py` | isolated order layer: paper/live, market/limit ($-denominated), risk caps, kill switch, official-result settlement verification |
| `recorder.py` / `replay.py` | record every tick+book event to `data/session_*.jsonl.gz`; replay deterministically through Analytics (virtual clock) |
| `scripts/gate.py` | P0 gate: pytest + replay determinism + fixtures/baseline; prints `RECEIPT ...` |
| `scripts/audit_gate.py` | audit-loop gate (docs/audit.md integrity) |
| `scripts/backtest_*.py` | year backtests (see §4); all read `data/cycles_BTCUSDT_365d.parquet` |
| `scripts/build_dataset.py` | materialize the reusable per-cycle dataset from cached klines |
| `tests/` | 67 tests (math, menu, execution, drift/exits) |
| `logbook.py` | split daily logs `logs/YYYY-MM-DD/{errors,warnings,trades,signals,data,session}.log` |
| `fixtures/` + `data/` | live recordings, klines cache, the master parquet, baselines |

## 2. Data sources — six venue feeds → `spot` and `perp` composites

Binance, Bybit, OKX (spot + perp each) + Polymarket. Composites: average of de-meaned member log
prices (+ common level) so a venue joining/dropping shifts the composite ~0, not by the inter-venue
spread; `$vol` = sum. `STALE_S=30` drops a silent venue to NaN. Reconnect backoff 2→60s; `[health]`
warnings; `silent venues:` report line.

**PLDT (user's ISP) blocks are INTERMITTENT** — Bybit/OKX/Binance-futures flip between blocked and
streaming within hours (TLS block page `prohibitedaccess.pldtsmart.com.ph` / handshake timeout).
Binance spot, Polymarket CLOB/gamma, and Hyperliquid are up in every probe. See
[[pldt-network-blocks]]. **Always probe for data FLOW, not just connection.**
Gamma API: live-market queries MUST pass `end_date_min=<now>`; resolved-market lookups MUST pass
`closed=true` (else results are silently empty). Markets resolve on **Chainlink** BTC/USD, not
Binance — our composite ≈ Chainlink (95% outcome agreement); the ~5% basis is a near-strike risk.

## 3. The drift strategy — model-as-signal (poly_drift.py `Analytics._signal`, params in `CFG`)

Analytics.compute() runs every second on a 1s grid. Poly analyzed in logit space. Reworked
2026-07-21: **the model is the fair value; lead/lag is only a timing trigger.** Order of the gate
chain (all must pass, each `[skip]`-logged with its reason to `signals.log`):

1. **Fair value** `p_up = calibrate(binary_p_up(S_composite, K_strike, sigma, tau), CALIB_SLOPE=0.9)`
   — calibrated probability BTC ends up. K = exchange price captured at cycle open (`ROLL['strike']`);
   sigma = `realized_sigma_per_s(..., VOL_LOOKBACK_S=120)`; tau = `ROLL['end'] - now`. Needs a strike +
   cycle end, so drift is **series-mode-native** (fixed-token = no signal).
2. **Executable edge per side** vs the BOOK-WALKED price for the real stake (`_paid_effective` →
   `_walk_price`, NOT the top-of-book quote): `edge_up = p_up - paid_yes`,
   `edge_dn = (1-p_up) - paid_no`. Pick the larger; require `>= max(MIN_EDGE=0.03, round-trip cost)`.
   Round-trip cost (RT_GATE, on) = `RT_SPREAD_MULT*(ask-bid) + RT_FEE_C + RT_BUFFER_C` — adapts to
   the live spread ("the stretch must cover fees"). `ENTRY_MAX_PX` still caps the price bought.
3. **Feasibility floor** `MIN_MODEL_P` (0.20): skip if the chosen side's model prob < this.
4. **TIMING gate** (lead/lag confirms poly is the laggard NOW, not us): a ref with `|corr|>=MIN_CORR`,
   `lag>=0`, correct `CORR_SIGN`; **robust z** `|robust_z(res)| >= DRIFT_Z_ALERT` (median/MAD, replaces
   the fat-tailed Gaussian z — audit A1); `vol_z >= VOL_Z_MIN`; **REF_DRIVEN** (the move is
   exchange-driven in the trade's direction, dominates poly's own).
5. per-side cooldown; no fire within 5s of a roll; `_tradable` (fresh poly book `POLY_STALE_S`,
   `ENTRY_CUTOFF_S=40` no-entry-near-close).

Scored at MARKET CLOSE (resolution P&L, pays 1/0), not a 30s mark. Flat `self.tally` (drift only).
`signals_outcomes.csv` records the ACTUAL fill price (book-walked), so losses aren't understated.
**Live observation:** the model almost always shows some edge, so the timing gate is the binding
constraint — tune frequency via `DRIFT_Z_ALERT` ([poly_drift.py:33]) / `MIN_CORR` (edit CFG; no flag yet).

### 3-exits. Optional exit management (OFF by default; `--exit` / menu)
A drift entry is a timing scalp, so instead of holding to resolution it can exit early on first-of:
**take-profit** (sell back for >= entry + `EXIT_TP_C` net of the spread) or **thesis-invalidation**
(`_model_edge` for the side <= `EXIT_THESIS_EDGE` — move reversed / poly caught up). `EXIT_MIN_HOLD_S`
anti-thrash. Live sells use v2 `Side.SELL`; a FAILED sell KEEPS the position (never phantom-close).

## 4. Backtests & the honest edge picture

**Datasets (saved, reusable):** `data/cycles_BTCUSDT_365d.parquet` (420k rows = 105k cycles × 4
checkpoints, raw+calibrated model probs + outcomes), `data/klines_BTCUSDT_1m_*.parquet` (year, cached),
`data/klines_BTCUSDT_1s_*.parquet` (3 days). Model = `binary_p_up`, walk-forward recalibrated
(30-day blocks, no lookahead). Brier ~0.176 vs 0.25 null; calibration slope ~0.89 (raw model
overconfident in tails).

**Favorite / blind-threshold results (105k cycles):** priced against a *naive Gaussian market* the
favorite bands are +EV (0.65–0.70 best, ~+10% at t+2min); the 0.60–0.70 band at **2 minutes left**
is the legit sweet spot. **BUT under an efficient (calibrated) market the edge nearly vanishes and
strong favorites (≥0.80) go NEGATIVE** (blind buy ≥0.70 = −0.3%, ≥0.80 = −0.8%). ⚠️ **The favorite
"edge" is entirely a bet that real Polymarket prices like an uncalibrated model — UNVALIDATED
against real prices.** Also found & flagged: `scripts/backtest_band_by_tau.py` t+4min checkpoint is
a **lookahead artifact** (observes the resolution bar → 100% win; ignore).

**Live (drift, real thresholds, ~45 min, pre-feasibility-gate):** 14 signals, 11W/3L, +15.2c avg
mark-to-mid — but that's mark-to-mid (audit D1: optimistic upper bound), tiny sample (audit D2).

**Bottom line:** drift has a mechanism (latency) but a tiny, hard-to-fill sample; favorite is
backtestable but likely competed away. Neither is proven. Next step is a **multi-day `--mode paper`
run**, comparing the two tallies against real settlement.

## 5. Execution & running

Interactive: `python poly_drift.py` → menu asks market / record / mode / order-type / stake / caps.
Flags still work: `--series btc-updown-5m --mode {off|paper|live} --order {market|limit} --stake N
--max-daily-loss N`. `off` = signals only; `paper` = simulated fills vs the real book; `live` =
real orders via **py-clob-client-v2** (the old client is archived+rejected). **Market orders are $-denominated ($1 min, walk the book); limit =
share-based (5-share min).** Risk: one position per market/side, daily-loss kill switch, settle at
cycle close with official gamma-result verification (~10–20 min to finalize; retries).

**LIVE is human-armed only**: `.env` (never commit; `.env.example` is the template) with
`POLY_PRIVATE_KEY` / `POLY_SIGNATURE_TYPE` / `POLY_FUNDER`, plus `POLY_DRIFT_ARM=YES-LIVE`, plus a
typed `ARM` at startup. Do NOT arm live for the user. (If a key ever touched the synced/committed
`.env.example`, treat it as exposed — rotate.)

## 6. Recurring gotchas & top audit findings

- **`global EXECUTOR` in `main()` has been lost/reverted TWICE.** Without it, paper/live builds the
  executor in local scope and NO orders fire (chart arrows still draw). Guard test:
  `test_main_publishes_executor_for_the_signal_engine`. If trades silently stop, check this first.
- A chart arrow is a signal, NOT a fill. Fills are `[exec]` console lines / `paper_trades.csv`.
- The chart's `record` is signal-quality (mark-to-mid), labeled `(not settlement)`; real P&L is the
  executor's settled trades.
- Top open audit findings to fix (see `docs/audit.md`, run `python scripts/audit_gate.py`):
  **B5** (no staleness guard on the poly feed → phantom z on a dead ws), **D6/D1** (no session/config
  provenance in logs; mark-to-mid optimistic), **A1/A2** (drift z self-suppression; xcorr multiplicity
  → planned rankZ + lag significance), **B3** (cross-roll stats contamination). B7/A5 (τ-blindness)
  partially addressed for drift by the feasibility gate.

## 7. Verification discipline

67 pytest tests + `scripts/gate.py` (`RECEIPT verified checks=5/5`) must stay green. The gate's
check 5 (`strategy_repro`) replays the newest fixture under a FIXED loosened profile
(`replay.py --loosen`, `LOOSEN_PROFILE`) and requires BOTH strategies to fire and the
signal→scoring path to be deterministic — so it validates present strategy behaviour, not just
determinism. Fixtures are current-schema (roll events carry `end`, so favorite/tau replay
faithfully). Pure math lives in `drift_math.py`, unit-tested with planted-parameter recovery +
no-lookahead asserts (one real lookahead bug in `grid_vol` was caught this way). Every nontrivial
change gets a live paper smoke before claiming it works. NOTE: strict-gate replay firing 0 signals
in a calm session is correct selectivity, not a bug — the loosened repro check is what exercises
the strategy path.

## Phantom drift: measured, 2026-07-21 15:03 (BTC 5m)

Poly UP collapsed **0.785 -> 0.455** in 15s while BTC spot moved **-4.6bp** (65,950.92 ->
65,920.90), then recovered to **0.855** within 45s as spot returned to 65,941. Buying the 0.55
ask and selling into the 0.83 bid is **+28c/share**. The tell: the last trade printed 0.455 while
the book was 0.54/0.55 — a market order swept the book. That is liquidation, not information.

Two separate defects kept us out:
1. `ENTRY_MAX_PX=0.45` rejected the 0.55 ask. The cap never bounded dollar risk (max loss = the
   stake regardless of entry price) and preferentially rejected the model's *highest*-conviction
   trades. Default is now 1.00.
2. The skip message always claimed "edge < required" — false in 1200 of 2305 logged skips (52%),
   where the price cap was the real blocker. It now names the binding gate.

Both are fixed. The episode remains **unverifiable in detail** because nothing recorded the book
depth or the model state at the trough — hence the telemetry below.

### market_state.csv (CFG `STATE_LOG`, on by default)
One row per tick at `logs/<date>/<market>/market_state.csv`: exchange spot/perp, bp vs strike,
tau, poly bid/ask **and resting size at each**, walked fill prices, model P_up, edge_up/edge_dn,
the required edge, robust z / corr / lag / vol_z / ref_move / poly_move, and `gate` — which
check declined the tick. `_signal` cannot return without setting `gate` (enforced by test).

Mine it with `python scripts/find_dislocations.py <csv>`: flags poly moves >= `--drop` that occur
while the exchange moved <= `--max-bp`, then reports realized reversion, fillable top-of-book
dollars, and a histogram of the gate that blocked each one. Grouping by `gate` is the direct
answer to "what is costing us trades".

**Open question — do NOT arm live on this yet.** The miner is direction-agnostic, so a genuine
trend leg looks identical to a panic dump until it fails to revert. The reversion rate on real
multi-day data is the number that decides whether this is a strategy or hindsight.

## Reward:risk gate (MIN_EDGE_ROI) + vol-spike guard — 2026-07-22

**Trigger (live log):** a BUY filled at **0.88** on `P_up 0.945` (edge 7.5c) while `volZ 2.5`.
Poly repriced 0.875 -> 0.825 immediately after.

### Why an expensive share is different
The bot stakes **dollars**, not shares, so the objective is EV per dollar risked:

| entry | shares per $1 | win | lose | risk:reward | EV @ +6.5c edge |
|---|---|---|---|---|---|
| 0.88 | 1.14 | +$0.14 | -$1.00 | **$7.30 : $1** | +7.4% |
| 0.30 | 3.33 | +$2.33 | -$1.00 | $0.40 : $1 | +31.7% |

Break-even model prob at 0.88 is 0.88 — a **6.5-point** model error erases the entire edge. And
`P_up` is most sigma-sensitive exactly at the extremes: at 13.5bp/150s, inflating sigma 40%
moves `P_up` 0.819 -> 0.742.

`_required_edge()` therefore adds a third floor: `edge >= MIN_EDGE_ROI * price_paid`, alongside
`MIN_EDGE` and the round-trip cost. Gate label `reward_risk`; `market_state.csv` gained
`req_up/req_dn/roi_up/roi_dn`.

### Measured on fixtures/session_20260720 (n=18, LOOSENED profile — weak evidence)
| MIN_EDGE_ROI | n | hit | EV per $ staked |
|---|---|---|---|
| 0.00 | 18 | 77.8% | +19.1% |
| 0.03 | 16 | 75.0% | +20.2% |
| **0.06** | 14 | 71.4% | **+21.1%** |
| 0.12 | 11 | 63.6% | +7.0% |
| 0.20 | 7 | 57.1% | -13.8% |

**Default set to 0.06, not 0.12.** 0.12 is what it takes to block the observed 0.88 fill, but on
the only data available it costs 14pp of EV/$ — it rejects sound *cheap* entries whose edge is
small in absolute terms. Runtime: `--min-edge-roi`, or the menu's indicator section.

### Two cautions on that table
1. **Per-SHARE cents is the wrong yardistick and inverts the answer.** `exec_px` is YES-space, so
   a SELL's cost is `1 - exec_px`. Scoring per share (or dividing by `exec_px` for a SELL) made
   the expensive trades look cheap and reversed the ranking. Always divide by
   `exec_px if side=='BUY' else 1-exec_px`.
2. **The "expensive trades won" result is consistent with luck.** 11 favorites at ~0.72 average
   cost went 11/11; the expectation is ~8/11. A fair favorite has zero EV by construction, so
   +29.5%/$ on that subset is a ~1-in-20 fluke, not demonstrated edge.

### Vol-spike guard (`VOL_SPIKE_GUARD=1`, on by default)
`sigma := max(realized(120s), realized(30s))`. Trailing vol is a poor forecast of the next tau
when the tape is accelerating, and being wrong toward 0.5 costs a skipped trade while being wrong
toward 1.0 costs money. Disable with `--vol-spike-guard 0`.

**Still unsettled:** whether expensive entries are genuinely worse needs a sample with real
variation in entry price. The current fixture has 11 of 18 above $0.60/share and every one won.

## Strategy change: exchange-implied fair value — 2026-07-22

**The exploit thesis, restated as code.** Binance/OKX/Bybit are the true price; poly should track
them; the tradeable quantity is how far poly has strayed from what the exchange move justifies.
That is exactly `beta_residual()`'s `res = poly_move - beta*ref_move`, which the codebase has
always computed and — until now — **threw away**. `fair_from_mid()` and `drift_z()` were dead
code, imported by nothing. Direction came 100% from the strike binary model.

### One rule, two scenarios
`fair = fair_from_mid(mid, dev)`; trade toward fair. Sign of `dev` gives the side:

| scenario | test | meaning | entry |
|---|---|---|---|
| `catchup` | `|ref_move| >= |poly_move|` | exchange moved, poly hasn't repriced | dearer — poly has partly corrected |
| `fade` | `|poly_move| > |ref_move|` | poly moved, exchange didn't follow | cheap — poly just dumped |

`|poly_move| / |ref_move|` is literally the overreaction ratio, logged as `overreact`.
**No strike is required**, so the first cycle after startup is tradeable and nothing depends on
a guessed cycle-open price.

The old `REF_DRIVEN=1` demanded `|ref_move| >= |poly_move|` — it *was* `SCENARIO_MODE='catchup'`
and structurally forbade every fade, i.e. every cheap entry. That single inequality explains both
the "we always get expensive entries" complaint and why the 15:03 dislocation was never traded.

### Verified on the 2026-07-21 15:03 episode
Book 0.54/0.55, poly down 1.48 logit, exchange justified 0.46 -> **3.2x overreaction**, scenario
`fade`, fair **0.768**, signal **BUY (UP)** at 0.55 with +21.8c edge, `strike=None` throughout.
Poly recovered to 0.83/0.84 within 40s.

### Measured on fixtures/session_20260720 — NOT supportive, and NOT conclusive
Scored as a scalp (poly move 40s after entry, which is what a fade actually targets):

| scenario | n | won | mean +40s |
|---|---|---|---|
| fade | 9 | 4/9 | −3.17c |
| catchup | 7 | 5/7 | −0.64c (median +5.50c) |

At production gates, hold-to-resolution: `drift/both` −13.7% EV/$ (n=5), `model/both` +53.3%
(n=5). **Every one of these samples is single digits.** They cannot distinguish the strategies;
they are recorded so the next run has a baseline, not because they mean anything. The fixture is
also a single session recorded under the old strategy, so its signals are counterfactual.

Runtime: `--fair-source drift|model|both`, `--scenario both|catchup|fade`, `--model-veto`,
`--fade-tau-min/max`, or the menu's indicator section.

### New market_state.csv columns (feed these back for analysis)
`fair`, `dev`, `gap_c` (dislocation in cents), `scenario`, `overreact`, `beta`, plus `p_up` kept
alongside for cross-checking the two fair values against each other.

## Rule set from 1,018 candidates / 11 sessions — 2026-07-22

Built with `scripts/collect_signals.py` (one permissive replay pass, records every candidate
plus its forward price path) then swept offline with `scripts/sweep_strategy.py`. Everything is
scored **per dollar staked** (`cost = exec_px if BUY else 1-exec_px`).

**An earlier read on 58 candidates said the opposite and was wrong.** At n=44 the fade showed
+18.0% EV/$ with a take-profit; at n=692 it is -5.6%. Treat any cell here with n under ~100 as
decoration.

### 1. Do NOT raise the fade threshold — it is significantly worse
| fade filter | n | hit | EV/$ | |
|---|---|---|---|---|
| all | 692 | 43.5% | -5.6% | |
| `|z|` >= 3.0 | 140 | 37.1% | **-26.0% +/-18.1** | **SIG** |
| `|z|` >= 4.0 | 80 | 31.2% | **-33.1% +/-23.4** | **SIG** |
| `|gap|` >= 16c | 22 | 27.3% | -33.0% | |
| `|gap|` >= 20c | 13 | 23.1% | -37.3% | |

The harder poly moves against the exchange, the more likely poly is **right**. The intuition
"Polymarket is sharp" is correct; the prescription that follows from it is to fade *less*, not to
fade only the big ones. Tightening the trigger fades harder into informed flow.

### 2. Combining with the old strike model is the biggest lever
| filter | n | hit | EV/$ | |
|---|---|---|---|---|
| model agrees | 498 | 57.0% | +11.4% | |
| model DISagrees | 467 | 37.0% | **-18.6% +/-14.1** | **SIG** |
| catchup + agrees | 133 | 67.7% | +23.4% | |

The only statistically significant *positive*-side result in the study is the stack:
**catchup + model agrees + TP 3c -> n=138, hit 81.2%, EV/$ +16.6% +/-15.5 (SIG)**.
8 of 11 recordings positive, so it is not one lucky session.

### 3. Exit: catchup's edge is front-loaded, so take profit early
Mean per-share P&L by holding time:
```
catchup  5s:+1.8c  10s:+1.3c  15s:+1.6c  20s:+1.1c  30s:+0.0c  40s:-0.1c
fade     5s:-0.4c  10s:-0.4c  15s:-0.2c  20s:+0.0c  30s:+0.2c  40s:+0.8c
```
catchup TP 2c +6.2% / TP 3c +5.7% / TP 20c +3.0% / hold-to-close +3.2% (n=273-291). The existing
`EXIT_TP_C=2.0` default is what the data supports. A stop adds nothing (TP 8c/stop 16c +5.1%).
Within the final stack, TP and hold-to-close are inside each other's error bars — the tight TP is
a mild preference plus a capital-turnover argument, not a proven edge.

### 4. Long shots lose
`cost < 0.25` -> -18.8% EV/$ (n=262) at an 11.8% hit rate, across all candidates. (Inside the
catchup+agree subset the filter *removed* winners, so it is NOT applied as a default.)

### Defaults changed
`FAIR_SOURCE='both'` and `SCENARIO_MODE='catchup'`. **Operational consequence:** `'both'` needs a
trusted strike, so the first cycle after startup cannot trade (gate `no_strike`). To trade the
fade as well, `--scenario both` (+4.9% +/-7.2, n=310 — positive but not significant).
