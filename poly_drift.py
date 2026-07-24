#!/usr/bin/env python3
# poly_drift.py — exploit-side drift monitor: aggregated spot/perp (Binance+Bybit+OKX) lead -> Polymarket executable edge
# pip install websockets numpy pandas matplotlib
# usage (fixed market): python poly_drift.py --token <CLOB_TOKEN_ID> [--symbol btcusdt]
# usage (auto-roll 5m): python poly_drift.py --series btc-updown-5m [--symbol btcusdt]   # slug substring match, rolls each cycle
# token_id: gamma-api.polymarket.com/markets?slug=<slug> -> clobTokenIds[0] (YES leg)
# feeds: 6 venue sockets -> composite 'spot' & 'perp' series (offset-aligned log avg, summed $vol);
#        analytics/signals run on the composites, so any venue can drop out (ISP blocks) without stalling.

import argparse, asyncio, json, os, re, sys, threading, time, urllib.request
from collections import deque
import numpy as np, pandas as pd
import websockets
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from drift_math import (grid_series, grid_vol, composite_px, logit, expit, rets,
                        xcorr_best, beta_residual, robust_z, binary_p_up,
                        realized_sigma_per_s, calibrate_prob, drift_z, fair_from_mid,
                        taker_fee, empirical_p_up)
from recorder import Recorder
try:
    from logbook import (init_logs, err as L_err, warn as L_warn,
                         signal as L_sig, data as L_data, session as L_sess,
                         state_row as L_state)
except Exception:                       # logbook optional; degrade to plain print
    def _lb_noop(m): print(m); return m
    L_err = L_warn = L_sig = L_data = L_sess = _lb_noop
    def L_state(row): pass
    def init_logs(*a, **k): pass

CFG = dict(
    WINDOW_S=600,        # plot/analysis window
    MAX_LAG_S=30,        # xcorr lag search
    XCORR_WIN_S=300,     # rolling xcorr window
    DRIFT_H_S=10,        # drift horizon (s): ref moved, poly hasn't
    DRIFT_Z_ALERT=2.0,
    LOGIT_POLY=True,     # analyze poly in logit space  
    PRINT_EVERY_S=5,
    EWMA_HL_S=30,        # volume EWMA half-life
    EDGE_MIN_C=1.0,      # min executable edge past bid/ask, in cents
    VOL_Z_MIN=1.5,       # ref $vol z-gate over drift horizon
    SIG_COOLDOWN_S=15,   # per-side signal cooldown
    MIN_CORR=0.15,       # min |xcorr| to trust beta/fair value
    ROLL_POLL_S=3,       # gamma poll cadence in --series mode
    EVAL_H_S=45,         # score each signal vs poly mid this many sec later
    STALE_S=30,          # venue quotes older than this drop out of composites
    CORR_SIGN=0,         # required corr sign for signals (0 = any); +1 for up/down series
    ENTRY_MAX_PX=1.00,   # max price per share we'll pay. NOTE: this does NOT bound dollar
                         # risk — max loss per trade is the STAKE either way (a $1 order loses
                         # $1 whether the share cost 0.20 or 0.74). It only filters WHICH
                         # trades are allowed. The old 0.45 "cheap entry" cap was a leftover
                         # from the buy-cheap-tails thesis and silently blocked 52% of
                         # otherwise-qualifying model signals (measured in live logs), because
                         # the model is most confident exactly where price is high. Use
                         # --stake / --max-daily-loss for risk; set this <1 only to
                         # deliberately restrict to underdogs.
    # --- FAIR VALUE SOURCE. 'drift' is the exploit thesis: Binance/OKX/Bybit are the true
    #     price, poly should track them, and the tradeable quantity is how far poly has strayed
    #     from what the exchange move justifies (the beta residual). It needs NO strike, so it
    #     works on the first cycle after startup and never depends on a guessed open price.
    #     'model' is the strike binary (spot vs strike) -- an absolute valuation, not a
    #     dislocation measure. 'both' requires the two to agree on direction. ---
    # --- REVERSION CALIBRATION. fair_from_mid(mid, dev) undoes the ENTIRE unjustified move,
    #     i.e. it assumes the gap closes 100%. MEASURED on 1018 candidates it closes 3-15%,
    #     with 95% CIs spanning zero and R^2 ~ 0.001:
    #         10s slope 0.030 [-0.05,+0.11] | 40s 0.086 [-0.06,+0.23] | 60s 0.131 [-0.04,+0.30]
    #     So the raw `edge` this engine prints is inflated roughly 7-30x, and the predicted gap
    #     explains almost none of the realised move. Two consequences:
    #       1. Every logged "edge 21.8c" is a raw displacement, NOT an expected profit.
    #       2. RT_GATE compares that inflated edge against a REAL cost (spread+fee), so it is
    #          ~10x too lenient. Set REVERT_FRAC to the fitted value to make them comparable --
    #          then MIN_EDGE/MIN_EDGE_ROI need recalibrating to the new scale.
    #     Default stays 1.0 because every threshold in this file was tuned against the raw
    #     scale; changing it silently would invalidate them. Scaling is monotonic, so it does
    #     not reorder signals -- it only makes the numbers mean something. ---
    REVERT_FRAC=1.0,      # fraction of the unjustified move assumed to revert. 1.0 = raw
                          # (uncalibrated, edges inflated); ~0.10 = measured.
    FAIR_SOURCE='both',   # 'drift' | 'model' | 'both'
                          # MEASURED on 1018 candidates / 11 sessions (2026-07-22): requiring
                          # the two sources to agree is the single biggest filter, and the
                          # DISAGREEMENT case is the only statistically significant result in
                          # the whole study: agree +11.4% EV/$ (n=498) vs disagree -18.6%
                          # +/-14.1 (n=467, SIG). NOTE 'both' needs a trusted strike, so it
                          # cannot trade the first cycle after startup (gate 'no_strike').
    # --- WHICH DISLOCATION to trade. Both reduce to the same rule (trade toward exchange-
    #     implied fair); they differ only in who moved first, which changes how fast it should
    #     close and how much to trust it.
    #       catchup: |ref_move| >= |poly_move|  exchange moved, poly has not repriced yet
    #       fade:    |poly_move| >  |ref_move|  poly moved, exchange did not follow
    #     The old REF_DRIVEN=1 was exactly SCENARIO_MODE='catchup' and structurally forbade the
    #     fade, which is the cheap-entry setup. ---
    SCENARIO_MODE='catchup',  # 'both' | 'catchup' | 'fade'
                          # MEASURED: fade LOSES at scale (-5.6% EV/$, n=692) and gets worse
                          # the harder you filter it -- |z|>=3 is -26.0% +/-18.1 (SIG) and
                          # |z|>=4 is -33.1% +/-23.4 (SIG). Big poly moves are INFORMED, so
                          # tightening the fade threshold fades harder into information.
                          # catchup is +3.2% held / +6.2% with a tight take-profit (n=273-291).
                          # With FAIR_SOURCE='both' the fade is ~+4.9% +/-7.2 (n=310, not
                          # significant) -- set 'both' to trade it, but it is not the earner.
    FADE_TAU_MIN=0,      # min seconds left to take a FADE (0 = only ENTRY_CUTOFF_S applies).
    FADE_TAU_MAX=0,      # max seconds left to take a FADE (0 = no cap)
    MODEL_VETO=0.0,      # >0 and a trusted strike present: block a trade when the strike model
                         # gives the traded side less than this probability. Catches "poly
                         # crashed because spot really did cross the strike". 0 = off.
    MIN_MODEL_P=0.20,    # tau-feasibility: skip a drift entry when the exchange binary
                         # model gives the traded side < this win prob (underdog can't
                         # realistically cross the strike in the time left). 0 = disable
    MIN_EDGE=0.03,       # REQUIRED model edge FLOOR: P_model(traded side) - price ACTUALLY PAID.
                         # Live evidence: without this the favorite bought a 0.62 event at
                         # 0.70 (-8c edge). Covers fee+spread+model error. 0 = disable.
    # --- round-trip-cost gate: require the model edge to also clear the LIVE cost of trading
    #     (the current bid-ask spread, which adapts to the book) instead of only the fixed
    #     MIN_EDGE floor. Required edge = max(MIN_EDGE, RT_SPREAD_MULT*spread + fee + buffer). ---
    RT_GATE=1,           # 1 = on (protective); 0 = off (fixed MIN_EDGE only)
                         # DEFAULT CHANGED TO 1 (2026-07-23). With FEE_RATE_TAKER now modelling
                         # the real crypto_fees_v2 schedule, leaving this off means every gate
                         # is calibrated against a trading cost of ZERO that is really ~2.6c/
                         # share round trip -- larger than the whole measured gross edge.
    RT_SPREAD_MULT=1.0,  # how many round-trip spreads the edge must clear (1.0 = one full spread)
    # --- TAKER FEE. Polymarket ships `crypto_fees_v2` on the 5-minute up/down markets and it
    #     is the single largest cost in this strategy. Read live off gamma-api `feeSchedule`:
    #         {'exponent': 1, 'rate': 0.07, 'takerOnly': True, 'rebateRate': 0.2}
    #     fee/share = rate * (p*(1-p))**exponent, charged to TAKERS ONLY. Verify against the
    #     live market with scripts/check_fees.py before trusting these numbers with size --
    #     Polymarket has changed this schedule once already and will again. ---
    FEE_RATE_TAKER=0.07, # 0 = model the market as fee-free (WRONG for crypto markets)
    FEE_EXPONENT=1,      # power applied to p*(1-p); 1 = the documented schedule
    RT_FEE_C=0.0,        # EXTRA per-share round-trip cost in cents ON TOP of the fee schedule
                         # (measured slippage, gas, whatever). Was the whole fee model; now an
                         # additive fudge that should normally stay 0.
    RT_BUFFER_C=0.5,     # extra safety margin, cents, on top of spread+fee
    # --- active exit management (OFF by default; a drift entry is a TIMING scalp, so exit
    #     when the mispricing closes rather than riding it to resolution) ---
    EXIT_ENABLE=0,       # master switch. 0 = hold to resolution (old behavior).
    EXIT_TP_C=2.0,       # TAKE-PROFIT: exit when the position can be sold back for >= entry
                         # + this many cents/share (net of crossing the spread). 0 = off.
    EXIT_THESIS=1,       # THESIS-INVALIDATION: exit when the exchange model no longer shows
                         # positive edge for the side at the current price (move reversed or
                         # poly caught up). Cuts losers the fixed timer would ride. 0 = off.
    EXIT_THESIS_EDGE=0.0,# model-edge level at/below which the thesis is considered dead.
    EXIT_MIN_HOLD_S=3,   # anti-thrash: don't manage a position in its first few seconds
                         # (paper/entry both cross the spread, so t=0 edge is already thin).
    # --- research telemetry: one CSV row per tick with every price, book level, model number
    #     and gate decision. Cheap (~1 row/s, ~3k rows/hour) and it is the ONLY way to measure
    #     a dislocation after it has closed. Leave on unless disk is a concern. ---
    # --- REWARD:RISK gate. The bot stakes DOLLARS, not shares, so the objective is EV per
    #     dollar risked, not edge per share. A $1 buy loses $1 whatever the share cost, but the
    #     upside shrinks as price rises: at 0.88 you risk $7.30 to make $1 and a 6.5-point model
    #     error erases the whole edge, while the same probability edge at 0.30 returns 4x the
    #     capital. Requiring edge >= MIN_EDGE_ROI * price_paid makes expensive entries earn
    #     their keep instead of being waved through by a flat 3c floor. ---
    MIN_EDGE_ROI=0.06,   # required edge as a FRACTION of capital at risk. 0 = off.
                         # MEASURED, do not raise blindly: on fixtures/session_20260720 this
                         # peaked EV/$ at 0.06 (+21.1%) and 0.12 cut it to +7.0%, because it
                         # also rejects sound cheap entries whose edge is small in absolute
                         # terms. 0.06 blocks worse-than-~16:1 risk:reward; 0.12 is what it
                         # takes to block the observed 0.88 fill (edge 7.5c, ROI 8.6%).
    RANK_BY_ROI=1,       # rank the two sides by return on capital instead of absolute edge.
                         # NOTE this cannot change WHICH trade fires: edge_up + edge_dn =
                         # bb - ba = -spread, so at most one side is ever positive and both
                         # must clear a positive floor. It only decides which side the skip
                         # message reports, which matters for reading the logs. 0 = by edge.
    # --- VOL-SPIKE GUARD. Trailing 120s vol is a bad forecast of the NEXT tau seconds when vol
    #     is accelerating, and P_up is most sensitive to sigma exactly at the extremes where we
    #     pay the most. Observed live: a BUY fired at 0.88 with P_up 0.945 while volZ was 2.5,
    #     and poly promptly repriced 0.875 -> 0.825. Never let the model be MORE confident than
    #     the fastest vol estimate justifies. ---
    VOL_SPIKE_GUARD=1,   # 1 = sigma := max(slow, fast); 0 = trailing window only
    VOL_FAST_S=30,       # fast realized-vol window that catches an accelerating regime
    RESEARCH_STAKE=1.0,  # size (USD) the gates price against when NO executor exists
                         # (--mode off). Without it research walked no book at all and scored
                         # top-of-book, so `--mode off` stats were systematically better than
                         # the paper/live fills they were supposed to predict.
    FEED_KEEP_S=900,     # seconds of tick history to retain per feed. Must exceed WINDOW_S
                         # (analytics) AND the longest pending-signal evaluation lag (one cycle,
                         # ~300s), with margin. Raising this raises per-tick CPU LINEARLY --
                         # see Feed.trim. 900 covers a 300s window + a 300s cycle 3x over.
    STATE_LOG=1,         # 1 = write logs/<date>/<tag>/market_state.csv
    STRIKE_MAX_LAG_S=10, # 0 = DISABLE the check (replay/research only, never live).
                         # the strike is the exchange price at CYCLE OPEN. If we did not see
                         # the open within this many seconds (bot started mid-cycle, poller
                         # missed a cycle, feed was dark) the strike is UNKNOWABLE and the
                         # whole binary model is meaningless for that cycle -> sit it out.
                         # Capturing "price when we happened to look" silently reprices an
                         # in-the-money cycle as at-the-money for up to a full 5 minutes.
    VOL_LOOKBACK_S=120,  # window for the realized-vol estimate feeding the model
    ENTRY_CUTOFF_S=40,   # HARD no-entry window before cycle end: never open a position with
                         # less than this many seconds left (roll poll lags up to ROLL_POLL_S)
    POLY_STALE_S=15,     # block ALL signals if the poly book hasn't updated in this many sec
                         # (a dead book + moving exchange = phantom fair value; audit B5)
    ROLL_MASK_S=12,      # zero poly returns for this long after a roll: the old market's last
                         # price (~0.99/0.01) forward-fills into the new cycle until the new
                         # book arrives, which otherwise prints |z| of 4-5 (observed live)
    EDGE_OVERRIDE=0.0,   # CONVICTION OVERRIDE (0 = off). When the model edge is at least this
                         # large, skip the lead/lag TIMING gate and trade on the model alone.
                         # Rationale: the timing gate exists to avoid acting on a marginal
                         # model estimate; at a huge edge (e.g. spot clearly above the strike
                         # while poly still prices UP cheap) waiting for lead/lag just forfeits
                         # it. Safety gates (freshness, feasibility, cutoff, edge) still apply.
    # --- BINARY PRICER CALIBRATION. 'empirical' replaces Phi() with the MEASURED conditional
    #     frequency P(up | ln(S/K)/(sigma*sqrt(tau))) from 419,444 rows / 104,861 real cycles
    #     (walk-forward fit; see scripts/fit_calibration.py and drift_math._EMP_P).
    #     Out-of-sample Brier: raw Phi 0.13063 | CALIB_SLOPE 0.9 0.13271 | EMPIRICAL 0.12655.
    #     At +-10bp from the strike -- where these markets actually trade -- the two disagree
    #     by up to 11 POINTS, i.e. 11c/share against a 3c MIN_EDGE and a 1.75c fee. ---
    CALIB_MODE='empirical',  # 'empirical' (measured table) | 'logit' (Phi + CALIB_SLOPE)
    CALIB_SLOPE=1.5,     # only used when CALIB_MODE='logit'. WAS 0.9, which was BACKWARDS:
                         # the Gaussian is UNDER-confident in the body, not overconfident, so
                         # a slope <1 compresses toward 0.5 and makes it worse. The sweep is
                         # monotone improving to ~1.5. 1.0 = raw Phi.
)
PRESET_5M = dict(WINDOW_S=300, XCORR_WIN_S=90, MAX_LAG_S=15, DRIFT_H_S=5, SIG_COOLDOWN_S=10,
                 EVAL_H_S=30, CORR_SIGN=1)  # Up-leg must co-move positively with the ref

