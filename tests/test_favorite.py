# tests/test_favorite.py — drift (model-as-signal) gates, exit management, execution guards.
# (Favorite strategy removed 2026-07-21; file kept to preserve the shared test fixtures.)
import os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('MPLBACKEND', 'Agg')
import poly_drift as pdft


def spot_series(bp_from_strike, n=300, sigma=7.4e-5, seed=11):
    """Realistic composite series (BTC-like sigma) ending `bp_from_strike` from strike=100."""
    rng = np.random.default_rng(seed)
    walk = np.cumsum(rng.normal(0, sigma, n))
    return 100.0 * (1 + bp_from_strike / 1e4) * np.exp(walk - walk[-1])


def dev_for(mid, fair):
    """Residual that makes the exchange-implied fair value come out at `fair`.
    fair_from_mid(mid, dev) = expit(logit(mid) - dev), so dev = logit(mid) - logit(fair).
    Negative dev = poly fell without the exchange following (too cheap -> buy UP)."""
    return float(pdft.logit(np.array([mid]))[0] - pdft.logit(np.array([fair]))[0])


def drift_stats(mid, fair, corr=0.9, lag=1, vol_z=2.0, scenario='catchup'):
    """A `stats` entry the new _signal can price off. `scenario` sets which of ref_move /
    poly_move dominates, which is what classifies catchup vs fade."""
    rm, pm = (1.0, 0.1) if scenario == 'catchup' else (0.1, 1.0)
    return {'spot': dict(lag=lag, corr=corr, beta=1.0, vps=1000.0, vol_z=vol_z,
                         ref_move=rm, poly_move=pm, dev=dev_for(mid, fair))}


def fresh(now):
    """Analytics with a live book/roll/snapshot, gates at their production defaults."""
    pdft.EXECUTOR = None
    pdft.CFG.update(pdft.PRESET_5M)
    pdft.CFG.update(MIN_EDGE=0.03, RT_GATE=0, MIN_MODEL_P=0.20, EXIT_ENABLE=0, CALIB_SLOPE=0.9,
                    FAIR_SOURCE='drift', SCENARIO_MODE='both', MIN_EDGE_ROI=0.0,
                    MODEL_VETO=0.0, FADE_TAU_MIN=0, FADE_TAU_MAX=0)
    pdft.ROLLS.clear()
    pdft.POLY_BOOK.bids = {0.30: 500.0}; pdft.POLY_BOOK.asks = {0.32: 500.0}
    pdft.FEEDS['poly'] = pdft.Feed('poly')
    pdft.FEEDS['poly'].quotes.append((now, 0.31))
    pdft.ROLL.update(token='Y', token_no='N', slug='btc-updown-5m-X',
                     end=now + 180, strike=100.0, strike_ref='spot')
    an = pdft.Analytics(siglog=None)
    an.snap = {'px': {'spot': spot_series(bp_from_strike=60.0)}}
    return an


def fire_drift(now, side='BUY', end=None, bp=None, book=None, fair=None,
               corr=0.9, lag=1, vol_z=2.0, rz=3.0, scenario='catchup', cfg=None):
    """Drive the real drift _signal with everything set up to fire, then flex one input.
    Direction now comes from the exchange-implied fair value vs the book, not from a strike."""
    an = fresh(now)
    if end is None: end = now + 180
    if book is None: book = (0.30, 0.32) if side == 'BUY' else (0.68, 0.70)
    bb, ba = book
    mid = (bb + ba) / 2
    # fair far enough past the paid side to clear MIN_EDGE on the intended leg
    if fair is None: fair = (ba + 0.15) if side == 'BUY' else (bb - 0.15)
    pdft.POLY_BOOK.bids = {bb: 500.0}; pdft.POLY_BOOK.asks = {ba: 500.0}
    pdft.FEEDS['poly'] = pdft.Feed('poly'); pdft.FEEDS['poly'].quotes.append((now, mid))
    if bp is None: bp = 60.0 if side == 'BUY' else -60.0
    an.snap = {'px': {'spot': spot_series(bp_from_strike=bp)}}
    pdft.ROLL.update(token='Y', token_no='N', slug='btc-updown-5m-X',
                     end=end, strike=100.0, strike_ref='spot')
    if cfg: pdft.CFG.update(cfg)          # AFTER fresh(), which resets the gates
    an._signal(drift_stats(mid, fair, corr, lag, vol_z, scenario), {'spot': rz}, now)
    return an


