# tests/test_execution.py — paper executor: fills, caps, settlement, kill switch.
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from execution import Executor


class Book:
    def __init__(self): self.bids = {}; self.asks = {}


def make_exec(**kw):
    log = os.path.join(tempfile.mkdtemp(), 'paper.csv')
    book, roll = Book(), {'token': 'Y', 'token_no': 'N', 'slug': 'm1'}
    ex = Executor('paper', book, roll, log_path=log, **kw)
    return ex, book, roll


def sig(side='BUY', slug='m1', bb=0.30, ba=0.32, ts=1000.0):
    return dict(ts=ts, side=side, slug=slug, bb=bb, ba=ba)


class FakeClient:
    """Mimics py-clob-client-v2's surface (the v1 client is archived and its orders
    are rejected with 'invalid order version')."""
    def __init__(self, resp): self.resp = resp; self.cancelled = []
    def create_and_post_market_order(self, order_args=None, order_type=None): return self.resp
    def create_and_post_order(self, args, ot=None): return self.resp
    def cancel_order(self, oid): self.cancelled.append(oid)
    def cancel_all(self): self.cancelled.append('ALL')   # must never be called


def test_live_order_rejected_books_no_position():
    ex, _, _ = make_exec()
    ex.mode = 'live'; ex.order_type = 'market'
    ex.client = FakeClient({'success': False, 'orderID': 'A', 'errorMsg': 'not matched'})
    filled, cost = ex._live_order('tok', 0.65)
    assert filled == 0.0 and cost == 0.0
    assert 'A' not in ex.open_order_ids                          # no fictional position/id


def test_live_order_missing_success_flag_is_treated_as_rejected():
    ex, _, _ = make_exec()
    ex.mode = 'live'; ex.order_type = 'market'
    ex.client = FakeClient({'orderID': 'X'})                     # ambiguous -> refuse
    assert ex._live_order('tok', 0.65) == (0.0, 0.0)


def test_live_order_confirmed_fill_is_not_tracked_as_open():
    """v2 returns STRING amounts: takingAmount=shares filled, makingAmount=USDC spent.
    A FILLED FOK/FAK order does not rest, so it must NOT land in open_order_ids —
    trying to cancel a fill later just errors."""
    ex, _, _ = make_exec(stake_usd=1.0)
    ex.mode = 'live'; ex.order_type = 'market'
    ex.client = FakeClient({'success': True, 'status': 'matched', 'errorMsg': '',
                            'orderID': 'B', 'takingAmount': '1.960783',
                            'makingAmount': '0.999999'})
    filled, cost = ex._live_order('tok', 0.51)
    assert abs(filled - 1.960783) < 1e-9          # real shares, parsed from string
    assert abs(cost - 0.999999) < 1e-9            # real USDC spent, not the assumed stake
    assert 'B' not in ex.open_order_ids           # filled != resting
    assert abs(cost / filled - 0.51) < 0.01       # implied entry ~ the book price


def test_exit_position_paper_take_profit_realizes_gain():
    ex, book, _ = make_exec(stake_usd=1.0, min_shares=1.0)
    book.asks = {0.30: 100.0}                        # BUY 10 YES @ 0.30 -> cost ~$3
    ex.on_signal(sig('BUY', bb=0.28, ba=0.30))
    p = ex.positions[0]; shares = p['shares']
    book.bids = {0.40: 100.0}                        # can now sell YES back at 0.40
    ok, _ = ex.exit_position(p, 'tp')
    assert ok and not ex.positions                    # position closed
    assert abs(ex.daily_pnl - shares * (0.40 - 0.30)) < 1e-9   # (sell - entry) * shares


def test_exit_position_sell_side_holds_no_token():
    ex, book, _ = make_exec(stake_usd=1.0, min_shares=1.0)
    book.bids = {0.70: 100.0}                        # SELL -> buy NO at 1-0.70 = 0.30
    ex.on_signal(sig('SELL', bb=0.70, ba=0.72))
    p = ex.positions[0]
    assert p['token'] == 'N'                          # holds the NO token
    book.asks = {0.60: 100.0}                        # YES ask fell -> NO worth 1-0.60 = 0.40
    ok, _ = ex.exit_position(p, 'tp')
    assert ok and abs(ex.daily_pnl - p['shares'] * (0.40 - p['entry'])) < 1e-6


def test_exit_kept_when_no_book_to_sell_into():
    ex, book, _ = make_exec(stake_usd=1.0, min_shares=1.0)
    book.asks = {0.30: 100.0}
    ex.on_signal(sig('BUY', bb=0.28, ba=0.30))
    p = ex.positions[0]
    book.bids = {}                                    # nothing to sell into
    ok, msg = ex.exit_position(p, 'tp')
    assert not ok and ex.positions == [p]             # never drop a position we can't close


