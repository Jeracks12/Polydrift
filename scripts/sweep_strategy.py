#!/usr/bin/env python
"""sweep_strategy.py — offline sweep of entry thresholds and EXIT rules over the candidate set
built by collect_signals.py. No replaying, so a full grid runs in seconds.

Everything is scored per DOLLAR STAKED, never per share: a $1 order buys 1/cost shares, so
per-share cents systematically flatter cheap entries and mis-rank the sides. cost is the price
actually paid for the leg held (ask for a BUY, 1-bid for a SELL).

  python scripts/sweep_strategy.py scratch/candidates.jsonl
  python scripts/sweep_strategy.py scratch/candidates.jsonl --exits
"""
import argparse, json, math, statistics as st, sys
from collections import defaultdict

HORIZONS = [5, 10, 15, 20, 30, 40, 50, 60, 80, 100, 120, 150, 180]


def load(path):
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            r = json.loads(line)
            r['path'] = {int(k): v for k, v in (r.get('path') or {}).items()}
            r['gap_c'] = None if (r.get('fair') is None or r.get('mid') is None) \
                else (r['fair'] - r['mid']) * 100
            rows.append(r)
    return rows


def ci95(xs):
    """Mean and half-width of the 95% CI. With n this small the CI is the whole story."""
    if len(xs) < 2:
        return (xs[0] if xs else 0.0), float('inf')
    return st.mean(xs), 1.96 * st.stdev(xs) / math.sqrt(len(xs))


def ret_hold(r):
    """Return per dollar if held to resolution."""
    return None if r.get('resolved_pnl_c') is None else (r['resolved_pnl_c'] / 100) / r['cost']


def ret_exit(r, tp_c, stop_c, max_hold):
    """Return per dollar under a take-profit / stop / timeout rule, walking the recorded path in
    order. Falls back to resolution if the position never triggers and the path runs out."""
    for h in sorted(r['path']):
        if h > max_hold:
            break
        pnl = r['path'][h]                       # per-share, YES-space, in the traded direction
        if tp_c and pnl * 100 >= tp_c:
            return pnl / r['cost']
        if stop_c and pnl * 100 <= -stop_c:
            return pnl / r['cost']
    pts = [h for h in sorted(r['path']) if h <= max_hold]
    if pts:
        return r['path'][pts[-1]] / r['cost']    # timed out: mark at the last point we saw
    return ret_hold(r)


def summarise(label, rets):
    rets = [x for x in rets if x is not None]
    if not rets:
        return f'{label:<34} n=0'
    m, hw = ci95(rets)
    wins = sum(1 for x in rets if x > 0)
    sig = 'yes' if abs(m) > hw else 'no'
    return (f'{label:<34} n={len(rets):>4}  hit {100*wins/len(rets):>5.1f}%  '
            f'EV/$ {m*100:>+7.1f}% +/-{hw*100:>5.1f}  total ${sum(rets):>+7.2f}  sig:{sig}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('candidates')
    ap.add_argument('--exits', action='store_true', help='sweep exit rules instead of entries')
    a = ap.parse_args()
    rows = load(a.candidates)
    if not rows:
        sys.exit('no candidates')
    print(f'{len(rows)} candidates from {len(set(r["src"] for r in rows))} recordings\n')

    fade = [r for r in rows if r['scenario'] == 'fade']
    catch = [r for r in rows if r['scenario'] == 'catchup']

    if not a.exits:
        print('=== held to resolution, by scenario ===')
        for lbl, g in (('all', rows), ('fade', fade), ('catchup', catch)):
            print(summarise(lbl, [ret_hold(r) for r in g]))

        print('\n=== FADE: raise the dislocation threshold (|gap| = fair - mid, cents) ===')
        for lo in (0, 3, 5, 8, 12, 16, 20, 25):
            g = [r for r in fade if r['gap_c'] is not None and abs(r['gap_c']) >= lo]
            print(summarise(f'  fade |gap| >= {lo}c', [ret_hold(r) for r in g]))

        print('\n=== FADE: raise the trigger strength (|robust z|) ===')
        for lo in (0, 1.5, 2.0, 2.5, 3.0, 4.0):
            g = [r for r in fade if r.get('z') is not None and abs(r['z']) >= lo]
            print(summarise(f'  fade |z| >= {lo}', [ret_hold(r) for r in g]))

        print('\n=== CATCHUP: same two knobs ===')
        for lo in (0, 3, 5, 8, 12):
            g = [r for r in catch if r['gap_c'] is not None and abs(r['gap_c']) >= lo]
            print(summarise(f'  catchup |gap| >= {lo}c', [ret_hold(r) for r in g]))

        print('\n=== combine with the OLD strike model (both must agree on direction) ===')
        def agrees(r):
            if r.get('model_p') is None or r.get('mid') is None:
                return False
            mp = r['model_p'] if r['side'] == 'BUY' else 1 - r['model_p']
            return mp > r['cost']            # model also sees edge in the leg we bought
        for lbl, g in (('all', rows), ('fade', fade), ('catchup', catch)):
            ag = [r for r in g if agrees(r)]
            print(summarise(f'  {lbl} + model agrees', [ret_hold(r) for r in ag]))
            dis = [r for r in g if not agrees(r)]
            print(summarise(f'  {lbl} + model DISagrees', [ret_hold(r) for r in dis]))

        print('\n=== entry price band (cost per share actually paid) ===')
        for lo, hi in ((0, .25), (.25, .45), (.45, .65), (.65, .85), (.85, 1.01)):
            g = [r for r in rows if lo <= r['cost'] < hi]
            print(summarise(f'  cost {lo:.2f}-{hi:.2f}', [ret_hold(r) for r in g]))
        return

    # ---------------- exit rules ----------------
    print('=== EXIT: take-profit sweep (no stop, 120s cap) ===')
    for lbl, g in (('all', rows), ('fade', fade), ('catchup', catch)):
        print(f'\n  -- {lbl} --')
        print(summarise('    hold to resolution', [ret_hold(r) for r in g]))
        for tp in (2, 3, 5, 8, 12, 20):
            print(summarise(f'    TP {tp}c', [ret_exit(r, tp, None, 120) for r in g]))

    print('\n=== EXIT: take-profit + stop (120s cap) ===')
    for lbl, g in (('fade', fade), ('catchup', catch)):
        print(f'\n  -- {lbl} --')
        for tp, sl in ((3, 6), (5, 5), (5, 10), (8, 8), (8, 16), (12, 12)):
            print(summarise(f'    TP {tp}c / stop {sl}c',
                            [ret_exit(r, tp, sl, 120) for r in g]))

    print('\n=== EXIT: pure time stop (no TP) ===')
    for lbl, g in (('fade', fade), ('catchup', catch)):
        print(f'\n  -- {lbl} --')
        for h in (20, 40, 60, 120, 180):
            print(summarise(f'    flat exit at {h}s',
                            [None if h not in r['path'] else r['path'][h] / r['cost']
                             for r in g]))

    print('\n=== mean per-share P&L by holding time (where does the edge live?) ===')
    for lbl, g in (('fade', fade), ('catchup', catch)):
        line = []
        for h in HORIZONS:
            v = [r['path'][h] for r in g if h in r['path']]
            line.append(f'{h}s:{st.mean(v)*100:+.1f}c(n{len(v)})' if len(v) >= 5 else f'{h}s:--')
        print(f'  {lbl:<8} ' + ' '.join(line))


if __name__ == '__main__':
    main()
