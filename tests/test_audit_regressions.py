# tests/test_audit_regressions.py — one test per bug found in the 2026-07-22 code audit.
# Each name states the invariant, and each docstring records the failure it prevents.
import os, sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('MPLBACKEND', 'Agg')
import poly_drift as pdft
import replay
from drift_math import realized_sigma_per_s


# ---------------- A1: volatility across dead-feed gaps ----------------
def test_sigma_does_not_book_a_gap_jump_as_a_one_second_move():
    """The old estimator compacted NaNs, so a 30s venue outage spanning a 1% move was measured
    as a single 1-second return: sigma came out ~8x true, collapsing every P_up onto 0.5."""
    rng = np.random.default_rng(7)
    true_sigma = 0.00012
    path = 65900 * np.exp(np.cumsum(rng.normal(0, true_sigma, 600)))
    clean = realized_sigma_per_s(path, 120)
    holed = path.copy(); holed[200:260] = np.nan          # 60s blackout mid-window
    assert clean is not None
    assert realized_sigma_per_s(holed, 120) == pytest.approx(clean, rel=1e-9)


def test_sigma_is_none_rather_than_wrong_when_the_feed_is_dark():
    x = np.concatenate([np.full(10, 100.0), np.full(130, np.nan)])
    assert realized_sigma_per_s(x, 120) is None


# ---------------- A2: never invent a strike ----------------
def _cycle(now, open_ts, end_ts, px=65900.0):
    """Cold module state + a live spot feed, then one real compute() tick."""
    an = replay.reset_state()
    pdft.CFG['STRIKE_MAX_LAG_S'] = 10
    for n in pdft.GROUPS['spot']:
        for t in range(int(now) - 300, int(now)):
            pdft.FEEDS[n].on_trade(float(t), px, 1000.0)
    pdft.ROLL.update(token='t', slug='s', end=end_ts, open=open_ts,
                     strike=None, strike_ref=None)
    an.compute(now=now)
    return an


def test_strike_is_captured_when_we_saw_the_cycle_open():
    now = 1000000.0
    _cycle(now, open_ts=now - 2, end_ts=now + 298)
    assert pdft.ROLL['strike'] == pytest.approx(65900.0)


def test_strike_is_refused_when_the_bot_started_mid_cycle():
    """Starting at 15:03 inside a 15:00-15:05 cycle used to set strike = the 15:03 price, so a
    deep in-the-money cycle was priced as at-the-money for its entire remaining life."""
    now = 1000000.0
    an = _cycle(now, open_ts=now - 180, end_ts=now + 120)   # 3 minutes late
    assert pdft.ROLL['strike'] is None
    assert an._model_p_win('BUY', now) is None              # and therefore never trades


def test_unknown_cycle_open_is_treated_as_infinite_lag():
    now = 1000000.0
    _cycle(now, open_ts=None, end_ts=now + 120)
    assert pdft.ROLL['strike'] is None


# ---------------- A3: replay must start cold ----------------
def test_reset_state_clears_every_cross_cycle_global():
    """A leftover strike/end from the previous replay priced the next recording against the
    wrong cycle — wrong, and non-deterministic in a way that depends on run order."""
    pdft.ROLL.update(token='old', token_no='oldno', slug='old-slug', end=1.7e9,
                     open=1.7e9 - 300, strike=65000.0, strike_ref='spot')
    replay.reset_state()
    for k in ('token', 'token_no', 'slug', 'end', 'open', 'strike', 'strike_ref'):
        assert pdft.ROLL[k] is None, f'ROLL[{k!r}] leaked across reset_state()'
    assert pdft.EXECUTOR is None


def test_loosened_profile_does_not_leak_into_the_next_replay():
    replay.reset_state(loosen=True)
    assert pdft.CFG['MIN_EDGE'] == 0.0 and pdft.CFG['POLY_STALE_S'] == 9999
    replay.reset_state(loosen=False)
    assert pdft.CFG['MIN_EDGE'] == replay._CFG_DEFAULTS['MIN_EDGE']
    assert pdft.CFG['POLY_STALE_S'] == replay._CFG_DEFAULTS['POLY_STALE_S']