# ---------------- state ----------------
class Feed:
    def __init__(self, name):
        self.name = name
        # maxlen bounds MEMORY but not COST: every compute() converts the whole deque with
        # np.asarray, so cost is O(len). Measured: 2.7k quotes/feed = 13 ms/tick, but 400k =
        # 1696 ms/tick -- past the 1s analytics loop, and compute() runs ON the asyncio thread,
        # so it stalls the WebSocket readers too and the feeds fall further behind. A busy BTC
        # session reaches 400k in hours. trim() keeps only what the window can actually use.
        self.quotes = deque(maxlen=400_000)   # (ts, price)
        self.vols   = deque(maxlen=400_000)   # (ts, notional_usd)

    def trim(self, now, keep_s):
        """Drop entries older than `keep_s` seconds. Amortised O(1) per append: analytics only
        ever look back WINDOW_S, and pending signals only to their own cycle's close."""
        cut = now - keep_s
        q, v = self.quotes, self.vols
        # keep ONE quote older than the cutoff: grid_series forward-fills from the last known
        # price at or before the window start, so dropping it blanks the head of the grid.
        while len(q) > 1 and q[1][0] < cut:
            q.popleft()
        while v and v[0][0] < cut:
            v.popleft()
    def on_trade(self, ts, p, notional):
        self.quotes.append((ts, p)); self.vols.append((ts, notional))
        if RECORDER and self.name != 'poly':  # poly is recorded as raw book events
            RECORDER.rec({'k': 't', 'f': self.name, 'ts': ts, 'p': p, 'n': notional})
    def on_quote(self, ts, p):
        self.quotes.append((ts, p))
        if RECORDER and self.name != 'poly':
            RECORDER.rec({'k': 'q', 'f': self.name, 'ts': ts, 'p': p})

# signals.csv schema in ONE place: the header and the row writer used to be separate literals,
# so adding a field silently shifted every column to its right.
SIG_FIELDS = ['ts', 'side', 'exec_px', 'fair_px', 'edge_c', 'z', 'lag', 'corr', 'beta', 'ref',
              'vol_z', 'bb', 'ba', 'slug', 'scenario', 'dev', 'mid_at_entry', 'model_p']

RECORDER = None

FEEDS = {}
GROUPS = {'perp': ['bin-perp', 'byb-perp', 'okx-perp'],
          'spot': ['bin-spot', 'byb-spot', 'okx-spot']}

class PolyBook:
    def __init__(self): self.bids = {}; self.asks = {}
    def snapshot(self, ev):
        self.bids = {float(l['price']): float(l['size']) for l in (ev.get('bids') or ev.get('buys') or [])}
        self.asks = {float(l['price']): float(l['size']) for l in (ev.get('asks') or ev.get('sells') or [])}
    def change(self, ev):
        for c in ev.get('changes', []):
            book = self.bids if c.get('side') == 'BUY' else self.asks
            p, s = float(c['price']), float(c['size'])
            if s <= 0: book.pop(p, None)
            else: book[p] = s
    def bbo(self):
        bb = max(self.bids) if self.bids else None
        ba = min(self.asks) if self.asks else None
        return bb, ba
    def mid(self):
        bb, ba = self.bbo()
        return (bb + ba) / 2.0 if bb and ba else None

POLY_BOOK = PolyBook()
# end: cycle-close ts (for time-remaining); strike/strike_ref: exchange price captured at
# cycle open (favorite-confirmation reference)
ROLL = dict(token=None, token_no=None, slug=None, end=None, open=None,
            strike=None, strike_ref=None)
EXECUTOR = None  # execution.Executor when --mode paper|live; None = signals only
MAKER = None     # maker.MakerQuoter when --maker; None = the taker path (_signal)
ROLLS = deque(maxlen=100)  # roll timestamps (mask poly returns across market boundary)

# ---------------- feeds ----------------
async def ws_loop(name, url, on_open, on_msg, keepalive=None):
    delay = 2
    while True:
        got = False
        try:
            async with websockets.connect(url, ping_interval=15, ping_timeout=20, max_size=2**23) as ws:
                if on_open: await on_open(ws)
                ka = asyncio.ensure_future(keepalive(ws)) if keepalive else None
                try:
                    async for raw in ws:
                        got = True
                        try: on_msg(raw)
                        except Exception as e: L_warn(f"[{name}] parse err: {e}")
                finally:
                    if ka: ka.cancel()
        except Exception as e:
            L_warn(f"[{name}] reconnect in {delay}s: {e}")
        delay = 2 if got else min(delay * 2, 60)  # back off while a venue stays dark
        await asyncio.sleep(delay)

def sub_open(payload):
    async def o(ws): await ws.send(json.dumps(payload))
    return o

def ping_ka(payload, interval=20):
    async def ka(ws):
        while True:
            await asyncio.sleep(interval)
            try: await ws.send(payload)
            except Exception: return
    return ka

def binance_handler(feed):
    def h(raw):
        m = json.loads(raw)
        if m.get('e') == 'aggTrade':
            p, q = float(m['p']), float(m['q'])
            feed.on_trade(m['T'] / 1000.0, p, p * q)
    return h

def binance_futs_handler(feed):
    # combined stream: aggTrade (price+$vol) and bookTicker mid (price only; some
    # networks get quotes but no trade stream from fstream — mid keeps price alive)
    last_mid = [None]
    def h(raw):
        m = json.loads(raw)
        d = m.get('data', m)
        e = d.get('e')
        if e == 'aggTrade':
            p, q = float(d['p']), float(d['q'])
            feed.on_trade(d['T'] / 1000.0, p, p * q)
        elif e == 'bookTicker':
            mid = (float(d['b']) + float(d['a'])) / 2.0
            if mid != last_mid[0]:
                last_mid[0] = mid
                feed.on_quote(float(d.get('T') or d.get('E') or time.time() * 1000) / 1000.0, mid)
    return h

def bybit_handler(feed):
    def h(raw):
        m = json.loads(raw)
        if 'publicTrade' in str(m.get('topic', '')):
            for t in m.get('data', []):
                p, v = float(t['p']), float(t['v'])
                feed.on_trade(t['T'] / 1000.0, p, p * v)
    return h

def okx_handler(feed_spot, feed_perp, swap_id, ctval):
    def h(raw):
        if raw == 'pong': return
        m = json.loads(raw)
        arg = m.get('arg') or {}
        if arg.get('channel') != 'trades': return
        perp = arg.get('instId') == swap_id
        feed = feed_perp if perp else feed_spot
        for t in m.get('data', []):
            p, sz = float(t['px']), float(t['sz'])
            mult = ctval if (perp and ctval) else 1.0  # swap sz is in contracts
            feed.on_trade(float(t['ts']) / 1000.0, p, p * sz * mult)
    return h

def _okx_ctval(inst):
    try:
        d = _http_json(f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={inst}")
        return float(d['data'][0]['ctVal'])
    except Exception:
        return None

def split_symbol(sym):
    for q in ('usdt', 'usdc', 'busd', 'usd'):
        if sym.endswith(q): return sym[:-len(q)], q
    return sym, ''

def apply_poly_event(ev, feed, book, now):
    # shared by live handler and replay.py — must stay clock-injected and global-free
    et = ev.get('event_type')
    if et == 'book':
        book.snapshot(ev)
    elif et == 'price_change':
        book.change(ev)
    elif et == 'last_trade_price':
        p = float(ev['price']); sz = float(ev.get('size', 0) or 0)
        feed.on_trade(now, p, p * sz)
        return
    else:
        return
    mid = book.mid()
    if mid: feed.on_quote(now, mid)

def poly_handler(feed):
    def h(raw):
        m = json.loads(raw)
        now = time.time()
        if RECORDER: RECORDER.rec({'k': 'praw', 'ts': now, 'ev': m})
        for ev in (m if isinstance(m, list) else [m]):
            apply_poly_event(ev, feed, POLY_BOOK, now)
    return h

async def poly_ws_task(on_msg):
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    while True:
        if not ROLL['token']:
            await asyncio.sleep(0.5); continue
        tok = ROLL['token']
        try:
            async with websockets.connect(url, ping_interval=15, ping_timeout=20, max_size=2**23) as ws:
                await ws.send(json.dumps({"assets_ids": [tok], "type": "market"}))
                async def keepalive():
                    while True:
                        await asyncio.sleep(10)
                        try: await ws.send("PING")
                        except Exception: return
                ka = asyncio.ensure_future(keepalive())
                try:
                    while ROLL['token'] == tok:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue  # re-check roll
                        if raw == "PONG": continue  # keepalive reply, not JSON
                        try: on_msg(raw)
                        except Exception as e: L_warn(f"[poly] parse err: {e}")
                finally:
                    ka.cancel()
        except Exception as e:
            L_warn(f"[poly] reconnect in 2s: {e}"); await asyncio.sleep(2)

def _http_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'poly-drift/1.0'})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())