def test_on_roll_cancels_only_owned_ids_never_cancel_all():
    ex, _, _ = make_exec()
    ex.mode = 'live'; ex.client = FakeClient({})
    ex.open_order_ids = {'X', 'Y'}
    ex.on_roll('m1', final_mid=0.99)
    assert set(ex.client.cancelled) == {'X', 'Y'}               # scoped, and 'ALL' never appears
    assert not ex.open_order_ids


def test_buy_fill_and_settle_win():
    ex, book, roll = make_exec(stake_usd=1.0, min_shares=1.0)
    book.asks[0.32] = 100.0
    ex.on_signal(sig('BUY'))
    assert len(ex.positions) == 1
    p = ex.positions[0]
    assert abs(p['shares'] - 1.0 / 0.32) < 1e-9 and p['entry'] == 0.32
    ex.on_roll('m1', final_mid=0.99)          # YES resolves up -> BUY wins
    assert not ex.positions
    assert abs(ex.daily_pnl - (p['shares'] - p['cost'])) < 1e-9


def test_sell_becomes_cheap_no_share_and_loss():
    ex, book, roll = make_exec(stake_usd=1.0, min_shares=1.0)
    book.bids[0.70] = 50.0                    # NO share costs 0.30
    ex.on_signal(sig('SELL', bb=0.70, ba=0.72))
    p = ex.positions[0]
    assert abs(p['entry'] - 0.30) < 1e-9
    ex.on_roll('m1', final_mid=0.99)          # YES wins -> NO position loses its cost
    assert abs(ex.daily_pnl + p['cost']) < 1e-9


def test_no_fill_when_bbo_empty():
    ex, book, roll = make_exec()
    ex.on_signal(sig('BUY'))                  # no size at the ask
    assert not ex.positions


def test_manual_order_uses_current_bbo_and_risk_path():
    ex, book, roll = make_exec(stake_usd=1.0, min_shares=1.0)
    book.bids[0.30] = 100.0; book.asks[0.32] = 100.0
    ok, message = ex.manual_order('BUY')
    assert ok and 'MANUAL' in message and len(ex.positions) == 1
    ok, message = ex.manual_order('BUY')
    assert not ok and 'already open' in message


def test_one_position_per_market_side():
    ex, book, roll = make_exec(min_shares=1.0)
    book.asks[0.32] = 100.0
    ex.on_signal(sig('BUY')); ex.on_signal(sig('BUY', ts=1001.0))
    assert len(ex.positions) == 1


def test_kill_switch_blocks_entries():
    ex, book, roll = make_exec(min_shares=1.0, max_daily_loss=0.5)
    ex.daily_pnl = -1.0                       # beyond the cap
    book.asks[0.32] = 100.0
    ex.on_signal(sig('BUY'))
    assert ex.killed and not ex.positions


def test_market_order_walks_levels_with_slippage():
    ex, book, roll = make_exec(stake_usd=1.0)         # default order_type='market'
    book.asks = {0.32: 2.0, 0.35: 100.0}              # only $0.64 at best level
    ex.on_signal(sig('BUY'))
    p = ex.positions[0]
    assert abs(p['cost'] - 1.0) < 1e-9                # spends exactly $1
    assert abs(p['shares'] - (2.0 + 0.36 / 0.35)) < 1e-9
    assert 0.32 < p['entry'] < 0.35                   # blended entry shows slippage


def test_market_order_spends_stake_on_no_side():
    ex, book, roll = make_exec(stake_usd=1.0)
    book.bids = {0.70: 1.0, 0.68: 100.0}              # NO costs 0.30 then 0.32
    ex.on_signal(sig('SELL', bb=0.70, ba=0.72))
    p = ex.positions[0]
    assert abs(p['cost'] - 1.0) < 1e-9
    assert abs(p['shares'] - (1.0 + 0.70 / 0.32)) < 1e-9   # 0.30 spent, then 0.70 at 0.32


def test_limit_order_caps_at_bbo_size():
    ex, book, roll = make_exec(order_type='limit', min_shares=1.0)
    book.asks = {0.32: 2.0, 0.35: 100.0}
    ex.on_signal(sig('BUY'))
    p = ex.positions[0]
    assert abs(p['shares'] - 2.0) < 1e-9              # never walks past the limit price
    assert abs(p['entry'] - 0.32) < 1e-9


