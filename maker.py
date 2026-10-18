# maker.py — RESTING two-sided quotes around the model's fair value.
#
# WHY THIS EXISTS. Polymarket charges `crypto_fees_v2` on the 5-minute up/down markets:
#     fee/share = 0.07 * p * (1-p),  takerOnly: True
# ~1.75c/share at the money, ~2.6c for a round trip at the prices this bot actually trades.
# Measured gross edge of the taking strategy over 16 recorded sessions is +0.78c/share. The
# fee is therefore larger than the entire edge, and it was introduced expressly to neutralise
# latency arbitrage. Taking cannot win here; the fee schedule says so arithmetically.
#
# MAKERS PAY NOTHING. So the same fair-value model, expressed as resting quotes, keeps the
# edge instead of donating it: you are paid the spread rather than paying it, and you owe no
# fee. scripts/sweep_fees.py scores this as design C/D (the only positive ones).
#
# THE INVERSION. The measured killer for a maker is adverse selection, and this repo already
# measured it from the other direction: FINDINGS.md §1 shows that the HARDER poly moves
# against the exchange, the more often poly turns out to be RIGHT (|z|>=3 -> -26% EV/$, SIG).
# As a taker that finding killed the fade. As a maker it is the toxicity signal: the same
# robust-z that used to mean "trade now" here means "PULL YOUR QUOTES NOW". Everything the
# analytics engine already computes is reused with the opposite sign.
#
# WHAT THIS IS NOT. It is not proven to make money. `rewardsMaxSpread=0` on these markets, so
# there is no liquidity-rewards subsidy to hide behind (only the 20% maker rebate on taker
# fees). Run it in paper first, then at the smallest live size that clears orderMinSize.
import math, time

try:
    from logbook import trade as _t, err as _e
except Exception:
    _t = _e = print

TICK = 0.01          # orderPriceMinTickSize on the 5m up/down markets
MIN_SHARES = 5.0     # orderMinSize; a smaller limit order is rejected outright


def _mlog(msg):
    (_e if any(k in msg.lower() for k in ('fail', 'reject', 'error', 'cannot')) else _t)(msg)


def _fee_from_response(st, filled_shares, price):
    """Extract the fee (USDC) a CLOB order/fill response reports, across the fields the API has
    used at various times. Returns 0.0 when it genuinely reports none. This is instrumentation
    for the maker-pays-zero premise, so it errs toward REPORTING a fee, not hiding one:
      - an explicit fee amount (`fee`, `fee_usdc`, `feePaid`) is taken as-is;
      - a rate in bps (`fee_rate_bps`, `maker_fee_rate_bps`) is applied to the fill notional
        (shares * price) so a nonzero rate surfaces as a nonzero fee to alarm on."""
    if not isinstance(st, dict):
        return 0.0
    for k in ('fee', 'fee_usdc', 'feePaid', 'fee_paid', 'maker_fee'):
        v = st.get(k)
        if v not in (None, ''):
            try:
                return abs(float(v))
            except (TypeError, ValueError):
                pass
    for k in ('maker_fee_rate_bps', 'fee_rate_bps', 'feeRateBps'):
        v = st.get(k)
        if v not in (None, ''):
            try:
                return abs(float(v)) / 1e4 * filled_shares * price
            except (TypeError, ValueError):
                pass
    return 0.0


def _safe_extreme(d, fn):
    """max()/min() over a dict of book levels that a DIFFERENT thread may be mutating. The
    maker runs on its own thread (audit M3) while the websocket reader rebuilds POLY_BOOK, so a
    bare max(book.bids) can raise 'dictionary changed size during iteration'. Retry on a
    consistent copy; give up as 'no book' rather than crash the quote loop."""
    for _ in range(4):
        try:
            snap = list(d)
            return fn(snap) if snap else None
        except RuntimeError:
            continue
    return None


def _level_size(book, px):
    """Total resting size at the tick `px` in a book that another thread may be mutating.
    Matches on the 2-decimal tick (book keys and our quotes are both cent-grid, but float
    representation makes 0.49 != 0.48999...). 0.0 if nothing is there."""
    if px is None:
        return 0.0
    target = round(px, 2)
    for _ in range(4):
        try:
            return float(sum(s for p, s in list(book.items()) if round(p, 2) == target))
        except RuntimeError:
            continue
    return 0.0