# ---- drift signal (model = fair value, lead/lag = timing) ----
def test_drift_fires_on_exchange_implied_edge_plus_timing():
    an = fire_drift(1000.0)
    assert len(an.signals) == 1
    s = an.signals[0]
    assert s['strat'] == 'drift' and s['side'] == 'BUY'
    assert s['at_close'] is True and s['eval_ts'] == pdft.ROLL['end']
    assert s['fair_px'] > s['ba']                  # fair beats the ask we'd pay
    assert s['scenario'] == 'catchup'


def test_drift_sell_side_fires():
    an = fire_drift(1000.0, side='SELL')
    assert len(an.signals) == 1 and an.signals[0]['side'] == 'SELL'


def test_drift_blocked_inside_entry_cutoff():
    an = fire_drift(1000.0, end=1000.0 + 25)       # 25s left < ENTRY_CUTOFF_S
    assert len(an.signals) == 0


def test_drift_blocked_weak_trigger():
    an = fire_drift(1000.0, rz=1.0)                # |robust z| below DRIFT_Z_ALERT
    assert len(an.signals) == 0


def test_catchup_blocked_when_poly_leads():
    an = fire_drift(1000.0, lag=-3)               # ref does NOT lead -> poly is fresh, not stale
    assert len(an.signals) == 0


def test_fade_setup_fires_when_poly_moved_alone():
    """Scenario 2: poly dumped, the exchange did not follow. The OLD REF_DRIVEN gate made this
    structurally impossible - it required |ref_move| >= |poly_move|, the opposite of a fade."""
    an = fire_drift(1000.0, book=(0.54, 0.55), fair=0.78, scenario='fade')
    assert len(an.signals) == 1
    assert an.signals[0]['side'] == 'BUY' and an.signals[0]['scenario'] == 'fade'


def test_scenario_mode_can_restrict_to_one_setup():
    an = fire_drift(1000.0, book=(0.54, 0.55), fair=0.78, scenario='fade',
                    cfg=dict(SCENARIO_MODE='catchup'))
    assert len(an.signals) == 0 and an.last_gate == 'scenario_fade'
    an = fire_drift(1000.0, scenario='catchup', cfg=dict(SCENARIO_MODE='fade'))
    assert len(an.signals) == 0 and an.last_gate == 'scenario_catchup'


def test_fade_ignores_the_lead_requirement_but_catchup_does_not():
    """In a fade poly moved FIRST, so lag<0 is expected and must not block it. In a catchup,
    'poly is stale' is unproven unless the exchange actually leads."""
    an = fire_drift(1000.0, book=(0.54, 0.55), fair=0.78, scenario='fade', lag=-3)
    assert len(an.signals) == 1
    an = fire_drift(1000.0, lag=-3, scenario='catchup')
    assert len(an.signals) == 0 and an.last_gate == 'no_leading_ref'


