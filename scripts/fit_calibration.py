#!/usr/bin/env python
"""fit_calibration.py — replace the Gaussian binary pricer with the empirical distribution.

`binary_p_up` prices the 5-minute up/down binary as Phi(ln(S/K)/(sigma*sqrt(tau))), then patches
the known overconfidence with a single logit-space slope (CALIB_SLOPE=0.9). Both steps are
assumptions. data/cycles_BTCUSDT_365d.parquet holds 420k rows / 105k real cycles with S, K,
sigma and the realised outcome, so the conditional frequency can simply be MEASURED.

Why it matters here specifically: 5-minute BTC returns are fat-tailed, so a Gaussian is wrong
exactly in the tails -- which is where the model is most confident, where it manufactures the
biggest apparent edge, and where the bot pays the most per share.

Walk-forward by construction: the table is fitted on the FIRST `--train-frac` of days and
scored on the remainder, so the reported Brier is out-of-sample.

  python scripts/fit_calibration.py
  python scripts/fit_calibration.py --emit      # print the knots to paste into drift_math.py
"""
import argparse, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from drift_math import calibrate_prob                                       # noqa: E402

PARQUET = os.path.join('data', 'cycles_BTCUSDT_365d.parquet')


def standardized_moneyness(d):
    """x = ln(S/K) / (sigma*sqrt(tau)) -- the exact argument Phi() is applied to, so an
    empirical P(up|x) is a drop-in replacement for the Gaussian CDF and nothing else in the
    pricing path has to change."""
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.log(d['S'] / d['K']) / (d['sigma_1m'] * np.sqrt(d['tau_min']))


def brier(p, y):
    return float(np.mean((np.asarray(p, float) - np.asarray(y, float)) ** 2))


def pava(p, w):
    """Weighted pool-adjacent-violators. Returns the isotonic (non-decreasing) fit on the same
    grid. P(up) MUST be non-decreasing in moneyness; letting sampling noise invert it anywhere
    would hand the bot a fake edge exactly at that inversion."""
    blocks = []                                   # [weighted_sum, weight, n_bins_covered]
    for pi, wi in zip(p, w):
        blocks.append([float(pi) * float(wi), float(wi), 1])
        while len(blocks) > 1 and (blocks[-2][0] / blocks[-2][1]
                                   > blocks[-1][0] / blocks[-1][1] + 1e-15):
            b, a_ = blocks.pop(), blocks.pop()
            blocks.append([a_[0] + b[0], a_[1] + b[1], a_[2] + b[2]])
    out = []
    for s, wt, cnt in blocks:
        out += [s / wt] * cnt
    return np.asarray(out, float)