async def roll_poller(series):
    loop = asyncio.get_running_loop()
    base = ("https://gamma-api.polymarket.com/markets?active=true&closed=false"
            "&order=endDate&ascending=true&limit=100")
    while True:
        try:
            # end_date_min: without it the API returns the 100 stalest open markets (all ended long ago)
            url = base + "&end_date_min=" + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            mkts = await loop.run_in_executor(None, _http_json, url)
            now = time.time(); cur = None
            for m in mkts:
                if series not in m.get('slug', ''): continue
                end = pd.Timestamp(m['endDate']).timestamp() if m.get('endDate') else 0
                if end > now: cur = m; break  # soonest-ending live cycle
            if cur:
                toks = cur.get('clobTokenIds')
                if isinstance(toks, str): toks = json.loads(toks)
                tok = toks[0]  # outcomes[0] leg (Up/Yes)
                if tok != ROLL['token']:
                    if EXECUTOR and ROLL['slug']:
                        # book is often already cleared at roll: fall back to last poly quote
                        final = POLY_BOOK.mid()
                        if final is None and FEEDS.get('poly') and FEEDS['poly'].quotes:
                            final = FEEDS['poly'].quotes[-1][1]
                        EXECUTOR.on_roll(ROLL['slug'], final)
                    POLY_BOOK.bids.clear(); POLY_BOOK.asks.clear()
                    # the cycle we are entering opened when the previous one closed. On the
                    # very first roll after startup there is no previous end, so the open is
                    # unknown and compute() will refuse to invent a strike for this cycle.
                    prev_end = ROLL['end']
                    ROLL['token'], ROLL['slug'] = tok, cur['slug']
                    ROLL['token_no'] = toks[1] if len(toks) > 1 else None
                    ROLL['end'] = end                 # cycle close ts, for time-remaining
                    ROLL['open'] = prev_end
                    ROLL['strike'] = ROLL['strike_ref'] = None  # recaptured at first compute
                    ROLLS.append(time.time())
                    if RECORDER: RECORDER.rec({'k': 'roll', 'ts': ROLLS[-1], 'token': tok,
                                               'token_no': ROLL['token_no'], 'slug': cur['slug'],
                                               'end': end, 'open': prev_end})  # end needed to replay tau/favorite logic
                    L_data(f"[roll] -> {cur['slug']} | ends {cur.get('endDate')} | token {tok[:18]}...")
        except Exception as e:
            L_err(f"[roll] poll err: {e}")
        await asyncio.sleep(CFG['ROLL_POLL_S'])

# ---------------- analytics ----------------
# (pure math lives in drift_math.py)

