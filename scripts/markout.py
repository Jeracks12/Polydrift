#!/usr/bin/env python
"""markout.py — the market-maker's verdict: after someone hits our quote, which way does the
market go?

This is the ONE number that decides whether maker mode is a business or a slow bleed, and it is
the number no backtest in this repo has ever produced. `scripts/sweep_fees.py` scored the maker
by re-pricing the TAKER's candidate signals as if we had been resting — a structural-advantage
calculation that never asks who traded against us or why.

THE DECOMPOSITION. For a bid fill (we bought YES at `price` while the mid was `mid0`):

    spread_captured = mid0 - price                 # what resting instead of crossing earned
    drift(h)        = mid(t+h) - mid0              # where the market went after we were hit
    markout(h)      = mid(t+h) - price = spread_captured + drift(h)

An ask fill is the mirror (we are short YES, so both terms flip sign). Then:

  * spread_captured > 0 and drift ~ 0   -> uninformed flow. The structural edge survives.
  * drift systematically NEGATIVE       -> ADVERSE SELECTION. We are being picked off faster
                                           than the toxicity gate can pull (~1.4s round trip),
                                           and the spread is not paying for it.

Markout is measured against the MID on purpose: it isolates the information content of the flow
from execution cost. Fees are reported separately (a maker should pay none).

  python scripts/markout.py                       # auto-discover today's logs
  python scripts/markout.py --fills maker_fills.csv --state logs/2026-07-24/btc-updown-5m/market_state.csv
  python scripts/markout.py --horizons 5,15,30,60
"""
import argparse, csv, glob, os, statistics as st, sys

HORIZONS = [1, 5, 15, 30, 60]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_fills(path):
    out = []
    with open(path, newline='') as fh:
        for r in csv.DictReader(fh):
            ts, px = _f(r.get('ts')), _f(r.get('price'))
            if ts is None or px is None:
                continue
            out.append(dict(ts=ts, slug=r.get('slug', ''), side=r.get('side', ''), price=px,
                            size=_f(r.get('size')) or 0.0, fee=_f(r.get('fee')) or 0.0,
                            mid0=_f(r.get('mid0')), z=_f(r.get('z')), tau=_f(r.get('tau')),
                            mode=r.get('mode', '')))
    out.sort(key=lambda x: x['ts'])
    return out


def load_mids(paths):
    """(ts, mid) from market_state.csv, mid = (bid+ask)/2, falling back to poly_last."""
    rows = []
    for p in paths:
        with open(p, newline='') as fh:
            for r in csv.DictReader(fh):
                ts = _f(r.get('ts'))
                if ts is None:
                    continue
                b, a = _f(r.get('poly_bid')), _f(r.get('poly_ask'))
                mid = (b + a) / 2 if (b and a) else _f(r.get('poly_last'))
                if mid is not None:
                    rows.append((ts, mid, r.get('slug', '')))
    rows.sort(key=lambda x: x[0])
    return rows


def mid_at(mids, ts, slug=None, max_gap=10.0):
    """Last mid at or before `ts` (same market if slug given). None if the tape is too stale —
    never extrapolate across a gap, that would invent a markout."""
    lo, hi = 0, len(mids) - 1
    best = None
    while lo <= hi:
        m = (lo + hi) // 2
        if mids[m][0] <= ts:
            best = m; lo = m + 1
        else:
            hi = m - 1
    if best is None:
        return None
    i = best
    while i >= 0 and ts - mids[i][0] <= max_gap:
        if slug is None or not mids[i][2] or mids[i][2] == slug:
            return mids[i][1]
        i -= 1
    return None


def markouts(fills, mids, horizons):
    """Attach signed spread/drift/markout per horizon to each fill."""
    out = []
    for f in fills:
        m0 = f['mid0'] if f['mid0'] is not None else mid_at(mids, f['ts'], f['slug'])
        if m0 is None:
            continue
        sign = 1.0 if f['side'] == 'bid' else -1.0        # bid = long YES, ask = short YES
        rec = dict(f, mid0=m0, spread_c=sign * (m0 - f['price']) * 100)
        ok = False
        for h in horizons:
            mh = mid_at(mids, f['ts'] + h, f['slug'])
            if mh is None:
                rec[f'drift_{h}'] = rec[f'mk_{h}'] = None
                continue
            ok = True
            rec[f'drift_{h}'] = sign * (mh - m0) * 100
            rec[f'mk_{h}'] = sign * (mh - f['price']) * 100
        if ok:
            out.append(rec)
    return out


