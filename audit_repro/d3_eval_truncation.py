# D3: eval-at-roll truncation — late-cycle signals are marked at near-RESOLUTION prices
# (pnl ~ 0/1 payouts) while early signals are marked mid-drift at +30s (partial moves):
# two different estimands pooled into one tally; mid-is-None skips are silently unlogged.
import numpy as np

rng = np.random.default_rng(11)
CYCLE, H = 300, 30
early_pnl, late_pnl, hz = [], [], []
for _ in range(4000):
    t0 = rng.uniform(0, CYCLE - 2)
    res = 1.0 if rng.random() < 0.55 else 0.0
    mid0 = 0.45 + 0.1 * rng.random()
    entry = mid0 + 0.01
    t_eval = min(t0 + H, CYCLE - 1)                 # replicate _eval_pending clamp
    w = min(t_eval / CYCLE, 1.0)
    mid_eval = (1 - w) * mid0 + w * res + rng.normal(0, 0.02)
    pnl = mid_eval - entry
    hz.append(t_eval - t0)
    (late_pnl if t_eval - t0 < H - 1e-9 else early_pnl).append(pnl)

e, l = np.array(early_pnl), np.array(late_pnl)
print(f"{100 * len(l) / (len(l) + len(e)):.0f}% of signals hit the roll clamp "
      f"(mean horizon {np.mean([h for h in hz if h < H - 1e-9]):.0f}s)")
print(f"mean |pnl|: clamped-late {100 * np.mean(np.abs(l)):.1f}c vs early {100 * np.mean(np.abs(e)):.1f}c "
      f"-> late marks are near-binary resolution payouts, early marks are partial drift")
print("one 'avg pnl/signal' number therefore averages two different experiments, weighted by"
      " when signals happen to fire; plus 'mid is None: continue' drops evals uncounted")
assert np.mean(np.abs(l)) > 1.5 * np.mean(np.abs(e))
print("DEMONSTRATED D3")