class Analytics:
    def __init__(self, siglog=None):
        self.snap = {}; self.signals = deque(maxlen=500)
        self.fair = None                  # latest exchange-implied fair value (set per tick)
        self.maker_input = None           # atomic handoff to the maker thread (None until set)
        self.last_sig = {'BUY': 0.0, 'SELL': 0.0}   # per-side cooldown
        self.last_gate = 'init'      # which gate blocked (or fired) on the latest tick
        self.pending = deque()                       # signals awaiting outcome eval
        self.tally = dict(n=0, win=0, pnl_c=0.0)     # session hit/miss record (drift only)
        self.siglog = siglog
        self.outlog = siglog.replace('.csv', '_outcomes.csv') if siglog else None
        if siglog and not os.path.exists(siglog):
            with open(siglog, 'w') as f:
                f.write(','.join(SIG_FIELDS) + '\n')
        # order_ok separates a research signal from an accepted/filled order (None = no executor).
        self._outhdr = "ts,strat,side,exec_px,eval_mid,eval_dt_s,pnl_c,win,order_ok,slug\n"
        if self.outlog and os.path.exists(self.outlog):
            with open(self.outlog) as f:
                if f.readline() != self._outhdr:        # schema drift: never corrupt an old file
                    self.outlog = self.outlog.replace('.csv', time.strftime('_%Y%m%d_%H%M%S.csv'))
        if self.outlog and not os.path.exists(self.outlog):
            with open(self.outlog, 'w') as f:
                f.write(self._outhdr)

    def compute(self, now=None):
        now = time.time() if now is None else now  # replay injects a virtual clock
        keep = CFG['FEED_KEEP_S']
        for f in FEEDS.values():
            f.trim(now, keep)
        grid = np.arange(now - CFG['WINDOW_S'], now, 1.0)
        px = {n: grid_series(f.quotes, grid, stale=None if n == 'poly' else CFG['STALE_S'])
              for n, f in FEEDS.items()}
        vol = {n: grid_vol(f.vols, grid) for n, f in FEEDS.items()}
        members = {}
        for g, mem in GROUPS.items():
            live = [m for m in mem if np.any(np.isfinite(px[m]))]
            members[g] = live
            px[g] = composite_px([px[m] for m in live]) if live else np.full(len(grid), np.nan)
            vol[g] = np.nansum([vol[m] for m in live], axis=0) if live else np.zeros(len(grid))
        self.snap = {'grid': grid, 'px': px, 'vol': vol, 'members': members}
        # capture the strike (exchange price at cycle open) once per cycle: the model fair
        # value is measured relative to it
        # An unknown cycle open counts as INFINITE lag: we cannot prove the strike is the
        # open price, so we do not invent one. CFG 0 disables the check for replay/research.
        strike_lag = (now - ROLL['open']) if ROLL['open'] is not None else float('inf')
        if ROLL['strike'] is None and (CFG['STRIKE_MAX_LAG_S'] <= 0
                                       or strike_lag <= CFG['STRIKE_MAX_LAG_S']):
            for g in ('spot', 'perp'):
                if not np.isnan(px[g][-1]):
                    ROLL['strike'], ROLL['strike_ref'] = float(px[g][-1]), g
                    break
        if np.isnan(px['poly'][-1]):
            # clear first: _write_state would otherwise stamp LAST tick's fair/dev/scenario with
            # this tick's timestamp, which reads as live data in the research CSV
            self.fair = None
            self.last_gate = 'no_poly'; self._write_state(now, {}, {}); return
        if all(np.isnan(px[g][-1]) for g in GROUPS):
            self.fair = None
            self.last_gate = 'no_ref'; self._write_state(now, {}, {}); return
        w = CFG['XCORR_WIN_S']; H = CFG['DRIFT_H_S']
        pr = rets(px['poly'], CFG['LOGIT_POLY'])
        for rts in list(ROLLS):  # zero returns spanning market boundary
            i = int(rts - grid[0])
            m = int(CFG['ROLL_MASK_S'])
            if -m < i < len(pr) + 1:
                for j in range(max(i - 1, 0), min(i + m, len(pr))): pr[j] = 0.0
        stats, drift, resid = {}, {}, {}
        alpha = 1 - np.exp(np.log(0.5) / CFG['EWMA_HL_S'])
        for n in GROUPS:
            if np.isnan(px[n][-1]): continue
            rr = rets(px[n])
            lag, c = xcorr_best(np.nan_to_num(rr[-w:]), np.nan_to_num(pr[-w:]), CFG['MAX_LAG_S'])
            beta, res, ref_h, poly_h = beta_residual(pr, rr, lag, w, H)
            if beta is None: continue           # not enough data yet
            rz = robust_z(res)                  # median/MAD trigger strength (fresh move?)
            if rz is None: continue             # gaps in the current window: no usable stat
            # ref $vol z over drift horizon
            vh = pd.Series(vol[n]).rolling(H).sum().to_numpy()[-w:]
            vmu, vsd = np.nanmean(vh), np.nanstd(vh)
            vz = (vh[-1] - vmu) / vsd if vsd > 0 else 0.0
            ew = pd.Series(vol[n]).ewm(alpha=alpha).mean().iloc[-1]
            dev, _ = drift_z(res)           # demeaned residual: how far poly has strayed
            stats[n] = dict(lag=lag, corr=c, beta=beta, vps=ew, vol_z=vz,
                            ref_move=ref_h, poly_move=poly_h, dev=dev)
            drift[n] = rz
        pvol = pd.Series(vol['poly']).ewm(alpha=alpha).mean().iloc[-1]
        # Rebind, never mutate in place. compute() runs on the asyncio thread while the
        # matplotlib animation reads AN.snap on the main thread; dict.update inserts keys one
        # at a time, so the plot could see 'stats' present and 'drift' still missing and die
        # with a KeyError. A single rebind is atomic under the GIL.
        self.snap = dict(self.snap, stats=stats, drift=drift, pvol=pvol)
        # fair value once per tick, BEFORE exits and entries so both price off the same number
        self.fair = self._fair_value(stats, now)   # once per tick: exits and entries agree
        self._eval_pending(now)
        self._manage_exits(now)          # close open positions before adding new ones
        if MAKER is not None:
            self._publish_maker_input(stats, drift, now)   # RESTING quotes instead of crossing
        else:
            self._signal(stats, drift, now)
        self._write_state(now, stats, drift)

    def _publish_maker_input(self, stats, drift, now):
        """Compute the maker's decision inputs on THIS (analytics) thread and hand them to the
        maker thread as one atomic dict. All the numpy stays here; the maker thread does only
        HTTP. The handoff is a single rebind (like AN.snap), so the maker never reads a
        half-updated state.

        MAKER and _signal are mutually exclusive: quoting both sides while also crossing the
        spread would trade against our own resting orders and pay the taker fee maker mode
        exists to avoid.

        Fair ANCHOR is the strike model probability, NOT the blended drift fair (audit M6). The
        drift fair is `fair_from_mid(mid, dev)` -- anchored to the current poly mid, so quoting
        around it is partly circular. model_p depends only on S/K/sigma/tau and is the
        independent estimate a maker should quote around; fall back to the blended fair only
        when there is no trusted strike."""
        ref, st = self._best_ref(stats)
        z = drift.get(ref) if ref else None
        tau = None if ROLL['end'] is None else ROLL['end'] - now
        sigma = None
        px = self.snap.get('px', {})
        if ROLL['strike_ref'] in px:
            sigma = realized_sigma_per_s(px[ROLL['strike_ref']], CFG['VOL_LOOKBACK_S'])
            if CFG['VOL_SPIKE_GUARD'] and sigma is not None:   # audit M7: same guard as the taker
                fast = realized_sigma_per_s(px[ROLL['strike_ref']], CFG['VOL_FAST_S'])
                if fast is not None:
                    sigma = max(sigma, fast)
        q = FEEDS['poly'].quotes
        fresh = bool(q) and (now - q[-1][0]) <= CFG['POLY_STALE_S']
        fv = self.fair
        anchor = None
        if fv:
            anchor = fv.get('model_p')            # independent of the poly mid
            if anchor is None:
                anchor = fv.get('fair')           # no strike -> fall back to the blended fair
        self.maker_input = dict(fair=anchor, sigma=(sigma or 0.0), tau=tau, z=z,
                                fresh=fresh, slug=ROLL['slug'], ts=now)
        self.last_gate = 'MAKER_' + ((MAKER.pulled_reason if MAKER else None) or 'quoting')

    def _tradable(self, now):
        """Shared pre-trade guard for BOTH strategies. Returns True only if it is safe to
        open a position right now: poly book is fresh, and (series mode) we are not inside
        the hard no-entry window before cycle end. Fixed-market mode skips the tau check."""
        q = FEEDS['poly'].quotes
        if not q or now - q[-1][0] > CFG['POLY_STALE_S']:
            return False                                   # stale/dead poly book
        if ROLL['end'] is not None and ROLL['end'] - now < CFG['ENTRY_CUTOFF_S']:
            return False                                   # too close to expiry to enter
        return True

    def _write_state(self, now, stats, drift):
        """One CSV row per tick with EVERY input a decision could depend on: exchange prices,
        the poly book (top-of-book AND resting size), the model's probability, both edges, the
        required edge, the timing statistics, and WHICH gate bound.

        This exists because dislocations are only visible after the fact. On 2026-07-21 poly UP
        fell 0.785 -> 0.455 while BTC moved 4.6bp and then recovered to 0.855 in ~45s; nothing in
        the logs let us measure how deep the book was at 0.55 or which gate declined it. Group
        this file by `gate` to find out what is actually costing us trades."""
        if not CFG['STATE_LOG']: return
        px = self.snap.get('px') or {}
        def last(k):
            a = px.get(k)
            if a is None or not len(a) or np.isnan(a[-1]): return None
            return float(a[-1])
        spot, perp = last('spot'), last('perp')
        bb, ba = POLY_BOOK.bbo()
        strike = ROLL['strike']
        ref_px = spot if spot is not None else perp
        row = dict(ts=f'{now:.3f}', slug=ROLL['slug'] or '',
                   tau_s=(None if ROLL['end'] is None else round(ROLL['end'] - now, 1)),
                   strike=strike, spot=spot, perp=perp,
                   ref_bp_vs_strike=(None if not (strike and ref_px)
                                     else (ref_px / strike - 1.0) * 1e4),
                   poly_last=last('poly'), poly_bid=bb, poly_ask=ba,
                   poly_bid_sz=(POLY_BOOK.bids.get(bb) if bb else None),
                   poly_ask_sz=(POLY_BOOK.asks.get(ba) if ba else None),
                   spread=(ba - bb if bb and ba else None),
                   gate=self.last_gate, n_signals=len(self.signals),
                   n_positions=(len(EXECUTOR.positions) if EXECUTOR else 0))
        fv = getattr(self, 'fair', None)
        if fv:
            mid = (bb + ba) / 2 if (bb and ba) else None
            rm, pm = (fv['st']['ref_move'], fv['st']['poly_move']) if fv['st'] else (None, None)
            row.update(fair=fv['fair'], dev=fv['dev'], scenario=fv['scenario'],
                       beta=fv['st']['beta'] if fv['st'] else None,
                       gap_c=(fv['fair'] - mid) * 100 if mid else None,
                       overreact=(abs(pm) / abs(rm)) if (rm and pm and abs(rm) > 1e-12) else None)
        if bb and ba:
            p_up = (fv['fair'] if fv else None)
            # reuse the strike model already computed by _fair_value this tick; recomputing it
            # here ran realized_sigma_per_s twice more per tick for an identical answer
            row['p_up'] = fv.get('model_p') if fv else self._model_p_win('BUY', now)
            if p_up is not None:
                pay_up = self._paid_effective('BUY', bb, ba)
                pay_dn = self._paid_effective('SELL', bb, ba)
                row.update(walk_px_yes=pay_up, walk_px_no=pay_dn,
                           edge_up=p_up - pay_up, edge_dn=(1 - p_up) - pay_dn,
                           req_up=self._required_edge(pay_up, bb, ba),
                           req_dn=self._required_edge(pay_dn, bb, ba),
                           roi_up=(p_up - pay_up) / pay_up if pay_up > 0 else None,
                           roi_dn=((1 - p_up) - pay_dn) / pay_dn if pay_dn > 0 else None)
        if stats:   # report the ref that leads poly most strongly right now
            n, s = max(stats.items(), key=lambda kv: abs(kv[1]['corr']))
            row.update(z=drift.get(n), corr=s['corr'], lag=s['lag'], vol_z=s['vol_z'],
                       ref_move=s['ref_move'], poly_move=s['poly_move'])
        L_state(row)

    def _signal(self, stats, drift, now):
        """DRIFT = SIGNAL. Fair value is where the exchange says poly should be; the trade is
        always TOWARD that fair value. Scenario 1 (catchup: the exchange moved, poly has not
        repriced) and Scenario 2 (fade: poly moved, the exchange did not follow) are the same
        rule -- they differ only in who opened the gap, which sets how fast it should close.

        The old logic took direction from the strike binary model and used lead/lag only as a
        veto, which (a) required a trusted strike and (b) via REF_DRIVEN structurally forbade
        the fade, i.e. every cheap entry."""
        if not self._tradable(now): self.last_gate = 'not_tradable'; return
        bb, ba = POLY_BOOK.bbo(); mid = POLY_BOOK.mid()
        if not (bb and ba and mid): self.last_gate = 'no_book'; return
        # compute() sets this once per tick; the fallback keeps _signal callable on its own
        # (tests drive it directly) without changing the production path.
        fv = self.fair or self._fair_value(stats, now)
        self.fair = fv
        if not fv:
            # _fair_value may already have named a specific cause (e.g. sources_disagree);
            # only fall back to a generic label when it did not.
            if self.last_gate not in ('sources_disagree',):
                self.last_gate = ('no_strike' if (ROLL['strike'] is None
                                                  and ROLL['end'] is not None
                                                  and CFG['FAIR_SOURCE'] in ('model', 'both'))
                                  else 'no_fair')
            return
        fair, scen, ref, st = fv['fair'], fv['scenario'], fv['ref'], fv['st']
        # executable edge per side vs the (book-walked) price we'd actually pay
        pay_up = self._paid_effective('BUY', bb, ba)
        pay_dn = self._paid_effective('SELL', bb, ba)
        edge_up, edge_dn = fair - pay_up, (1 - fair) - pay_dn
        # required edge is PER SIDE: the reward:risk floor scales with what that side costs
        req_up, req_dn = self._required_edge(pay_up, bb, ba), self._required_edge(pay_dn, bb, ba)
        mx = CFG['ENTRY_MAX_PX']
        # rank by return on capital, not absolute edge: the bot stakes dollars, so between two
        # qualifying sides the cheaper one turns the same probability edge into more EV
        key_up = (edge_up / pay_up) if (CFG['RANK_BY_ROI'] and pay_up > 0) else edge_up
        key_dn = (edge_dn / pay_dn) if (CFG['RANK_BY_ROI'] and pay_dn > 0) else edge_dn
        if key_up >= key_dn and edge_up >= req_up and pay_up <= mx:
            side, exec_px, edge, p_side = 'BUY', ba, edge_up, fair
        elif key_dn > key_up and edge_dn >= req_dn and pay_dn <= mx:
            side, exec_px, edge, p_side = 'SELL', bb, edge_dn, 1 - fair
        else:
            # report the REAL binding reason. (Was: always "edge < required", which was false
            # 52% of the time -- the price cap was the actual blocker and it lied about it.)
            best, pay, req = ((edge_up, pay_up, req_up) if key_up >= key_dn
                              else (edge_dn, pay_dn, req_dn))
            if best < req:
                roi_floor = CFG['MIN_EDGE_ROI'] * pay
                if CFG['MIN_EDGE_ROI'] > 0 and req <= roi_floor + 1e-12:
                    self.last_gate = 'reward_risk'
                    why = (f"edge {best:+.3f} is only {best / pay * 100:.1f}% of the {pay:.3f} "
                           f"at risk, need {CFG['MIN_EDGE_ROI'] * 100:.0f}% (={roi_floor:.3f}) "
                           f"- too little room on an expensive share")
                else:
                    self.last_gate = 'edge'
                    why = f"edge {best:+.3f} < required {req:.3f}"
            else:
                self.last_gate = 'price_cap'
                why = f"pay {pay:.3f} > ENTRY_MAX_PX {mx:.2f} (edge {best:+.3f} was fine)"
            L_sig(f"[skip] DRIFT {why} (fair {fair:.3f} mid {mid:.3f} {scen} "
                  f"spread {ba - bb:.3f})")
            return
        # the edge must be executable IN SIZE. _paid_effective now prices a short book at the
        # partial VWAP, which keeps the number honest but still lets an unfillable quote score
        # as an entry; this is the gate that declines it, and it names itself in the state CSV.
        stake_req = getattr(EXECUTOR, 'stake', None) or CFG['RESEARCH_STAKE']
        if not self._book_absorbs(side, stake_req):
            self.last_gate = 'thin_book'
            L_sig(f"[skip] DRIFT {side}: book cannot absorb ${stake_req:.2f} at {exec_px:.3f} "
                  f"(edge {edge:+.3f} is not executable in size)"); return
        if now - self.last_sig[side] < CFG['SIG_COOLDOWN_S']: self.last_gate = 'cooldown'; return
        # CONVICTION OVERRIDE: a large enough edge trades on the dislocation alone.
        conviction = CFG['EDGE_OVERRIDE'] > 0 and edge >= CFG['EDGE_OVERRIDE']
        if conviction:
            L_sig(f"[conviction] DRIFT {side} edge {edge:+.3f} >= {CFG['EDGE_OVERRIDE']} "
                  f"-> bypassing timing gate (fair {fair:.3f}, pay {exec_px:.3f})")
            s_, rz = dict(lag=0, corr=0.0, beta=0.0, vol_z=0.0), 0.0
        else:
            if st is None:                       # FAIR_SOURCE='model': no lead/lag context
                self.last_gate = 'no_leading_ref'
                L_sig(f"[skip] DRIFT {side}: no lead/lag context for the timing gate"); return
            s_ = st
            rz = drift.get(ref)                  # robust z = strength of the fresh dislocation
            if rz is None or abs(rz) < CFG['DRIFT_Z_ALERT'] or s_['vol_z'] < CFG['VOL_Z_MIN']:
                self.last_gate = 'weak_trigger'
                L_sig(f"[skip] DRIFT {side}: weak trigger (z {rz} volZ {s_['vol_z']:.1f})"); return
            # ---- scenario gates (drift sources only; 'model' has no who-moved-first) ----
            want = CFG['SCENARIO_MODE']
            if scen in ('catchup', 'fade') and want != 'both' and scen != want:
                self.last_gate = 'scenario_' + scen
                L_sig(f"[skip] DRIFT {side}: {scen} setup but SCENARIO_MODE={want}"); return
            if scen == 'catchup':
                # the exchange must actually LEAD poly, else "poly is stale" is unproven
                if s_['lag'] < 0:
                    self.last_gate = 'no_leading_ref'
                    L_sig(f"[skip] DRIFT {side}: catchup but poly leads (lag {s_['lag']}s)"); return
            elif scen == 'fade':
                # poly moved on its own. Optional tau window -- a binary near expiry is dragged
                # to 0/1 by resolution, which looks like a dislocation that never closes.
                tau = None if ROLL['end'] is None else ROLL['end'] - now
                if tau is not None:
                    if CFG['FADE_TAU_MIN'] and tau < CFG['FADE_TAU_MIN']:
                        self.last_gate = 'fade_tau'
                        L_sig(f"[skip] DRIFT {side}: fade with only {tau:.0f}s left "
                              f"(< FADE_TAU_MIN {CFG['FADE_TAU_MIN']})"); return
                    if CFG['FADE_TAU_MAX'] and tau > CFG['FADE_TAU_MAX']:
                        self.last_gate = 'fade_tau'
                        L_sig(f"[skip] DRIFT {side}: fade with {tau:.0f}s left "
                              f"(> FADE_TAU_MAX {CFG['FADE_TAU_MAX']})"); return
            # ---- optional strike-model veto: did spot genuinely cross the strike? ----
            if CFG['MODEL_VETO'] > 0 and fv.get('model_p') is not None:
                mp = fv['model_p'] if side == 'BUY' else 1 - fv['model_p']
                if mp < CFG['MODEL_VETO']:
                    self.last_gate = 'model_veto'
                    L_sig(f"[skip] DRIFT {side}: strike model gives this side {mp:.2f} "
                          f"< MODEL_VETO {CFG['MODEL_VETO']} (the move may be real)"); return
        if ROLLS and now - ROLLS[-1] < 5: self.last_gate = 'post_roll'; return  # book still forming
        # ---- fire ----
        self.last_gate = 'FIRE_' + side
        self.last_sig[side] = now
        sig = dict(ts=now, strat='drift', side=side, exec_px=exec_px, fair_px=fair,
                   edge_c=edge * 100, z=rz, lag=s_['lag'], corr=s_['corr'], beta=s_['beta'],
                   ref=ref, vol_z=s_['vol_z'], bb=bb, ba=ba, slug=ROLL['slug'] or '',
                   scenario=scen, dev=fv.get('dev'), mid_at_entry=mid,
                   model_p=fv.get('model_p'), cycle_end=ROLL['end'],
                   at_close=ROLL['end'] is not None,
                   eval_ts=ROLL['end'] if ROLL['end'] is not None else now + CFG['EVAL_H_S'])
        self.signals.append(sig)
        self.pending.append(sig)
        sig['order_ok'] = None
        if EXECUTOR:
            ok, _msg = EXECUTOR.on_signal(sig)   # research signal != accepted/filled order
            sig['order_ok'] = 1 if ok else 0
            if ok and EXECUTOR.positions:
                # tie the position back to its signal so an early exit can rescore it
                EXECUTOR.positions[-1]['sig'] = sig
                # score research on the price we ACTUALLY filled at, back in YES-space
                got = EXECUTOR.positions[-1]['entry']
                sig['fill_px'] = got
                sig['exec_px'] = got if side == 'BUY' else 1 - got
                slip = abs(sig['exec_px'] - exec_px)
                if slip >= 0.01:
                    L_sig(f"[slip] {side} quoted {exec_px:.3f} -> filled {sig['exec_px']:.3f} "
                          f"({slip*100:.1f}c worse; book too thin for ${EXECUTOR.stake:.2f})")
        L_sig(f">>> DRIFT {side} [{scen}] @ {exec_px:.3f} | fair {fair:.3f} (mid {mid:.3f}) | "
              f"edge {edge*100:.1f}c | z {rz:+.2f} ref {ref} lag {s_['lag']}s "
              f"corr {s_['corr']:.2f} volZ {s_['vol_z']:.1f}")
        if self.siglog:
            with open(self.siglog, 'a') as f:
                f.write(','.join(str(sig[k]) for k in SIG_FIELDS) + '\n')

    @staticmethod
    def _best_ref(stats):
        """The composite that tracks poly most strongly right now (timing context)."""
        cand = [(n, st) for n, st in stats.items()
                if abs(st['corr']) >= CFG['MIN_CORR']
                and (CFG['CORR_SIGN'] == 0 or CFG['CORR_SIGN'] * st['corr'] > 0)]
        if not cand:
            return None, None
        return max(cand, key=lambda kv: abs(kv[1]['corr']))

    def _fair_value(self, stats, now):
        """Where poly SHOULD be right now, and why. Returns a dict or None.

        drift fair = fair_from_mid(mid, dev): undo the part of poly's recent move that the
        exchange did NOT justify. Sign convention: dev>0 means poly ran ahead of the exchange
        (too rich, fair<mid); dev<0 means poly fell without the exchange following (too cheap,
        fair>mid). That single number gives BOTH scenarios the same direction rule -- trade
        toward fair -- and needs no strike."""
        mid = POLY_BOOK.mid()
        if not mid:
            return None
        model_p = self._model_p_win('BUY', now)      # None unless a trusted strike exists
        ref, st = self._best_ref(stats)              # timing context for every source
        src = CFG['FAIR_SOURCE']
        if src == 'model':
            if model_p is None:
                return None
            return dict(fair=model_p, dev=None, scenario='model', ref=ref or 'model',
                        st=st, model_p=model_p)
        if st is None or st.get('dev') is None:
            return None
        rm, pm = st['ref_move'], st['poly_move']
        # who moved first: ref_move is already beta-scaled into poly-logit units, so the two
        # are directly comparable and |pm|/|rm| IS the overreaction ratio
        scenario = 'catchup' if abs(rm) >= abs(pm) else 'fade'
        fair = fair_from_mid(mid, st['dev'] * CFG['REVERT_FRAC'])
        if src == 'both':
            # require the strike model to point the same way; a disagreement means the move may
            # be real information rather than a dislocation, so sit it out
            if model_p is None:
                return None
            if (fair - mid) * (model_p - mid) <= 0:
                self.last_gate = 'sources_disagree'
                return None
            fair = min(fair, model_p) if fair > mid else max(fair, model_p)   # the tamer of the two
        return dict(fair=fair, dev=st['dev'], scenario=scenario, ref=ref, st=st, model_p=model_p)

    def _fair_p_up(self, now):
        """P(Up) under the configured fair-value source, for exits and telemetry."""
        f = getattr(self, 'fair', None)
        return None if not f else f['fair']

    def _model_p_win(self, side, now):
        """Calibrated exchange binary-model win prob for the traded side (the drift FAIR
        VALUE): calibrate(Phi(ln(S/K)/(sigma*sqrt(tau)))) from the captured strike, current
        composite price, realized vol, and time remaining. None if not computable (no
        strike/end -> fixed-market mode, or vol not ready)."""
        if ROLL['strike'] is None or ROLL['end'] is None:
            return None
        ref = ROLL['strike_ref']; px = self.snap.get('px', {})
        if ref not in px or np.isnan(px[ref][-1]):
            return None
        tau = ROLL['end'] - now
        sigma = realized_sigma_per_s(px[ref], CFG['VOL_LOOKBACK_S'])
        if CFG['VOL_SPIKE_GUARD'] and sigma is not None:
            # An accelerating tape makes the trailing estimate stale-low, which inflates P_up
            # toward 0/1 and manufactures edge on the expensive side. Take the higher estimate:
            # being wrong toward 0.5 costs a skipped trade, being wrong toward 1.0 costs money.
            fast = realized_sigma_per_s(px[ref], CFG['VOL_FAST_S'])
            if fast is not None:
                sigma = max(sigma, fast)
        if tau <= 0 or sigma is None:
            return None
        S = float(px[ref][-1])
        if CFG['CALIB_MODE'] == 'empirical':
            p_up = empirical_p_up(S, ROLL['strike'], sigma, tau)
        else:
            p_up = calibrate_prob(binary_p_up(S, ROLL['strike'], sigma, tau), CFG['CALIB_SLOPE'])
        return p_up if side == 'BUY' else 1 - p_up   # BUY bets Up, SELL bets Down

    @staticmethod
    def paid_price(side, bb, ba):
        """Price per share ACTUALLY spent: BUY takes YES at the ask; SELL buys NO at 1-bid.
        Gates must test this, never the mid (you never trade at the mid)."""
        return ba if side == 'BUY' else 1.0 - bb

    @staticmethod
    def _walk_price(no_side, stake, partial=False):
        """VWAP price actually paid to spend `stake` walking the LIVE book (mirrors the
        executor's market-order fill). None if the book can't fill the whole stake.
        Top-of-book is a lie for anything bigger than the top level: a live BUY quoted at
        0.43 filled at 0.52 because $1 swept several levels.

        `partial=True` returns the VWAP of what the book CAN absorb instead of None. That
        price is never better than top-of-book (levels are walked worst-ward), so it is the
        conservative mark to gate on; the None contract is what the thin-book GATE tests."""
        levels = POLY_BOOK.bids if no_side else POLY_BOOK.asks
        spend = shares = 0.0
        for p in sorted(levels, reverse=no_side):
            cost_px = (1 - p) if no_side else p
            if cost_px <= 0 or levels[p] <= 0: continue
            take = min(stake - spend, levels[p] * cost_px)
            if take <= 1e-9: break
            shares += take / cost_px; spend += take
        if shares <= 0 or (spend < stake * 0.999 and not partial):
            return None                        # book can't absorb the stake
        return spend / shares

    @staticmethod
    def _sell_vwap(side, shares):
        """Proceeds PER SHARE for selling `shares` of an open position into the LIVE book, and
        how many shares the book can actually take. Mirrors Executor.exit_position: a BUY holds
        YES and sells into the bids (descending); a SELL holds NO and sells it into the asks
        (ascending), where the NO bid behind a YES ask `a` is 1-a.

        Top-of-book is the wrong mark here for the same reason it is wrong on entry, and it is
        wrong in the direction that MANUFACTURES take-profits: measured over 13,016 live ticks
        the resting top-of-book bid is 2 shares at p1, 5 at p5, 8 at p10 -- routinely less than
        a scalp holds. Returns (None, 0.0) when nothing can be sold."""
        levels = POLY_BOOK.bids if side == 'BUY' else POLY_BOOK.asks
        got = sold = 0.0
        for p in sorted(levels, reverse=(side == 'BUY')):
            px = p if side == 'BUY' else 1.0 - p
            if px <= 0 or levels[p] <= 0: continue
            take = min(shares - sold, levels[p])
            if take <= 1e-9: break
            got += take * px; sold += take
        return ((got / sold) if sold > 0 else None), sold

    @staticmethod
    def _book_absorbs(side, stake):
        """Can the live book fill `stake` dollars on this side at all? A book too thin to fill
        the entry is the same book that will refuse the exit, so an unfillable quote is not a
        tradable edge -- it is a quote."""
        return stake <= 0 or Analytics._walk_price(side == 'SELL', stake) is not None

    def _paid_effective(self, side, bb, ba):
        """Price per share we EXPECT to pay for the configured stake, walked against the REAL
        book. When the book cannot absorb the whole stake we take the VWAP of the part it can
        (>= top-of-book, i.e. conservative) rather than the old top-of-book fallback, which
        quoted the most optimistic price in exactly the book where the fill is worst.
        Top-of-book survives only as the last resort on an unreadable/empty book."""
        stake = getattr(EXECUTOR, 'stake', None) or CFG['RESEARCH_STAKE']
        if stake > 0:
            eff = self._walk_price(side == 'SELL', stake, partial=True)
            if eff is not None:
                return eff
        return self.paid_price(side, bb, ba)

    def _required_edge(self, pay, bb, ba):
        """Edge a side must clear to be worth doing, in probability units. Three floors: the
        fixed MIN_EDGE, the live round-trip cost, and — the reward:risk term — a minimum return
        on the capital the trade puts at risk. The last one is what stops a 3c edge from
        justifying an 88c share, where 3c is a 3.4% return against a 100% downside."""
        req = CFG['MIN_EDGE']
        if CFG['RT_GATE']:
            rtc = self._round_trip_cost(bb, ba, pay)
            if rtc is not None: req = max(req, rtc)
        if CFG['MIN_EDGE_ROI'] > 0 and pay > 0:
            req = max(req, CFG['MIN_EDGE_ROI'] * pay)
        return req

    def _model_edge(self, side, bb, ba, now):
        """P_fair(traded side) - price we'd ACTUALLY pay (book-walked, not the quote). Uses the
        SAME fair value the entry used, so a thesis-invalidation exit measures the mispricing
        closing rather than a different model disagreeing."""
        p = self._fair_p_up(now)
        if p is None: p = self._model_p_win('BUY', now)
        if p is None: return None
        p = p if side == 'BUY' else 1 - p
        return p - self._paid_effective(side, bb, ba)

    @staticmethod
    def _round_trip_cost(bb, ba, pay=None):
        """Live cost (in price units) to round-trip a trade at the current book: cross the
        bid-ask spread + the REAL taker fee + buffer. 'The stretch the edge must cover'.
        None on a crossed/empty book.

        The fee is charged per LEG TRADED and depends on that leg's price, so it needs `pay`.
        An early exit is a second taker trade and a second fee; holding to resolution is a
        SETTLEMENT, not a trade, and pays no exit fee -- so the number of chargeable legs is
        exactly `2 if EXIT_ENABLE else 1`. Getting this wrong in either direction is worth
        more than every threshold in this file: at p=0.5 one leg is 1.75c/share."""
        if not (bb and ba) or ba <= bb:
            return None
        fee = 0.0
        if pay is not None and CFG['FEE_RATE_TAKER'] > 0:
            legs = 2 if CFG['EXIT_ENABLE'] else 1
            fee = legs * taker_fee(pay, CFG['FEE_RATE_TAKER'], CFG['FEE_EXPONENT'])
        return (CFG['RT_SPREAD_MULT'] * (ba - bb) + fee
                + (CFG['RT_FEE_C'] + CFG['RT_BUFFER_C']) / 100.0)

    def _manage_exits(self, now):
        """Close open positions early when the drift scalp is done: take-profit (poly moved
        our way enough to sell back at a net gain) or thesis-invalidation (the exchange model
        no longer shows edge for the side). OFF unless CFG['EXIT_ENABLE']."""
        if not CFG['EXIT_ENABLE'] or EXECUTOR is None: return
        bb, ba = POLY_BOOK.bbo()
        if not (bb and ba): return
        for pos in list(EXECUTOR.positions):
            if now - pos['ts'] < CFG['EXIT_MIN_HOLD_S']: continue   # anti-thrash
            side = pos['side']
            # per-share P&L if we cross the spread to exit right now (entry stored as the
            # price PAID per held share: BUY=YES ask, SELL=NO cost=1-bid). Mark against the
            # book the exit will ACTUALLY hit for the size held -- top-of-book overstates every
            # scalp exactly where the book is thinnest, and that is where the TP used to fire.
            held = pos.get('shares') or 0.0
            if held > 0:
                sell_px, sellable = self._sell_vwap(side, held)
                # 0.01 sh is the CLOB's tradable step (Executor._live_sell floors to it): below
                # that there is no exit to take, so do not manufacture one.
                if sell_px is None or sellable < 0.01: continue
            else:                       # size unknown (research/legacy position): top-of-book
                sell_px = bb if side == 'BUY' else 1 - ba
            pnl_ps = sell_px - pos['entry']
            reason = None
            if CFG['EXIT_TP_C'] > 0 and pnl_ps >= CFG['EXIT_TP_C'] / 100.0:
                reason = 'tp'
            elif CFG['EXIT_THESIS']:
                edge = self._model_edge(side, bb, ba, now)   # model value in the side NOW
                if edge is not None and edge <= CFG['EXIT_THESIS_EDGE']:
                    reason = 'thesis'
            if reason:
                ok, msg = EXECUTOR.exit_position(pos, reason)
                if ok:
                    # The tally used to score every signal at RESOLUTION even when the scalp
                    # was closed 20s in, so the research hit-rate described a strategy we were
                    # not running. Rescore against the price we actually sold at (YES-space:
                    # a BUY sells YES at the bid, a SELL sells NO at 1-ask == YES ask).
                    psig = pos.get('sig')
                    if psig is not None:
                        psig['at_close'] = False
                        psig['eval_ts'] = now
                        # YES-space, and on the SAME executable mark the exit decision used:
                        # a BUY sells YES at sell_px, a SELL sells NO at sell_px == YES 1-px
                        psig['exit_px'] = sell_px if side == 'BUY' else 1 - sell_px
                L_sig(f">>> EXIT {reason.upper()} {side} {pos['slug']} @sell~{sell_px:.3f} "
                      f"entry {pos['entry']:.3f} ({pnl_ps*100:+.1f}c/sh) | {msg}")

    def _poly_mid_at(self, ts):
        q = FEEDS['poly'].quotes
        if not q: return None
        a = np.asarray(q, float)
        i = np.searchsorted(a[:, 0], ts, side='right') - 1
        return float(a[i, 1]) if i >= 0 else None

    def _eval_pending(self, now):
        # Score each signal at its own horizon (drift: +EVAL_H_S; fav: cycle close), but
        # ALWAYS close it no later than its market's roll — a signal cannot outlive its cycle.
        # Heterogeneous horizons, so scan the whole (small) deque rather than FIFO-break.
        still = deque()
        for sig in self.pending:
            eval_ts = sig['eval_ts']
            roll_after = next((r for r in ROLLS if r > sig['ts'] + 0.5), None)
            if roll_after: eval_ts = min(eval_ts, roll_after - 0.5)  # market closed -> settle now
            if now < eval_ts:
                still.append(sig); continue
            # a realized exit beats any mark: it is the price we were actually paid
            mid = sig.get('exit_px')
            if mid is None: mid = self._poly_mid_at(eval_ts)
            if mid is None:
                # `continue` here USED TO DROP THE SIGNAL: it never reached `still`, so it
                # vanished from pending without ever being tallied. Every such loss biased the
                # hit-rate toward whichever signals happened to have a quote. Keep it and
                # retry; give up only after a bounded number of attempts, and say so.
                sig['no_mid'] = sig.get('no_mid', 0) + 1
                if sig['no_mid'] <= 30:
                    still.append(sig)
                else:
                    L_warn(f"[eval] discarding {sig['side']} {sig.get('slug', '')} @ "
                           f"{sig['exec_px']:.3f}: no poly quote at its eval time after 30 tries")
                continue
            if sig.get('at_close'):
                # RESOLUTION P&L at the market's 5-min close: the binary pays 1 or 0.
                # BUY holds YES bought at exec_px; SELL is short YES (= NO bought at 1-exec_px).
                up_won = mid >= 0.5
                win = up_won if sig['side'] == 'BUY' else not up_won
                if sig['side'] == 'BUY':
                    pnl_c = ((1.0 - sig['exec_px']) if win else -sig['exec_px']) * 100
                else:
                    pnl_c = (sig['exec_px'] if win else -(1.0 - sig['exec_px'])) * 100
            else:                                   # fallback: mark-to-mid (no cycle end known)
                pnl_c = ((mid - sig['exec_px']) if sig['side'] == 'BUY'
                         else (sig['exec_px'] - mid)) * 100
                win = pnl_c > 0
            # keep the per-signal result on the signal: a dollar-staked bot buys 1/exec_px
            # shares, so per-SHARE cents cannot be averaged into a portfolio return
            sig['pnl_c'], sig['win'] = pnl_c, int(win)
            t = self.tally; t['n'] += 1; t['win'] += win; t['pnl_c'] += pnl_c
            L_sig(f"<<< OUTCOME {sig['side']} @ {sig['exec_px']:.3f} -> mid {mid:.3f} "
                  f"after {int(eval_ts - sig['ts'])}s | {'WIN' if win else 'LOSS'} {pnl_c:+.1f}c | "
                  f"{t['win']}/{t['n']} ({100*t['win']/t['n']:.0f}%) avg {t['pnl_c']/t['n']:+.1f}c")
            if self.outlog:
                ook = sig.get('order_ok')
                ook = '' if ook is None else int(ook)
                with open(self.outlog, 'a') as f:
                    f.write(f"{sig['ts']},{sig.get('strat', 'drift')},{sig['side']},{sig['exec_px']},"
                            f"{mid},{int(eval_ts - sig['ts'])},{pnl_c:.2f},{int(win)},{ook},{sig['slug']}\n")
        self.pending = still

    def _tally_str(self):
        t = self.tally
        return f"{t['win']}W/{t['n'] - t['win']}L avg {t['pnl_c']/t['n']:+.1f}c" if t['n'] else ''

    def report(self):
        s = self.snap
        if not s.get('stats'): return
        lines = [f"{'feed':<9}{'last':>12}{'$vol/s':>10}{'lead(s)':>9}{'corr':>7}{'driftZ':>8}{'volZ':>7}"]
        for n, st in s['stats'].items():
            z = s['drift'][n]
            flag = '  <-- POLY LAGGING' if abs(z) >= CFG['DRIFT_Z_ALERT'] else ''
            lbl = f"{n}[{len(s['members'].get(n, []))}]"  # composite + live venue count
            lines.append(f"{lbl:<9}{s['px'][n][-1]:>12.2f}{st['vps']:>10.0f}{st['lag']:>9d}"
                         f"{st['corr']:>7.2f}{z:>8.2f}{st['vol_z']:>7.1f}{flag}")
        bb, ba = POLY_BOOK.bbo()
        rec = self._tally_str()
        lines.append(f"{'poly':<9}{s['px']['poly'][-1]:>12.4f}{s['pvol']:>10.0f}"
                     f"   bb/ba {bb if bb else '-'}/{ba if ba else '-'}  sigs {len(self.signals)}"
                     f"{('  ' + rec) if rec else ''}  [{ROLL['slug'] or 'fixed'}]")
        now = time.time()
        silent = [n for n, f in FEEDS.items() if not f.quotes or now - f.quotes[-1][0] > 30]
        if silent:
            lines.append(f"silent venues: {', '.join(silent)}")
        L_data('\n'.join(lines) + '\n')