def round_to_tick(p, side):
    """Round INTO the book, never through it: a bid rounds down, an ask rounds up. Rounding
    the wrong way turns a passive quote into a crossing (taker) order and reintroduces the
    exact fee this whole module exists to avoid."""
    if side == 'bid':
        return math.floor(p / TICK + 1e-9) * TICK
    return math.ceil(p / TICK - 1e-9) * TICK


class MakerQuoter:
    """Two-sided resting quotes on ONE market. Owns only its own orders.

    Live orders go through `executor.client`, which exists only after Executor._arm_live()
    passed POLY_DRIFT_ARM=YES-LIVE *and* a typed ARM. There is deliberately no other path to
    a client here, so maker mode cannot bypass the arming lock."""

    # kappa is NOT the textbook value. Avellaneda-Stoikov assumes a continuous price; here the
    # tick is 0.01 and the median market spread is exactly 1 tick. The classic kappa~6 yields a
    # 13.5c half-spread -- a 0.36/0.64 quote into a 0.49/0.51 market, which never fills and
    # makes the whole engine inert. (1/gamma)*ln(1+gamma/kappa) = 1 tick needs kappa ~= 100.
    def __init__(self, executor, book, roll, cfg, shares=MIN_SHARES, gamma=3.0, kappa=100.0,
                 toxic_z=2.5, min_edge_c=0.5, max_inventory=2, inv_skew_ticks=1.0,
                 latency_log='maker_latency.csv', fills_log='maker_fills.csv'):
        self.ex, self.book, self.roll, self.cfg = executor, book, roll, cfg
        self.shares = max(float(shares), MIN_SHARES)
        self.gamma, self.kappa = float(gamma), float(kappa)
        self.inv_skew_ticks = float(inv_skew_ticks)   # reservation lean, in TICKS, at full cap
        self.toxic_z = float(toxic_z)
        self.min_edge_c = float(min_edge_c)   # don't quote if our edge over the touch is dust
        self.max_inventory = int(max_inventory)
        self.resting = {}                     # 'bid'/'ask' -> dict(price, size, id, ts, slug)
        self.inventory = 0.0                  # net YES shares (long YES > 0, long NO < 0)
        self.fills = []
        self.last_slug = None
        self.pulled_reason = None
        self.latency_log = latency_log
        self.fills_log = fills_log
        self._lat_init = False
        self._fills_init = False
        # last decision context, stamped onto each fill so markout can be split by regime
        self._ctx = dict(fair=None, z=None, sigma=None, tau=None)
        self.stats = dict(quotes=0, cancels=0, fills=0, pulls=0, rejects=0)

    # ---------- pricing ----------
    def quote_prices(self, fair, sigma_per_s, tau_s):
        """Avellaneda-Stoikov, specialised to a binary that expires at tau.

            reservation r = fair - q * gamma * sigma^2 * tau
            half-spread d = gamma*sigma^2*tau/2 + (1/gamma)*ln(1 + gamma/kappa)

        For a binary the terminal payoff variance is p(1-p), which is the SAME shape as the
        fee schedule, so inventory risk and fee both peak at 0.50 — quoting wider near the
        money is right for both reasons at once.

        Returns (bid, ask) tick-rounded, or (None, None) if there is no sane quote."""
        if fair is None or not (0.0 < fair < 1.0):
            return None, None
        var = max((sigma_per_s or 0.0) ** 2 * max(tau_s or 0.0, 0.0), 0.0)
        pv = max(fair * (1.0 - fair), 1e-6)
        # INVENTORY SKEW in TICK units (audit M5). The textbook A-S term `inventory*gamma*var*pv`
        # is ~1e-4c at this asset's vol -- utterly inert against a 1c tick, so the only working
        # risk control was the hard cap. Instead lean the reservation price by up to
        # `inv_skew_ticks` ticks as net inventory runs from 0 to the cap, saturating there. This
        # is a heuristic sizing choice, NOT a fitted edge: the point is that the control MOVES.
        cap = self.max_inventory * self.shares
        qn = 0.0 if cap <= 0 else max(-1.0, min(1.0, self.inventory / cap))
        r = fair - self.inv_skew_ticks * TICK * qn
        # half-spread: the kappa term is ~1 tick at the defaults (gamma 3, kappa 100), plus the
        # (usually negligible) variance widening. Floored at half a tick.
        d = self.gamma * var * pv / 2.0 + (1.0 / self.gamma) * math.log1p(self.gamma / self.kappa)
        d = max(d, TICK / 2.0)
        bid = round_to_tick(min(max(r - d, TICK), 1 - TICK), 'bid')
        ask = round_to_tick(min(max(r + d, TICK), 1 - TICK), 'ask')
        if ask - bid < TICK:                  # never quote a crossed/locked pair
            bid, ask = round_to_tick(bid - TICK / 2, 'bid'), round_to_tick(ask + TICK / 2, 'ask')
        if bid < TICK or ask > 1 - TICK or bid >= ask:
            return None, None
        return bid, ask

    # ---------- toxicity ----------
    def toxicity(self, z, tau_s, poly_fresh, fair):
        """Reasons to have NO quotes resting. Returns a reason string, or None to quote.

        Order matters only for the log line; any one of these is disqualifying."""
        if self.ex is not None and getattr(self.ex, 'killed', False):
            return 'kill_switch'
        if not self.roll.get('slug'):
            return 'no_market'
        if fair is None:
            return 'no_fair'
        if not poly_fresh:
            return 'stale_book'
        if tau_s is not None and tau_s <= self.cfg.get('ENTRY_CUTOFF_S', 40):
            return 'near_expiry'
        # THE INVERSION: a big robust-z is informed flow. Taking it lost 26% EV/$; resting
        # into it means being the counterparty to whoever is right.
        if z is not None and abs(z) >= self.toxic_z:
            return 'toxic_z'
        # NOTE: the inventory cap is deliberately NOT here. Pulling BOTH sides when full would
        # cancel the offloading quote and freeze the position to resolution; the cap is applied
        # per-side in on_tick so the reducing side stays live.
        return None

    # ---------- order plumbing ----------
    def _post(self, side, price, size):
        """Place ONE resting order. Returns (order_id, latency_s) or (None, latency_s)."""
        t0 = time.time()
        token = self.roll.get('token') if side == 'bid' else self.roll.get('token_no')
        # An ASK on YES is economically a BID on NO. Quoting it as a NO bid keeps every order
        # a BUY, which is what the CLOB wants and avoids needing YES inventory to sell.
        px = price if side == 'bid' else round_to_tick(1.0 - price, 'bid')
        if token is None:
            self.stats['rejects'] += 1
            return None, time.time() - t0
        if self.ex is None or self.ex.mode == 'paper':
            return f'paper-{side}-{int(t0*1000)}', time.time() - t0
        try:
            from py_clob_client_v2 import OrderArgs, OrderType, Side
            # KEYWORDS ARE MANDATORY HERE. The signature is
            #   create_and_post_order(order_args, options, order_type, post_only, ...)
            # so passing OrderType positionally lands it in `options`, and since OrderType.GTC
            # is the plain string 'GTC' the client then does options.tick_size and dies with
            #   'str' object has no attribute 'tick_size'
            # -- which silently killed EVERY live maker order. options=None lets the client
            # resolve the market's tick size itself (correct on any market; it caches).
            # post_only=True makes the EXCHANGE guarantee the order rests or is rejected, so a
            # maker order can never cross and pay the taker fee even if our price clamp is wrong.
            r = self.ex.client.create_and_post_order(
                OrderArgs(price=round(px, 2), size=round(size, 2), side=Side.BUY,
                          token_id=token),
                options=None, order_type=OrderType.GTC, post_only=True)
            rd = r if isinstance(r, dict) else {}
            oid = rd.get('orderID') or rd.get('orderId') or rd.get('id')
            err = str(rd.get('errorMsg') or rd.get('error') or '')
            if err or not rd.get('success', False) or not oid:
                self.stats['rejects'] += 1
                _mlog(f'[maker] {side} REJECTED @{px:.2f}x{size:.2f}: {err or "no id"}')
                return None, time.time() - t0
            return oid, time.time() - t0
        except Exception as e:
            self.stats['rejects'] += 1
            _mlog(f'[maker] {side} post FAILED: {e}')
            return None, time.time() - t0

    def _cancel(self, side):
        o = self.resting.pop(side, None)
        if not o:
            return
        self.stats['cancels'] += 1
        if self.ex is not None and self.ex.mode == 'live' and not str(o['id']).startswith('paper-'):
            try:
                self.ex.client.cancel_order(o['id'])
            except Exception as e:
                # A cancel that fails leaves a LIVE order resting. Say so loudly: silently
                # dropping it from self.resting would make the bot blind to real exposure.
                _mlog(f'[maker] CANCEL FAILED for {side} {o["id"]}: {e} — ORDER MAY STILL REST')

    def cancel_all(self, reason=''):
        for side in list(self.resting):
            self._cancel(side)
        if reason:
            _mlog(f'[maker] flat: cancelled all quotes ({reason})')

    def reconcile_open_orders(self, tokens):
        """Sweep ORPHANED resting orders on this market's tokens (audit residual #2). After a
        crash/restart the bot has no memory of orders it left resting; they would double our
        exposure and desync inventory. Cancel any open order on `tokens` that we are NOT
        currently tracking. Scoped to OUR market's tokens so it never kills a manual order on
        some other market, and it excludes our own live quotes by id. Live only; safe no-op
        otherwise. Runs on the maker thread before we place new quotes on the market."""
        if self.ex is None or getattr(self.ex, 'mode', None) != 'live' or self.ex.client is None:
            return 0
        want = set(t for t in (tokens or []) if t)
        try:
            orders = self.ex.client.get_open_orders() or []
        except Exception as e:
            _mlog(f'[maker] startup reconcile: get_open_orders failed: {e} — proceeding')
            return 0
        tracked = {o['id'] for o in self.resting.values()}
        n = 0
        for od in orders:
            if not isinstance(od, dict):
                continue
            oid = od.get('id') or od.get('orderID') or od.get('order_id')
            asset = od.get('asset_id') or od.get('token_id') or od.get('market')
            if not oid or oid in tracked:
                continue                        # our own live quote — leave it
            if want and asset not in want:
                continue                        # a different market — never touch it
            try:
                self.ex.client.cancel_order(oid); n += 1
            except Exception as e:
                _mlog(f'[maker] reconcile: cancel orphan {oid} failed: {e}')
        if n:
            _mlog(f'[maker] reconcile: cancelled {n} orphaned order(s) on this market — flat')
        return n

    # ---------- the tick ----------
    def on_tick(self, fair, sigma_per_s, tau_s, z, poly_fresh=True, now=None):
        """Call once per analytics tick. Idempotent: it only cancels/replaces when the desired
        price actually moved, because churning quotes burns rate limit and queue position."""
        now = now or time.time()
        self._ctx = dict(fair=fair, z=z, sigma=sigma_per_s, tau=tau_s)
        slug = self.roll.get('slug')
        if slug != self.last_slug:              # the market rolled under us (or first tick)
            self.cancel_all('market rolled')
            # sweep any orphaned resting orders on the market we're about to quote BEFORE we
            # place new ones, so a prior crash can't leave us double-quoting (residual #2)
            self.reconcile_open_orders([self.roll.get('token'), self.roll.get('token_no')])
            self.inventory = 0.0
            self.last_slug = slug
        reason = self.toxicity(z, tau_s, poly_fresh, fair)
        if reason:
            if self.resting:
                self.stats['pulls'] += 1
                self.cancel_all(reason)
            self.pulled_reason = reason
            return
        self.pulled_reason = None
        bid, ask = self.quote_prices(fair, sigma_per_s, tau_s)
        if bid is None:
            self.cancel_all('no sane quote')
            return
        bb = _safe_extreme(self.book.bids, max)
        ba = _safe_extreme(self.book.asks, min)
        # Never post through the touch: that is a marketable order and pays the taker fee.
        if ba is not None:
            bid = min(bid, round_to_tick(ba - TICK, 'bid'))
        if bb is not None:
            ask = max(ask, round_to_tick(bb + TICK, 'ask'))
        cap = self.max_inventory * self.shares
        for side, px in (('bid', bid), ('ask', ask)):
            # Directional inventory cap: a bid adds to long YES, an ask adds to short YES. When
            # the cap is hit on one side, cancel ONLY that side so the opposite (reducing) quote
            # keeps resting and can flatten the book back toward zero.
            if (side == 'bid' and self.inventory >= cap) or \
               (side == 'ask' and self.inventory <= -cap):
                self._cancel(side)
                continue
            if px is None or px <= 0 or px >= 1:
                self._cancel(side)
                continue
            cur = self.resting.get(side)
            if cur and abs(cur['price'] - px) < TICK / 2:
                continue                        # already where we want to be — keep the queue
            if cur:
                self._cancel(side)
            oid, lat = self._post(side, px, self.shares)
            self._log_latency(now, side, px, lat, oid)
            if oid:
                self.stats['quotes'] += 1
                # queue AHEAD of us at our price (FIFO): size already resting there when we
                # joined. In paper this is the whole reason we don't fill on a mere touch.
                lvl = self.book.bids if side == 'bid' else self.book.asks
                self.resting[side] = dict(price=px, size=self.shares, id=oid, ts=now, slug=slug,
                                          booked=0.0, queue=_level_size(lvl, px))

    # ---------- fills ----------
    def simulate_paper_fills(self, now=None):
        """Paper mode with FIFO QUEUE PRIORITY (audit residual #1).

        The old model filled the instant the opposite touch reached our price — i.e. it assumed
        we were always first in line. In a 1-tick book you are usually behind whatever size was
        already resting at that price. Here, when the opposite side crosses to our level, the
        incoming marketable flow (proxied by the size resting at the crossing level) eats the
        `queue` ahead of us FIRST; only once that queue is exhausted do WE fill. This UNDER-states
        fills relative to reality (we ignore that the queue also drains from cancels), which is
        the safe direction for deciding whether to risk real money.

        It remains a simulation: without the trade tape we proxy sweep size by the crossing
        level's resting size. Treat paper fill COUNT as a floor, not a forecast."""
        now = now or time.time()
        bb = _safe_extreme(self.book.bids, max)
        ba = _safe_extreme(self.book.asks, min)
        for side in list(self.resting):
            o = self.resting[side]
            if side == 'bid':
                crossing = ba is not None and ba <= o['price'] + 1e-9
                incoming = _level_size(self.book.asks, ba) if crossing else 0.0
            else:
                crossing = bb is not None and bb >= o['price'] - 1e-9
                incoming = _level_size(self.book.bids, bb) if crossing else 0.0
            if not crossing:
                continue
            o['queue'] = o.get('queue', 0.0) - incoming     # FIFO: others ahead fill first
            if o['queue'] > 1e-9:
                continue                                    # still behind the queue at our price
            self._book_fill(side, o['price'], o['size'], now)
            self.resting.pop(side, None)

    def poll_live_fills(self, now=None):
        """Live mode: reconcile against the CLOB. An order we hold that is no longer open was
        either filled or cancelled — ask for it explicitly rather than assuming, because
        assuming a fill fabricates inventory and assuming a cancel hides real exposure."""
        now = now or time.time()
        if self.ex is None or self.ex.mode != 'live' or not self.resting:
            return
        for side in list(self.resting):
            o = self.resting[side]
            try:
                st = self.ex.client.get_order(o['id']) or {}
            except Exception as e:
                _mlog(f'[maker] get_order failed for {o["id"]}: {e} — leaving it tracked')
                continue
            # size_matched is CUMULATIVE on the order, so book only the NEW fill since the last
            # poll. Re-booking the cumulative figure every tick inflates inventory: a 2-then-5
            # partial fill would book 2 then 5 = 7 shares for a 5-share order.
            matched = float(st.get('size_matched') or st.get('sizeMatched') or 0)
            status = str(st.get('status') or '').upper()
            delta = matched - o.get('booked', 0.0)
            if delta > 1e-9:
                # Capture the ACTUAL fee the CLOB reports on this fill, if any. The maker premise
                # says this is zero; instrument it so the first real fill proves or breaks that
                # instead of us assuming. Read defensively across the fields the API might use.
                fee = _fee_from_response(st, delta, o['price'])
                self._book_fill(side, o['price'], delta, now, fee=fee)
                o['booked'] = matched
            if matched >= o['size'] - 1e-9 or status in ('MATCHED', 'FILLED'):
                self.resting.pop(side, None)
            elif status in ('CANCELED', 'CANCELLED', 'EXPIRED'):
                self.resting.pop(side, None)

    def _book_fill(self, side, price, size, now, fee=0.0):
        """A maker fill (fee SHOULD be 0), and the leg we acquired is YES on a bid, NO on an ask.

        The position is handed to the Executor so it settles at cycle end through the SAME
        on_roll/verify_settlement path as a taker entry and counts against the daily-loss cap.
        Without this the fill only moved a local `inventory` counter that was silently zeroed at
        the next roll -- real money in, no P&L, no risk accounting (audit M1). `self.inventory`
        stays as a per-cycle quoting aid; the money is owned by the Executor."""
        self.stats['fills'] += 1
        self.inventory += size if side == 'bid' else -size
        # mid AT THE MOMENT OF THE FILL is the markout baseline: (mid0 - price) is the spread we
        # captured, and everything the mid does afterwards is adverse selection (or luck).
        bb = _safe_extreme(self.book.bids, max)
        ba = _safe_extreme(self.book.asks, min)
        mid0 = (bb + ba) / 2.0 if (bb and ba) else None
        rec = dict(ts=now, slug=self.roll.get('slug'), side=side, price=price, size=size,
                   inventory=self.inventory, fee=fee, mid0=mid0,
                   fair=self._ctx.get('fair'), z=self._ctx.get('z'),
                   tau=self._ctx.get('tau'))
        self.fills.append(rec)
        self._log_fill(rec)
        if self.ex is not None and getattr(self.ex, 'register_maker_fill', None):
            # bid fill -> hold YES (BUY, YES token); ask fill -> hold NO (SELL, NO token)
            exec_side = 'BUY' if side == 'bid' else 'SELL'
            token = self.roll.get('token') if side == 'bid' else self.roll.get('token_no')
            self.ex.register_maker_fill(self.roll.get('slug'), exec_side, size, price, token,
                                        now, fee=fee)
        _mlog(f'[maker] FILL {side} {size:.2f} sh @ {price:.2f} (no fee) '
              f'-> inventory {self.inventory:+.2f} YES-equivalent')

    # ---------- telemetry ----------
    def _log_fill(self, rec):
        """One row per FILL. This is the input to scripts/markout.py — the only way to answer
        the question that decides whether market making here is a business: after someone hits
        our quote, which way does the market go? Fills that are systematically followed by
        adverse moves mean we are being picked off faster than the toxicity gate can pull."""
        try:
            if not self._fills_init:
                with open(self.fills_log, 'a') as f:
                    if f.tell() == 0:
                        f.write('ts,slug,side,price,size,fee,mid0,fair,z,tau,inventory,mode\n')
                self._fills_init = True
            n = lambda v: '' if v is None else f'{v:.6g}'
            with open(self.fills_log, 'a') as f:
                f.write(f"{rec['ts']},{rec['slug'] or ''},{rec['side']},{rec['price']:.4f},"
                        f"{rec['size']:.4f},{rec['fee']:.6f},{n(rec['mid0'])},{n(rec['fair'])},"
                        f"{n(rec['z'])},{n(rec['tau'])},{rec['inventory']:.4f},"
                        f"{getattr(self.ex, 'mode', 'off')}\n")
        except Exception:
            pass          # telemetry must never break trading

    def _log_latency(self, now, side, px, latency_s, oid):
        """One row per order attempt. This is the file to read after a live run: it is the
        only place the REAL post->ack round trip is recorded."""
        try:
            if not self._lat_init:
                with open(self.latency_log, 'a') as f:
                    if f.tell() == 0:
                        f.write('ts,slug,side,price,latency_ms,order_id,ok,mode,inventory\n')
                self._lat_init = True
            with open(self.latency_log, 'a') as f:
                f.write(f'{now},{self.roll.get("slug") or ""},{side},{px:.2f},'
                        f'{latency_s*1000:.1f},{oid or ""},{1 if oid else 0},'
                        f'{getattr(self.ex, "mode", "off")},{self.inventory:.2f}\n')
        except Exception:
            pass          # telemetry must never break trading

    def status_line(self):
        r = ' '.join(f'{s}@{o["price"]:.2f}' for s, o in sorted(self.resting.items())) or '(none)'
        return (f'[maker] quotes {r} | inv {self.inventory:+.1f} | '
                f'{"PULLED:" + self.pulled_reason if self.pulled_reason else "quoting"} | '
                f'q{self.stats["quotes"]} c{self.stats["cancels"]} f{self.stats["fills"]} '
                f'p{self.stats["pulls"]} rej{self.stats["rejects"]}')
