# tests/test_maker.py — the resting-quote engine. This module can place REAL orders, so the
# invariants that keep it from (a) crossing the spread, (b) quoting into informed flow, and
# (c) losing track of a live order are pinned here rather than left to a live run.
import os, sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from maker import MakerQuoter, round_to_tick, TICK, MIN_SHARES


class Book:
    def __init__(self, bids=None, asks=None):
        self.bids = dict(bids or {}); self.asks = dict(asks or {})


class FakeExec:
    mode = 'paper'
    killed = False
    client = None


CFG = {'ENTRY_CUTOFF_S': 40}


def mk(**kw):
    book = Book({0.49: 500}, {0.51: 500})
    roll = {'slug': 'btc-updown-5m-1', 'token': 'Y', 'token_no': 'N'}
    q = MakerQuoter(FakeExec(), book, roll, CFG,
                    latency_log=os.devnull, **kw)
    return q, book, roll


# ---------- tick rounding: the difference between a maker and a taker ----------
def test_rounding_never_turns_a_quote_into_a_crossing_order():
    assert round_to_tick(0.5149, 'bid') == 0.51          # bid rounds DOWN
    assert round_to_tick(0.5101, 'ask') == 0.52          # ask rounds UP
    assert abs(round_to_tick(0.50, 'bid') - 0.50) < 1e-12   # exact ticks are untouched
    assert abs(round_to_tick(0.50, 'ask') - 0.50) < 1e-12


def test_quotes_straddle_fair_and_are_never_locked_or_crossed():
    q, _, _ = mk()
    for fair in (0.15, 0.35, 0.50, 0.68, 0.91):
        bid, ask = q.quote_prices(fair, sigma_per_s=0.00016, tau_s=150)
        assert bid is not None and ask - bid >= TICK - 1e-12, (fair, bid, ask)
        assert 0 < bid < ask < 1
        assert bid <= fair + TICK and ask >= fair - TICK   # straddle, modulo tick rounding


def test_never_posts_through_the_touch():
    """Posting through the touch is a marketable order: it pays the 1.75c taker fee this whole
    module exists to avoid. With a 0.49/0.51 book our bid must stay <= 0.50 and ask >= 0.50."""
    q, book, _ = mk()
    q.on_tick(fair=0.90, sigma_per_s=1e-9, tau_s=150, z=0.0)   # fair far above the ask
    assert q.resting['bid']['price'] <= 0.50 + 1e-12
    q2, _, _ = mk()
    q2.on_tick(fair=0.10, sigma_per_s=1e-9, tau_s=150, z=0.0)  # fair far below the bid
    assert q2.resting['ask']['price'] >= 0.50 - 1e-12


# ---------- the inversion: big |z| means PULL, not trade ----------
def test_toxic_z_pulls_quotes_instead_of_trading_them():
    q, _, _ = mk(toxic_z=2.5)
    q.on_tick(fair=0.50, sigma_per_s=0.00016, tau_s=150, z=0.5)
    assert len(q.resting) == 2 and q.pulled_reason is None
    q.on_tick(fair=0.50, sigma_per_s=0.00016, tau_s=150, z=3.4)   # informed flow
    assert q.resting == {} and q.pulled_reason == 'toxic_z'
    assert q.stats['pulls'] == 1


def test_every_disqualifying_condition_pulls_and_names_itself():
    q, _, roll = mk()
    base = dict(fair=0.5, sigma_per_s=0.00016, tau_s=150, z=0.0, poly_fresh=True)
    assert q.toxicity(z=0.0, tau_s=150, poly_fresh=True, fair=0.5) is None
    assert q.toxicity(0.0, 150, False, 0.5) == 'stale_book'
    assert q.toxicity(0.0, 20, True, 0.5) == 'near_expiry'      # inside ENTRY_CUTOFF_S
    assert q.toxicity(0.0, 150, True, None) == 'no_fair'
    # inventory cap is applied PER SIDE in on_tick (see test_inventory_cap_flattens...), not as
    # a toxicity full-pull — a full inventory must NOT disqualify quoting, or the offloading
    # side gets cancelled and the position freezes to resolution.
    q.inventory = 99 * MIN_SHARES
    assert q.toxicity(0.0, 150, True, 0.5) is None
    q.inventory = 0.0
    q.ex.killed = True
    assert q.toxicity(0.0, 150, True, 0.5) == 'kill_switch'
    q.ex.killed = False
    roll['slug'] = None
    assert q.toxicity(0.0, 150, True, 0.5) == 'no_market'


