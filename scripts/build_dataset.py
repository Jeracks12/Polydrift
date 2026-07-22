# scripts/build_dataset.py — materialize the reusable per-cycle dataset from cached klines.
# One row per (cycle, checkpoint): raw + walk-forward-calibrated model probs, outcome.
# Every strategy/scenario script should read this parquet instead of recomputing.
# Usage: python scripts/build_dataset.py [--days 365] [--symbol BTCUSDT]
import argparse, math, os, sys, time
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_year import load_year, fit_recal, _phi, logit_np, DATA


def build(symbol='BTCUSDT', days=365, hl_min=15):
    df = load_year(symbol, days)
    ts = df.ts.to_numpy(); close = df.close.to_numpy(); openp = df.open.to_numpy()
    lr = np.diff(np.log(close), prepend=np.log(close[0]))
    sig = np.sqrt(pd.Series(lr ** 2).ewm(halflife=hl_min).mean().to_numpy())

    t0 = int(math.ceil(ts[0] / 300) * 300)
    starts = np.arange(t0, ts[-1] - 300, 300)
    i0 = ((starts - ts[0]) // 60).astype(int)
    K = openp[i0]; endp = close[i0 + 4]
    up = endp > K; tie = endp == K
    day = ((starts - starts[0]) // 86400).astype(int)
    date = np.array([time.strftime('%Y-%m-%d', time.gmtime(s)) for s in starts])

    p_raw = {}
    for m in (1, 2, 3, 4):
        S = close[i0 + m]; s_ = np.maximum(sig[i0 + m], 1e-8)
        p_raw[m] = np.clip(_phi(np.log(S / K) / (s_ * np.sqrt(5 - m))), 1e-6, 1 - 1e-6)

    # walk-forward calibration, 30d blocks, per checkpoint (no lookahead)
    p_cal = {m: p_raw[m].copy() for m in (1, 2, 3, 4)}
    for b in range(int(np.ceil((day.max() + 1) / 30))):
        lo, hi = b * 30, (b + 1) * 30
        mask_fit = (day < lo) & ~tie
        mask_app = (day >= lo) & (day < hi)
        for m in (1, 2, 3, 4):
            a_, b_ = fit_recal(p_raw[m][mask_fit], up[mask_fit].astype(float)) \
                     if mask_fit.sum() >= 500 else (1.0, 0.0)
            z = np.clip(a_ * logit_np(p_raw[m][mask_app]) + b_, -30, 30)
            p_cal[m][mask_app] = 1 / (1 + np.exp(-z))

    rows = []
    for m in (1, 2, 3, 4):
        rows.append(pd.DataFrame({
            'cycle_ts': starts, 'date': date, 'day': day, 'checkpoint_min': m,
            'tau_min': 5 - m, 'K': K, 'S': close[i0 + m], 'end': endp,
            'sigma_1m': sig[i0 + m], 'p_raw': p_raw[m], 'p_cal': p_cal[m],
            'up': up, 'tie': tie,
        }))
    out = pd.concat(rows, ignore_index=True).sort_values(['cycle_ts', 'checkpoint_min'])
    path = os.path.join(DATA, f'cycles_{symbol}_{days}d.parquet')
    out.to_parquet(path, index=False)
    print(f"dataset -> {path}  ({len(out):,} rows, {len(starts):,} cycles, "
          f"{os.path.getsize(path) / 1e6:.1f} MB)")
    return path


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=365)
    ap.add_argument('--symbol', default='BTCUSDT')
    a = ap.parse_args()
    build(a.symbol, a.days)
