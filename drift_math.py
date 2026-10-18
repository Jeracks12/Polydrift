# drift_math.py — pure math for poly_drift: no I/O, no globals, no clocks.
# Every function is deterministic in its inputs (P0 harness contract).

import math
import warnings
import numpy as np
import pandas as pd


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def robust_z(res):
    """Median/MAD z-score of the LAST residual vs its window. Robust to the fat-tailed
    logit returns that make a Gaussian z miscalibrated, and a big dislocation no longer
    inflates its own scale (audit A1). 1.4826 makes MAD match std for a normal. None if
    not computable."""
    r = np.asarray(res, float)
    last = r[-1] if len(r) else np.nan
    r = r[np.isfinite(r)]
    if len(r) < 10 or not np.isfinite(last):
        return None
    med = np.median(r)
    mad = np.median(np.abs(r - med))
    if mad <= 0:
        return None
    return float((last - med) / (1.4826 * mad))


def calibrate_prob(p, slope):
    """Logit-space recalibration: expit(slope * logit(p)). slope < 1 pulls the overconfident
    Gaussian tails toward 0.5 (the year backtest measured a calibration slope ~0.9)."""
    if slope == 1.0:
        return float(p)
    x = min(max(float(p), 1e-6), 1 - 1e-6)
    z = slope * math.log(x / (1 - x))
    return 1.0 / (1.0 + math.exp(-z))


def binary_p_up(S, K, sigma_per_s, tau_s):
    """P(underlying ends above strike K) for driftless GBM over tau_s seconds:
    Phi(ln(S/K) / (sigma*sqrt(tau))). sigma is per-sqrt-second. This is the same
    pricer validated on a year of cycles (Brier ~0.18, calibration slope ~0.9)."""
    denom = sigma_per_s * math.sqrt(max(tau_s, 1e-9))
    if denom <= 0 or S <= 0 or K <= 0:
        return 0.5
    return float(np.clip(norm_cdf(math.log(S / K) / denom), 1e-6, 1 - 1e-6))


def realized_sigma_per_s(px, lookback=120):
    """Per-sqrt-second log-return volatility from a ONE-SECOND price grid.

    A return is a one-second return only when BOTH endpoints are present. Compacting the NaNs
    away first (the old behaviour) books the jump across a dead-venue hole as a single
    one-second move: measured on a synthetic path, a 30s outage spanning a 1% drift reported
    sigma at ~8x its true value. Because sigma is the denominator of ln(S/K)/(sigma*sqrt(tau)),
    that collapses every P_up onto 0.5 and silently stops the strategy right after the venue
    outages this bot is explicitly built to survive.

    Only adjacent finite pairs contribute, so `lookback` is a true time window and gaps reduce
    the sample instead of corrupting it. None when too few clean pairs remain (callers treat
    that as 'model not ready', which is the correct conservative response to a broken feed)."""
    x = np.asarray(px, float)[-(lookback + 1):]
    if len(x) < 6:
        return None
    a, b = x[:-1], x[1:]
    ok = np.isfinite(a) & np.isfinite(b) & (a > 0) & (b > 0)
    if ok.sum() < 5:
        return None
    sd = float(np.std(np.log(b[ok] / a[ok])))
    return sd if sd > 0 else None


def grid_series(quotes, grid, stale=None):
    """Last-known price per grid second from (ts, px) pairs. NaN before first quote;
    with `stale`, quotes older than that many seconds go NaN (dead-venue cutoff)."""
    if not len(quotes): return np.full(len(grid), np.nan)
    a = np.asarray(quotes, float)
    idx = np.searchsorted(a[:, 0], grid, side='right') - 1
    ok = idx >= 0
    out = np.where(ok, a[np.clip(idx, 0, None), 1], np.nan)
    if stale is not None:
        age = grid - np.where(ok, a[np.clip(idx, 0, None), 0], -np.inf)
        out = np.where(age <= stale, out, np.nan)
    return out


