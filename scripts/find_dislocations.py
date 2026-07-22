#!/usr/bin/env python
"""find_dislocations.py — mine market_state.csv for PHANTOM DRIFT: poly repricing hard while
the exchange barely moved, then reverting.

This is the measurement tool for the pattern observed live on 2026-07-21 15:03 (BTC 5m):
poly UP 0.785 -> 0.455 in 15s while BTC spot moved -4.6bp, then back to 0.855 within 45s.
Buying the 0.55 ask and selling at 0.84 is +29c/share. The question this script answers is
not "did it happen" but "how OFTEN, how DEEP is the book, how RELIABLY does it revert, and
which gate declined it" — the only numbers that decide whether it is a strategy.

Usage:
  python scripts/find_dislocations.py logs/2026-07-22/btc-updown-5m/market_state.csv
  python scripts/find_dislocations.py <csv> --drop 0.08 --max-bp 12 --window 20 --revert 60
"""
import argparse, csv, os, sys
from collections import Counter


def load(path):
    rows = []
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            def num(k):
                v = r.get(k, '')
                try: return float(v)
                except (TypeError, ValueError): return None
            rows.append(dict(ts=num('ts'), slug=r.get('slug', ''), tau=num('tau_s'),
                             spot=num('spot'), bp=num('ref_bp_vs_strike'),
                             bid=num('poly_bid'), ask=num('poly_ask'),
                             bid_sz=num('poly_bid_sz'), ask_sz=num('poly_ask_sz'),
                             mid=num('poly_last'), p_up=num('p_up'), gate=r.get('gate', '')))
    rows = [r for r in rows if r['ts'] is not None]
    rows.sort(key=lambda r: r['ts'])
    return rows


def polymid(r):
    if r['bid'] is not None and r['ask'] is not None: return (r['bid'] + r['ask']) / 2
    return r['mid']


def find(rows, drop, max_bp, window, revert):
    """A dislocation = poly mid falls (or rises) >= `drop` within `window` seconds while the
    exchange moved <= `max_bp` basis points. Direction-agnostic: an UP crash is a cheap YES,
    an UP spike is a cheap NO."""
    events, i, n = [], 0, len(rows)
    while i < n:
        a = rows[i]
        pa = polymid(a)
        if pa is None or a['bp'] is None:
            i += 1; continue
        best = None
        j = i + 1
        while j < n and rows[j]['ts'] - a['ts'] <= window:
            b = rows[j]
            pb = polymid(b)
            if pb is not None and b['bp'] is not None and b['slug'] == a['slug']:
                dp, dbp = pb - pa, abs(b['bp'] - a['bp'])
                if abs(dp) >= drop and dbp <= max_bp and (best is None or abs(dp) > abs(best[1])):
                    best = (b, dp, dbp)
            j += 1
        if best is None:
            i += 1; continue
        trough, dp, dbp = best
        side = 'YES' if dp < 0 else 'NO'                    # buy whichever side got dumped
        entry = trough['ask'] if side == 'YES' else (1 - trough['bid'] if trough['bid'] else None)
        size = trough['ask_sz'] if side == 'YES' else trough['bid_sz']
        # best exit available within `revert` seconds, same market
        exit_px, exit_dt = None, None
        k = j
        while k < n and rows[k]['ts'] - trough['ts'] <= revert:
            c = rows[k]
            if c['slug'] == trough['slug']:
                q = c['bid'] if side == 'YES' else (1 - c['ask'] if c['ask'] else None)
                if q is not None and (exit_px is None or q > exit_px):
                    exit_px, exit_dt = q, c['ts'] - trough['ts']
            k += 1
        events.append(dict(t0=a['ts'], t1=trough['ts'], slug=trough['slug'], tau=trough['tau'],
                           side=side, poly_from=pa, poly_to=polymid(trough), d_poly=dp,
                           d_bp=dbp, entry=entry, size=size, exit_px=exit_px, exit_dt=exit_dt,
                           edge=(None if (entry is None or exit_px is None) else exit_px - entry),
                           gate=trough['gate']))
        i = j                                               # non-overlapping episodes
    return events


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('csv', nargs='+', help='market_state.csv file(s)')
    ap.add_argument('--drop', type=float, default=0.08, help='min poly move, prob units (0.08)')
    ap.add_argument('--max-bp', type=float, default=12.0, help='max exchange move, bp (12)')
    ap.add_argument('--window', type=float, default=20.0, help='dislocation window, s (20)')
    ap.add_argument('--revert', type=float, default=60.0, help='exit search horizon, s (60)')
    ap.add_argument('--fee-c', type=float, default=0.0, help='round-trip cost, cents/share')
    a = ap.parse_args()

    rows = []
    for p in a.csv:
        if not os.path.exists(p):
            sys.exit(f'no such file: {p}')
        rows += load(p)
    rows.sort(key=lambda r: r['ts'])
    if not rows:
        sys.exit('no usable rows (is STATE_LOG on?)')

    ev = find(rows, a.drop, a.max_bp, a.window, a.revert)
    span_h = (rows[-1]['ts'] - rows[0]['ts']) / 3600 or 1e-9
    print(f'{len(rows)} ticks over {span_h:.2f}h -> {len(ev)} dislocations '
          f'({len(ev)/span_h:.1f}/h) | drop>={a.drop} bp<={a.max_bp} win={a.window}s')
    if not ev: return
    print(f"\n{'time':<9}{'side':<5}{'poly move':<18}{'exch':>7}{'entry':>7}{'size':>8}"
          f"{'exit':>7}{'dt':>5}{'edge':>7}  gate")
    import datetime
    scored = []
    for e in ev:
        t = datetime.datetime.fromtimestamp(e['t1']).strftime('%H:%M:%S')
        edge = None if e['edge'] is None else e['edge'] - a.fee_c / 100
        if edge is not None: scored.append((edge, e))
        f = lambda v, s='{:.3f}': '  n/a' if v is None else s.format(v)
        dt = f(e['exit_dt'], '{:.0f}')
        print(f"{t:<9}{e['side']:<5}{e['poly_from']:.3f}->{e['poly_to']:.3f} "
              f"({e['d_poly']:+.3f}) {e['d_bp']:>6.1f}{f(e['entry']):>7}"
              f"{f(e['size'], '{:.0f}'):>8}{f(e['exit_px']):>7}{dt:>5}"
              f"{f(edge, '{:+.3f}'):>7}  {e['gate']}")
    if scored:
        edges = [s for s, _ in scored]
        wins = sum(1 for s in edges if s > 0)
        print(f"\nreverting within {a.revert}s: {wins}/{len(edges)} = {wins/len(edges)*100:.0f}% "
              f"| mean edge {sum(edges)/len(edges)*100:+.2f}c | "
              f"median {sorted(edges)[len(edges)//2]*100:+.2f}c")
        fill = [e['size'] * e['entry'] for _, e in scored if e['size'] and e['entry']]
        if fill:
            fill.sort()
            print(f"top-of-book $ at entry: median ${fill[len(fill)//2]:,.0f} "
                  f"min ${fill[0]:,.0f} max ${fill[-1]:,.0f}")
        print('\ngate at the dislocation (why we did not trade it):')
        for g, c in Counter(e['gate'] for _, e in scored).most_common():
            print(f'  {g or "(none)":<18}{c:>5}')


if __name__ == '__main__':
    main()