# ---------------- A4: pending signals are never silently lost ----------------
def test_pending_signal_is_retried_not_dropped_when_no_quote_exists(monkeypatch):
    """`continue` skipped the re-queue, so the signal vanished from pending without ever being
    tallied — biasing the hit rate toward whichever signals happened to have a quote."""
    now = 1000.0
    an = pdft.Analytics()
    pdft.ROLLS.clear()
    pdft.FEEDS['poly'] = pdft.Feed('poly')                  # no quotes at all
    an.pending.append(dict(ts=now, strat='drift', side='BUY', exec_px=0.4, slug='s',
                           eval_ts=now, at_close=False))
    an._eval_pending(now + 1)
    assert len(an.pending) == 1, 'signal was dropped instead of retried'
    assert an.tally['n'] == 0
    for i in range(40):                                     # bounded: it must not retry forever
        an._eval_pending(now + 2 + i)
    assert len(an.pending) == 0


# ---------------- A5: research stats must describe the strategy we actually ran ----------------
def test_early_exit_is_scored_at_the_exit_price_not_at_resolution(monkeypatch):
    """With EXIT_ENABLE on, a scalp closed 20s in was still tallied as if held to resolution,
    so the reported hit-rate belonged to a strategy nobody was running."""
    now = 1000.0
    an = replay.reset_state()
    monkeypatch.setitem(pdft.CFG, 'EXIT_ENABLE', 1)
    monkeypatch.setitem(pdft.CFG, 'EXIT_TP_C', 2.0)
    monkeypatch.setitem(pdft.CFG, 'EXIT_MIN_HOLD_S', 0)
    pdft.POLY_BOOK.bids = {0.60: 500}; pdft.POLY_BOOK.asks = {0.62: 500}
    pdft.ROLLS.clear()

    sig = dict(ts=now, strat='drift', side='BUY', exec_px=0.50, slug='s',
               at_close=True, eval_ts=now + 300)
    an.pending.append(sig)
    pos = dict(slug='s', side='BUY', shares=2.0, entry=0.50, cost=1.0, ts=now,
               token='t', sig=sig)

    class _Ex:
        stake = 1.0
        def __init__(self): self.positions = [pos]
        def exit_position(self, p, reason):
            self.positions.remove(p); return True, 'ok'
    monkeypatch.setattr(pdft, 'EXECUTOR', _Ex())

    an._manage_exits(now + 1)
    assert sig['exit_px'] == 0.60 and sig['at_close'] is False
    an._eval_pending(now + 2)
    assert an.tally['n'] == 1
    assert an.tally['pnl_c'] == pytest.approx(10.0)   # 0.60 - 0.50 = +10c, not a 0/1 payout


def test_compute_publishes_stats_and_drift_together():
    """The plot thread reads AN.snap concurrently; it must never see stats without drift."""
    now = 1000000.0
    an = _cycle(now, open_ts=now - 2, end_ts=now + 298)
    assert ('stats' in an.snap) == ('drift' in an.snap)


# ---------------- A10: reward:risk — expensive shares must earn their keep ----------------
def _drift(now, bb, ba, p_up, **cfg):
    """Drive the real _signal with a controlled model probability and book."""
    an = replay.reset_state()
    pdft.CFG.update(pdft.PRESET_5M)
    base = dict(MIN_EDGE=0.03, RT_GATE=0, MIN_MODEL_P=0.0, EXIT_ENABLE=0,
                MIN_EDGE_ROI=0.12, RANK_BY_ROI=1,
                FAIR_SOURCE='model')   # p_up is injected below, so use it as the fair value
    base.update(cfg)                       # caller overrides win
    pdft.CFG.update(base)
    pdft.POLY_BOOK.bids = {bb: 5000.0}; pdft.POLY_BOOK.asks = {ba: 5000.0}
    pdft.FEEDS['poly'].quotes.append((now, (bb + ba) / 2))
    pdft.ROLL.update(token='Y', token_no='N', slug='s', end=now + 180,
                     open=now - 120, strike=100.0, strike_ref='spot')
    an._model_p_win = lambda side, t: p_up if side == 'BUY' else 1 - p_up
    stats = {'spot': dict(lag=1, corr=0.9, beta=1.0, vps=1e3, vol_z=2.0,
                          ref_move=1.0, poly_move=0.1, dev=0.0)}
    an._signal(stats, {'spot': 3.0}, now)
    return an