def grid_vol(vols, grid):
    """Sum notional per grid bucket from (ts, notional) pairs."""
    if not len(vols): return np.zeros(len(grid))
    a = np.asarray(vols, float)
    bins = np.searchsorted(grid, a[:, 0], side='right') - 1
    out = np.zeros(len(grid))
    # exclude trades beyond the last 1s bucket: without the ts bound, FUTURE trades
    # collapse into the final bin (lookahead)
    ok = (bins >= 0) & (bins < len(grid)) & (a[:, 0] < grid[-1] + 1.0)
    np.add.at(out, bins[ok], a[ok, 1])
    return out


def composite_px(grids):
    """Average de-meaned log prices, add back the common level: venues joining or
    dropping mid-window shift the composite by ~0 instead of the inter-venue spread."""
    L = np.array([np.log(g) for g in grids])
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        lvl = np.nanmean(L, axis=1, keepdims=True)
        comp = np.nanmean(L - lvl, axis=0) + np.nanmean(lvl)
    return np.exp(comp)


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4); return np.log(p / (1 - p))


def expit(x): return 1.0 / (1.0 + np.exp(-x))


def rets(px, use_logit=False):
    x = logit(px) if use_logit else np.log(px)
    return np.diff(x, prepend=x[0])


def xcorr_best(ref_r, poly_r, max_lag):
    """(lag, corr) maximizing |corr|; k>0 means ref leads poly by k sec."""
    best = (0, 0.0); n = len(ref_r)
    for k in range(-max_lag, max_lag + 1):
        if k >= 0: a, b = (ref_r[:n - k] if k else ref_r), poly_r[k:]
        else:      a, b = ref_r[-k:], poly_r[:n + k]
        if len(a) < 30 or np.nanstd(a) == 0 or np.nanstd(b) == 0: continue
        c = np.corrcoef(a, b)[0, 1]
        if np.isfinite(c) and abs(c) > abs(best[1]): best = (k, c)
    return best


def hsums(r, H):
    """Rolling H-second cumulative returns (NaN head)."""
    return pd.Series(r).rolling(H).sum().to_numpy()


def beta_residual(pr, rr, lag, w, H):
    """Beta of poly's H-sum on the ref's H-sum (ref shifted by its lead), plus the
    contemporaneous residual series over the last w seconds and its current
    decomposition: res[-1] = poly_h - ref_h, where ref_h = beta * ref's H-sum now.
    The decomposition tells WHO moved: ref_h dominant = ref-driven gap (tradeable
    catch-up); poly_h dominant = poly moved on its own (informed/noise - do not fade).
    Returns (beta, res, ref_h, poly_h) or (None, None, None, None)."""
    cp, cr = hsums(pr, H), hsums(rr, H)
    k = max(lag, 0)
    a = cr[-w:len(cr) - k] if k else cr[-w:]
    b = cp[len(cp) - w + k:]
    if np.all(np.isnan(a)) or np.all(np.isnan(b)): return None, None, None, None
    va = np.nanvar(a)
    beta = (np.nanmean(a * b) - np.nanmean(a) * np.nanmean(b)) / va if va > 0 else 0.0
    res = (cp - beta * cr)[-w:]
    if np.all(np.isnan(res)): return None, None, None, None
    return beta, res, beta * cr[-1], cp[-1]


def drift_z(res):
    """Demeaned last residual (dev) and its z vs the window. (None, None) if unusable."""
    mu, sd = np.nanmean(res), np.nanstd(res)
    dev = res[-1] - mu
    if not np.isfinite(dev): return None, None
    return dev, (dev / sd if sd > 0 else 0.0)


def fair_from_mid(mid, dev):
    """Fair poly price if it closes the abnormal gap: shift mid by -dev in logit space."""
    return float(expit(logit(np.array([mid]))[0] - dev))