def test_rt_gate_requires_edge_above_live_spread():
    """Required edge = max(MIN_EDGE floor, live spread). Control P(up) to isolate the gate."""
    now = 1000.0

    def run(target_edge, rt_gate):
        an = fresh(now)
        pdft.POLY_BOOK.bids = {0.30: 500}; pdft.POLY_BOOK.asks = {0.34: 500}   # 4c spread, ba=0.34
        pdft.FEEDS['poly'] = pdft.Feed('poly'); pdft.FEEDS['poly'].quotes.append((now, 0.32))
        pdft.ROLL.update(strike=100.0, strike_ref='spot', end=now + 180,
                         token='Y', token_no='N', slug='s')
        # MIN_EDGE_ROI off: this test isolates the ROUND-TRIP floor, and the reward:risk
        # floor (0.12 * 0.34 = 0.041) would otherwise bind first and mask it.
        pdft.CFG.update(MIN_EDGE=0.03, MIN_MODEL_P=0.0, RT_GATE=rt_gate, MIN_EDGE_ROI=0.0,
                        RT_SPREAD_MULT=1.0, RT_FEE_C=0.0, RT_BUFFER_C=0.5,
                        FAIR_SOURCE='model')   # injecting P(up) directly, so use it as fair
        pup = 0.34 + target_edge                                # edge_up = pup - ask = target
        an._model_p_win = lambda side, t: pup if side == 'BUY' else 1 - pup
        stats = {'spot': dict(lag=1, corr=0.9, beta=1.0, vps=1e3, vol_z=2.0,
                              ref_move=1.0, poly_move=0.1)}
        an._signal(stats, {'spot': 3.0}, now)
        return len(an.signals)

    assert run(0.035, rt_gate=1) == 0    # clears 3c floor but NOT the 4.5c live spread
    assert run(0.050, rt_gate=1) == 1    # clears the spread-driven requirement
    assert run(0.035, rt_gate=0) == 1    # RT off -> only the fixed floor
    pdft.CFG.update(MIN_MODEL_P=0.20, RT_GATE=0, MIN_EDGE_ROI=0.12)


def test_settles_at_cycle_close_when_known():
    an = fire_drift(1000.0, end=1000.0 + 240)
    assert len(an.signals) == 1
    assert an.signals[0]['eval_ts'] == pdft.ROLL['end'] and an.signals[0]['at_close'] is True


# ---- model / pricing helpers ----
def test_tradable_guard_blocks_stale_book_and_late_entry():
    now = 1000.0
    an = fresh(now)
    assert an._tradable(now) is True
    pdft.FEEDS['poly'].quotes.clear(); pdft.FEEDS['poly'].quotes.append((now - 60, 0.31))
    assert an._tradable(now) is False
    pdft.FEEDS['poly'].quotes.clear(); pdft.FEEDS['poly'].quotes.append((now, 0.31))
    pdft.ROLL['end'] = now + 20
    assert an._tradable(now) is False


def test_model_p_win_feasibility_by_time_left():
    now = 1000.0
    an = fresh(now)
    px = 100.0 * (1 - 0.004) * np.exp(np.cumsum(np.random.default_rng(0).normal(0, 4e-4, 300)))
    px = px * (100.0 * (1 - 0.004) / px[-1])       # end exactly 40bp below strike
    an.snap = {'px': {'spot': px}}
    pdft.ROLL.update(strike=100.0, strike_ref='spot', end=now + 60)
    p1 = an._model_p_win('BUY', now)
    assert p1 is not None and p1 < 0.5
    pdft.ROLL['end'] = now + 240                   # more time -> reverts toward 0.5
    assert an._model_p_win('BUY', now) > p1


def test_model_p_win_calibration_pulls_tails_in():
    now = 1000.0
    an = fresh(now)
    an.snap = {'px': {'spot': spot_series(bp_from_strike=40.0)}}
    pdft.ROLL.update(strike=100.0, strike_ref='spot', end=now + 120)
    pdft.CFG['CALIB_SLOPE'] = 1.0
    raw = an._model_p_win('BUY', now)
    pdft.CFG['CALIB_SLOPE'] = 0.9
    cal = an._model_p_win('BUY', now)
    assert cal < raw and cal > 0.5                 # slope<1 pulls an extreme prob toward 0.5


def test_feasibility_gate_absent_without_strike():
    now = 1000.0
    an = fresh(now)
    pdft.ROLL['strike'] = None
    assert an._model_p_win('BUY', now) is None


def test_paid_price_is_ask_for_buy_and_one_minus_bid_for_sell():
    assert pdft.Analytics.paid_price('BUY', 0.30, 0.34) == 0.34
    assert abs(pdft.Analytics.paid_price('SELL', 0.70, 0.72) - 0.30) < 1e-12


def test_round_trip_cost_scales_with_spread():
    pdft.CFG.update(RT_SPREAD_MULT=1.0, RT_FEE_C=0.0, RT_BUFFER_C=0.5)
    assert abs(pdft.Analytics._round_trip_cost(0.50, 0.51) - 0.015) < 1e-9
    assert abs(pdft.Analytics._round_trip_cost(0.50, 0.54) - 0.045) < 1e-9
    assert pdft.Analytics._round_trip_cost(0.50, 0.50) is None


