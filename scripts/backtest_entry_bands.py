# scripts/backtest_entry_bands.py — the cheap-entry strategy, tested on the saved dataset.
# Market proxy: shares quoted at the RAW Gaussian model + 1c spread (a naive market maker).
# Truth proxy:  the walk-forward CALIBRATED probability.
# Trade rule:   buy a side when truth > its ask; at most one trade per cycle (first
#               qualifying checkpoint). Results bucketed by ENTRY PRICE of the share
#               bought — the user's hypothesis: cheap shares carry the edge.
# Usage: python scripts/backtest_entry_bands.py [--dataset data/cycles_BTCUSDT_365d.parquet]
import argparse, os, sys
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD = 0.01
BANDS = [(0.02, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40), (0.40, 0.50),
         (0.50, 0.65), (0.65, 0.80), (0.80, 0.98)]


def run(path, min_edge=0.0):
    d = pd.read_parquet(path)
    d = d[~d.tie]

    # per row: candidate = the side with positive model edge vs its naive ask
    ask_up = np.clip(d.p_raw + SPREAD, 0.02, 0.99)
    ask_dn = np.clip(1 - d.p_raw + SPREAD, 0.02, 0.99)
    edge_up = d.p_cal - ask_up
    edge_dn = (1 - d.p_cal) - ask_dn
    take_up = edge_up > np.maximum(edge_dn, min_edge)
    take_dn = edge_dn > np.maximum(edge_up, min_edge)
    d = d.assign(side_up=take_up, qualifies=take_up | take_dn,
                 entry=np.where(take_up, ask_up, ask_dn),
                 truth=np.where(take_up, d.p_cal, 1 - d.p_cal))

    # one trade per cycle: first qualifying checkpoint
    t = d[d.qualifies].sort_values(['cycle_ts', 'checkpoint_min']).groupby('cycle_ts').first().reset_index()
    t['win'] = np.where(t.side_up, t.up, ~t.up)
    t['pnl'] = np.where(t.win, (1 - t.entry) / t.entry, -1.0)

    n_cycles = d.cycle_ts.nunique()
    print(f"dataset: {os.path.basename(path)} | cycles={n_cycles:,} | trades={len(t):,} "
          f"({100 * len(t) / n_cycles:.1f}% of cycles) | spread={SPREAD:.2f} min_edge={min_edge}")
    print(f"\n{'entry band':<12}{'trades':>8}{'win%':>8}{'avg entry':>11}{'payoff x':>10}"
          f"{'EV/$1':>9}{'total $':>10}")
    for lo, hi in BANDS:
        b = t[(t.entry >= lo) & (t.entry < hi)]
        if not len(b):
            print(f"{lo:.2f}-{hi:.2f}  {0:>8}"); continue
        print(f"{lo:.2f}-{hi:.2f}  {len(b):>8,}{100 * b.win.mean():>8.1f}{b.entry.mean():>11.3f}"
              f"{((1 - b.entry) / b.entry).mean():>10.2f}{b.pnl.mean():>+9.4f}{b.pnl.sum():>+10.0f}")
    cheap = t[t.entry <= 0.45]; rich = t[t.entry > 0.45]
    print(f"\n{'CHEAP <=0.45':<14}{len(cheap):>6,} trades  win {100 * cheap.win.mean():5.1f}%  "
          f"EV {cheap.pnl.mean():+.4f}/$1  total {cheap.pnl.sum():+,.0f}")
    print(f"{'RICH  >0.45':<14}{len(rich):>6,} trades  win {100 * rich.win.mean():5.1f}%  "
          f"EV {rich.pnl.mean():+.4f}/$1  total {rich.pnl.sum():+,.0f}")

    # stability: first vs second half of the sample
    mid = t.cycle_ts.median()
    for name, h in (('first half', t[t.cycle_ts <= mid]), ('second half', t[t.cycle_ts > mid])):
        c = h[h.entry <= 0.45]
        if len(c):
            print(f"  cheap<=0.45 {name}: {len(c):,} trades, win {100 * c.win.mean():.1f}%, "
                  f"EV {c.pnl.mean():+.4f}")

    out = os.path.join(ROOT, 'data', 'entry_bands_results.csv')
    t[['cycle_ts', 'date', 'checkpoint_min', 'side_up', 'entry', 'truth', 'win', 'pnl']].to_csv(out, index=False)
    print(f"\nper-trade log -> {out}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dataset', default=os.path.join(ROOT, 'data', 'cycles_BTCUSDT_365d.parquet'))
    ap.add_argument('--min-edge', type=float, default=0.0)
    a = ap.parse_args()
    run(a.dataset, a.min_edge)
