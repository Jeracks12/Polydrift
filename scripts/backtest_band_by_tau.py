# scripts/backtest_band_by_tau.py — the 0.60-0.70 mild-favorite band, by time remaining.
# For each entry time (tau = 3,2,1 min), of the cycles whose favorite is priced in the
# band, report trades / win% / edge / EV under naive-market and efficient (calibrated) pricing.
# EV per $1 on a favorite bought at `e` with realized win rate wr:  wr/e - 1.
import argparse, os
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def band_stats(d, col, lo, hi, fee):
    p = d[col].to_numpy(); up = d.up.to_numpy().astype(bool)
    fav_up = p >= 0.5
    entry = np.clip(np.where(fav_up, p, 1 - p) + fee, 0.01, 0.99)
    win = fav_up == up
    m = (entry >= lo) & (entry < hi)
    if m.sum() < 30:
        return None
    wr = win[m].mean(); e = entry[m].mean()
    pnl = np.where(win[m], 1.0 / entry[m] - 1.0, -1.0)
    return dict(n=int(m.sum()), wr=wr, e=e, edge=wr - e, ev=pnl.mean(), tot=pnl.sum())


def main(path, lo, hi):
    full = pd.read_parquet(path); full = full[~full.tie]
    print(f"dataset {os.path.basename(path)} | band {lo:.2f}-{hi:.2f} | {full.cycle_ts.nunique():,} cycles\n")
    # checkpoint m -> tau = 5-m min remaining; user asked tau = 3,2,1 -> m = 2,3,4
    print(f"{'time left':>10}{'trades':>8}{'win%':>8}{'entry':>8}{'edge':>8}"
          f"{'EV naive0c':>12}{'EV naive2c':>12}{'EV effic.':>11}")
    for m in (2, 3, 4):
        tau = 5 - m
        sub = full[full.checkpoint_min == m]
        r0 = band_stats(sub, 'p_raw', lo, hi, 0.0)
        r2 = band_stats(sub, 'p_raw', lo, hi, 0.02)
        rc = band_stats(sub, 'p_cal', lo, hi, 0.0)
        if not r0:
            print(f"{tau} min{' ':>4}(thin)"); continue
        # m=4 observes S=close[i0+4], which IS the resolution bar -> lookahead, win%=100
        flag = '  <-- LOOKAHEAD (obs=resolution bar); IGNORE' if m == 4 else ''
        print(f"{tau} min left {r0['n']:>7}{100*r0['wr']:>8.1f}{r0['e']:>8.3f}"
              f"{100*r0['edge']:>+7.1f}c{r0['ev']:>+12.4f}"
              f"{(r2['ev'] if r2 else float('nan')):>+12.4f}{rc['ev']:>+11.4f}{flag}")

    print("\ninterpretation:")
    print(" - EV naive = edge vs an uncalibrated Gaussian market maker (optimistic).")
    print(" - EV effic. = edge if the market already prices at our best fair (the honest floor).")
    print(" - win% climbing toward 100% at 1 min left is near-certain resolution, NOT more edge:")
    print("   that regime is the most efficient live and the most exposed to near-strike oracle basis.")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dataset', default=os.path.join(ROOT, 'data', 'cycles_BTCUSDT_365d.parquet'))
    ap.add_argument('--lo', type=float, default=0.60)
    ap.add_argument('--hi', type=float, default=0.70)
    a = ap.parse_args()
    main(a.dataset, a.lo, a.hi)