AN = None
HEALTH = dict(t0=None, warned=set())

async def settle_verify_task():
    # fire due official-result checks (execution.verify_settlement) off-thread
    while True:
        await asyncio.sleep(5)
        ex = EXECUTOR
        if not ex or not ex.verify_queue: continue
        now = time.time()
        loop = asyncio.get_running_loop()
        for slug in [s for s, t in list(ex.verify_queue.items()) if now >= t]:
            ex.verify_queue.pop(slug, None)
            await loop.run_in_executor(None, ex.verify_settlement, slug)

async def analytics_task():
    HEALTH['t0'] = time.time()
    while True:
        await asyncio.sleep(1)
        try:
            now = time.time()
            for n, f in FEEDS.items():
                if n not in HEALTH['warned'] and not f.quotes and now - HEALTH['t0'] > 20:
                    HEALTH['warned'].add(n)
                    hint = ("check token/market is live" if n == 'poly'
                            else "feed likely geo/ISP-blocked; continuing with live feeds")
                    L_warn(f"[health] no data from '{n}' after 20s — {hint}")
            AN.compute()
            if int(time.time()) % CFG['PRINT_EVERY_S'] == 0: AN.report()
        except Exception as e:
            L_err(f"[analytics] {e}")

def maker_worker(period=1.0):
    """Dedicated thread that owns ALL maker order I/O. It runs OFF the asyncio event loop, so a
    blocking CLOB round-trip (post/cancel/get_order, tens to hundreds of ms) never stalls the
    websocket feed readers -- the hazard that made every feed go stale during a live quote
    (audit M3). It consumes the atomic snapshot the analytics thread publishes and does the
    HTTP itself. Nothing here touches numpy or the feeds."""
    import threading
    while True:
        time.sleep(period)
        try:
            inp = getattr(AN, 'maker_input', None)   # single atomic read of the published dict
            if MAKER is None or inp is None:
                continue
            MAKER.on_tick(inp['fair'], inp['sigma'], inp['tau'], inp['z'],
                          poly_fresh=inp['fresh'], now=time.time())
            if EXECUTOR is not None and EXECUTOR.mode == 'live':
                MAKER.poll_live_fills(time.time())
            else:
                MAKER.simulate_paper_fills(time.time())
        except Exception as e:
            L_err(f"[maker] worker: {e}")

