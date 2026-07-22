# C2: signal markers — searchsorted pins the marker one grid-second late (insertion
# index), the y re-normalizes every frame, and signals older than the window vanish.
import numpy as np

now = 1000.7
grid = np.arange(now - 300, now, 1.0)
t = grid - grid[-1]
sig_ts = 950.2                                    # signal fired at 950.2
xs = np.array([sig_ts - grid[-1]])
ii = np.clip(np.searchsorted(t, xs), 0, len(t) - 1)   # replicate update()'s marker index
true_idx = int(np.floor(sig_ts - grid[0]))
print(f"marker drawn at grid index {ii[0]} vs signal's actual second {true_idx} "
      f"-> plotted {ii[0] - true_idx} column(s) late")

old_sig = now - 400                               # 100s older than the window
visible = old_sig >= grid[0]
print(f"signal from {400:.0f}s ago: visible on chart = {visible} "
      f"(record silently leaves the screen; no cumulative marker trail)")
assert ii[0] != true_idx and not visible
print("DEMONSTRATED C2")
