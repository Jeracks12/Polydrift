# C1: minmax normalization renders two INDEPENDENT walks as visually co-moving,
# and hides amplitude: a 2bp ref wiggle displays as tall as a 30c poly swing.
import numpy as np

rng = np.random.default_rng(10)
cors = []
for _ in range(300):
    a = np.cumsum(rng.normal(0, 1, 300))
    b = np.cumsum(rng.normal(0, 1, 300))
    na = (a - a.min()) / (a.max() - a.min())
    nb = (b - b.min()) / (b.max() - b.min())
    cors.append(abs(np.corrcoef(na, nb)[0, 1]))
ret_cor = abs(np.corrcoef(np.diff(a), np.diff(b))[0, 1])
print(f"independent random walks, minmax-normalized levels: mean |corr| = {np.mean(cors):.2f} "
      f"(returns corr ~ {ret_cor:.2f}) -> the chart 'shows' co-movement that is not there")
print("amplitude erased: normalization maps any range to [0,1], so magnitude cannot be read")
assert np.mean(cors) > 0.4
print("DEMONSTRATED C1")