# ---------------- plot ----------------
def make_plot():
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                   gridspec_kw={'height_ratios': [3, 1]})
    colors = {'perp': 'tab:orange', 'spot': 'tab:blue', 'poly': 'tab:red'}
    lines = {n: ax1.plot([], [], lw=1.2, color=colors.get(n), label=n)[0] for n in colors}
    buy_sc = ax1.scatter([], [], marker='^', c='lime', s=70, zorder=5, label='BUY')
    sell_sc = ax1.scatter([], [], marker='v', c='red', s=70, zorder=5, label='SELL')
    dlines = {}
    cyc_lines = []          # vertical cycle-boundary markers, redrawn each frame
    ax1.set_ylabel('minmax-normalized'); ax1.legend(loc='upper left', fontsize=8); ax1.grid(alpha=.3)
    ax2.axhline(CFG['DRIFT_Z_ALERT'], color='r', ls='--', lw=.7)
    ax2.axhline(-CFG['DRIFT_Z_ALERT'], color='r', ls='--', lw=.7)
    ax2.set_ylabel('drift z'); ax2.set_xlabel('sec ago'); ax2.grid(alpha=.3)

    def norm(x):
        if np.all(np.isnan(x)): return x  # dead feed: draw nothing, not a flat line
        m, M = np.nanmin(x), np.nanmax(x)
        return (x - m) / (M - m) if M > m else np.zeros_like(x)

    def update(_):
        s = AN.snap
        if not s: return
        t = s['grid'] - s['grid'][-1]
        npoly = norm(s['px']['poly'])
        for n, ln in lines.items():
            ln.set_data(t, npoly if n == 'poly' else norm(s['px'][n]))
        # entry markers pinned to the poly line
        def place(sc, want_side):
            pts = [sg['ts'] - s['grid'][-1] for sg in AN.signals
                   if sg['side'] == want_side and sg['ts'] >= s['grid'][0]]
            if pts:
                xs = np.array(pts)
                ii = np.clip(np.searchsorted(t, xs), 0, len(t) - 1)
                sc.set_offsets(np.c_[xs, npoly[ii]])
            else:
                sc.set_offsets(np.empty((0, 2)))
        place(buy_sc, 'BUY')
        place(sell_sc, 'SELL')
        # ---- 5-minute cycle boundaries: a market ends/starts at a point in TIME, so these
        # are vertical. Solid grey = a completed round; dashed green = the current round's
        # close (drawn to the right of 'now', with the countdown).
        while cyc_lines:
            cyc_lines.pop().remove()
        now_ts = s['grid'][-1]
        tau = (ROLL['end'] - now_ts) if ROLL['end'] else None
        # only project the close once it is near, else it wastes half the axis; the exact
        # countdown is always in the title anyway
        LOOKAHEAD = 60.0
        right = min(max(tau, 0.0), LOOKAHEAD) if tau is not None else 0.0
        for rts in list(ROLLS):                       # past rolls = rounds already finished
            x = rts - now_ts
            if x < t[0]: continue
            for ax in (ax1, ax2):
                cyc_lines.append(ax.axvline(x, color='0.45', lw=1.1, ls='-', alpha=.85, zorder=1))
            cyc_lines.append(ax1.text(x, 1.02, 'new round', fontsize=7, color='0.35',
                                      ha='center', va='bottom'))
        if tau is not None and 0 < tau <= LOOKAHEAD:  # close is imminent -> show it coming
            for ax in (ax1, ax2):
                cyc_lines.append(ax.axvline(tau, color='tab:green', lw=1.3, ls='--',
                                            alpha=.9, zorder=1))
            cyc_lines.append(ax1.text(tau, 1.02, f'ends {int(tau)}s', fontsize=7,
                                      color='tab:green', ha='center', va='bottom'))
            cyc_lines.append(ax1.axvspan(0, tau, color='tab:green', alpha=.06, zorder=0))

        if s.get('stats'):
            title = ' | '.join(f"{n}: lead {st['lag']}s r={st['corr']:.2f} z={s['drift'][n]:+.1f}"
                               for n, st in s['stats'].items())
            if tau is not None:
                title = (f"round ends in {int(max(tau, 0)):>3}s | " + title)
            rec = AN._tally_str()
            if rec:
                title += ' | ' + rec
            ax1.set_title(title, fontsize=9)
            for n in s['drift']:
                if n not in dlines:
                    dlines[n] = ax2.plot([], [], lw=1, color=colors.get(n), label=n)[0]
                    ax2.legend(loc='upper left', fontsize=7)
                hist = getattr(update, f'_z_{n}', deque(maxlen=CFG['WINDOW_S']))
                setattr(update, f'_z_{n}', hist)
                hist.append(s['drift'][n])
                dlines[n].set_data(np.arange(-len(hist) + 1, 1), np.array(hist))
        # extend the right edge so the pending close is visible before it happens
        ax1.set_xlim(t[0], right + 2); ax1.set_ylim(-0.05, 1.05)
        ax2.set_xlim(t[0], right + 2); ax2.relim(); ax2.autoscale_view(scalex=False)
        return list(lines.values())

    return fig, update

def run_plot():
    fig, update = make_plot()
    ani = FuncAnimation(fig, update, interval=1000, cache_frame_data=False)
    plt.tight_layout(); plt.show()

