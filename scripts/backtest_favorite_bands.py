# scripts/backtest_favorite_bands.py — test the "buy the 70-80c favorite" hypothesis on
# the saved year dataset, and check whether 50-60c is the edge-killer.
#
# Two framings, because the answer depends entirely on what the MARKET prices like:
#   binned by p_raw  = a naive Gaussian market maker (proxy). Edge = its miscalibration.
#   binned by p_cal  = the market already prices at our best fair. Null: EV~0 everywhere.
# The real market sits between; only paper trading against live books settles it.
#
# EV per $1 staked on a favorite priced `entry` with realized win rate wr:  wr/entry - 1
# (>0 iff the favorite resolves YES more often than its price implies).
import argparse, os
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANDS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.98)]


def analyze(d, price_col, label, fee=0.0):
    p = d[price_col].to_numpy()
    up = d.up.to_numpy().astype(bool)
    fav_up = p >= 0.5
    entry = np.where(fav_up, p, 1 - p) + fee     # price of the favored side (+fee/slippage)
    win = fav_up == up
    print(f"\n=== priced by {label} (fee/slip +{fee*100:.0f}c) ===")
    print(f"{'band':<12}{'n':>8}{'win%':>8}{'entry':>8}{'edge(wr-px)':>13}{'EV/$1':>9}{'acc%':>7}")
    for lo, hi in BANDS:
        m = (entry >= lo) & (entry < hi)
        if m.sum() < 50:
            print(f"{lo:.2f}-{hi:.2f} {m.sum():>8}  (thin)"); continue
        wr = win[m].mean(); px = entry[m].mean()
        ev = wr / px - 1
        print(f"{lo:.2f}-{hi:.2f} {m.sum():>8}{100*wr:>8.1f}{px:>8.3f}"
              f"{100*(wr-px):>+12.1f}c{ev:>+9.4f}{100*wr:>7.1f}")


def main(path, checkpoint):
    d = pd.read_parquet(path)
    d = d[(~d.tie) & (d.checkpoint_min == checkpoint)]
    print(f"dataset {os.path.basename(path)} | checkpoint t+{checkpoint}min "
          f"(tau={5-checkpoint}min) | {len(d):,} cycles")

    # 1) null framing: market == our best fair -> is 70-80 special? (expect EV~0 all bands)
    analyze(d, 'p_cal', 'calibrated fair (efficient-market null)')

    # 2) naive-Gaussian-market framing: where a raw-model market maker misprices
    for fee in (0.0, 0.01, 0.02):
        analyze(d, 'p_raw', 'raw Gaussian model (naive-market proxy)', fee=fee)

    # 3) the 50-60 danger check: directional accuracy vs |edge from 0.5|
    print("\n=== accuracy vs how far the favorite is from a coin flip (p_cal) ===")
    p = d.p_cal.to_numpy(); up = d.up.to_numpy().astype(bool)
    acc = (p >= 0.5) == up
    for lo, hi in [(0.50, 0.55), (0.55, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.0)]:
        fav = np.maximum(p, 1 - p)
        m = (fav >= lo) & (fav < hi)
        if m.sum() < 50: continue
        print(f"  favorite {lo:.2f}-{hi:.2f}: n={m.sum():>6}  directional accuracy {100*acc[m].mean():.1f}%")
    print("\nread: EV/$1 is positive only where realized win% exceeds the price paid.")
    print("50-60c near a coin flip: any fee makes it -EV -> the zone to avoid, as you suspected.")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dataset', default=os.path.join(ROOT, 'data', 'cycles_BTCUSDT_365d.parquet'))
    ap.add_argument('--checkpoint', type=int, default=2, help='minutes into the 5m cycle (1-4)')
    a = ap.parse_args()
    main(a.dataset, a.checkpoint)
