# A1: residual z-score — baseline contamination + fat-tail miscalibration.
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm

rng = np.random.default_rng(0)

# 1) contamination: a dislocation ramping over the last 8s sits inside its own baseline
res = rng.normal(0, 1.0, 90)
clean_mu, clean_sd = res[:-8].mean(), res[:-8].std()
res[-8:] += np.linspace(0, 5.0, 8)              # true 5-sigma event
dev, z = dm.drift_z(res)
z_true = (res[-1] - clean_mu) / clean_sd
print(f"drift_z reports z={z:.2f}; uncontaminated z={z_true:.2f} "
      f"-> self-suppression {100 * (1 - z / z_true):.0f}%")

# 2) fat tails (informational): |z|>=2 rate, t(3) vs gaussian. Measured result: the
# same-window std ROUGHLY re-calibrates a single-point z under fat tails, so the
# demonstrated defect in this cell is the self-suppression above, not tail inflation.
def fp_rate(draw, trials=4000):
    hits = 0
    for _ in range(trials):
        x = draw(90)
        d, zz = dm.drift_z(x)
        if zz is not None and abs(zz) >= 2.0: hits += 1
    return hits / trials

fp_g = fp_rate(lambda n: rng.normal(0, 1, n))
fp_t = fp_rate(lambda n: rng.standard_t(3, n))
print(f"informational: |z|>=2 rate gaussian={100*fp_g:.1f}% vs t(3)={100*fp_t:.1f}% "
      f"(self-normalization largely absorbs tail heaviness for the LAST point)")

assert z < 0.8 * z_true, "expected self-suppression"
print("DEMONSTRATED A1")
