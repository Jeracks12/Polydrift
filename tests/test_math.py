# tests/test_math.py — P0 gate: estimator recovery on planted synthetic data + no-lookahead.
import sys, os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm


def synth(n=300, lag=3, beta=0.6, seed=7):
    """ref returns rr; poly returns pr = beta * rr lagged by `lag`, plus small noise."""
    rng = np.random.default_rng(seed)
    rr = rng.normal(0, 1e-3, n)
    pr = np.zeros(n)
    pr[lag:] = beta * rr[:-lag]
    pr += rng.normal(0, 5e-5, n)
    return rr, pr


def test_xcorr_recovers_planted_lag():
    rr, pr = synth()
    lag, c = dm.xcorr_best(rr, pr, 10)
    assert lag == 3
    assert c > 0.9


def test_beta_recovery_on_h_sums():
    rr, pr = synth()
    beta, res, ref_h, poly_h = dm.beta_residual(pr, rr, lag=3, w=200, H=5)
    assert beta is not None
    assert abs(beta - 0.6) < 0.1
    dev, z = dm.drift_z(res)
    assert dev is not None and np.isfinite(z)
    # decomposition identity: current residual = poly contribution - ref contribution
    assert abs(res[-1] - (poly_h - ref_h)) < 1e-12


def test_ref_vs_poly_driven_gaps_are_distinguishable():
    n, H, w, lag = 300, 5, 200, 3
    rng = np.random.default_rng(3)
    # (a) ref-driven gap: poly tracks lagged ref normally, then ref jumps in the
    #     last `lag` seconds - too fresh for poly to have reacted -> ref_h dominates
    rr = rng.normal(0, 1e-4, n); rr[-lag:] += 1e-3
    pr = 0.6 * np.roll(rr, lag); pr[:lag] = 0              # poly = ref lagged by 3s
    beta, res, ref_h, poly_h = dm.beta_residual(pr, rr, lag=lag, w=w, H=H)
    assert ref_h > 0 and abs(ref_h) > abs(poly_h)          # exchange opened the gap
    # (b) poly-driven gap: ref quiet, poly dumps on its own -> poly_h dominates
    rr2 = rng.normal(0, 1e-4, n)
    pr2 = 0.6 * np.roll(rr2, lag); pr2[:lag] = 0
    pr2[-H:] -= 1e-3                                        # poly moves alone
    beta2, res2, ref_h2, poly_h2 = dm.beta_residual(pr2, rr2, lag=lag, w=w, H=H)
    assert abs(poly_h2) > abs(ref_h2)                       # poly moved first: do not fade


def test_no_lookahead_grid_series():
    quotes = [(float(t), 100.0 + t) for t in range(50)]
    grid = np.arange(10.0, 40.0, 1.0)
    a = dm.grid_series(quotes, grid)
    b = dm.grid_series(quotes + [(100.0, 999.0)], grid)  # future quote appended
    assert np.array_equal(a, b, equal_nan=True)


def test_no_lookahead_grid_vol():
    vols = [(float(t), 10.0) for t in range(50)]
    grid = np.arange(10.0, 40.0, 1.0)
    a = dm.grid_vol(vols, grid)
    b = dm.grid_vol(vols + [(100.0, 500.0)], grid)  # future trade appended
    assert np.array_equal(a, b)


def test_robust_z_not_self_suppressed_by_the_dislocation():
    rng = np.random.default_rng(0)
    res = rng.normal(0, 1.0, 90)
    res[-1] = 8.0                                  # a big fresh dislocation
    rz = dm.robust_z(res)
    assert rz is not None and rz > 4              # median/MAD is not inflated by the outlier
    assert dm.robust_z([0.0] * 20) is None        # zero spread -> undefined
    assert dm.robust_z([1.0, 2.0]) is None        # too few points


def test_calibrate_prob_pulls_extremes_toward_half():
    assert abs(dm.calibrate_prob(0.5, 0.9) - 0.5) < 1e-9      # 0.5 is a fixed point
    assert dm.calibrate_prob(0.97, 0.9) < 0.97               # slope<1 shrinks a high prob
    assert dm.calibrate_prob(0.03, 0.9) > 0.03               # and lifts a low one
    assert dm.calibrate_prob(0.8, 1.0) == 0.8                # slope 1 = identity


def test_binary_pricer_basics():
    # at the strike -> coin flip; far above with tight vol/time -> near-certain up
    assert abs(dm.binary_p_up(100.0, 100.0, 1e-3, 60) - 0.5) < 1e-9
    assert dm.binary_p_up(101.0, 100.0, 1e-4, 30) > 0.99   # 100bp above, tiny sigma*sqrt(tau)
    assert dm.binary_p_up(99.0, 100.0, 1e-4, 30) < 0.01    # symmetric below
    # more time / more vol pulls a given gap back toward 0.5
    near = dm.binary_p_up(101.0, 100.0, 4e-4, 240)
    assert 0.5 < near < dm.binary_p_up(101.0, 100.0, 4e-4, 30)


def test_binary_pricer_tau_feasibility():
    # a cheap underdog: 30bp below strike, 1 minute left, modest vol -> P(up) small
    p = dm.binary_p_up(100.0 * (1 - 0.0030), 100.0, 4e-4, 60)
    assert p < 0.20            # would be vetoed by MIN_MODEL_P=0.20
    # same gap but 4 minutes left -> more feasible
    assert dm.binary_p_up(100.0 * (1 - 0.0030), 100.0, 4e-4, 240) > p


def test_realized_sigma():
    rng = np.random.default_rng(1)
    px = 100.0 * np.exp(np.cumsum(rng.normal(0, 5e-4, 300)))
    sd = dm.realized_sigma_per_s(px, 200)
    assert sd is not None and 3e-4 < sd < 8e-4
    assert dm.realized_sigma_per_s([100.0, 100.0], 200) is None   # too few points


def test_fair_value_direction():
    assert abs(dm.fair_from_mid(0.4, 0.0) - 0.4) < 1e-9   # no gap -> fair == mid
    assert dm.fair_from_mid(0.4, 0.5) < 0.4               # poly overshot -> fair below mid
    assert dm.fair_from_mid(0.4, -0.5) > 0.4              # poly lagging -> fair above mid


def test_composite_no_jump_on_venue_drop():
    # flat prices with a 10bp inter-venue offset; venue 2 dies at t=60
    n = 100
    p1 = np.full(n, 100.0)
    p2 = np.full(n, 100.0 * 1.001)
    p2d = p2.copy(); p2d[60:] = np.nan
    comp = dm.composite_px([p1, p2d])
    naive = np.nanmean(np.array([p1, p2d]), axis=0)
    comp_jump = abs(np.log(comp[60]) - np.log(comp[59]))
    naive_jump = abs(np.log(naive[60]) - np.log(naive[59]))
    assert comp_jump < 1e-9            # composite: no jump at the drop
    assert naive_jump > 4e-4           # naive mean: ~half the spread (what we're avoiding)


def test_grid_series_staleness_cutoff():
    quotes = [(0.0, 100.0), (5.0, 101.0)]  # feed dies after t=5
    grid = np.arange(0.0, 60.0, 1.0)
    out = dm.grid_series(quotes, grid, stale=30)
    assert out[10] == 101.0            # fresh enough at t=10
    assert np.isnan(out[40])           # 35s stale at t=40 -> dropped
