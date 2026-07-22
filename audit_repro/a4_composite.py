# A4: composite_px — trending venue-drop level shift + equal-weight thin-venue distortion.
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm

rng = np.random.default_rng(3)
t = np.arange(120.0)
DRIFT = 1e-5                                    # 1bp/s trend (a real directional minute)
p1 = 100.0 * np.exp(DRIFT * t)
p2 = p1 * 1.001                                 # 10bp inter-venue offset
p2d = p2.copy(); p2d[72:] = np.nan              # venue dies at t=72 mid-trend
comp = dm.composite_px([p1, p2d])
r = np.diff(np.log(comp))
spike = abs(r[71])                              # composite return across the drop second
print(f"trending venue-drop: composite return at drop = {spike/1e-4:.1f}bp "
      f"vs true drift {DRIFT/1e-4:.2f}bp -> {spike/DRIFT:.0f}x artifact "
      f"(de-mean windows differ across venues under drift)")

# equal weight: one thin venue with 5x noise gets the same vote as two clean venues
p3 = p1 * np.exp(rng.normal(0, 5e-4, 120))
comp2 = dm.composite_px([p1, p1 * 1.0001, p3])
noise_out = np.std(np.diff(np.log(comp2)))
print(f"thin noisy venue (sd 5bp) in 3-venue composite: composite ret sd "
      f"{noise_out/1e-4:.1f}bp vs clean {DRIFT/1e-4:.2f}bp (no volume weighting)")

assert spike > 5 * DRIFT, "expected drop artifact >> drift"
assert noise_out > 5 * DRIFT, "expected thin-venue noise passthrough"
print("DEMONSTRATED A4")
