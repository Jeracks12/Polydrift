# execution.py — isolated order layer for poly_drift. Three modes:
#   off   (default) no orders of any kind
#   paper simulate fills against the live book; positions settle at cycle end; logs paper_trades.csv
#   live  real orders via py-clob-client. DOUBLE-LOCKED: requires POLY_DRIFT_ARM=YES-LIVE in the
#         environment AND typing ARM at startup. Credentials come ONLY from .env (never hardcode,
#         never share your private key with anyone or paste it into any chat).
#
# .env keys (create the file yourself next to this script; see .env.example):
#   POLY_PRIVATE_KEY=0x...        signer key (funds/settlement wallet or EOA)
#   POLY_SIGNATURE_TYPE=0         0=EOA, 1=email/Magic proxy, 2=browser-wallet proxy
#   POLY_FUNDER=0x...             proxy/funder address (required for signature types 1 and 2)
import json, math, os, sys, time, urllib.request

try:
    from logbook import trade as _t, err as _e
except Exception:                       # logbook optional; degrade to plain print
    _t = _e = print


def _xlog(msg):
    """Route one execution line to trades.log, or errors.log if it reports a failure.
    Always mirrors to the console (logbook does), so the live view is unchanged."""
    low = msg.lower()
    if any(k in low for k in ('fail', 'reject', 'error', 'cannot', 'not closed',
                              'no position', 'zero fill', 'no final price')):
        _e(msg)
    else:
        _t(msg)


def _fetch_result(slug):
    """Official resolution from gamma: True = YES won, False = NO won, None = not final yet.
    closed=true is required - gamma's slug lookup silently omits resolved markets without it."""
    req = urllib.request.Request(
        f"https://gamma-api.polymarket.com/markets?slug={slug}&closed=true",
        headers={'User-Agent': 'poly-drift/1.0'})
    with urllib.request.urlopen(req, timeout=8) as r:
        d = json.loads(r.read().decode())
    if not d: return None
    op = d[0].get('outcomePrices')
    if isinstance(op, str): op = json.loads(op)
    if not op: return None
    v = float(op[0])
    return True if v == 1.0 else (False if v == 0.0 else None)


def apply_exchange_override():
    """Polymarket migrated exchange contracts; a released py-clob-client can lag behind and
    sign against a retired exchange -> the CLOB answers 'invalid order version'. Setting
    POLY_EXCHANGE=0x... patches the address the order is signed against, so a stale client
    can be pointed at the live exchange without editing site-packages.
    Verify the target with scripts/check_balance_allowance.py: the exchange you sign against
    MUST be one your account has granted an allowance to."""
    addr = os.environ.get('POLY_EXCHANGE')
    if not addr:
        return None
    import py_clob_client.config as _cfg
    from py_clob_client.clob_types import ContractConfig
    orig = _cfg.get_contract_config

    def patched(chainID: int, neg_risk: bool = False):
        base = orig(chainID, neg_risk)
        return ContractConfig(exchange=addr, collateral=base.collateral,
                              conditional_tokens=base.conditional_tokens)

    _cfg.get_contract_config = patched
    # the client imports the symbol directly, so patch that binding too
    import py_clob_client.client as _cl
    if hasattr(_cl, 'get_contract_config'):
        _cl.get_contract_config = patched
    return addr


def load_env(path='.env'):
    """Minimal .env loader (no dependency). Existing environment wins."""
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())


