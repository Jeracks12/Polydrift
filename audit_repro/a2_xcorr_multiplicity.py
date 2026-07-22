# A2: xcorr lag search — best-of-31-lags on pure noise clears MIN_CORR far too often.
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm

rng = np.random.default_rng(1)
trials, n, max_lag, MIN_CORR = 600, 90, 15, 0.15
hits = hits_gated = 0
for _ in range(trials):
    a, b = rng.normal(0, 1, n), rng.normal(0, 1, n)   # pure noise, zero true relation
    lag, c = dm.xcorr_best(a, b, max_lag)
    if abs(c) >= MIN_CORR: hits += 1
    if c >= MIN_CORR and lag >= 0: hits_gated += 1     # after CORR_SIGN + lead gates
print(f"pure noise, n={n}, {2*max_lag+1} lags searched:")
print(f"  |corr|>=0.15 rate: {100*hits/trials:.0f}%  (MIN_CORR gate alone)")
print(f"  corr>=+0.15 AND lag>=0: {100*hits_gated/trials:.0f}%  (with sign+lead gates)")
assert hits / trials > 0.30, "expected high multiplicity false-positive rate"
print("DEMONSTRATED A2")
