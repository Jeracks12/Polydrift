# A6: roll-boundary zeroing covers i-1..i+2 around the ROLL timestamp — a delayed first
# quote from the new market (ws resubscribe lag > 3s) lands OUTSIDE the mask.
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm

R = 1051.0                                        # roll detected at ts R
grid = np.arange(1000.0, 1090.0)
quotes = [(ts, 0.97) for ts in np.arange(1000.0, R - 0.4)]        # old market pinned high
quotes += [(ts, 0.50) for ts in np.arange(R + 4.5, 1090.0)]       # new market, 4.5s ws lag
px = dm.grid_series(quotes, grid)
pr = dm.rets(px, use_logit=True)

i = int(R - grid[0])                              # replicate compute()'s mask indices
masked = set(range(max(i - 1, 0), min(i + 3, len(pr))))
jump_idx = int(np.nanargmax(np.abs(pr)))
print(f"mask covers indices {sorted(masked)}; boundary jump lands at index {jump_idx} "
      f"(|logit ret| = {abs(pr[jump_idx]):.2f})")
print("jump escapes the mask whenever the new market's first quote arrives >3s after roll")
assert jump_idx not in masked, "expected unmasked cross-market jump"
assert abs(pr[jump_idx]) > 1.0
print("DEMONSTRATED A6")