# ---------- inventory ----------
def test_inventory_skews_the_reservation_price_away_from_more_of_the_same():
    q, _, _ = mk(gamma=3.0)
    flat = q.quote_prices(0.50, 0.002, 150)
    q.inventory = +50.0                       # long YES: quote lower to stop buying more
    long_ = q.quote_prices(0.50, 0.002, 150)
    q.inventory = -50.0
    short = q.quote_prices(0.50, 0.002, 150)
    assert long_[0] < flat[0] and long_[1] < flat[1]
    assert short[0] > flat[0] and short[1] > flat[1]


def test_size_floor_respects_the_exchange_minimum():
    q, _, _ = mk(shares=1.0)                  # ask for less than the exchange allows
    assert q.shares == MIN_SHARES             # orderMinSize=5; a smaller order is rejected


# ---------- lifecycle ----------
def test_requote_keeps_queue_position_when_the_price_has_not_moved():
    q, _, _ = mk()
    q.on_tick(fair=0.50, sigma_per_s=0.00016, tau_s=150, z=0.0)
    first = dict(q.resting)
    n_quotes = q.stats['quotes']
    q.on_tick(fair=0.50, sigma_per_s=0.00016, tau_s=150, z=0.0)   # same fair -> same prices
    assert q.stats['quotes'] == n_quotes and q.stats['cancels'] == 0
    assert q.resting['bid']['id'] == first['bid']['id']


def test_market_roll_cancels_everything_and_flattens_inventory():
    q, _, roll = mk()
    q.on_tick(fair=0.50, sigma_per_s=0.00016, tau_s=150, z=0.0)
    q.inventory = 7.0
    assert q.resting
    roll['slug'] = 'btc-updown-5m-2'                    # the cycle rolled
    q.on_tick(fair=0.50, sigma_per_s=0.00016, tau_s=150, z=0.0)
    assert q.inventory == 0.0
    assert all(o['slug'] == 'btc-updown-5m-2' for o in q.resting.values())


def test_paper_fill_moves_inventory_the_right_way_and_costs_no_fee():
    q, book, _ = mk()
    q.on_tick(fair=0.50, sigma_per_s=1e-9, tau_s=150, z=0.0)
    bid_px = q.resting['bid']['price']
    book.asks = {bid_px: 500}                           # someone sells into our bid
    q.simulate_paper_fills()
    assert q.inventory == MIN_SHARES                    # long YES
    assert q.fills[-1]['fee'] == 0.0 and q.fills[-1]['side'] == 'bid'
    assert 'bid' not in q.resting


def test_ask_side_fill_shorts_yes():
    q, book, _ = mk()
    q.on_tick(fair=0.50, sigma_per_s=1e-9, tau_s=150, z=0.0)
    ask_px = q.resting['ask']['price']
    book.bids = {ask_px: 500}
    q.simulate_paper_fills()
    assert q.inventory == -MIN_SHARES


# ---------- live safety ----------
def test_live_mode_requires_an_armed_client_and_never_builds_one_itself():
    """MakerQuoter has no path to a ClobClient except executor.client, which only exists
    after POLY_DRIFT_ARM=YES-LIVE plus a typed ARM. Maker mode cannot bypass the lock."""
    import inspect, maker
    src = inspect.getsource(maker)
    assert 'ClobClient' not in src            # never constructs a client
    # and never reads credentials or arming state itself — the only route to a live client is
    # executor.client, which Executor._arm_live() populates after the double lock.
    assert 'os.environ' not in src and 'getenv' not in src and 'load_env' not in src
    assert not hasattr(maker, 'os')           # maker.py does not even import os


def test_a_failed_cancel_is_reported_not_silently_forgotten(capsys):
    """Dropping a live order from self.resting after a failed cancel would make the bot blind
    to real resting exposure. It must warn."""
    class Boom:
        mode = 'live'; killed = False
        class client:
            @staticmethod
            def cancel_order(oid): raise RuntimeError('network down')
    q, _, _ = mk()
    q.ex = Boom()
    q.resting['bid'] = dict(price=0.49, size=5.0, id='real-1', ts=0, slug='s')
    q._cancel('bid')
    out = capsys.readouterr().out.lower()
    assert 'cancel failed' in out and 'may still rest' in out


# ---------- audit regressions (2026-07-23) ----------
class _LiveClient:
    """get_order returns CUMULATIVE size_matched, as the real CLOB does."""
    def __init__(self, seq): self.seq = list(seq)
    def get_order(self, oid): return self.seq.pop(0) if self.seq else {'size_matched': 0}
    def cancel_order(self, oid): pass


class _LiveExec:
    mode = 'live'; killed = False
    def __init__(self, client): self.client = client


