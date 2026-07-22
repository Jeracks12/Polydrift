# B2: ref selection is winner-take-all on max |corr| — on near-equal composites the
# chosen ref flaps between consecutive windows, swapping beta/lag/fair mid-decision.
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm

rng = np.random.default_rng(6)
latent = np.cumsum(rng.normal(0, 1e-3, 500))
poly = np.diff(np.concatenate([[0], latent])) + rng.normal(0, 1e-3, 500)
refA = np.diff(np.concatenate([[0], latent])) + rng.normal(0, 8e-4, 500)   # near-identical
refB = np.diff(np.concatenate([[0], latent])) + rng.normal(0, 8e-4, 500)   # information

picks = []
for start in range(0, 400, 5):                    # consecutive sliding windows
    w = slice(start, start + 90)
    _, ca = dm.xcorr_best(refA[w], poly[w], 15)
    _, cb = dm.xcorr_best(refB[w], poly[w], 15)
    picks.append('A' if abs(ca) >= abs(cb) else 'B')
flips = sum(1 for i in range(1, len(picks)) if picks[i] != picks[i - 1])
print(f"chosen ref across {len(picks)} consecutive windows: {''.join(picks)}")
print(f"ref flips {flips}/{len(picks) - 1} transitions - beta/lag/fair swap sources mid-stream")
assert flips >= 3
print("DEMONSTRATED B2")