def test_walk_price_reveals_slippage_beyond_top_of_book():
    pdft.POLY_BOOK.bids = {}; pdft.POLY_BOOK.asks = {0.43: 1.0, 0.50: 5.0, 0.55: 100.0}
    eff = pdft.Analytics._walk_price(no_side=False, stake=1.0)
    assert eff is not None and 0.45 < eff < 0.55
    pdft.POLY_BOOK.asks = {0.43: 0.5}
    assert pdft.Analytics._walk_price(no_side=False, stake=100.0) is None


def _thin_book_edge_setup(an, now):
    an.snap = {'px': {'spot': spot_series(bp_from_strike=0.0)}}
    pdft.ROLL.update(strike=100.0, strike_ref='spot', end=now + 180)
    # only $0.40 rests at the 0.40 ask; a $1 order sweeps up to 0.48
    pdft.POLY_BOOK.bids = {0.30: 500}; pdft.POLY_BOOK.asks = {0.40: 1.0, 0.48: 500}


def test_model_edge_uses_effective_fill_not_quote():
    now = 1000.0
    an = fresh(now)
    _thin_book_edge_setup(an, now)

    class _Ex: stake = 1.0; positions = []
    pdft.EXECUTOR = _Ex()
    try:
        eff_edge = an._model_edge('BUY', 0.30, 0.40, now)
    finally:
        pdft.EXECUTOR = None
    p_up = an._model_p_win('BUY', now)
    assert eff_edge < p_up - 0.40          # scored on the swept fill, never the 0.40 quote


def test_research_mode_prices_the_same_fill_as_the_executor():
    """--mode off must not flatter itself. It used to fall back to top-of-book because no
    executor existed, so research edges were better than the paper/live fills they predicted."""
    now = 1000.0
    an = fresh(now)
    _thin_book_edge_setup(an, now)

    class _Ex: stake = pdft.CFG['RESEARCH_STAKE']; positions = []
    pdft.EXECUTOR = _Ex()
    try:
        with_exec = an._model_edge('BUY', 0.30, 0.40, now)
    finally:
        pdft.EXECUTOR = None
    research = an._model_edge('BUY', 0.30, 0.40, now)
    assert abs(with_exec - research) < 1e-12


# ---- resolution scoring + close-at-roll (flat tally) ----
def test_resolution_pnl_at_market_close_not_mark_to_mid():
    now = 1000.0
    an = fresh(now)
    pdft.FEEDS['poly'] = pdft.Feed('poly')
    pdft.FEEDS['poly'].quotes.extend([(now, 0.50), (now + 100, 0.99)])   # resolves UP
    pdft.ROLLS.clear()
    an.pending.append(dict(ts=now, strat='drift', side='SELL', exec_px=0.30, slug='s',
                           at_close=True, eval_ts=now + 100))
    an._eval_pending(now + 120)
    # SELL @0.30 (short YES) with UP winning -> loses 1-0.30 = -70c
    assert an.tally['n'] == 1 and an.tally['win'] == 0
    assert abs(an.tally['pnl_c'] + 70.0) < 1e-9


def test_signal_closes_at_market_roll_not_after():
    now = 1000.0
    an = fresh(now)
    pdft.FEEDS['poly'] = pdft.Feed('poly')
    pdft.FEEDS['poly'].quotes.extend([(now + 10, 0.20), (now + 90, 0.99)])
    pdft.ROLLS.clear(); pdft.ROLLS.append(now + 60)
    an.pending.append(dict(ts=now, strat='drift', side='BUY', exec_px=0.65,
                           slug='s', at_close=True, eval_ts=now + 120))
    an._eval_pending(now + 130)
    assert an.tally['n'] == 1 and an.tally['win'] == 0   # scored at pre-roll 0.20, not 0.99


# ---- exit management ----
class _FakeExec:
    def __init__(self, positions): self.positions = positions; self.exited = []
    def exit_position(self, pos, reason):
        self.exited.append((pos, reason)); self.positions.remove(pos)
        return True, f'exit {reason}'


