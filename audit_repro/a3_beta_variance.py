# A3: beta on overlapping H-sums — huge variance at effective n ~ w/H; alignment itself is correct.
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm

rng = np.random.default_rng(2)
TRUE, w, H, lag = 0.6, 90, 5, 3
betas = []
for _ in range(400):
    rr = rng.normal(0, 1e-3, 300)
    pr = TRUE * np.roll(rr, lag); pr[:lag] = 0
    pr += rng.normal(0, 1.5e-3, 300)                # realistic: poly mid noise dominates at 1s
                                                    # (per-sec SNR ~0.4, cf. live beta ~3e3 scale)
    beta, res, rh, ph = dm.beta_residual(pr, rr, lag, w, H)
    betas.append(beta)
b = np.array(betas)
print(f"true beta={TRUE}: est mean={b.mean():.2f} sd={b.std():.2f} "
      f"(eff n ~ w/H = {w // H}); sign-flip rate={(b < 0).mean() * 100:.1f}%")

# alignment sanity: the BETA ESTIMATE peaks at the true lag (residual is contemporaneous
# by design - it measures "ref moved, poly hasn't yet" - so beta, not res-var, is the probe)
rr = rng.normal(0, 1e-3, 300)
pr = TRUE * np.roll(rr, lag); pr[:lag] = 0; pr += rng.normal(0, 1e-4, 300)
betas_k = {}
for k in range(0, 7):
    bk, _, _, _ = dm.beta_residual(pr, rr, k, w, H)
    betas_k[k] = bk
best = max(betas_k, key=lambda k: abs(betas_k[k]))
print("beta(k):", {k: round(v, 2) for k, v in betas_k.items()})
print(f"|beta| maximized at lag={best} (planted {lag}) -> lag-shift alignment correct")
assert b.std() > 0.15, "expected large beta sampling noise"
assert best == lag, "alignment check"
print("DEMONSTRATED A3")