# ---------------- interactive launcher ----------------
def _live_updown_coins():
    """Coins with a live 5-minute up/down series on Polymarket right now."""
    try:
        url = ("https://gamma-api.polymarket.com/markets?active=true&closed=false"
               "&order=endDate&ascending=true&limit=100&end_date_min="
               + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        coins = []
        for m in _http_json(url):
            mt = re.match(r'([a-z0-9]+)-updown-5m-', m.get('slug', ''))
            if mt and mt.group(1) not in coins: coins.append(mt.group(1))
        return coins
    except Exception:
        return []


def interactive_menu(args):
    """No-flag launcher: pick a market, toggle recording, go."""
    try:
        _interactive_menu(args)
    except (EOFError, KeyboardInterrupt):
        sys.exit("\naborted.")


def _interactive_menu(args):
    print("\nPolymarket Drift Monitor")
    print("=" * 24)
    print("finding live 5-minute markets...")
    coins = _live_updown_coins() or ['btc', 'eth', 'sol', 'xrp']
    if 'btc' in coins:
        coins.remove('btc'); coins.insert(0, 'btc')
    for i, c in enumerate(coins, 1):
        print(f"  {i}) {c.upper():<5} 5-minute up/down  ({c}-updown-5m)")
    n = len(coins)
    print(f"  {n + 1}) custom series slug")
    print(f"  {n + 2}) fixed market (CLOB token id)")
    while True:
        sel = input(f"\nselect market [1-{n + 2}, Enter=1]: ").strip() or '1'
        if sel.isdigit() and 1 <= int(sel) <= n + 2: break
        print("  invalid choice, try again")
    sel = int(sel)
    if sel <= n:
        coin = coins[sel - 1]
        args.series, args.symbol = f"{coin}-updown-5m", f"{coin}usdt"
    elif sel == n + 1:
        args.series = input("series slug substring (e.g. btc-updown-5m): ").strip()
        args.symbol = input("exchange symbol [btcusdt]: ").strip() or 'btcusdt'
    else:
        args.token = input("CLOB token id: ").strip()
        args.symbol = input("exchange symbol [btcusdt]: ").strip() or 'btcusdt'
    args.record = input("record session data? [Y/n]: ").strip().lower() not in ('n', 'no')
    if input("tune indicator settings? (trigger sensitivity) [y/N]: ").strip().lower() in ('y', 'yes'):
        print("  these control HOW OFTEN drift fires. Lower z / lower corr = more signals.")
        def _num(prompt, cur, cast=float):
            v = input(f"  {prompt} [{cur}]: ").strip()
            try: return cast(v) if v else None
            except ValueError: return None
        def _pick(prompt, cur, choices):
            v = input(f"  {prompt} {choices} [{cur}]: ").strip().lower()
            return v if v in choices else None
        print("  fair value = where the EXCHANGE says poly should be (drift, no strike needed),")
        print("  the strike binary model, or 'both' = only trade when they agree.")
        args.fair_source = _pick("fair value source (FAIR_SOURCE)", CFG['FAIR_SOURCE'],
                                 ['drift', 'model', 'both'])
        print("  which dislocation:  catchup = exchange moved first, poly has not repriced")
        print("                      fade    = poly moved first, exchange did not follow (cheap)")
        args.scenario = _pick("scenario (SCENARIO_MODE)", CFG['SCENARIO_MODE'],
                              ['both', 'catchup', 'fade'])
        args.drift_z = _num("robust-z trigger (DRIFT_Z_ALERT)", CFG['DRIFT_Z_ALERT'])
        args.min_corr = _num("min |corr| to count as leading (MIN_CORR)", CFG['MIN_CORR'])
        args.corr_sign = _num("corr sign  1=positive only  0=either(incl. decoupled)  -1=negative",
                              CFG['CORR_SIGN'], int)
        args.min_edge = _num("required model edge (MIN_EDGE)", CFG['MIN_EDGE'])
        print("  reward:risk — an 88c share risks $7.30 to make $1, so a 3c edge there is a")
        print("  3.4% return on a 100% downside. This asks the edge to be a minimum FRACTION")
        print("  of what you pay. 0.06 = mild, 0.12 = rejects that 88c/7.5c trade, 0 = off.")
        args.min_edge_roi = _num("min edge as a fraction of price paid (MIN_EDGE_ROI)",
                                 CFG['MIN_EDGE_ROI'])
        print("  vol-spike guard: when the tape accelerates, trailing vol understates the next")
        print("  tau and P_up gets overconfident exactly where shares are dearest.")
        args.vol_spike_guard = _num("vol-spike guard 1=on 0=off (VOL_SPIKE_GUARD)",
                                    CFG['VOL_SPIKE_GUARD'], int)
    m = input("trading mode: 1) off - signals only  2) paper  3) live [Enter=1]: ").strip() or '1'
    args.mode = {'1': 'off', '2': 'paper', '3': 'live'}.get(m, 'off')
    if args.mode != 'off':
        print("  strategy: TAKER crosses the spread on a signal (pays the ~1.75c/share taker")
        print("  fee, which is larger than the measured taking edge). MAKER rests two-sided")
        print("  quotes around fair value and pays NO fee, but only earns if it gets filled.")
        strat = input("strategy: 1) taker  2) maker (resting quotes) [Enter=1]: ").strip() or '1'
        if strat == '2':
            args.maker = True
            print("  MAKER sizing is in SHARES (exchange min 5), NOT stake $: 5 sh is ~$2.50 of")
            print("  risk at 0.50. Quotes anchor to the strike model; big |z| = informed flow,")
            print("  so the bot PULLS its quotes rather than trading into it.")
            try: args.maker_shares = float(input("  shares to rest per side [5]: ").strip() or '5')
            except ValueError: args.maker_shares = 5.0
            try: args.maker_toxic_z = float(input("  pull all quotes when |robust z| >= this [2.5]: ").strip() or '2.5')
            except ValueError: args.maker_toxic_z = 2.5
            try: args.maker_inv_skew_ticks = float(input("  inventory lean toward offloading, in ticks at the cap [1]: ").strip() or '1')
            except ValueError: args.maker_inv_skew_ticks = 1.0
            try: args.max_daily_loss = float(input("  max daily loss (kill switch), USD [10]: ").strip() or '10')
            except ValueError: args.max_daily_loss = 10.0
        else:
            o = input("execution: 1) market - spends stake $ exactly  2) limit - shares at BBO [Enter=1]: ").strip() or '1'
            args.order = 'limit' if o == '2' else 'market'
            try: args.stake = float(input("stake per trade, USD [1]: ").strip() or '1')
            except ValueError: args.stake = 1.0
            try: args.max_daily_loss = float(input("max daily loss (kill switch), USD [10]: ").strip() or '10')
            except ValueError: args.max_daily_loss = 10.0
            ex = input("exit management? scalp: take-profit + thesis-invalidation "
                       "instead of hold-to-resolution [y/N]: ").strip().lower() in ('y', 'yes')
            args.exit_mgmt = ex
            if ex:
                try: args.exit_tp = float(input("  take-profit, cents/share net of spread [2]: ").strip() or '2')
                except ValueError: args.exit_tp = 2.0
    if args.mode == 'live':
        if _live_creds_prompt():
            os.environ['POLY_DRIFT_ARM'] = 'YES-LIVE'
        else:
            print("live not armed -> running in PAPER mode instead")
            args.mode = 'paper'
    what = f"series {args.series}" if args.series else f"token {(args.token or '')[:20]}..."
    trade = f" | mode {args.mode}" + (f" ({args.order}, ${args.stake:.2f}/trade)" if args.mode != 'off' else '')
    print(f"\nstarting: {what} | symbol {args.symbol} | record {'on' if args.record else 'off'}{trade}")
    print("close the chart window (or Ctrl+C) to stop.\n")


def _derive_signer(key):
    """Public address of a private key (so the user can eyeball-match it). None if invalid."""
    try:
        from eth_account import Account
        return Account.from_key(key).address
    except Exception:
        return None


def _live_creds_prompt():
    """Collect live credentials in the console. Typed locally, hidden input, never
    transmitted anywhere except in your own signed orders. Returns True if armed."""
    import getpass
    from execution import load_env
    load_env()
    if os.environ.get('POLY_PRIVATE_KEY'):
        cur = _derive_signer(os.environ['POLY_PRIVATE_KEY']) or '?'
        print(f"  saved credentials found: type={os.environ.get('POLY_SIGNATURE_TYPE', '?')} "
              f"signer={cur} funder={os.environ.get('POLY_FUNDER') or '(none)'}")
        if input("use these saved credentials? [Y/n]: ").strip().lower() in ('n', 'no'):
            for k in ('POLY_PRIVATE_KEY', 'POLY_FUNDER', 'POLY_SIGNATURE_TYPE'):
                os.environ.pop(k, None)
    if not os.environ.get('POLY_PRIVATE_KEY'):
        print("\n  YOUR POLYMARKET ACCOUNT HAS TWO ADDRESSES - THEY ARE NOT INTERCHANGEABLE:")
        print("    * SIGNER  - derived from your private key. You do NOT type this anywhere.")
        print("                (Polymarket shows it in Settings as 'Signer Address'.)")
        print("    * PROFILE - the proxy wallet holding your money. THIS is the funder.")
        print("                polymarket.com -> avatar -> URL: polymarket.com/profile/0x<THIS>")
        print("  Putting the signer in the funder field breaks every order.\n")
        st = input("  account type - 0=plain wallet (MetaMask)  1=email/Google/Magic  "
                   "2=browser-wallet [1]: ").strip() or '1'
        os.environ['POLY_SIGNATURE_TYPE'] = st

        print("\n  PRIVATE KEY: Polymarket -> Settings -> Export Private Key.")
        print("  It is a SECRET ('0x' + 64 hex chars), NOT an address. Input is hidden.")
        key = getpass.getpass("  private key (hidden): ").strip()
        if not key:
            return False
        signer = _derive_signer(key)
        if signer is None:
            print("  !! that does not parse as a private key (an address is 40 hex chars, a key is 64).")
            return False
        os.environ['POLY_PRIVATE_KEY'] = key
        print(f"  -> derived SIGNER address: {signer}")
        print("     confirm this matches the 'Signer Address' shown in Polymarket Settings.")

        if st in ('1', '2'):
            print("\n  FUNDER = your PROFILE address (holds your balance), NOT the signer above.")
            while True:
                f = input("  profile/funder address (0x...): ").strip()
                if not f:
                    print("  !! required for this account type."); continue
                if signer and f.lower() == signer.lower():
                    print("  !! that is the SIGNER address. Orders will be rejected.")
                    print("     Open polymarket.com -> avatar -> copy the 0x... from the profile URL.")
                    continue
                os.environ['POLY_FUNDER'] = f
                break
        else:
            os.environ.pop('POLY_FUNDER', None)     # plain wallets must not set a funder
            print("  (plain wallet: no funder needed)")

        if input("\n  save to .env for next launch? (plain text on this PC) [y/N]: ").strip().lower() in ('y', 'yes'):
            _save_env()
    return input("  type YES-LIVE to arm live trading: ").strip() == 'YES-LIVE'


def _save_env():
    keys = ['POLY_PRIVATE_KEY', 'POLY_SIGNATURE_TYPE', 'POLY_FUNDER']
    lines = []
    if os.path.exists('.env'):
        lines = [l for l in open('.env').read().splitlines()
                 if not any(l.startswith(k + '=') for k in keys)]
    for k in keys:
        if os.environ.get(k):
            lines.append(f"{k}={os.environ[k]}")
    with open('.env', 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print("saved to .env (keep this file private; it is gitignored)")


# ---------------- main ----------------
async def amain(args):
    sym = args.symbol.lower(); symU = sym.upper()
    base, quote = split_symbol(sym)
    okx_spot = f"{base}-{quote}".upper(); okx_swap = f"{okx_spot}-SWAP"
    for g in GROUPS.values():
        for n in g: FEEDS[n] = Feed(n)
    FEEDS['poly'] = Feed('poly')
    loop = asyncio.get_running_loop()
    ctval = await loop.run_in_executor(None, _okx_ctval, okx_swap)  # swap contract size (None -> sz as-is)
    tasks = [
        ws_loop('bin-spot', f"wss://stream.binance.com:9443/ws/{sym}@aggTrade",
                None, binance_handler(FEEDS['bin-spot'])),
        ws_loop('bin-perp', f"wss://fstream.binance.com/stream?streams={sym}@aggTrade/{sym}@bookTicker",
                None, binance_futs_handler(FEEDS['bin-perp'])),
        ws_loop('byb-spot', "wss://stream.bybit.com/v5/public/spot",
                sub_open({"op": "subscribe", "args": [f"publicTrade.{symU}"]}),
                bybit_handler(FEEDS['byb-spot']), keepalive=ping_ka('{"op":"ping"}')),
        ws_loop('byb-perp', "wss://stream.bybit.com/v5/public/linear",
                sub_open({"op": "subscribe", "args": [f"publicTrade.{symU}"]}),
                bybit_handler(FEEDS['byb-perp']), keepalive=ping_ka('{"op":"ping"}')),
        ws_loop('okx', "wss://ws.okx.com:8443/ws/v5/public",
                sub_open({"op": "subscribe", "args": [{"channel": "trades", "instId": okx_spot},
                                                      {"channel": "trades", "instId": okx_swap}]}),
                okx_handler(FEEDS['okx-spot'], FEEDS['okx-perp'], okx_swap, ctval),
                keepalive=ping_ka('ping', 25)),
        poly_ws_task(poly_handler(FEEDS['poly'])),
        analytics_task(),
        settle_verify_task(),
    ]
    if args.series:
        tasks.append(roll_poller(args.series))
    else:
        ROLL['token'] = args.token
    await asyncio.gather(*tasks)

def main():
    global AN, RECORDER, EXECUTOR, MAKER   # EXECUTOR must be module-global: Analytics._signal
    # reads it. MAKER has the IDENTICAL hazard -- Analytics._run_maker reads the module global,
    # so dropping it from this line builds the quoter in local scope and NO quotes ever rest
    # (the console still prints "[maker] RESTING QUOTES ON"). This exact bug has bitten
    # EXECUTOR twice; see FINDINGS.md §6. Guarded by tests/test_menu.py.
    ap = argparse.ArgumentParser()
    ap.add_argument('--token', default=None, help='Polymarket CLOB token_id (fixed-market mode)')
    ap.add_argument('--series', default=None, help='slug substring for auto-roll (e.g. btc-updown-5m); applies 5m preset')
    ap.add_argument('--symbol', default='btcusdt')
    ap.add_argument('--bybit', action='store_true', help='(deprecated, ignored: bybit is always included)')
    ap.add_argument('--siglog', default='signals.csv')
    ap.add_argument('--record', action=argparse.BooleanOptionalAction, default=True,
                    help='record all ticks + poly book events to --record-dir (jsonl.gz)')
    ap.add_argument('--record-dir', default='data')
    ap.add_argument('--dump', default=None, help='parquet path: dump 1s grid on exit')
    ap.add_argument('--menu', action='store_true', help='force the interactive launcher')
    ap.add_argument('--mode', choices=['off', 'paper', 'live'], default='off',
                    help='order execution: off (signals only), paper (simulated fills), '
                         'live (REAL FUNDS; needs .env + POLY_DRIFT_ARM=YES-LIVE + typed confirm)')
    ap.add_argument('--stake', type=float, default=1.0, help='USD per trade')
    ap.add_argument('--order', choices=['market', 'limit'], default='market',
                    help='market: spend exactly --stake dollars ($1 min, walks the book); '
                         'limit: shares at BBO price (5-share exchange minimum)')
    ap.add_argument('--max-daily-loss', type=float, default=10.0, help='USD kill-switch threshold')
    ap.add_argument('--exit', dest='exit_mgmt', action=argparse.BooleanOptionalAction, default=None,
                    help='active exit management: take-profit + thesis-invalidation instead of '
                         'holding to resolution (default off). --exit on / --no-exit off')
    ap.add_argument('--exit-tp', type=float, default=None,
                    help='take-profit in cents/share, net of the spread (default 2.0; 0 disables)')
    # --- indicator settings (the timing trigger; these control HOW OFTEN drift fires) ---
    ap.add_argument('--drift-z', type=float, default=None,
                    help='robust-z trigger threshold (CFG DRIFT_Z_ALERT, default 2.0). '
                         'Lower = more signals from weaker moves')
    ap.add_argument('--min-corr', type=float, default=None,
                    help='min |corr| for a ref to count as leading poly (CFG MIN_CORR, default 0.15)')
    ap.add_argument('--corr-sign', type=int, choices=[-1, 0, 1], default=None,
                    help='required correlation sign: 1=positive only (5m preset), 0=either, -1=negative. '
                         'NOTE: 0 also admits the DECOUPLED/negative-corr regime')
    ap.add_argument('--min-edge', type=float, default=None,
                    help='required model edge floor in probability units (CFG MIN_EDGE, default 0.03)')
    ap.add_argument('--entry-max-px', type=float, default=None,
                    help='max price per share to pay (CFG ENTRY_MAX_PX, default 1.0 = no cap). '
                         'Does NOT bound dollar risk (stake does); set <1 to force underdogs only')
    ap.add_argument('--fair-source', choices=['drift', 'model', 'both'], default=None,
                    help="where fair value comes from (CFG FAIR_SOURCE, default drift). "
                         "drift = exchange-implied (poly's move minus what Binance/OKX/Bybit "
                         "justified) - needs NO strike. model = spot vs strike binary. "
                         "both = trade only when they agree on direction")
    ap.add_argument('--scenario', choices=['both', 'catchup', 'fade'], default=None,
                    help="which dislocation to trade (CFG SCENARIO_MODE, default both). "
                         "catchup = exchange moved, poly has not repriced (entries are dearer). "
                         "fade = poly moved, exchange did not follow (entries are cheap)")
    ap.add_argument('--fade-tau-min', type=float, default=None,
                    help='min seconds left to take a FADE (CFG FADE_TAU_MIN, 0 = off)')
    ap.add_argument('--fade-tau-max', type=float, default=None,
                    help='max seconds left to take a FADE (CFG FADE_TAU_MAX, 0 = no cap)')
    ap.add_argument('--model-veto', type=float, default=None,
                    help='block a trade when the strike model gives the traded side less than '
                         'this probability - catches "poly crashed because spot really did '
                         'cross the strike" (CFG MODEL_VETO, 0 = off; needs a trusted strike)')
    ap.add_argument('--min-edge-roi', type=float, default=None,
                    help='REWARD:RISK gate. Required edge as a fraction of the price paid '
                         '(CFG MIN_EDGE_ROI, default 0.06; 0 = off). Raises the bar on '
                         'expensive shares, where a small edge is a poor return against a '
                         '100%% downside. Use 0.12 to reject an 88c share with a 7.5c edge')
    ap.add_argument('--vol-spike-guard', type=int, choices=[0, 1], default=None,
                    help='1 = never let the model be more confident than the FAST realized-vol '
                         'window justifies (sigma := max(slow, fast)); 0 = trailing window only '
                         '(CFG VOL_SPIKE_GUARD, default 1)')
    ap.add_argument('--edge-override', type=float, default=None,
                    help='conviction override: model edge >= this trades on the model alone, '
                         'bypassing the lead/lag timing gate (CFG EDGE_OVERRIDE, default 0=off; '
                         'try 0.15)')
    ap.add_argument('--fee-rate', type=float, default=None,
                    help='taker fee rate for fee = rate*p*(1-p) per share (CFG FEE_RATE_TAKER, '
                         'default 0.07 = the live crypto_fees_v2 schedule). 0 models the market '
                         'as fee-free, which it is NOT. Check the live value with '
                         'scripts/check_fees.py')
    ap.add_argument('--rt-gate', type=int, choices=[0, 1], default=None,
                    help='require the edge to clear the live round-trip cost (spread + taker '
                         'fee + buffer), not just MIN_EDGE (CFG RT_GATE, default 1)')
    ap.add_argument('--maker', action='store_true',
                    help='MAKER MODE: rest two-sided limit quotes around model fair value '
                         'instead of crossing the spread. Makers pay NO taker fee '
                         '(0.07*p*(1-p), ~1.75c/share at the money) -- that fee is larger than '
                         'the whole measured taking edge. Disables the taker path. '
                         'Needs --mode paper|live; live still requires the usual ARM')
    ap.add_argument('--maker-shares', type=float, default=5.0,
                    help='shares to rest PER SIDE (exchange minimum 5; at 0.50 that is $2.50 '
                         'of risk per quote, which is NOT bounded by --stake)')
    ap.add_argument('--maker-gamma', type=float, default=3.0,
                    help='Avellaneda-Stoikov risk aversion: higher = wider quotes and a '
                         'stronger inventory skew (default 3.0)')
    ap.add_argument('--maker-kappa', type=float, default=100.0,
                    help='A-S order-arrival intensity: higher = tighter quotes. NOT the '
                         'textbook ~6: the tick here is 0.01 and the median market spread is '
                         '1 tick, so kappa~100 is what puts the quote at the touch instead of '
                         '13c away from it (default 100)')
    ap.add_argument('--maker-inv-skew-ticks', type=float, default=1.0,
                    help='how many TICKS the reservation price leans to offload when net '
                         'inventory hits the cap (default 1.0). The A-S variance skew is '
                         'numerically dead at this vol, so this is the real inventory control')
    ap.add_argument('--maker-toxic-z', type=float, default=2.5,
                    help='pull ALL quotes when |robust z| >= this. Big poly moves are INFORMED '
                         '(FINDINGS: |z|>=3 was -26%% EV/$ to trade), so as a maker this is the '
                         'signal to get out of the way, not to trade (default 2.5)')
    ap.add_argument('--maker-latency-log', default='maker_latency.csv',
                    help='CSV of every order attempt with its post->ack round trip (ms)')
    ap.add_argument('--maker-fills-log', default='maker_fills.csv',
                    help='CSV of every maker FILL with its decision context. This is the input '
                         'to scripts/markout.py, which measures adverse selection -- the number '
                         'that decides whether market making here is a business')
    ap.add_argument('--no-state-log', dest='state_log', action='store_false', default=True,
                    help='disable the per-tick market_state.csv research telemetry '
                         '(CFG STATE_LOG; on by default, ~3k rows/hour)')
    args = ap.parse_args()
    CFG['STATE_LOG'] = int(args.state_log)
    if not args.token and not args.series:
        if args.menu or (sys.stdin is not None and sys.stdin.isatty()):
            interactive_menu(args)
        if not args.token and not args.series:
            ap.error('provide --token or --series (or run with no flags in a terminal for the menu)')
    if args.series:
        CFG.update(PRESET_5M)
    # tag the log dir with the market so two runs on different symbols never interleave
    init_logs(tag=(args.series or args.symbol or 'run'))
    if args.exit_mgmt is not None:              # CLI/menu override of the exit switch
        CFG['EXIT_ENABLE'] = int(args.exit_mgmt)
    if args.exit_tp is not None:
        CFG['EXIT_TP_C'] = args.exit_tp
    # indicator overrides LAST so they beat PRESET_5M (which sets CORR_SIGN)
    for key, val in (('DRIFT_Z_ALERT', args.drift_z), ('MIN_CORR', args.min_corr),
                     ('CORR_SIGN', args.corr_sign), ('MIN_EDGE', args.min_edge),
                     ('EDGE_OVERRIDE', args.edge_override),
                     ('ENTRY_MAX_PX', args.entry_max_px),
                     ('MIN_EDGE_ROI', args.min_edge_roi),
                     ('FEE_RATE_TAKER', args.fee_rate), ('RT_GATE', args.rt_gate),
                     ('VOL_SPIKE_GUARD', args.vol_spike_guard),
                     ('FAIR_SOURCE', args.fair_source), ('SCENARIO_MODE', args.scenario),
                     ('FADE_TAU_MIN', args.fade_tau_min), ('FADE_TAU_MAX', args.fade_tau_max),
                     ('MODEL_VETO', args.model_veto)):
        if val is not None:
            CFG[key] = val
    if CFG['EXIT_ENABLE'] and args.mode == 'off':
        L_sess("[exit] note: exit management only acts on real/paper positions; "
              "it does nothing in --mode off")
    if args.record:
        RECORDER = Recorder(args.record_dir)
        RECORDER.rec({'k': 'hdr', 'ts': time.time(), 'symbol': args.symbol, 'series': args.series or ''})
        L_sess(f"[rec] -> {RECORDER.path}")
    # full provenance to session.log: which market, and every gate value in force this run
    L_sess(f"[session] start series={args.series} token={args.token} symbol={args.symbol} "
           f"mode={args.mode} order={getattr(args, 'order', '-')} "
           f"stake={getattr(args, 'stake', '-')}")
    L_sess("[session] CFG " + " ".join(f"{k}={v}" for k, v in CFG.items()))
    L_data(f"[indicator] DRIFT_Z_ALERT={CFG['DRIFT_Z_ALERT']} MIN_CORR={CFG['MIN_CORR']} "
           f"CORR_SIGN={CFG['CORR_SIGN']} MIN_EDGE={CFG['MIN_EDGE']} "
           f"VOL_Z_MIN={CFG['VOL_Z_MIN']} FAIR_SOURCE={CFG['FAIR_SOURCE']} "
           f"SCENARIO_MODE={CFG['SCENARIO_MODE']} MIN_EDGE_ROI={CFG['MIN_EDGE_ROI']} "
           f"ENTRY_MAX_PX={CFG['ENTRY_MAX_PX']} EDGE_OVERRIDE={CFG['EDGE_OVERRIDE']}")
    AN = Analytics(siglog=args.siglog)
    if args.mode != 'off':
        from execution import Executor
        EXECUTOR = Executor(args.mode, POLY_BOOK, ROLL, stake_usd=args.stake,
                            max_daily_loss=args.max_daily_loss, order_type=args.order)
        ex_desc = (f"exit=on (tp {CFG['EXIT_TP_C']:.0f}c + thesis)" if CFG['EXIT_ENABLE']
                   else "exit=off (hold to resolution)")
        L_sess(f"[exec] mode={args.mode} order={args.order} stake=${args.stake:.2f} "
              f"max daily loss=${args.max_daily_loss:.2f} | {ex_desc}")
    if args.maker:
        # MAKER MODE. Live orders are placed through EXECUTOR.client, which exists only after
        # Executor._arm_live() cleared POLY_DRIFT_ARM=YES-LIVE *and* a typed ARM -- so this
        # flag cannot arm anything by itself. --mode off has no client, hence no quoting.
        if args.mode == 'off':
            ap.error('--maker needs --mode paper or --mode live (it places resting orders)')
        from maker import MakerQuoter, MIN_SHARES
        want = float(args.maker_shares)
        if want < MIN_SHARES:
            L_warn(f"[maker] --maker-shares {want:g} is below the exchange minimum "
                   f"({MIN_SHARES:g}); using {MIN_SHARES:g}. At 0.50 that is "
                   f"${MIN_SHARES*0.5:.2f} of risk per quote, NOT your --stake ${args.stake:.2f}.")
        MAKER = MakerQuoter(EXECUTOR, POLY_BOOK, ROLL, CFG, shares=want,
                            gamma=args.maker_gamma, kappa=args.maker_kappa,
                            toxic_z=args.maker_toxic_z,
                            inv_skew_ticks=args.maker_inv_skew_ticks,
                            latency_log=args.maker_latency_log,
                            fills_log=args.maker_fills_log)
        L_sess(f"[maker] RESTING QUOTES ON. shares={MAKER.shares:g}/side gamma={args.maker_gamma} "
               f"kappa={args.maker_kappa} toxic_z={args.maker_toxic_z} "
               f"latency->{args.maker_latency_log} | the taker path (_signal) is DISABLED")
        # order I/O on its OWN thread so a blocking CLOB round-trip never stalls the feeds
        threading.Thread(target=maker_worker, daemon=True).start()
    t = threading.Thread(target=lambda: asyncio.run(amain(args)), daemon=True)
    t.start()
    try:
        run_plot()
    finally:
        if RECORDER: RECORDER.close()
        if args.dump and AN.snap:
            s = AN.snap
            df = pd.DataFrame({'ts': s['grid'], **{f'px_{n}': s['px'][n] for n in s['px']},
                               **{f'vol_{n}': s['vol'][n] for n in s['vol']}})
            df.to_parquet(args.dump); print(f"dumped {args.dump}")

if __name__ == '__main__':
    main()