def test_partial_fills_are_not_double_counted(monkeypatch):
    """M2: size_matched is cumulative. Re-booking it each poll inflated a 5-share order to 7."""
    ex = _LiveExec(_LiveClient([{'size_matched': 2, 'status': 'LIVE'},
                                {'size_matched': 5, 'status': 'MATCHED'}]))
    q, _, _ = mk()
    q.ex = ex
    q.resting['bid'] = dict(price=0.49, size=5.0, id='o1', ts=0, slug='btc-updown-5m-1')
    q.poll_live_fills(now=1)
    assert q.inventory == 2.0                      # first 2 of 5
    q.poll_live_fills(now=2)
    assert q.inventory == 5.0                      # NOT 7 — only the +3 delta is booked
    assert 'bid' not in q.resting


def test_inventory_cap_flattens_instead_of_freezing():
    """M4: when long the cap must keep the ASK (to offload) and pull only the BID."""
    q, _, _ = mk(shares=5.0, max_inventory=2)      # cap = 10 shares
    q.on_tick(fair=0.50, sigma_per_s=0.00016, tau_s=150, z=0.0)   # prime the slug first
    q.inventory = 10.0                             # fully long YES (set AFTER the roll reset)
    q.on_tick(fair=0.50, sigma_per_s=0.00016, tau_s=150, z=0.0)
    assert 'bid' not in q.resting                  # stop accumulating
    assert 'ask' in q.resting                      # but KEEP the offloading quote
    q.inventory = -10.0                            # fully short
    q.on_tick(fair=0.50, sigma_per_s=0.00016, tau_s=150, z=0.0)
    assert 'ask' not in q.resting and 'bid' in q.resting


# ---------- residual-risk fixes (2026-07-23) ----------
def test_paper_fill_waits_behind_the_queue(monkeypatch):
    """#1: a resting bid with size ahead of it must NOT fill on first touch — the queue ahead
    fills first. Only once swept do we fill. Under-states fills (safe direction)."""
    book = Book({0.49: 500}, {0.51: 500})          # 500 already resting at 0.49
    roll = {'slug': 'm', 'token': 'Y', 'token_no': 'N'}
    q = MakerQuoter(FakeExec(), book, roll, CFG, latency_log=os.devnull)
    # force our bid to join 0.49 behind the 500 already there
    q.resting['bid'] = dict(price=0.49, size=5.0, id='b1', ts=0, slug='m', booked=0.0, queue=500.0)
    book.asks = {0.49: 100}                         # a 100-share sell crosses to 0.49
    q.simulate_paper_fills()
    assert q.inventory == 0.0                       # 100 < 500 queue ahead -> we do NOT fill
    assert 'bid' in q.resting and q.resting['bid']['queue'] == 400.0
    book.asks = {0.49: 400}                         # the rest of the queue is swept
    q.simulate_paper_fills()
    assert q.inventory == MIN_SHARES                # now at the front -> filled


def test_queue_ahead_is_recorded_at_post_time():
    """The size already resting at our price when we join is the queue we sit behind."""
    q, book, _ = mk()                               # book 0.49/0.51, 500 a side
    q.on_tick(fair=0.50, sigma_per_s=1e-9, tau_s=150, z=0.0)
    # our bid clamps onto 0.49 where 500 already rests -> queue 500 ahead of us
    assert q.resting['bid']['queue'] == 500.0


def test_fee_extraction_reads_the_fields_the_api_might_use():
    """#3: parse the fee across amount and bps fields; default 0 when genuinely none."""
    from maker import _fee_from_response
    assert _fee_from_response({'fee_rate_bps': 70}, 5.0, 0.50) == pytest.approx(70/1e4*5*0.5)
    assert _fee_from_response({'fee': '0.0125'}, 5.0, 0.50) == pytest.approx(0.0125)  # amount wins
    assert _fee_from_response({}, 5.0, 0.50) == 0.0
    assert _fee_from_response('not a dict', 5.0, 0.50) == 0.0


def test_a_reported_maker_fee_is_captured_alarmed_and_hits_pnl(capsys, tmp_path):
    """The whole maker thesis is 'makers pay zero'. If a real fill reports a fee it must be
    booked into cost (so daily_pnl and the kill switch see it) and screamed about — never
    swallowed. Uses a real Executor so the settlement path is exercised end to end."""
    import time as _time
    from execution import Executor

    class Book2:
        bids = {}; asks = {}
    ex = Executor('paper', Book2(), {'slug': 'm'}, log_path=str(tmp_path / 'p.csv'))
    ex.mode = 'live'                                    # so poll_live_fills runs
    ex.client = _LiveClient([{'size_matched': 5, 'status': 'MATCHED', 'fee': 0.02}])
    q, _, roll = mk()
    q.ex = ex
    roll['slug'] = 'm'
    q.resting['bid'] = dict(price=0.50, size=5.0, id='o1', ts=0, slug='m', booked=0.0, queue=0.0)
    q.poll_live_fills(now=_time.time())
    assert q.fills[-1]['fee'] == pytest.approx(0.02)
    assert ex.maker_fees_paid == pytest.approx(0.02)
    assert ex.positions[-1]['cost'] == pytest.approx(5 * 0.50 + 0.02)   # fee folded into cost
    assert 'MAKER FEE CHARGED' in capsys.readouterr().out


