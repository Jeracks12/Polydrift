# Execution and chart UI notes

## 2026-07-19 - live-order path repair

- `poly_drift.main()` must declare `global EXECUTOR`. `Analytics._signal()` reads
  the module-global executor; without that declaration, selecting `--mode paper`
  or `--mode live` constructed an executor only in `main`'s local scope. Signals
  still drew chart arrows and were logged, but no order was passed to
  `Executor.on_signal()`.
- A green/red arrow is a strategy signal, not proof of a Polymarket order or fill.
  Check console lines beginning `[exec]` and the executor's order response when
  diagnosing execution.
- `Analytics._eval_pending()` records a 30-second mark-to-mid result. It is not
  market settlement and can be negative while the 5-minute market is still live.
  The chart and console now label this explicitly as `30s mark ... (not settlement)`.
- The Matplotlib chart has `BUY YES market` and `BUY NO market` controls. They
  call `Executor.manual_order()` and therefore reuse the normal stake, book,
  kill-switch, and one-position-per-market-side protections. `BUY NO` is the
  strategy's historical `SELL` direction: it buys the NO token rather than
  short-selling YES.
- Buttons are intentionally inactive in `--mode off`; start in `--mode paper`
  first to test the path, or `--mode live` to submit a real order. Live startup
  remains double-confirmed and requires the signer private key plus the correct
  signature type/funder configuration for proxy accounts.

## 2026-07-19 — two strategies, feasibility gate, per-strategy stats

- **`main()` must declare `global EXECUTOR`** — this was LOST/REVERTED AGAIN and re-applied
  2026-07-19. It has now regressed twice; if paper/live stops trading, check this line first.
- **Two strategies now run** with separate tallies (`AN.tally['drift']` / `['fav']`), shown in the
  console report and chart title as `drift NW/NL ...  fav NW/NL ...`. Chart markers: drift ▲/▼
  (lime/red), favorite gold ★. See `FINDINGS.md` §3 for the full gate chains.
  - drift = lead/lag scalp (`_signal`), scored at +EVAL_H_S mark-to-mid.
  - favorite = buy the 0.60–0.70 favorite at ~2 min left, exchange-confirmed vs the captured
    strike (`_favorite_signal`), HELD TO RESOLUTION, scored at market close.
- **τ-feasibility gate for drift** (`MIN_MODEL_P=0.20`, `Analytics._model_p_win`): skips a drift
  entry when the exchange binary model gives the traded side < 0.20 win prob — i.e. a cheap
  underdog that cannot realistically cross the strike in the time left. Prints `[skip] ...`.
  Uses `drift_math.binary_p_up` + `realized_sigma_per_s`. Only active in --series mode (needs the
  strike/cycle-end captured in `ROLL['strike'|'strike_ref'|'end']`).
- **Every signal force-closes at its market's roll** (`_eval_pending` clamps eval to the roll) —
  no signal outlives its cycle. Favorites therefore always score at resolution.
- `signals_outcomes.csv` gained a `strat` column; `replay.py` metrics expose `drift_n`/`fav_n`.

## Regression coverage

- `tests/test_menu.py::test_main_publishes_executor_for_the_signal_engine` — global-executor wiring.
- `tests/test_execution.py::test_manual_order_uses_current_bbo_and_risk_path` — manual BBO + dedup.
- `tests/test_favorite.py` — favorite gates (band/time/confirm/one-per-cycle), per-strategy tally
  separation, close-at-roll, and the drift τ-feasibility veto (end-to-end through `_signal`).
- `tests/test_math.py` — binary pricer + realized-sigma.
- Current: `python -m pytest -q` -> **42 passed**; `python scripts/gate.py` -> `RECEIPT verified 4/4`.