class Executor:
    def __init__(self, mode, book, roll, stake_usd=1.0, max_daily_loss=10.0,
                 min_shares=5.0, order_type='market', log_path='paper_trades.csv'):
        assert mode in ('paper', 'live')
        assert order_type in ('market', 'limit')
        self.mode, self.book, self.roll = mode, book, roll
        self.stake, self.max_daily_loss, self.min_shares = stake_usd, max_daily_loss, min_shares
        self.order_type = order_type   # market: spend stake $ exactly, walk the book ($1 min)
        self.log_path = log_path       # limit: shares at BBO price, 5-share exchange minimum
        self.positions = []          # open: dict(slug, side, shares, entry, cost, ts)
        self.daily_pnl, self.day = 0.0, time.strftime('%Y-%m-%d')
        self.killed = False
        self.client = None
        # RESTING order ids only. FOK/FAK never rest, so with the current order types this
        # stays empty — a FILLED order id must never go in here (you cannot cancel a fill).
        self.open_order_ids = set()
        self.settled = {}            # slug -> proxy-settled rows awaiting official verification
        self.verify_queue = {}       # slug -> ts when the official result should be checked
        self.verify_delay = 75.0     # first check ~75s after roll
        self.verify_retry = 180.0    # then every 3 min: official results take ~10-20+ min to finalize
        self.verify_attempts = 12    # ~35 min of coverage before giving up
        # a close within this band of 0.5 is NOT a result. Guessing one fabricates P&L and
        # blinds the daily-loss cap, so an indecisive close books a provisional loss instead.
        self.settle_band = 0.10
        # A failed exit keeps the position (correct), so _manage_exits retries it EVERY tick.
        # Observed live: 25 rejected SELLs in 56s against the same position. Back off instead
        # of hammering the CLOB, and surface the repeat count once rather than once per attempt.
        self._exit_fail = {}         # id(pos) -> [consecutive failures, next retry ts]
        self._fetch = _fetch_result  # patchable in tests
        if mode == 'live':
            self._arm_live()
        if not os.path.exists(log_path):
            with open(log_path, 'w') as f:
                f.write('ts,mode,slug,side,entry,shares,cost,settle_mid,payout,pnl,daily_pnl\n')

    # ---------- live arming (human decision, double-locked) ----------
    def _arm_live(self):
        load_env()
        if os.environ.get('POLY_DRIFT_ARM') != 'YES-LIVE':
            sys.exit('live mode locked: set POLY_DRIFT_ARM=YES-LIVE in your environment to arm')
        key = os.environ.get('POLY_PRIVATE_KEY')
        if not key:
            sys.exit('live mode: POLY_PRIVATE_KEY missing from .env')
        try:
            ans = input('LIVE TRADING with real funds. Type ARM to confirm: ').strip()
        except EOFError:
            ans = ''
        if ans != 'ARM':
            sys.exit('live mode not confirmed; aborting')
        # MUST be the v2 client: the CLOB migrated its order version and rejects orders
        # built by the archived py-clob-client with
        #   "invalid order version, please use the latest clob-client".
        try:
            from py_clob_client_v2 import ClobClient
        except ImportError:
            sys.exit('live mode: pip install "py-clob-client-v2>=1.0.2"  '
                     '(the old py-clob-client is archived and its orders are rejected)')
        sig_type = int(os.environ.get('POLY_SIGNATURE_TYPE', '0'))
        funder = os.environ.get('POLY_FUNDER') or None
        self.client = ClobClient(host='https://clob.polymarket.com', chain_id=137, key=key,
                                 signature_type=sig_type, funder=funder or None)
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        _xlog('[exec] LIVE armed. caps: stake=$%.2f/trade, max daily loss=$%.2f'
              % (self.stake, self.max_daily_loss))

    # ---------- signal entry ----------
    def on_signal(self, sig):
        source = sig.get('source', 'signal')
        label = 'MANUAL' if source == 'manual' else 'signal'
        if self.killed:
            msg = f'{label} blocked: kill switch is active'
            _xlog(f'[exec] {msg}')
            return False, msg
        self._roll_day()
        # Worst case, every open position expires worthless, so open cost is money already at
        # risk today. Checking realized P&L alone let the bot keep opening positions while the
        # limit was already breached on paper (a binary settles only at cycle end, so realized
        # P&L lags real exposure by up to a full cycle).
        # HARD kill (permanent for the day) on REALIZED losses only: an open position may
        # still win, so unrealized risk must not permanently disarm the bot.
        if self.daily_pnl <= -self.max_daily_loss:
            self.killed = True
            _xlog(f'[exec] KILL SWITCH: daily pnl {self.daily_pnl:+.2f} <= -{self.max_daily_loss}. '
                  f'no further entries today.')
            return False, f'{label} blocked: daily-loss limit reached'
        # SOFT gate (clears when positions settle): a binary settles only at cycle end, so
        # realized P&L lags real risk by up to a full cycle. Without this the bot could open
        # many positions whose combined cost already exceeds the day's entire loss budget.
        exposure = sum(p['cost'] for p in self.positions)
        budget = self.max_daily_loss + self.daily_pnl      # profit earned today extends it
        if exposure + self.stake > budget:
            msg = (f'{label} blocked: ${self.stake:.2f} more would put open risk at '
                   f'${exposure + self.stake:.2f}, past the ${budget:.2f} remaining daily cap')
            _xlog(f'[exec] {msg}')
            return False, msg
        slug = sig.get('slug') or ''
        if any(p['slug'] == slug and p['side'] == sig['side'] for p in self.positions):
            msg = f'{label} blocked: {sig["side"]} position already open for {slug}'
            _xlog(f'[exec] {msg}')
            return False, msg
        # BUY = YES share (ask side of YES book); SELL = NO share at (1 - bid) (bid side)
        no_side = sig['side'] == 'SELL'
        token = self.roll.get('token_no') if no_side else self.roll.get('token')
        bbo_px = (1 - sig['bb']) if no_side else sig['ba']
        if self.mode == 'paper':
            if self.order_type == 'market':
                fill, cost = self._walk_book(no_side, self.stake)
            else:
                avail = self.book.bids.get(sig['bb'], 0.0) if no_side else self.book.asks.get(sig['ba'], 0.0)
                fill = min(max(self.stake / max(bbo_px, 0.01), self.min_shares), avail)
                cost = fill * bbo_px
            if fill <= 0:
                msg = f'{label} {sig["side"]} {slug}: no size on book'
                _xlog(f'[exec] paper MISS {msg}')
                return False, msg
        else:
            if token is None:
                msg = f'{label} {sig["side"]}: token id unknown (fixed-market NO leg?)'
                _xlog(f'[exec] live skip {msg}')
                return False, msg
            fill, cost = self._live_order(token, bbo_px)
            if not fill:
                return False, f'{label} {sig["side"]}: live order was rejected or failed (see console)'
        # `token` is the CONDITIONAL token we now hold (YES for BUY, NO for SELL); store it so
        # an exit can sell exactly that token even after the market has rolled the ROLL dict.
        pos = dict(slug=slug, side=sig['side'], shares=fill, entry=cost / fill,
                   cost=cost, ts=sig['ts'], token=token, strat=sig.get('strat', 'drift'))
        self.positions.append(pos)
        _xlog(f'[exec] {self.mode} FILL {label} {sig["side"]} {fill:.2f} sh @ {pos["entry"]:.3f} '
              f'(${cost:.2f}, {self.order_type}) {slug}')
        return True, (f'{self.mode.upper()} {label}: {sig["side"]} submitted — '
                      f'{fill:.2f} sh @ {pos["entry"]:.3f} (${cost:.2f})')

    # ---------- active exit (close a position before resolution) ----------
    def exit_position(self, pos, reason):
        """Sell the held token back into the book, in full or in part.

        Exits use FAK (fill-and-kill), so a thin book fills what it can instead of killing the
        whole order. Entries stay FOK: there, an all-or-nothing fill is what bounds slippage,
        but on the way OUT a partial exit beats being stuck holding. A partial reduces the
        position pro-rata and stays open for the rest; a live-sell FAILURE keeps it untouched.
        Never drop a position we could not actually close."""
        if pos not in self.positions:
            return False, 'position already closed'
        fails, next_try = self._exit_fail.get(id(pos), (0, 0.0))
        if time.time() < next_try:
            return False, f'exit backoff ({fails} failures, retrying shortly)'
        bb = max(self.book.bids) if self.book.bids else None
        ba = min(self.book.asks) if self.book.asks else None
        if self.mode == 'paper':
            # sell what we hold at the far side of the spread (BUY holds YES -> sell at YES
            # bid; SELL holds NO -> sell NO at its bid = 1 - YES ask), capped by the size
            # actually resting there -- a paper exit that always fills in full would overstate
            # every scalp exactly where the live book is thinnest.
            px = bb if pos['side'] == 'BUY' else (None if ba is None else 1 - ba)
            if not px:
                return False, 'no book to exit into'
            avail = (self.book.bids.get(bb, 0.0) if pos['side'] == 'BUY'
                     else self.book.asks.get(ba, 0.0))
            sold = min(pos['shares'], avail)
            if sold <= 0:
                return False, 'no size on the book to exit into'
            proceeds = sold * px
        else:
            got = self._live_sell(pos['token'], pos['shares'])
            if got is None:
                fails += 1
                delay = min(2 ** fails, 60)        # 2s, 4s, 8s ... capped at a minute
                self._exit_fail[id(pos)] = (fails, time.time() + delay)
                if fails in (1, 5, 10) or fails % 25 == 0:
                    _xlog(f'[exec] exit for {pos["side"]} {pos["slug"]} has failed {fails}x; '
                          f'backing off {delay}s. Position IS still held.')
                return False, f'live sell failed ({fails}x) — position still held'
            proceeds, sold = got
            if sold <= 0:
                return False, 'live sell filled nothing — position still held'
        self._exit_fail.pop(id(pos), None)
        # pro-rata cost basis: selling a fraction f realises f of the cost, the rest stays open
        frac = min(sold / pos['shares'], 1.0) if pos['shares'] > 0 else 1.0
        closed_cost = pos['cost'] * frac
        pnl = proceeds - closed_cost
        self._roll_day(); self.daily_pnl += pnl
        partial = frac < 0.999
        if partial:
            pos['shares'] -= sold
            pos['cost'] -= closed_cost
        else:
            self.positions = [p for p in self.positions if p is not pos]
        _xlog(f'[exec] EXIT ({reason}{"-partial" if partial else ""}) {pos["side"]} '
              f'{pos["slug"]}: sold {sold:.2f} sh, proceeds ${proceeds:.2f} '
              f'pnl {pnl:+.2f} (day {self.daily_pnl:+.2f})'
              + (f' | {pos["shares"]:.2f} sh still open' if partial else ''))
        with open(self.log_path, 'a') as f:
            f.write(f'{time.time()},{self.mode}-exit-{reason}{"-partial" if partial else ""},'
                    f'{pos["slug"]},{pos["side"]},{pos["entry"]},{sold:.2f},'
                    f'{closed_cost:.2f},{proceeds / sold:.4f},{proceeds:.2f},'
                    f'{pnl:.2f},{self.daily_pnl:.2f}\n')
        return True, (f'exit {reason} pnl {pnl:+.2f}'
                      + (f' (partial, {pos["shares"]:.2f} sh left)' if partial else ''))

    def _live_sell(self, token, shares):
        """Sell `shares` of `token` for USDC (v2 market SELL, FAK).

        Returns (proceeds_usdc, shares_sold), or None if not confirmably filled at all (the
        caller then keeps the whole position). FAK rather than FOK: a thin book should fill
        what it can instead of killing the order and leaving us holding. Nothing rests
        afterwards either way, so there is still no order id to track or cancel.

        Size is FLOORED, never rounded. A buy fills an exact fractional share count
        (takingAmount 1.369862); round(1.369862, 2) = 1.37 asks the CLOB to sell 138
        micro-shares that do not exist, and it answers:
            'not enough balance / allowance: balance: 1369862, order amount: 1370000'
        which reads like a *funding* problem and is nothing of the kind. Observed live on
        2026-07-22 across three markets; every occurrence was this rounding."""
        try:
            from py_clob_client_v2 import MarketOrderArgs, OrderType, Side
            size = math.floor(shares * 100) / 100.0      # never ask for more than we hold
            if size <= 0:
                _xlog(f'[exec] SELL skipped: {shares:.6f} sh is below the 0.01 tradable step '
                      f'(dust) — position dropped from tracking.')
                return 0.0, shares
            r = self.client.create_and_post_market_order(
                order_args=MarketOrderArgs(token_id=token, amount=size, side=Side.SELL),
                order_type=OrderType.FAK)      # partial fill beats being stuck holding
            rd = r if isinstance(r, dict) else {}
            oid = rd.get('orderID') or rd.get('id')
            ok = bool(rd.get('success', False))
            err = str(rd.get('errorMsg') or rd.get('error') or '')
            _xlog(f'[exec] live SELL response (id={oid}, ok={ok}): {r}')
            if not ok or err:
                _xlog(f'[exec] SELL rejected: {err or "no success flag"} — position NOT closed.')
                return None
            taking = float(rd.get('takingAmount') or 0)   # USDC received on a sell
            making = float(rd.get('makingAmount') or 0)   # SHARES actually sold
            if taking <= 0:
                _xlog('[exec] SELL accepted but zero proceeds — treating as not closed.')
                return None
            sold = making if making > 0 else size        # FAK may fill less than requested
            if sold < size - 1e-9:
                _xlog(f'[exec] SELL partial: {sold:.2f} of {size:.2f} sh filled '
                      f'(book too thin) — remainder stays open.')
            return taking, sold
        except Exception as e:
            _xlog(f'[exec] live SELL FAILED: {e}')
            return None

    def manual_order(self, side):
        """Submit one operator-clicked market entry using the current market's BBO.

        BUY means buy YES; SELL means buy NO. It intentionally goes through the
        same risk caps and one-position-per-side rule as an automated signal.
        """
        if side not in ('BUY', 'SELL'):
            return False, f'manual order rejected: unknown side {side!r}'
        if not self.roll.get('slug'):
            return False, 'manual order rejected: waiting for the first market roll'
        if not self.book.bids or not self.book.asks:
            return False, 'manual order rejected: waiting for a complete Polymarket book'
        bb, ba = max(self.book.bids), min(self.book.asks)
        if not (0 < bb < ba < 1):
            return False, f'manual order rejected: invalid BBO {bb:.3f}/{ba:.3f}'
        return self.on_signal(dict(ts=time.time(), side=side, slug=self.roll['slug'],
                                   bb=bb, ba=ba, source='manual'))

    def _walk_book(self, no_side, stake):
        """Market-order fill simulation: spend `stake` dollars walking real book levels.
        YES buy walks asks ascending; NO buy walks bids descending (NO cost = 1 - bid)."""
        levels = self.book.bids if no_side else self.book.asks
        spend = shares = 0.0
        for p in sorted(levels, reverse=no_side):
            cost_px = (1 - p) if no_side else p
            if cost_px <= 0 or levels[p] <= 0: continue
            take = min(stake - spend, levels[p] * cost_px)
            if take <= 1e-9: break
            shares += take / cost_px; spend += take
        return shares, spend

    def _live_order(self, token, bbo_px):
        """Returns (filled_shares, cost_usd), or (0,0) if not CONFIRMABLY filled.
        Never fabricates a fill: an unparseable/zero-fill response books NO position and
        warns to reconcile the order id against the CLOB (an assumed fill = fictional
        risk/P&L). FOK/FAK don't rest, so 'no takingAmount' means the order didn't match."""
        try:
            from py_clob_client_v2 import OrderArgs, MarketOrderArgs, OrderType, Side
            if self.order_type == 'market':
                # dollar-denominated: spends up to stake USD ($1 exchange minimum). FOK =>
                # fill the whole budget or cancel (bounds slippage, no partial fills).
                r = self.client.create_and_post_market_order(
                    order_args=MarketOrderArgs(token_id=token,
                                               amount=round(max(self.stake, 1.0), 2),
                                               side=Side.BUY),
                    order_type=OrderType.FOK)
            else:
                shares = max(self.stake / max(bbo_px, 0.01), self.min_shares)
                r = self.client.create_and_post_order(
                    OrderArgs(price=round(bbo_px, 2), size=round(shares, 2),
                              side=Side.BUY, token_id=token),
                    OrderType.FAK)                          # no resting risk
            rd = r if isinstance(r, dict) else {}
            oid = rd.get('orderID') or rd.get('orderId') or rd.get('id')
            err = str(rd.get('errorMsg') or rd.get('error') or '')
            ok = bool(rd.get('success', False)) if rd else bool(r)
            _xlog(f'[exec] live order response (id={oid}, ok={ok}): {r}')
            if not ok or err:
                _xlog(f'[exec] order REJECTED (id={oid}): {err or "no success flag"} — no position booked.')
                return 0.0, 0.0
            # takingAmount is the authoritative filled size when present. Otherwise FOK/FAK
            # semantics make an accepted order all-or-nothing, so the requested size stands.
            # v2 returns these as STRINGS: takingAmount = shares received,
            # makingAmount = USDC actually spent. Prefer them over any assumption.
            taking = float(rd.get('takingAmount') or 0)
            making = float(rd.get('makingAmount') or 0)
            if self.order_type == 'market':
                filled = taking if taking > 0 else max(self.stake, 1.0) / max(bbo_px, 0.01)
                cost = making if making > 0 else max(self.stake, 1.0)
            else:
                filled = taking if taking > 0 else max(self.stake / max(bbo_px, 0.01), self.min_shares)
                cost = making if making > 0 else filled * bbo_px
            if filled <= 0:
                _xlog(f'[exec] WARNING: accepted but zero fill size (id={oid}) — no position booked.')
                return 0.0, 0.0
            return filled, cost      # filled: nothing rests, so nothing to track/cancel
        except Exception as e:
            _xlog(f'[exec] live order FAILED: {e}')
            return 0.0, 0.0

    # ---------- cycle end ----------
    def on_roll(self, ended_slug, final_mid):
        """Settle open positions in the ended market. final_mid ~0/1 near resolution."""
        if self.mode == 'live' and self.client and self.open_order_ids:
            # cancel ONLY orders this session placed — never a blanket cancel_all (that would
            # kill unrelated/manual orders on the same account). FOK/FAK rarely leave any.
            for oid in list(self.open_order_ids):
                try: self.client.cancel_order(oid)     # v2 API is cancel_order, not cancel
                except Exception as e: _xlog(f'[exec] cancel {oid}: {e}')
            self.open_order_ids.clear()
        # NEVER guess a winner. The old code forced final_mid=0.5 and then tested `>= 0.5`, so
        # every unknown settlement resolved as YES-won: a BUY booked a full profit it had not
        # earned. That corrupts paper P&L *and* disables risk — fake profit keeps daily_pnl
        # above the kill-switch threshold. An indecisive close is booked as a PROVISIONAL LOSS
        # (the conservative side for a risk limit) and corrected by verify_settlement.
        unresolved = final_mid is None or abs(final_mid - 0.5) < self.settle_band
        if unresolved:
            _xlog(f'[exec] WARNING: final price {final_mid} is not decisive '
                  f'(need |mid-0.5| >= {self.settle_band}); booking a PROVISIONAL LOSS pending '
                  f'the official result.')
        yes_won = None if unresolved else (final_mid >= 0.5)
        still, settled_rows = [], []
        for p in self.positions:
            if p['slug'] != ended_slug:
                still.append(p); continue
            won = False if unresolved else (yes_won if p['side'] == 'BUY' else not yes_won)
            payout = p['shares'] * (1.0 if won else 0.0)
            pnl = payout - p['cost']
            self._roll_day(); self.daily_pnl += pnl
            _xlog(f'[exec] SETTLE {p["side"]} {p["slug"]}: {"WIN" if won else "LOSS"} '
                  f'{pnl:+.2f} (day {self.daily_pnl:+.2f}) [proxy - verifying official result]')
            with open(self.log_path, 'a') as f:
                f.write(f'{p["ts"]},{self.mode},{p["slug"]},{p["side"]},{p["entry"]},'
                        f'{p["shares"]:.2f},{p["cost"]:.2f},'
                        f'{"" if final_mid is None else final_mid},{payout:.2f},'
                        f'{pnl:.2f},{self.daily_pnl:.2f}\n')
            settled_rows.append(dict(p, won=won, pnl=pnl))
        self.positions = still
        if settled_rows:
            self.settled[ended_slug] = dict(rows=settled_rows, attempts=0)
            self.verify_queue[ended_slug] = time.time() + self.verify_delay

    def verify_settlement(self, slug):
        """Reconcile a proxy settlement against the official Polymarket resolution.
        Blocking (HTTP) - run from a worker thread. Requeues itself while not final."""
        ent = self.settled.get(slug)
        if not ent: return
        try:
            yes_won = self._fetch(slug)
        except Exception as e:
            _xlog(f'[exec] verify {slug}: {e}'); yes_won = None
        if yes_won is None:
            ent['attempts'] += 1
            if ent['attempts'] < self.verify_attempts:
                self.verify_queue[slug] = time.time() + self.verify_retry
                _xlog(f'[exec] verify {slug}: result not final yet '
                      f'(attempt {ent["attempts"]}/{self.verify_attempts}); retrying')
            else:
                _xlog(f'[exec] verify {slug}: no official result after retries; proxy settlement stands')
                self.settled.pop(slug, None)
            return
        fixed = 0
        for r in ent['rows']:
            true_won = yes_won if r['side'] == 'BUY' else not yes_won
            if true_won == r['won']: continue
            payout = r['shares'] if true_won else 0.0
            pnl = payout - r['cost']
            delta = pnl - r['pnl']
            self._roll_day(); self.daily_pnl += delta
            fixed += 1
            _xlog(f'[exec] CORRECTION {r["side"]} {slug}: official result {"YES" if yes_won else "NO"} '
                  f'-> {"WIN" if true_won else "LOSS"} {pnl:+.2f} (delta {delta:+.2f}, '
                  f'day {self.daily_pnl:+.2f})')
            with open(self.log_path, 'a') as f:
                f.write(f'{r["ts"]},{self.mode}-corr,{slug},{r["side"]},{r["entry"]},'
                        f'{r["shares"]:.2f},{r["cost"]:.2f},{1.0 if yes_won else 0.0},{payout:.2f},'
                        f'{pnl:.2f},{self.daily_pnl:.2f}\n')
        if not fixed:
            _xlog(f'[exec] verified {slug}: official result confirms proxy settlement')
        self.settled.pop(slug, None)

    def _roll_day(self):
        day = time.strftime('%Y-%m-%d')
        if day != self.day:
            self.day, self.daily_pnl, self.killed = day, 0.0, False
