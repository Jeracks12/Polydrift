# D4: construction leak — on a NULL market (mean-reverting mid, zero information),
# the trigger rule "fire when fair is beyond the touch" still books positive
# mark-to-mid P&L: part of the measured edge is mechanical reversion capture.
import numpy as np

rng = np.random.default_rng(12)
SPREAD, EDGE, H = 0.02, 0.01, 30
def run(n_paths=800):
    strat, rand = [], []
    for _ in range(n_paths):
        mid = np.empty(400); mid[0] = 0.5
        for t in range(1, 400):                          # OU around 0.5: no information
            mid[t] = mid[t - 1] + 0.15 * (0.5 - mid[t - 1]) + rng.normal(0, 0.012)
        fair = 0.5
        for t in range(50, 400 - H):
            ask = mid[t] + SPREAD / 2
            if fair > ask + EDGE:                        # replicate the BUY trigger
                strat.append(mid[t + H] - ask)
                break
        tr = rng.integers(50, 400 - H)                   # random-time control entry
        rand.append(mid[tr + H] - (mid[tr] + SPREAD / 2))
    return np.array(strat), np.array(rand)

s, r = run()
print(f"null market (no drift information): trigger-rule entries avg {100 * s.mean():+.2f}c "
      f"(n={len(s)}) vs random-time entries {100 * r.mean():+.2f}c")
print("the trigger conditions on 'mid displaced below fair' -> reversion alone books profit;"
      " measured live edge = mechanical capture + true drift closure, currently unseparated")
assert s.mean() > r.mean() + 0.005
print("DEMONSTRATED D4")