def taker_fee(p, rate=0.07, exponent=1):
    """Polymarket taker fee PER SHARE, in price units, under `crypto_fees_v2`.

        fee = rate * (p * (1 - p)) ** exponent

    Verified against the live market config on the 5-minute up/down markets
    (gamma-api `feeSchedule`): {'exponent': 1, 'rate': 0.07, 'takerOnly': True,
    'rebateRate': 0.2}, and against the documented worked examples -- 100 shares at
    $0.50 costs $1.75, at $0.30 or $0.70 costs $1.47.

    `p` is the price of the LEG BEING TRADED, so it is symmetric between YES and NO
    (fee(p) == fee(1-p)) and no side is structurally cheaper. MAKERS PAY NOTHING
    (`takerOnly: True`), which is the whole reason a resting quote is worth building.

    In cents the fee peaks at p=0.5 (1.75c), but as a FRACTION OF CAPITAL it is
    rate*(1-p) -- worst at the cheap end (6.3% of capital at p=0.10, 0.7% at p=0.90).
    Any gate that reasons in EV-per-dollar is therefore biased toward exactly the
    entries this fee punishes hardest."""
    p = min(max(float(p), 0.0), 1.0)
    return float(rate * (p * (1.0 - p)) ** exponent)


# --- EMPIRICAL binary calibration -------------------------------------------------------
# P(up | x) measured on 419,444 rows / 104,861 real BTC 5-minute cycles, where
#     x = ln(S/K) / (sigma*sqrt(tau))
# is exactly the argument binary_p_up() feeds to Phi(). Fitted on 2025-07-18..2026-02-21 and
# scored on 2026-02-22..2026-07-17 (walk-forward, no lookahead), isotonic so P(up) cannot
# decrease in moneyness. Rebuild with `python scripts/fit_calibration.py --emit`.
#
# WHY: the Gaussian is not overconfident here, it is UNDER-confident in the body. Measured
# out-of-sample error of Phi() reaches 8-9 POINTS (at x~-0.7 it says 0.235 where the truth is
# 0.159; at x~+0.7 it says 0.760 where the truth is 0.839) -- roughly 3x the entire MIN_EDGE
# threshold the strategy trades on. CALIB_SLOPE=0.9 compresses toward 0.5 and therefore makes
# it WORSE (Brier 0.13271 vs 0.13063 raw); the sweep is monotone up to ~1.5.
#   Brier, out of sample:  raw Phi 0.13063 | slope 0.9 0.13271 | best slope 1.5 0.12688
#                          EMPIRICAL 0.12655
_EMP_X = [-2.925, -2.775, -2.625, -2.475, -2.325, -2.175, -2.025, -1.875, -1.725, -1.575,
          -1.425, -1.275, -1.125, -0.975, -0.825, -0.675, -0.525, -0.375, -0.225, -0.075,
          0.075, 0.225, 0.375, 0.525, 0.675, 0.825, 0.975, 1.125, 1.275, 1.425,
          1.575, 1.725, 1.875, 2.025, 2.175, 2.325, 2.475, 2.625, 2.775, 2.925]
_EMP_P = [0.00041, 0.00079, 0.00079, 0.00243, 0.00528, 0.0064, 0.00769, 0.01007, 0.01008,
          0.0151, 0.02544, 0.03631, 0.04839, 0.06569, 0.10707, 0.13659, 0.18495, 0.24153,
          0.30527, 0.38764, 0.63621, 0.69034, 0.75632, 0.813, 0.86138, 0.89466, 0.9295,
          0.95151, 0.96729, 0.97392, 0.98281, 0.98952, 0.99412, 0.99412, 0.99584, 0.99629,
          0.9974, 0.99921, 0.99921, 0.99972]


def standardized_moneyness(S, K, sigma_per_s, tau_s):
    """x = ln(S/K)/(sigma*sqrt(tau)) — the sufficient statistic both pricers consume.
    None when it is not computable (dead vol, expired, bad price)."""
    if not (S and K) or S <= 0 or K <= 0:
        return None
    denom = sigma_per_s * math.sqrt(max(tau_s, 1e-9))
    if denom <= 0:
        return None
    return math.log(S / K) / denom


def empirical_p_up(S, K, sigma_per_s, tau_s):
    """P(up) from the measured conditional frequency instead of Phi(). Linear between knots,
    flat outside the fitted range (|x|>2.925 is already 0.9997/0.0004 — clamping there is the
    conservative choice, since extrapolating a fitted tail is how you manufacture edge)."""
    x = standardized_moneyness(S, K, sigma_per_s, tau_s)
    if x is None:
        return 0.5
    return float(np.clip(np.interp(x, _EMP_X, _EMP_P), 1e-6, 1 - 1e-6))