def summarize(label, rows, horizons, min_n=5):
    vals = [r['spread_c'] for r in rows]
    if len(rows) < min_n:
        return f'  {label:<26} n={len(rows):<4} (too few)'
    line = (f'  {label:<26} n={len(rows):<4} spread {st.mean(vals):+6.2f}c  |')
    for h in horizons:
        d = [r[f'drift_{h}'] for r in rows if r.get(f'drift_{h}') is not None]
        m = [r[f'mk_{h}'] for r in rows if r.get(f'mk_{h}') is not None]
        line += (f'  {h}s drift {st.mean(d):+6.2f} mk {st.mean(m):+6.2f}'
                 if d else f'  {h}s   --  ')
    return line


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fills', default='maker_fills.csv')
    ap.add_argument('--state', nargs='*', default=None,
                    help='market_state.csv file(s); default = auto-discover under logs/')
    ap.add_argument('--horizons', default=','.join(map(str, HORIZONS)))
    ap.add_argument('--toxic-z', type=float, default=2.5,
                    help='the --maker-toxic-z the run used, for the gate-effectiveness split')
    a = ap.parse_args()
    horizons = [int(x) for x in a.horizons.split(',') if x.strip()]

    if not os.path.exists(a.fills):
        sys.exit(f'no fills log at {a.fills}. Run maker mode first:\n'
                 f'  python poly_drift.py --series btc-updown-5m --mode paper --maker')
    fills = load_fills(a.fills)
    if not fills:
        sys.exit(f'{a.fills} has no usable rows yet (no fills recorded).')

    state = a.state or sorted(glob.glob(os.path.join('logs', '*', '*', 'market_state.csv')))
    state = [p for p in state if os.path.exists(p)]
    if not state:
        sys.exit('no market_state.csv found (is CFG STATE_LOG on?). Pass --state explicitly.')
    mids = load_mids(state)
    if not mids:
        sys.exit('market_state.csv had no usable mid data.')

    rows = markouts(fills, mids, horizons)
    print(f'{len(fills)} fills / {len(rows)} with usable forward tape | '
          f'{len(mids):,} state ticks from {len(state)} file(s)')
    if not rows:
        sys.exit('no fill had forward tape within the gap limit — record a longer session.')
    modes = sorted(set(r['mode'] for r in rows if r['mode']))
    print(f"modes: {', '.join(modes) or 'unknown'}   "
          f"(paper fills are a CONSERVATIVE simulation; live is the real answer)\n")

    print('drift = where the mid went AFTER we were hit (the adverse-selection term).')
    print('mk    = total markout from our fill price = spread + drift. All in cents/share.\n')
    print(summarize('ALL FILLS', rows, horizons))
    print()
    for side, lbl in (('bid', 'bid fills (long YES)'), ('ask', 'ask fills (short YES)')):
        print(summarize(lbl, [r for r in rows if r['side'] == side], horizons))

    print('\n--- by |z| at fill: is the toxicity gate catching the informed flow? ---')
    zs = [r for r in rows if r.get('z') is not None]
    if zs:
        for lo, hi in ((0, 1), (1, 2), (2, a.toxic_z), (a.toxic_z, 1e9)):
            g = [r for r in zs if lo <= abs(r['z']) < hi]
            tag = '  <-- gate should have PULLED these' if lo >= a.toxic_z else ''
            print(summarize(f'|z| {lo:g}-{hi:g}' + tag, g, horizons))
        print(f'  (fills above |z|={a.toxic_z:g} got through despite the gate: either the pull was'
              f'\n   too slow — ~1.4s round trip — or z spiked only after the fill.)')
    else:
        print('  no z recorded on fills (older log format)')

    print('\n--- by time left in the cycle ---')
    ts_ = [r for r in rows if r.get('tau') is not None]
    for lo, hi in ((0, 60), (60, 150), (150, 1e9)):
        print(summarize(f'tau {lo:g}-{hi:g}s', [r for r in ts_ if lo <= r['tau'] < hi], horizons))

    h = horizons[-1]
    fin = [r for r in rows if r.get(f'mk_{h}') is not None]
    if fin:
        mk = [r[f'mk_{h}'] for r in fin]
        sp = [r['spread_c'] for r in fin]
        dr = [r[f'drift_{h}'] for r in fin]
        fee = sum(r['fee'] for r in fin)
        win = sum(1 for x in mk if x > 0)
        print(f'\n=== VERDICT at {h}s ===')
        print(f'  spread captured   {st.mean(sp):+6.2f}c/share   (resting instead of crossing)')
        print(f'  post-fill drift   {st.mean(dr):+6.2f}c/share   (adverse selection)')
        print(f'  net markout       {st.mean(mk):+6.2f}c/share   ({win}/{len(mk)} = '
              f'{100*win/len(mk):.0f}% of fills positive)')
        print(f'  fees paid         {fee:.4f} USDC total        '
              f'({"OK - makers pay zero" if fee <= 1e-9 else "!!! PREMISE VIOLATED"})')
        if st.mean(dr) < -abs(st.mean(sp)):
            print('\n  ADVERSE SELECTION EXCEEDS THE SPREAD. The flow hitting these quotes is\n'
                  '  informed: widen, tighten --maker-toxic-z, or do not make this market.')
        elif st.mean(mk) > 0:
            print('\n  Net positive: the spread is paying for the toxicity. Sample size is the\n'
                  '  next question — this needs hundreds of fills across many sessions.')
        else:
            print('\n  Net negative but drift alone does not explain it — check the per-side and\n'
                  '  per-tau splits above for where it leaks.')


if __name__ == '__main__':
    main()
