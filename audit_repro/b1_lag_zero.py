# B1: gate chain — lag>=0 admits lag=0, but sub-second poly leadership is invisible
# on the 1s grid: a market that leads by 0.5s measures lag 0 (or ties) and passes the gate.
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm

rng = np.random.default_rng(5)
trials, admitted = 200, 0
for _ in range(trials):
    base = np.cumsum(rng.normal(0, 1, 3010))       # 10Hz latent price, 301s
    ref_1s = base[::10][:300]                      # ref sampled on the second
    poly_1s = base[5::10][:300]                    # poly HALF A SECOND AHEAD of ref
    lag, c = dm.xcorr_best(np.diff(ref_1s), np.diff(poly_1s), 15)
    if lag >= 0 and abs(c) >= 0.15:
        admitted += 1
print(f"poly truly leads by 0.5s; measured on the 1s grid: "
      f"{100 * admitted / trials:.0f}% of windows pass the lag>=0 + MIN_CORR gates")
print("the lead gate cannot see sub-second poly leadership - lag=0 is ambiguous")
assert admitted / trials > 0.25
print("DEMONSTRATED B1")
