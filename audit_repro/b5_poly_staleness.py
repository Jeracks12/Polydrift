# B5: refs have STALE_S=30 but poly has none — a frozen poly feed forward-fills forever.
# With the beta the system already trusts, a sharp ref move then manufactures a huge
# "drift gap" against a DEAD market (book equally frozen -> phantom bb/ba for signals).
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm

now = 1000.0
grid = np.arange(now - 90, now, 1.0)
# poly websocket died 40s ago: quotes stop, grid_series(stale=None) forward-fills forever
poly_quotes = [(t, 0.50 + 0.02 * np.sin(t / 7)) for t in np.arange(now - 90, now - 40)]
px_poly = dm.grid_series(poly_quotes, grid, stale=None)      # compute()'s exact poly call
print(f"poly dead 40s -> grid still shows a live-looking price at t=now: {px_poly[-1]:.3f}")

# ref: quiet, then a 40bp move in the last 10s (the exact moment signals matter)
ref_quotes = [(t, 100.0 * np.exp(4e-4 * max(0.0, t - (now - 10)))) for t in np.arange(now - 90, now)]
px_ref = dm.grid_series(ref_quotes, grid, stale=30)

BETA_HIST = 50.0                                             # scale the system carries live
res = (dm.hsums(dm.rets(px_poly, use_logit=True), 5)
       - BETA_HIST * dm.hsums(dm.rets(px_ref), 5))[-90:]
dev, z = dm.drift_z(res)
print(f"drift z against the dead feed: {z:+.1f} (|z|>=2 fires the alert) — "
      f"pure websocket-disconnection artifact, not a market gap")
assert abs(z) >= 2.0
print("DEMONSTRATED B5")