def _settled_buy(ex, book, final_mid):
    book.asks = {0.48: 100.0}
    ex.on_signal(sig('BUY', bb=0.46, ba=0.48))
    ex.on_roll('m1', final_mid=final_mid)


def test_official_result_corrects_wrong_proxy_settlement():
    ex, book, roll = make_exec(stake_usd=1.0)
    _settled_buy(ex, book, final_mid=0.86)            # decisive: proxy says YES won -> WIN
    assert ex.daily_pnl > 0 and 'm1' in ex.verify_queue
    ex._fetch = lambda slug: False                    # official: NO won
    ex.verify_settlement('m1')
    assert abs(ex.daily_pnl + 1.0) < 1e-9             # corrected to a full $1 loss
    assert 'm1' not in ex.settled
    assert '-corr' in open(ex.log_path).read()


def test_indecisive_close_is_never_booked_as_a_win():
    """A near-0.5 close is 'unknown', not 'YES won'. Guessing a win fabricates P&L AND hides
    the daily-loss kill switch, so the provisional must be the LOSS side, then be corrected."""
    ex, book, roll = make_exec(stake_usd=1.0)
    _settled_buy(ex, book, final_mid=0.51)
    assert ex.daily_pnl < 0, 'a coin-flip close must not book a profit'
    assert 'm1' in ex.verify_queue                    # still reconciled against the official result
    ex._fetch = lambda slug: True                     # official: YES actually won
    ex.verify_settlement('m1')
    assert ex.daily_pnl > 0                           # corrected up once the truth arrives


def test_unknown_final_price_is_not_a_win():
    ex, book, roll = make_exec(stake_usd=1.0)
    _settled_buy(ex, book, final_mid=None)            # book already cleared at roll
    assert ex.daily_pnl < 0 and 'm1' in ex.verify_queue


def test_open_exposure_counts_against_the_daily_loss_cap():
    """A binary only settles at cycle end, so realized P&L lags real risk by a whole cycle.
    Without counting open cost the bot keeps firing after the cap is already spent."""
    ex, book, roll = make_exec(stake_usd=1.0, max_daily_loss=1.0)
    book.asks = {0.48: 1000.0}
    ok, _ = ex.on_signal(sig('BUY', bb=0.46, ba=0.48))
    assert ok                                          # first $1 uses the whole cap
    roll['slug'] = 'm2'
    ok2, msg = ex.on_signal(dict(sig('BUY', bb=0.46, ba=0.48), slug='m2'))
    assert not ok2 and 'daily cap' in msg


def test_official_result_confirms_proxy_settlement():
    ex, book, roll = make_exec(stake_usd=1.0)
    _settled_buy(ex, book, final_mid=0.99)
    before = ex.daily_pnl
    ex._fetch = lambda slug: True                     # official agrees: YES won
    ex.verify_settlement('m1')
    assert ex.daily_pnl == before and 'm1' not in ex.settled
    assert '-corr' not in open(ex.log_path).read()


def test_verify_requeues_until_result_is_final():
    ex, book, roll = make_exec(stake_usd=1.0)
    _settled_buy(ex, book, final_mid=0.99)
    ex._fetch = lambda slug: None                     # not resolved yet
    ex.verify_settlement('m1')
    assert ex.settled['m1']['attempts'] == 1 and 'm1' in ex.verify_queue
    ex.verify_attempts = 2                            # shorten for the give-up path
    ex.verify_settlement('m1')
    assert 'm1' not in ex.settled                     # gives up, proxy stands


# ---------------- live sell sizing (2026-07-22 live failure) ----------------
class _FakeV2Client:
    """Mimics the CLOB: rejects a sell larger than the holding exactly as the live API did,
    and (FAK) fills only up to `depth` when the book is thin."""
    def __init__(self, held, depth=None):
        self.held, self.depth, self.asked = held, depth, None
    def create_and_post_market_order(self, order_args, order_type):
        self.asked = order_args.amount
        if order_args.amount > self.held + 1e-12:
            raise RuntimeError(
                "PolyApiException[status_code=400, error_message={'error': 'not enough balance "
                f"/ allowance: the balance is not enough -> balance: {int(self.held*1e6)}, "
                f"order amount: {int(order_args.amount*1e6)}'}}]")
        sold = order_args.amount if self.depth is None else min(order_args.amount, self.depth)
        return {'success': True, 'status': 'matched', 'errorMsg': '', 'orderID': 'S',
                'takingAmount': str(sold * 0.65), 'makingAmount': str(sold)}


