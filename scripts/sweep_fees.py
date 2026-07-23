#!/usr/bin/env python
"""sweep_fees.py -- net-of-fee economics. Live market config (gamma-api, btc/eth/sol/xrp-updown-5m):
    feeSchedule = {'exponent': 1, 'rate': 0.07, 'takerOnly': True, 'rebateRate': 0.2}
    feesEnabled = True | rewardsMaxSpread = 0 (NO liquidity rewards) | orderMinSize = 5

    fee_per_share = rate * p * (1-p)      p = price of the leg traded
    Makers pay ZERO. Settlement is not a trade, so holding to resolution pays no exit fee.

Four execution designs:
  A taker in / taker out  (WHAT THE BOT DOES TODAY)   2 fees
  B taker in / hold to resolution                     1 fee
  C maker in / taker out                              1 fee
  D maker in / hold to resolution                     0 fees
"""
import os, sys, json, random, statistics as st
from collections import defaultdict
random.seed(9)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from drift_math import taker_fee                                            # noqa: E402

RATE = 0.07


def fee(p):
    return taker_fee(p, RATE, 1)


def load(p):
    rows, seen = [], set()
    for line in open(p, encoding='utf-8'):
        try: r = json.loads(line)
        except Exception: continue
        k = (r['src'], r['ts'], r['side'])
        if k in seen: continue
        seen.add(k)
        r['path'] = {int(x): v for x, v in (r.get('path') or {}).items()}
        r['half'] = abs(r['entry'] - r['mid']) if r.get('mid') is not None else .008
        mp = None if r.get('model_p') is None else (r['model_p'] if r['side'] == 'BUY'
                                                    else 1 - r['model_p'])
        r['mp'] = mp
        rows.append(r)
    return rows


def legs(r, tp_c=3, mh=120):
    """(gross per-share pnl, exit price of the leg held) under the TP rule, executable mark."""
    for h in sorted(r['path']):
        if h > mh: break
        p = r['path'][h] - r['half']
        if p * 100 >= tp_c: return p, r['cost'] + p
    pts = [h for h in sorted(r['path']) if h <= mh]
    if not pts: return None, None
    p = r['path'][pts[-1]] - r['half']
    return p, r['cost'] + p


def design(r, maker_in, hold):
    """per-share NET pnl. maker_in: entry pays no fee AND earns the half-spread instead of
    paying it. hold: exit by settlement (no exit fee, resolution payoff)."""
    g, exit_px = legs(r)
    if g is None: return None
    cost = r['cost']
    if hold:
        if r.get('resolved_pnl_c') is None: return None
        pnl = r['resolved_pnl_c'] / 100.0
    else:
        pnl = g
    if maker_in:
        # resting instead of crossing: you get the leg for half a spread less, no entry fee
        pnl += r['half']
        eff_cost = cost - r['half']
    else:
        pnl -= fee(cost)
        eff_cost = cost
    if not hold:
        pnl -= fee(min(max(exit_px, 0.01), 0.99))
    return pnl, eff_cost


def report(lbl, g, maker_in, hold):
    by = defaultdict(list); ps = []
    for r in g:
        d = design(r, maker_in, hold)
        if d is None: continue
        pnl, c = d
        by[r['slug']].append(pnl / c); ps.append(pnl)
    v = [x for s in by.values() for x in s]
    if len(v) < 20: return f'  {lbl:<40} n={len(v)} too few'
    keys = list(by); bs = []
    for _ in range(6000):
        pick = [random.choice(keys) for _ in keys]
        bs.append(st.mean([x for k in pick for x in by[k]]))
    bs.sort()
    return (f'  {lbl:<40} n={len(v):>4}  per-share {st.mean(ps)*100:>+6.2f}c'
            f'  EV/$ {st.mean(v)*100:>+7.1f}%  CI[{bs[150]*100:>+6.1f},{bs[5850]*100:>+6.1f}]'
            f'{"  SIG" if bs[150] > 0 else ""}')


cand = load(sys.argv[1] if len(sys.argv) > 1 else 'scratch/candidates.jsonl')
allr = load(sys.argv[2] if len(sys.argv) > 2 else 'scratch/all.jsonl')
seen = set(r['src'] for r in allr)
hold_set = [r for r in cand if r['src'] not in seen]
ag = lambda rows: [r for r in rows if r['mp'] and r['mp'] > r['cost']]

print('=== FEE TABLE (rate 0.07, exponent 1) ===')
print(f'{"price":>7}{"fee c/share":>14}{"as % of capital":>18}')
for p in (.10, .20, .30, .40, .50, .60, .70, .80, .90):
    print(f'{p:>7.2f}{fee(p)*100:>13.3f}c{fee(p)/p*100:>17.1f}%')
print('  -> fee peaks in CENTS at 0.50, but peaks as a FRACTION OF CAPITAL at the cheap end.')
print('     MIN_EDGE_ROI pushes toward cheap shares; the fee punishes exactly those hardest.')

for nm, rows in (('POOLED (16 sessions)', cand), ('HOLDOUT ONLY (5 unseen)', hold_set)):
    g = ag(rows)
    m = [r for r in g if r['scenario'] == 'catchup']
    print(f'\n=== {nm} — "model agrees", TP 3c, executable mark ===')
    print(f'  mean entry cost ${st.mean([r["cost"] for r in g]):.3f}  '
          f'mean entry fee {st.mean([fee(r["cost"]) for r in g])*100:.2f}c/share')
    print(report('A taker in / taker out  (TODAY)', g, False, False))
    print(report('B taker in / hold to resolution', g, False, True))
    print(report('C maker in / taker out', g, True, False))
    print(report('D maker in / hold to resolution', g, True, True))
    print(f'  -- catchup subset (current SCENARIO_MODE) --')
    print(report('A taker in / taker out  (TODAY)', m, False, False))
    print(report('D maker in / hold to resolution', m, True, True))

print('\n=== GROSS vs NET, the one number that matters (pooled, agrees) ===')
g = ag(cand)
gr = [legs(r)[0] for r in g if legs(r)[0] is not None]
print(f'  gross per-share edge (before fees)   {st.mean(gr)*100:+.2f}c')
print(f'  round-trip taker fee at mean prices  '
      f'{-2*st.mean([fee(r["cost"]) for r in g])*100:+.2f}c')
print(f'  -> net as a taker round-trip         '
      f'{(st.mean(gr) - 2*st.mean([fee(r["cost"]) for r in g]))*100:+.2f}c')
