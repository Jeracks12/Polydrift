# A5: fair value expit(logit(mid)-dev) is tau-blind — error vs binary-option reference near expiry.
import os, sys
from math import erf, log, sqrt
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm

S, K = 100.0, 100.0 * np.exp(-0.0005)           # spot sits 5bp above the strike
SIGMA = 4e-4                                     # per-sqrt-second log vol
mid, dev = 0.55, -0.10                           # a typical observed drift gap
fair_reg = dm.fair_from_mid(mid, dev)            # constant in tau

print(f"regression fair (tau-blind) = {fair_reg:.3f}")
errs = []
for tau in (240, 120, 60, 20, 5):
    d = log(S / K) / (SIGMA * sqrt(tau))
    true = 0.5 * (1 + erf(d / sqrt(2)))
    errs.append(abs(true - fair_reg))
    print(f"  tau={tau:>3}s  model fair={true:.3f}  |error|={abs(true - fair_reg)*100:.1f}c")
print("same book state, same dev -> fair should sharpen toward 0/1 as tau->0; it cannot.")
assert max(errs) > 0.10, "expected >=10c fair error near expiry"
print("DEMONSTRATED A5")