def test_manage_exits_off_by_default():
    now = 1000.0
    an = fresh(now)
    pdft.EXECUTOR = _FakeExec([dict(side='BUY', entry=0.30, ts=now - 60, slug='s')])
    pdft.POLY_BOOK.bids = {0.50: 100}; pdft.POLY_BOOK.asks = {0.51: 100}
    pdft.CFG['EXIT_ENABLE'] = 0
    an._manage_exits(now)
    assert pdft.EXECUTOR.exited == []
    pdft.EXECUTOR = None


def test_manage_exits_take_profit_fires():
    now = 1000.0
    an = fresh(now)
    pos = dict(side='BUY', entry=0.30, ts=now - 60, slug='s')
    pdft.EXECUTOR = _FakeExec([pos])
    pdft.POLY_BOOK.bids = {0.40: 100}; pdft.POLY_BOOK.asks = {0.41: 100}
    pdft.CFG.update(EXIT_ENABLE=1, EXIT_TP_C=2.0, EXIT_THESIS=0)
    an._manage_exits(now)
    assert pdft.EXECUTOR.exited and pdft.EXECUTOR.exited[0][1] == 'tp'
    pdft.CFG.update(EXIT_ENABLE=0); pdft.EXECUTOR = None


def test_manage_exits_thesis_invalidation_fires():
    now = 1000.0
    an = fresh(now)
    pos = dict(side='BUY', entry=0.30, ts=now - 60, slug='s')
    pdft.EXECUTOR = _FakeExec([pos])
    pdft.POLY_BOOK.bids = {0.305: 100}; pdft.POLY_BOOK.asks = {0.315: 100}
    an.snap = {'px': {'spot': spot_series(bp_from_strike=-5.0)}}
    pdft.ROLL.update(strike=100.0, strike_ref='spot', end=now + 120)
    pdft.CFG.update(EXIT_ENABLE=1, EXIT_TP_C=50.0, EXIT_THESIS=1)
    an._manage_exits(now)
    assert pdft.EXECUTOR.exited and pdft.EXECUTOR.exited[0][1] == 'thesis'
    pdft.CFG.update(EXIT_ENABLE=0, EXIT_TP_C=2.0); pdft.EXECUTOR = None


def test_manage_exits_anti_thrash_min_hold():
    now = 1000.0
    an = fresh(now)
    pos = dict(side='BUY', entry=0.30, ts=now - 1, slug='s')
    pdft.EXECUTOR = _FakeExec([pos])
    pdft.POLY_BOOK.bids = {0.90: 100}; pdft.POLY_BOOK.asks = {0.91: 100}
    pdft.CFG.update(EXIT_ENABLE=1, EXIT_TP_C=2.0, EXIT_MIN_HOLD_S=3)
    an._manage_exits(now)
    assert pdft.EXECUTOR.exited == []
    pdft.CFG.update(EXIT_ENABLE=0); pdft.EXECUTOR = None


def test_outcome_log_schema_versioning(tmp_path):
    sigp = tmp_path / "sig.csv"
    outp = tmp_path / "sig_outcomes.csv"
    outp.write_text("ts,strat,side,exec_px,eval_mid,eval_dt_s,pnl_c,win,slug\n")   # OLD schema
    an = pdft.Analytics(siglog=str(sigp))
    assert an.outlog != str(outp) and "sig_outcomes_" in an.outlog and os.path.exists(an.outlog)
    with open(an.outlog) as f:
        assert "order_ok" in f.readline()