def _sell_with(held, monkeypatch, depth=None):
    import types
    fake = _FakeV2Client(held, depth)
    mod = types.ModuleType('py_clob_client_v2')
    mod.MarketOrderArgs = lambda token_id, amount, side: types.SimpleNamespace(
        token_id=token_id, amount=amount, side=side)
    mod.OrderType = types.SimpleNamespace(FOK='FOK', FAK='FAK')
    mod.Side = types.SimpleNamespace(BUY='BUY', SELL='SELL')
    monkeypatch.setitem(sys.modules, 'py_clob_client_v2', mod)
    ex, book, roll = make_exec()
    ex.mode, ex.client = 'live', fake
    return ex._live_sell('tok', held), fake


def test_live_sell_never_asks_for_more_shares_than_are_held(monkeypatch):
    """The live failure: a $1 buy filled 1.369862 shares; round(1.369862, 2) = 1.37 asked the
    CLOB for 138 micro-shares that did not exist, and it answered 'not enough balance /
    allowance' — which reads like a funding problem and is not one. Reproduced for all three
    holdings seen in the 2026-07-22 logs."""
    for held in (1.369862, 1.886791, 1.666665):
        got, fake = _sell_with(held, monkeypatch)
        assert got is not None, f'sell of {held} was rejected'
        proceeds, sold = got
        assert fake.asked <= held, f'asked {fake.asked} > held {held}'
        assert proceeds > 0 and sold > 0


def test_live_sell_treats_sub_step_dust_as_closed(monkeypatch):
    (proceeds, sold), fake = _sell_with(0.004, monkeypatch)
    assert proceeds == 0.0 and fake.asked is None      # never submitted


def test_live_exit_uses_fak_and_reports_a_partial_fill(monkeypatch):
    """Entries stay FOK (all-or-nothing bounds slippage); exits are FAK so a thin book fills
    what it can instead of leaving us stuck holding. Live logs showed repeated
    'FOK orders are fully filled or killed' on exits."""
    got, fake = _sell_with(2.0, monkeypatch, depth=0.5)   # only 0.5 sh of depth
    assert got is not None
    proceeds, sold = got
    assert abs(sold - 0.5) < 1e-9 and proceeds > 0


def test_partial_exit_reduces_the_position_pro_rata(monkeypatch):
    ex, book, _ = make_exec(stake_usd=1.0, min_shares=1.0)
    book.asks = {0.50: 100.0}
    ex.on_signal(sig('BUY', bb=0.48, ba=0.50))
    p = ex.positions[0]
    shares0, cost0 = p['shares'], p['cost']
    book.bids = {0.60: shares0 / 4}                   # only a quarter of our size rests
    ok, msg = ex.exit_position(p, 'tp')
    assert ok and 'partial' in msg
    assert p in ex.positions                          # still open for the remainder
    assert abs(p['shares'] - shares0 * 0.75) < 1e-9
    assert abs(p['cost'] - cost0 * 0.75) < 1e-9       # cost basis scales with what was sold
    assert abs(ex.daily_pnl - (shares0 / 4) * (0.60 - 0.50)) < 1e-9


def test_full_exit_still_closes_the_position(monkeypatch):
    ex, book, _ = make_exec(stake_usd=1.0, min_shares=1.0)
    book.asks = {0.50: 100.0}
    ex.on_signal(sig('BUY', bb=0.48, ba=0.50))
    p = ex.positions[0]
    book.bids = {0.60: 1000.0}                        # plenty of depth
    ok, msg = ex.exit_position(p, 'tp')
    assert ok and 'partial' not in msg and not ex.positions


def test_failed_exit_backs_off_instead_of_retrying_every_tick(monkeypatch):
    """Live logs show 25 rejected SELLs in 56 seconds against one position."""
    import types
    mod = types.ModuleType('py_clob_client_v2')
    mod.MarketOrderArgs = lambda **k: types.SimpleNamespace(**k)
    mod.OrderType = types.SimpleNamespace(FOK='FOK')
    mod.Side = types.SimpleNamespace(BUY='BUY', SELL='SELL')
    monkeypatch.setitem(sys.modules, 'py_clob_client_v2', mod)
    ex, book, roll = make_exec()
    ex.mode = 'live'
    monkeypatch.setattr(ex, '_live_sell', lambda t, s: None)      # always fails
    pos = dict(slug='m1', side='BUY', shares=2.0, entry=0.5, cost=1.0, ts=0.0, token='t')
    ex.positions = [pos]

    ok, msg = ex.exit_position(pos, 'tp')
    assert not ok and 'still held' in msg
    ok2, msg2 = ex.exit_position(pos, 'tp')                       # immediate retry
    assert not ok2 and 'backoff' in msg2
    assert pos in ex.positions                                    # never dropped
