# Polymarket Drift

A research-grade trading bot for Polymarket's **5-minute crypto up/down markets** (BTC, ETH, SOL, XRP).
It watches the real exchange price on six venue feeds, decides where Polymarket *should* be trading,
and either takes the dislocation or makes a market around it.

> **Read this before risking money.** Neither strategy is proven profitable. The taker path, priced
> honestly against Polymarket's fee, is **negative in expectation**. The maker path is positive in
> backtest but depends on fills it may not get. This README tells you exactly why, so you can decide
> for yourself. See [The honest edge picture](#the-honest-edge-picture).

---

## Table of contents
1. [The idea in one minute](#the-idea-in-one-minute)
2. [How the market works](#how-the-market-works)
3. [The math](#the-math)
4. [The two strategies](#the-two-strategies)
5. [Architecture](#architecture)
6. [Running it](#running-it)
7. [Configuration reference](#configuration-reference)
8. [Risk controls & safety](#risk-controls--safety)
9. [The honest edge picture](#the-honest-edge-picture)
10. [Research tooling](#research-tooling)
11. [Testing & verification](#testing--verification)
12. [Going live](#going-live)

---

## The idea in one minute

Polymarket's 5-minute markets ask a single question: **will BTC be higher 5 minutes from now than it
is at the cycle open?** They settle on a Chainlink price feed. The order book lags the real exchange
price (Binance/OKX/Bybit) by seconds to tens of seconds.

That lag is the whole opportunity. If BTC jumps on Binance and Polymarket hasn't repriced yet, the
"Up" share is momentarily too cheap. Two ways to profit:

- **Taker (drift):** cross the spread and buy the mispriced side, expecting Polymarket to catch up.
- **Maker:** rest limit orders around where the exchange says fair value is, and get paid the spread
  when someone trades against you.

The catch — and the reason this repo is more honest than most — is that **Polymarket charges takers a
fee designed specifically to kill this trade**. Understanding that fee is the difference between a
strategy and a slow bleed.

---

## How the market works

| Property | Value | Why it matters |
|---|---|---|
| Cycle length | 5 minutes | Everything resets every 5 min; positions resolve at cycle close |
| Resolution | Chainlink BTC/USD feed | ~95% agreement with our exchange composite; the ~5% basis is near-strike risk |
| Tick size | `0.01` | You cannot quote inside a 1-tick spread — the minimum market |
| Min order | `5` shares | A limit order below 5 shares is rejected (≈ $2.50 at 0.50) |
| **Taker fee** | `0.07 × p × (1−p)` per share | **The dominant cost.** Makers pay zero |
| Maker rewards | none on these markets (`rewardsMaxSpread = 0`) | Making here is unsubsidised — you earn only the spread |

The `strike` is the exchange price captured **at cycle open**. If the bot starts mid-cycle and never
saw the open, it refuses to invent one and sits the cycle out (`STRIKE_MAX_LAG_S`).

Verify the live fee/tick/reward config any time:
```bash
python scripts/check_fees.py
```

---

## The math

### 1. Fair value — where Polymarket *should* be

Two independent estimates, combined by requiring agreement (`FAIR_SOURCE='both'`):

**(a) The drift residual** (no strike needed). Regress Polymarket's recent move on the exchange's:

```
res = poly_move − β · ref_move          (β from a rolling cross-covariance)
```

`res` is the part of Polymarket's move the exchange did **not** justify — the tradeable dislocation.
Fair value undoes it in logit space:

```
fair = expit( logit(mid) − dev · REVERT_FRAC )
```

`REVERT_FRAC = 1.0` assumes the gap fully closes (raw). Measured reversion is only ~3–15%, so the raw
`edge` this prints is a **displacement, not an expected profit** — kept at 1.0 only because every
threshold was tuned to that scale.

**(b) The strike binary model.** The probability BTC ends above the strike `K`, given spot `S`,
realized vol `σ`, and time left `τ`:

```
x = ln(S / K) / (σ · √τ)          (standardized moneyness)
p_up = P(up | x)
```

Instead of the textbook Gaussian `Φ(x)`, we use the **empirical distribution** measured on 419,444
rows / 104,861 real cycles (`CALIB_MODE='empirical'`). This matters: the Gaussian is *under-confident
in the body* — at ±10 bp from the strike (where these markets live) it disagrees with reality by up to
**11 percentage points**, roughly 4× the entire `MIN_EDGE`.

| Pricer | Out-of-sample Brier |
|---|---|
| Gaussian `Φ` (raw) | 0.13063 |
| `Φ` + `CALIB_SLOPE=0.9` (old default — *worse than raw*) | 0.13271 |
| **Empirical table (default)** | **0.12655** |

Vol is `max(σ_slow, σ_fast)` when `VOL_SPIKE_GUARD` is on: an accelerating tape makes trailing vol
stale-low, which inflates `p_up` toward 0/1 exactly where shares are most expensive.

### 2. Edge — what you'd actually make

Priced against the **book-walked** price for your real stake, never the top-of-book quote (a live $1
order slipped 0.43 → 0.52 by sweeping several levels):

```
edge = fair − paid_effective(side, stake)
```

### 3. The fee — the term the old code ignored

```
fee_per_share = 0.07 · p · (1 − p)      (takers only; makers pay zero)
```

| Entry price | Fee ¢/share | As % of capital |
|---|---|---|
| 0.10 | 0.63¢ | **6.3%** |
| 0.50 | 1.75¢ | 3.5% |
| 0.90 | 0.63¢ | 0.7% |

Note the fee is symmetric in YES/NO but, **as a fraction of capital, worst on cheap shares** — the
opposite of what a per-dollar objective assumes.

### 4. Required edge — three floors

A trade must clear the largest of:

```
required = max( MIN_EDGE ,                                  # fixed floor (0.03)
                RT_SPREAD_MULT·spread + legs·fee + buffer , # live round-trip cost (RT_GATE)
                MIN_EDGE_ROI · price_paid )                 # reward:risk (0.06)
```

- **Round-trip cost** charges `legs = 2 if EXIT_ENABLE else 1` fees — an early exit is a second taker
  trade; holding to resolution is a settlement, not a trade, so it pays one fee.
- **Reward:risk** (`MIN_EDGE_ROI`): the bot stakes *dollars*. An 88¢ share risks $7.30 to make $1, so
  a 3¢ edge there is a 3.4% return on a 100% downside. This makes expensive shares earn their keep.

### 5. Maker pricing (Avellaneda–Stoikov, adapted to a binary)

```
reservation  r = fair − inv_skew_ticks · TICK · clamp(inventory / cap, −1, +1)
half-spread  d = γ·σ²·τ·p(1−p)/2 + (1/γ)·ln(1 + γ/κ),   floored at TICK/2
bid = r − d,   ask = r + d          (tick-rounded, never through the touch)
```

- The inventory skew is expressed **in ticks** (not the textbook `q·γ·σ²` term, which is ~0.0002¢ at
  this asset's vol — numerically dead against a 1¢ tick). At full inventory it leans a full tick to
  offload.
- `κ` defaults to **100**, not the textbook ~6: on a 1¢-tick market κ=6 would quote 13¢ wide and never
  fill. κ=100 puts the quote at the touch.
- The binary payoff variance `p(1−p)` is the *same shape as the fee* — both argue for wider quotes
  near 0.50.

---

## The two strategies

### Taker — "drift"

The signal engine (`Analytics._signal`) fires when **all** gates pass:

1. **Fair value exists** (strike captured, sources agree on direction).
2. **Edge clears the required floor** (§4 above), per side, on the book-walked price.
3. **Feasibility** — the model gives the traded side ≥ `MIN_MODEL_P` win probability.
4. **Timing** — a reference venue leads Polymarket (`lag ≥ 0`, `|corr| ≥ MIN_CORR`), the trigger is
   strong (robust median/MAD `z ≥ DRIFT_Z_ALERT`), and vol confirms (`vol_z ≥ VOL_Z_MIN`).
5. **Scenario** — `catchup` (exchange moved, Polymarket hasn't) by default. `fade` (Polymarket moved
   on its own) is available but **loses at scale** — big Polymarket moves are informed.
6. **Freshness / cutoff** — Polymarket book fresh within `POLY_STALE_S`; no entry inside the last
   `ENTRY_CUTOFF_S` seconds of the cycle.

Each skipped tick logs *which* gate blocked it to `signals.log`, and every tick's full state
(prices, book depth, model, edges, gate) goes to `market_state.csv` — the only way to study a
dislocation after it has closed.

### Maker — resting quotes (`--maker`)

Same fair-value model, opposite side of the trade. `maker.py`:

- Rests two-sided GTC limit quotes anchored to the **strike model probability** (independent of the
  Polymarket mid — quoting around the mid would be circular).
- **The inversion:** the taker research found that the harder Polymarket moves against the exchange,
  the more often Polymarket is *right* (`|z|≥3 → −26% EV/$`). As a taker that killed the fade. As a
  maker it's the **toxicity signal** — a big `|z|` means *pull your quotes*, don't trade into informed
  flow (`--maker-toxic-z`, default 2.5).
- Per-side inventory cap that *flattens* (keeps the offloading quote live), tick-scaled inventory
  skew, cancel-on-roll.
- Fills settle through the executor's existing machinery, so a matched YES+NO pair nets to **spread
  capture**: buy YES @0.49 + NO @0.49 costs 0.98, pays 1 → +0.02, guaranteed.

The two paths are **mutually exclusive** — quoting both sides while also crossing the spread would
trade against your own orders and pay the fee maker mode exists to avoid.

---

## Architecture

| File | Role |
|---|---|
| `poly_drift.py` | Live monitor: six WebSocket feeds → composites, analytics loop, drift signal, chart, CLI/menu |
| `drift_math.py` | All pure math: composites, cross-correlation, beta/residual, robust z, binary pricer, **empirical calibration**, **taker fee**. No I/O, no globals |
| `maker.py` | The resting-quote engine: A-S pricing, toxicity, inventory, fill/settlement, order I/O |
| `execution.py` | Order layer (py-clob-client-v2): paper/live, market/limit, risk caps, kill switch, settlement + official-result verification, maker-fill booking |
| `recorder.py` / `replay.py` | Record every tick+book event; replay deterministically through the analytics (virtual clock) |
| `logbook.py` | Split daily logs under `logs/YYYY-MM-DD/` |
| `scripts/` | Gate, fee checker, calibration fit, fee sweep, dislocation miner, backtests |
| `tests/` | 147 tests (math, execution, menu, maker, regressions) |

**Threading model (important for live):**

```
asyncio loop ─► compute() ─► publishes AN.maker_input {fair, σ, τ, z, fresh}   (numpy only)
                                          │ atomic handoff
maker_worker thread ─► on_tick() ─► post / cancel / poll   (ALL HTTP here, off the loop)
                            └─ fill ─► Executor settlement ─► daily_pnl ─► kill switch
```

Order I/O runs on a dedicated thread so a blocking CLOB round-trip never stalls the feed readers.

---

## Running it

**Interactive menu (recommended):**
```bash
python poly_drift.py
```
Pick a market → `paper` or `live` → **strategy: 1) taker  2) maker** → tune settings (each has a
sensible default; press Enter to accept).

**Flags (scriptable):**
```bash
# Taker, paper, $1 stake
python poly_drift.py --series btc-updown-5m --mode paper --stake 1

# Maker, paper, 5 shares/side
python poly_drift.py --series btc-updown-5m --mode paper --maker --maker-shares 5
```

`--mode off` = signals only (no orders). Logs split to `logs/<date>/<market>/`. Latency of every
maker order lands in `maker_latency.csv`.

---

## Configuration reference

Defaults live in `CFG` at the top of `poly_drift.py`. The knobs you'll actually touch:

| Key | Default | Meaning |
|---|---|---|
| `FAIR_SOURCE` | `both` | `drift` \| `model` \| `both` (require agreement) |
| `SCENARIO_MODE` | `catchup` | `catchup` \| `fade` \| `both` |
| `CALIB_MODE` | `empirical` | `empirical` (measured table) \| `logit` (Gaussian + slope) |
| `MIN_EDGE` | `0.03` | Fixed edge floor, probability units |
| `MIN_EDGE_ROI` | `0.06` | Reward:risk floor as a fraction of price paid |
| `RT_GATE` | `1` | Require edge to clear the live round-trip cost (incl. fee) |
| `FEE_RATE_TAKER` | `0.07` | The live crypto_fees_v2 rate — verify with `check_fees.py` |
| `DRIFT_Z_ALERT` | `2.0` | Robust-z trigger strength (lower = more signals) |
| `MIN_CORR` | `0.15` | Min `|corr|` to count a venue as leading |
| `ENTRY_CUTOFF_S` | `40` | Hard no-entry window before cycle close |
| `POLY_STALE_S` | `15` | Block all signals if the book hasn't updated in this long |
| `EXIT_ENABLE` | `0` | Off = hold to resolution; on = take-profit + thesis exit |

**Maker flags:** `--maker-shares` (5), `--maker-toxic-z` (2.5), `--maker-inv-skew-ticks` (1.0),
`--maker-gamma` (3.0), `--maker-kappa` (100).

---

## Risk controls & safety

- **Daily-loss kill switch** (`--max-daily-loss`, default $10): permanently disarms new entries once
  realized losses breach the cap. Covers **both** taker and maker fills.
- **Freshness / staleness guards**: a dead Polymarket socket with a moving exchange produces a phantom
  fair value — blocked by `POLY_STALE_S`; the maker pulls all quotes.
- **Toxicity pull**: the maker cancels everything on an informed-flow spike (`|z| ≥ toxic_z`).
- **Thin-book gate**: a taker entry the book can't actually fill in size is rejected (`thin_book`).
- **Fee premise alarm**: if a *live maker* fill ever reports a fee, it's folded into P&L and logged
  as `!!! MAKER FEE CHARGED` — the "makers pay zero" assumption proves or breaks itself automatically.
- **Orphan reconciliation**: on restart the maker cancels untracked orders on its market's tokens so a
  crash can't leave you double-quoting.
- **Live is double-locked**: requires `POLY_DRIFT_ARM=YES-LIVE` in the environment **and** a typed
  `ARM` at startup. Credentials come only from `.env` (never committed).

---

## The honest edge picture

Everything below is **net of the real taker fee**, scored per dollar staked, on the 16 recorded
sessions (`scripts/sweep_fees.py`).

```
gross edge (before fees)          +0.78¢/share
round-trip taker fee              −2.58¢/share
net as a taker round-trip         −1.80¢/share
```

Four execution designs, same signal:

| Design | Pooled EV/$ | Holdout EV/$ |
|---|---|---|
| **A** taker in / taker out | −0.7% | −2.2% |
| **B** taker in / hold to resolution | −2.5% | −11.7% |
| **C** maker in / taker out | **+5.4%** | +3.7% |
| **D** maker in / hold to resolution | +3.3% | −6.7% |

**Every taker design is negative. Only maker-entry designs are positive — and none is statistically
significant.** Design C assumes you get filled at your resting price, which overstates fills, so treat
it as an upper bound. The famous "catchup + model agrees +16.6% (SIG)" result does **not** survive:
counted by independent cycle it's +9.3%, and on 5 never-seen sessions it's +8.0% gross → roughly break-
even net.

**Bottom line:** there is a real latency mechanism, but Polymarket's fee was built to neutralise the
taker version of it, and it largely does. The maker version is the only honest path to positive
expectancy, and it is unproven until real fills confirm both the fill rate and the zero-fee premise.

---

## Research tooling

| Script | What it answers |
|---|---|
| `scripts/check_fees.py` | What is the live fee / tick / min-size / reward config right now? |
| `scripts/fit_calibration.py` | Fit & score the empirical binary pricer (`--emit` prints the table) |
| `scripts/sweep_fees.py` | Net-of-fee EV of the four execution designs, cluster-bootstrapped |
| `scripts/find_dislocations.py` | Mine `market_state.csv` for phantom-drift episodes and which gate blocked them |
| `scripts/collect_signals.py` + `sweep_strategy.py` | Build a candidate set from recordings and sweep entry/exit rules offline |
| `scripts/backtest_*.py` | Year backtests over the cached klines / cycle parquet |

`FINDINGS.md` is the canonical research log — read it for the full evidence trail behind every default.

---

## Testing & verification

```bash
python -m pytest -q tests      # 147 tests: math, execution, menu, maker, regressions
python scripts/gate.py         # P0 gate -> "RECEIPT verified checks=5/5"
```

The gate runs unit tests, replay determinism on a frozen fixture, and a loosened-profile replay that
proves the full signal→scoring path still fires deterministically. Pure math in `drift_math.py` is
unit-tested with planted-parameter recovery and no-lookahead asserts. Every nontrivial change gets a
paper smoke before it's claimed to work.

---

## Going live

Do this in order. Do **not** skip to the end.

1. `python scripts/check_fees.py` — confirm the fee is still `0.07` and makers are exempt.
2. Run **paper** for several cycles: `python poly_drift.py --series btc-updown-5m --mode paper --maker`.
   Watch `paper_trades.csv` (settlements) and `maker_latency.csv` (post→ack latency).
3. Paper fills are **queue-aware and conservative** — treat the fill *count* as a floor, not a
   forecast. Without the trade tape it's still a simulation.
4. One **live** cycle at the 5-share minimum to verify the two premises real money depends on:
   **makers actually pay zero** (`makerBaseFee` is unverified against a real fill) and **you actually
   get filled**. The bot alarms automatically if the fee premise breaks.
5. Only then size up.

**Live arming is human-only.** Set `POLY_DRIFT_ARM=YES-LIVE`, provide `.env` credentials, and type
`ARM` at startup. The bot will never arm itself.

---

*This is a personal research project, not financial advice. Prediction markets are risky and most
short-term crypto strategies lose money after costs. Trade only what you can afford to lose.*