def fit_table(x, y, knots, min_n=30):
    """Empirical P(up) per equal-width bin of the clipped standardized moneyness, then isotonic."""
    mid = (knots[:-1] + knots[1:]) / 2
    idx = np.clip(np.searchsorted(knots, np.clip(x, knots[0], knots[-1])) - 1, 0, len(mid) - 1)
    n = np.bincount(idx, minlength=len(mid)).astype(float)
    s = np.bincount(idx, weights=y, minlength=len(mid))
    p = np.where(n >= min_n, s / np.maximum(n, 1), np.nan)
    ok = ~np.isnan(p)
    if ok.sum() < 3:
        raise SystemExit('not enough populated bins')
    p = np.interp(mid, mid[ok], p[ok])            # thin bins borrow from their neighbours
    return mid, np.clip(pava(p, np.maximum(n, 1.0)), 1e-4, 1 - 1e-4)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--parquet', default=PARQUET)
    ap.add_argument('--bins', type=int, default=41)
    ap.add_argument('--clip', type=float, default=3.0, help='|x| clip for the table (3.0)')
    ap.add_argument('--train-frac', type=float, default=0.6)
    ap.add_argument('--calib-slope', type=float, default=0.9)
    ap.add_argument('--emit', action='store_true', help='print knots for drift_math.py')
    a = ap.parse_args()

    if not os.path.exists(a.parquet):
        sys.exit(f'missing {a.parquet} — build it with scripts/build_dataset.py')
    d = pd.read_parquet(a.parquet)
    d = d[~d['tie']] if 'tie' in d else d
    d['x'] = standardized_moneyness(d)
    d = d[np.isfinite(d['x']) & np.isfinite(d['p_raw'])]
    d = d.sort_values('cycle_ts')
    y = d['up'].astype(float).values
    print(f'{len(d):,} usable rows / {d["cycle_ts"].nunique():,} cycles, '
          f'{d["date"].min()} .. {d["date"].max()}')

    # walk-forward split on TIME, never on row order within a day
    days = np.sort(d['date'].unique())
    cut = days[int(len(days) * a.train_frac)]
    tr, te = d['date'].values < cut, d['date'].values >= cut
    print(f'train {tr.sum():,} rows (< {cut})   test {te.sum():,} rows (>= {cut})')

    knots = np.linspace(-a.clip, a.clip, a.bins)
    mid, tbl = fit_table(d['x'].values[tr], y[tr], knots)

    p_emp = np.interp(np.clip(d['x'].values, -a.clip, a.clip), mid, tbl)
    p_raw = d['p_raw'].values
    p_cal = np.array([calibrate_prob(v, a.calib_slope) for v in p_raw])   # scalar-only helper

    print(f'\n{"model":<34}{"Brier (test)":>14}{"vs null":>10}')
    null = brier(np.full(te.sum(), y[te].mean()), y[te])
    for lbl, p in (('Gaussian binary_p_up (raw)', p_raw),
                   (f'  + CALIB_SLOPE={a.calib_slope} (current)', p_cal),
                   ('EMPIRICAL table (this script)', p_emp)):
        b = brier(p[te], y[te])
        print(f'{lbl:<34}{b:>14.5f}{(1 - b / null) * 100:>9.2f}%')
    print(f'{"base rate (null)":<34}{null:>14.5f}')

    # SIGNED moneyness: bucketing by |x| averages the outcome to 0.5 by construction and
    # hides every calibration error. The sign is the whole point -- x>0 means spot is above
    # the strike, so P(up) must rise with x.
    print('\ncalibration by SIGNED moneyness decile (test rows). "gauss err" > 0 = overpriced:')
    xt = d['x'].values[te]
    qs = np.quantile(xt, np.linspace(0, 1, 11))
    print(f'  {"x band":<20}{"n":>9}{"actual":>9}{"raw":>8}{"gauss+cal":>11}{"empirical":>11}'
          f'{"gauss err":>11}{"emp err":>10}')
    for i in range(10):
        m = (xt >= qs[i]) & ((xt < qs[i + 1]) if i < 9 else (xt <= qs[10]))
        if m.sum() < 50:
            continue
        yy = y[te][m].mean()
        pr, pc, pe = p_raw[te][m].mean(), p_cal[te][m].mean(), p_emp[te][m].mean()
        print(f'  [{qs[i]:>+5.2f},{qs[i+1]:>+5.2f}){m.sum():>9,}{yy:>9.3f}{pr:>8.3f}{pc:>11.3f}'
              f'{pe:>11.3f}{pr - yy:>+11.3f}{pe - yy:>+10.3f}')
    tail = np.abs(xt) >= 1.5
    print(f'\n  TAILS (|x|>=1.5, n={tail.sum():,}): actual {y[te][tail].mean():.4f}  '
          f'raw {p_raw[te][tail].mean():.4f}  emp {p_emp[te][tail].mean():.4f}')
    print(f'  Brier in the tails: raw {brier(p_raw[te][tail], y[te][tail]):.5f}  '
          f'cal {brier(p_cal[te][tail], y[te][tail]):.5f}  '
          f'EMPIRICAL {brier(p_emp[te][tail], y[te][tail]):.5f}')

    print('\nCALIB_SLOPE sweep (out-of-sample Brier). slope<1 pushes toward 0.5, >1 sharpens:')
    best = None
    for s in (0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6):
        b = brier([calibrate_prob(v, s) for v in p_raw[te]], y[te])
        best = (b, s) if best is None or b < best[0] else best
        print(f'   slope {s:.1f}   Brier {b:.5f}'
              + ('   <-- CURRENT DEFAULT' if abs(s - a.calib_slope) < 1e-9 else ''))
    print(f'   best slope {best[1]:.1f} (Brier {best[0]:.5f}) — the empirical table still beats '
          f'it at {brier(p_emp[te], y[te]):.5f}')

    if a.emit:
        print('\n# --- paste into drift_math.py ---')
        print(f'_EMP_X = {[round(v, 4) for v in mid.tolist()]}')
        print(f'_EMP_P = {[round(v, 5) for v in tbl.tolist()]}')


if __name__ == '__main__':
    main()