def test_conviction_override_bypasses_timing_gate():
    """A huge model edge should trade on the model alone; the timing gate guards marginal
    estimates, not unambiguous ones. Default (0) keeps the gate in force."""
    now = 1000.0

    def run(edge_override):
        an = fresh(now)
        pdft.POLY_BOOK.bids = {0.18: 500}; pdft.POLY_BOOK.asks = {0.20: 500}
        pdft.FEEDS['poly'] = pdft.Feed('poly'); pdft.FEEDS['poly'].quotes.append((now, 0.19))
        pdft.ROLL.update(strike=100.0, strike_ref='spot', end=now + 180,
                         token='Y', token_no='N', slug='s')
        pdft.CFG.update(MIN_EDGE=0.03, MIN_MODEL_P=0.0, RT_GATE=0, EDGE_OVERRIDE=edge_override,
                        FAIR_SOURCE='model', MIN_EDGE_ROI=0.0)   # P(up) injected below
        an._model_p_win = lambda side, t: 0.95 if side == 'BUY' else 0.05   # edge_up = 0.75
        # timing gate deliberately UNSATISFIABLE: no ref leads poly
        stats = {'spot': dict(lag=-5, corr=-0.9, beta=1.0, vps=1e3, vol_z=-9,
                              ref_move=-1.0, poly_move=5.0)}
        an._signal(stats, {'spot': 0.0}, now)
        return len(an.signals)

    assert run(0.0) == 0        # override off -> timing gate blocks even a 75c edge
    assert run(0.15) == 1       # override on  -> model conviction trades it
    pdft.CFG.update(EDGE_OVERRIDE=0.0, MIN_MODEL_P=0.20)


def test_skip_message_names_price_cap_not_a_false_edge_claim():
    """Regression: 52% of live skips claimed 'edge < required' when the edge was fine and
    ENTRY_MAX_PX was the real blocker."""
    now = 1000.0
    an = fresh(now)
    pdft.POLY_BOOK.bids = {0.73: 500}; pdft.POLY_BOOK.asks = {0.74: 500}
    pdft.FEEDS['poly'] = pdft.Feed('poly'); pdft.FEEDS['poly'].quotes.append((now, 0.735))
    pdft.ROLL.update(strike=100.0, strike_ref='spot', end=now + 180,
                     token='Y', token_no='N', slug='s')
    pdft.CFG.update(MIN_EDGE=0.03, MIN_MODEL_P=0.0, RT_GATE=0, ENTRY_MAX_PX=0.45,
                    FAIR_SOURCE='model', MIN_EDGE_ROI=0.0)   # P(up) injected below
    an._model_p_win = lambda side, t: 0.95 if side == 'BUY' else 0.05    # edge_up = +0.21
    msgs = []
    import poly_drift
    orig = poly_drift.L_sig; poly_drift.L_sig = lambda m: msgs.append(m)
    try:
        an._signal({'spot': dict(lag=1, corr=0.9, beta=1.0, vps=1e3, vol_z=2.0,
                                 ref_move=1.0, poly_move=0.1, dev=0.0)}, {'spot': 3.0}, now)
    finally:
        poly_drift.L_sig = orig
    assert len(an.signals) == 0
    assert any('ENTRY_MAX_PX' in m for m in msgs), msgs      # names the true cause
    assert not any('edge +0.210 < required' in m for m in msgs)
    # with the cap at its new default the same setup trades
    an2 = fresh(now)                       # fresh() resets the gates, so re-apply after it
    pdft.CFG.update(ENTRY_MAX_PX=1.00, MIN_EDGE=0.03, MIN_MODEL_P=0.0, RT_GATE=0,
                    FAIR_SOURCE='model', MIN_EDGE_ROI=0.0)
    pdft.POLY_BOOK.bids = {0.73: 500}; pdft.POLY_BOOK.asks = {0.74: 500}
    pdft.FEEDS['poly'] = pdft.Feed('poly'); pdft.FEEDS['poly'].quotes.append((now, 0.735))
    pdft.ROLL.update(strike=100.0, strike_ref='spot', end=now + 180,
                     token='Y', token_no='N', slug='s')
    an2._model_p_win = lambda side, t: 0.95 if side == 'BUY' else 0.05
    an2._signal({'spot': dict(lag=1, corr=0.9, beta=1.0, vps=1e3, vol_z=2.0,
                              ref_move=1.0, poly_move=0.1, dev=0.0)}, {'spot': 3.0}, now)
    assert len(an2.signals) == 1 and abs(an2.signals[0]['exec_px'] - 0.74) < 1e-9
    pdft.CFG.update(MIN_MODEL_P=0.20)