def test_expensive_share_with_a_thin_edge_is_rejected():
    """The live trade: BUY at 0.88 on P_up 0.945. +6.5c clears the flat 3c floor, but it is a
    7.4% return against a 100% downside — risking $7.30 to make $1."""
    an = _drift(1000.0, bb=0.87, ba=0.88, p_up=0.945)
    assert len(an.signals) == 0
    assert an.last_gate == 'reward_risk'


def test_expensive_share_still_trades_on_a_big_enough_edge():
    an = _drift(1000.0, bb=0.87, ba=0.88, p_up=0.995)      # 11.5c on 0.88 = 13% ROI
    assert len(an.signals) == 1


def test_same_edge_is_accepted_when_the_share_is_cheap():
    """Identical +6.5c edge, cheap side: 21.7% return on capital instead of 7.4%."""
    an = _drift(1000.0, bb=0.29, ba=0.30, p_up=0.365)
    assert len(an.signals) == 1 and an.signals[0]['side'] == 'BUY'


def test_roi_gate_can_be_switched_off():
    an = _drift(1000.0, bb=0.87, ba=0.88, p_up=0.945, MIN_EDGE_ROI=0.0)
    assert len(an.signals) == 1


def test_only_one_side_can_ever_show_positive_edge():
    """edge_up + edge_dn = (p - ba) + ((1-p) - (1-bb)) = bb - ba = -spread, always negative.
    Documents why side RANKING never decides a trade — only the per-side floors do. Guards
    against a future 'improvement' that assumes the two sides compete."""
    for bb, ba, p in ((0.30, 0.70, 0.50), (0.87, 0.88, 0.945), (0.29, 0.30, 0.365)):
        edge_up, edge_dn = p - ba, (1 - p) - (1 - bb)
        assert edge_up + edge_dn == pytest.approx(bb - ba)
        assert not (edge_up > 0 and edge_dn > 0)


# ---------------- A11: a vol spike must not leave the model overconfident ----------------
def test_vol_spike_pulls_an_extreme_probability_toward_a_coin_flip(monkeypatch):
    """Trailing 120s vol is a bad forecast of the next tau when vol is accelerating, and P_up is
    most sensitive to sigma exactly where we pay the most."""
    now = 1000000.0
    an = replay.reset_state()
    rng = np.random.default_rng(3)
    calm = np.cumsum(rng.normal(0, 0.00005, 250))          # quiet first, then a burst
    burst = calm[-1] + np.cumsum(rng.normal(0, 0.00040, 50))
    path = 100.0 * np.exp(np.concatenate([calm, burst]))
    an.snap = {'px': {'spot': path * (100.0 * 1.0012 / path[-1])}}
    pdft.ROLL.update(strike=100.0, strike_ref='spot', end=now + 150, open=now - 150)

    pdft.CFG.update(VOL_SPIKE_GUARD=0, VOL_LOOKBACK_S=120, VOL_FAST_S=30)
    unguarded = an._model_p_win('BUY', now)
    pdft.CFG['VOL_SPIKE_GUARD'] = 1
    guarded = an._model_p_win('BUY', now)
    assert guarded < unguarded, 'spike guard must not increase confidence'
    assert abs(guarded - 0.5) < abs(unguarded - 0.5)


