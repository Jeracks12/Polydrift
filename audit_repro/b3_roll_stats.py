# B3: roll handling — the ±3s mask hides the boundary jump, but the 90s stats windows
# still MIX two different markets' dynamics; beta is distorted for up to w seconds post-roll.
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm

rng = np.random.default_rng(7)
TRUE = 0.6
rr = rng.normal(0, 1e-3, 90)
# first 45s: OLD market resolved and pinned (poly logit returns ~ 0, no relation to ref)
pr = np.zeros(90)
# last 45s: NEW market tracks ref with beta 0.6 (lag 0 for clarity)
pr[45:] = TRUE * rr[45:] + rng.normal(0, 2e-4, 45)

beta_mixed, _, _, _ = dm.beta_residual(pr, rr, 0, 90, 5)
beta_clean, _, _, _ = dm.beta_residual(np.concatenate([pr[45:], pr[45:]]),
                                       np.concatenate([rr[45:], rr[45:]]), 0, 90, 5)
print(f"beta on mixed old+new window: {beta_mixed:.2f} vs clean new-market beta: {beta_clean:.2f} "
      f"({100 * (1 - beta_mixed / beta_clean):.0f}% attenuation)")
print("every roll: fair value runs on attenuated beta for up to XCORR_WIN_S=90s of the 300s cycle")
assert beta_mixed < 0.75 * beta_clean
print("DEMONSTRATED B3")
