# scripts/backtest_blind_thresholds.py — blind favorite entry on the saved year dataset.
# No drift signal: every cycle, at a fixed checkpoint, buy the favored side at its price.
#   Test 1: fine band sweep (5c bands) -> trades, win%, entry, edge, EV.
#   Test 2: threshold policies "buy if favorite price >= {0.60,0.70,0.80}" -> aggregate EV.
# Two pricings: p_raw = naive Gaussian market maker (edge = its miscalibration);
#               p_cal = calibrated fair (efficient-market null, EV~0). Real market is between.
# EV per $1 on a favorite bought at `e` with realized win rate wr:  wr/e - 1.
import argparse, os
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fav(d, col, fee):
    p = d[col].to_numpy(); up = d.up.to_numpy().astype(bool)
    fav_up = p >= 0.5
    entry = np.clip(np.where(fav_up, p, 1 - p) + fee, 0.01, 0.99)
    win = fav_up == up
    pnl = np.where(win, 1.0 / entry - 1.0, -1.0)          # $1 staked per trade
    return entry, win, pnl


def sweep(d, col, fee, label):
    entry, win, pnl = fav(d, col, fee)
    print(f"\n=== TEST 1: band sweep — {label}, fee +{fee*100:.0f}c ===")
    print(f"{'band':<12}{'trades':>8}{'win%':>8}{'avg entry':>11}{'edge':>8}{'EV/$1':>9}{'tot$':>9}")
    for lo in np.arange(0.50, 0.98, 0.05):
        hi = lo + 0.05
        m = (entry >= lo) & (entry < hi)
        if m.sum() < 30: continue
        wr = win[m].mean(); e = entry[m].mean()
        print(f"{lo:.2f}-{hi:.2f} {m.sum():>8}{100*wr:>8.1f}{e:>11.3f}"
              f"{100*(wr-e):>+7.1f}c{pnl[m].mean():>+9.4f}{pnl[m].sum():>+9.0f}")


def thresholds(d, col, fee, label):
    entry, win, pnl = fav(d, col, fee)
    print(f"\n=== TEST 2: blind entry if favorite price >= X — {label}, fee +{fee*100:.0f}c ===")
    print(f"{'X':>6}{'trades':>9}{'win%':>8}{'avg entry':>11}{'EV/$1':>9}{'tot$':>10}")
    for x in (0.60, 0.70, 0.80):
        m = entry >= x
        if m.sum() < 30: continue
        print(f">={x:.2f}{m.sum():>9}{100*win[m].mean():>8.1f}{entry[m].mean():>11.3f}"
              f"{pnl[m].mean():>+9.4f}{pnl[m].sum():>+10.0f}")


def main(path, checkpoint):
    d = pd.read_parquet(path)
    d = d[(~d.tie) & (d.checkpoint_min == checkpoint)]
    print(f"dataset {os.path.basename(path)} | entry at t+{checkpoint}min (tau={5-checkpoint}min) "
          f"| {len(d):,} cycles (one blind trade each)")

    sweep(d, 'p_raw', 0.0, 'naive-market price')
    sweep(d, 'p_raw', 0.02, 'naive-market price')
    sweep(d, 'p_cal', 0.0, 'calibrated fair (null)')

    for fee in (0.0, 0.02):
        thresholds(d, 'p_raw', fee, 'naive-market price')
    thresholds(d, 'p_cal', 0.0, 'calibrated fair (null)')

    print("\n=== EV of the >=0.70 policy by entry time (naive-market, 0c) ===")
    full = pd.read_parquet(path); full = full[~full.tie]
    for cp in (1, 2, 3, 4):
        sub = full[full.checkpoint_min == cp]
        e, w, pnl = fav(sub, 'p_raw', 0.0)
        m = e >= 0.70
        print(f"  t+{cp}min (tau={5-cp}min): trades={m.sum():>6} win={100*w[m].mean():.1f}% "
              f"EV/$1={pnl[m].mean():+.4f}")
    print("\nnote: EV>0 needs realized win% > price paid. p_cal columns ~0 = efficient-market null;")
    print("the p_raw edge is real ONLY if live Polymarket prices like the uncalibrated model.")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dataset', default=os.path.join(ROOT, 'data', 'cycles_BTCUSDT_365d.parquet'))
    ap.add_argument('--checkpoint', type=int, default=2)
    a = ap.parse_args()
    main(a.dataset, a.checkpoint)