# ---------------- A12: per-tick cost must not grow with uptime ----------------
def test_feed_history_is_bounded_by_time_not_just_count(monkeypatch):
    """compute() converts each feed deque with np.asarray every tick, so cost is O(len).
    maxlen=400_000 bounds memory but not time: measured 13 ms/tick at 2.7k quotes and
    1696 ms at 400k, past the 1s analytics loop -- and compute() runs ON the asyncio thread,
    so overrunning also stalls the WebSocket readers."""
    now = 1_000_000.0
    an = replay.reset_state()
    pdft.CFG.update(pdft.PRESET_5M)
    monkeypatch.setitem(pdft.CFG, 'FEED_KEEP_S', 900)
    monkeypatch.setitem(pdft.CFG, 'STATE_LOG', 0)
    f = pdft.FEEDS['bin-spot']
    for t in range(int(now) - 20_000, int(now)):        # 5.5 hours of 1/s ticks
        f.on_trade(float(t), 65000.0, 100.0)
    assert len(f.quotes) == 20_000
    an.compute(now=now)
    assert len(f.quotes) <= 902, f'feed not trimmed: {len(f.quotes)}'


def test_trim_keeps_one_quote_before_the_window():
    """grid_series forward-fills from the last price at or before the window start. Dropping
    that quote blanks the head of the grid and silently shortens every statistic."""
    # invariant: the head is always the NEWEST quote at or before the cutoff (the forward-fill
    # source for grid[0]); everything inside the window is kept; older quotes are dropped.
    f = pdft.Feed('x')
    for ts in (100.0, 950.0, 960.0):     # only 100 predates the cutoff
        f.on_trade(ts, 1.0, 1.0)
    f.trim(now=1000.0, keep_s=100)       # cutoff 900
    assert [q[0] for q in f.quotes] == [100.0, 950.0, 960.0]

    f = pdft.Feed('x')
    for ts in (100.0, 500.0, 800.0, 950.0, 960.0):   # three predate the cutoff
        f.on_trade(ts, 1.0, 1.0)
    f.trim(now=1000.0, keep_s=100)
    assert [q[0] for q in f.quotes] == [800.0, 950.0, 960.0]   # 100 and 500 are redundant


def test_early_return_ticks_do_not_log_a_stale_fair_value(monkeypatch, tmp_path):
    """_write_state runs on the no_poly/no_ref paths too. Without clearing, it stamps the
    PREVIOUS tick's fair/dev/scenario with this tick's timestamp -- indistinguishable from
    live data in the research CSV."""
    now = 1_000_000.0
    an = replay.reset_state()
    monkeypatch.setitem(pdft.CFG, 'STATE_LOG', 0)
    an.fair = dict(fair=0.9, dev=-1.0, scenario='fade', ref='spot', st=None, model_p=None)
    an.compute(now=now)                   # no feeds -> bails at no_poly
    assert an.last_gate in ('no_poly', 'no_ref')
    assert an.fair is None, 'stale fair survived an early-return tick'


# ---------------- A13: the reversion assumption must be calibratable ----------------
def test_revert_frac_scales_the_fair_value_toward_the_mid():
    """fair_from_mid undoes the WHOLE unjustified move (100% reversion). Measured reversion is
    3-15%, so the raw edge is inflated ~7-30x and RT_GATE compares it against a real cost.
    REVERT_FRAC has to be able to bring the two onto the same scale."""
    now = 1_000_000.0
    an = replay.reset_state()
    pdft.POLY_BOOK.bids = {0.50: 500.0}; pdft.POLY_BOOK.asks = {0.52: 500.0}
    stats = {'spot': dict(lag=1, corr=0.9, beta=1.0, vps=1e3, vol_z=2.0,
                          ref_move=1.0, poly_move=0.1, dev=-1.0)}
    pdft.CFG.update(FAIR_SOURCE='drift', MIN_CORR=0.15, CORR_SIGN=0)
    mid = 0.51

    pdft.CFG['REVERT_FRAC'] = 1.0
    full = an._fair_value(stats, now)['fair']
    pdft.CFG['REVERT_FRAC'] = 0.10
    tenth = an._fair_value(stats, now)['fair']
    pdft.CFG['REVERT_FRAC'] = 0.0
    none_ = an._fair_value(stats, now)['fair']

    assert full > tenth > mid, 'scaling must pull fair toward the mid, not past it'
    assert abs(none_ - mid) < 1e-9, 'zero reversion means fair IS the mid'
    # and it must be monotonic, so signal ORDER is preserved and only the scale changes
    assert (full - mid) > (tenth - mid) > 0
    pdft.CFG['REVERT_FRAC'] = 1.0