def test_reconcile_cancels_only_orphans_on_our_market():
    """#2: cancel resting orders on OUR market's tokens that we don't track (orphans from a
    crash), but never our own live quotes and never orders on other markets."""
    class Client:
        def __init__(self): self.cancelled = []
        def get_open_orders(self):
            return [{'id': 'orphan', 'asset_id': 'Y'},        # our token, untracked -> cancel
                    {'id': 'mine', 'asset_id': 'Y'},          # our token, but WE track it
                    {'id': 'other', 'asset_id': 'ZZZ'}]       # different market -> leave
        def cancel_order(self, oid): self.cancelled.append(oid)

    class Ex:
        mode = 'live'; killed = False
        def __init__(self, c): self.client = c
    c = Client(); ex = Ex(c)
    q, _, _ = mk(); q.ex = ex
    q.resting['bid'] = dict(price=0.49, size=5.0, id='mine', ts=0, slug='m', booked=0.0, queue=0.0)
    n = q.reconcile_open_orders(['Y', 'N'])
    assert n == 1 and c.cancelled == ['orphan']               # only the orphan on our token


def test_reconcile_is_a_noop_in_paper():
    q, _, _ = mk()                                            # FakeExec.mode == 'paper'
    assert q.reconcile_open_orders(['Y', 'N']) == 0


def test_reconcile_runs_on_first_quote_of_a_new_market(monkeypatch):
    """The slug-change branch must reconcile before placing quotes, so a restart never leaves
    orphans double-quoting alongside our fresh orders."""
    seen = {}
    def fake_reconcile(tokens): seen['tokens'] = tokens; return 0
    q, _, _ = mk()
    monkeypatch.setattr(q, 'reconcile_open_orders', fake_reconcile)
    q.on_tick(fair=0.50, sigma_per_s=1e-9, tau_s=150, z=0.0)   # first tick -> slug change
    assert seen['tokens'] == ['Y', 'N']


def test_live_order_is_posted_with_correct_client_call_shape():
    """LIVE REGRESSION (2026-07-24). create_and_post_order's 2nd POSITIONAL arg is
    PartialCreateOrderOptions, not the order type. OrderType.GTC is the bare string 'GTC', so
    passing it positionally made the client evaluate options.tick_size and raise
        'str' object has no attribute 'tick_size'
    Every live maker order failed silently for a whole session. Pin the call shape."""
    import types
    seen = {}

    class Client:
        def create_and_post_order(self, order_args, options=None, order_type='GTC',
                                  post_only=False, defer_exec=False):
            # mimic the real client: it will touch options.tick_size when options is given
            if options is not None and not hasattr(options, 'tick_size'):
                raise AttributeError("'str' object has no attribute 'tick_size'")
            seen.update(options=options, order_type=order_type, post_only=post_only,
                        price=order_args.price, size=order_args.size, token=order_args.token_id)
            return {'success': True, 'orderID': 'ok-1'}

    class Ex:
        mode = 'live'; killed = False
        def __init__(self): self.client = Client()

    q, _, _ = mk()
    q.ex = Ex()
    oid, _lat = q._post('bid', 0.49, 5.0)
    assert oid == 'ok-1', 'live post must succeed, not swallow an exception'
    assert seen['order_type'] == 'GTC'          # passed as a KEYWORD, never positionally
    assert seen['options'] is None              # the options slot must not hold the order type
    assert seen['post_only'] is True            # exchange-enforced: rest or reject, never cross
    assert seen['token'] == 'Y' and seen['price'] == 0.49


def test_ask_side_posts_the_no_leg_at_the_complement_price():
    """A YES ask at 0.56 is economically a NO bid at 0.44, so every order stays a BUY."""
    seen = {}

    class Client:
        def create_and_post_order(self, order_args, options=None, order_type='GTC',
                                  post_only=False, defer_exec=False):
            seen.update(price=order_args.price, token=order_args.token_id)
            return {'success': True, 'orderID': 'ok-2'}

    class Ex:
        mode = 'live'; killed = False
        def __init__(self): self.client = Client()

    q, _, _ = mk()
    q.ex = Ex()
    q._post('ask', 0.56, 5.0)
    assert seen['token'] == 'N' and seen['price'] == 0.44
