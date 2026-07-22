# B7: no time-to-expiry conditioning anywhere. The same share price means a different
# trade at different tau; the cheap-entry gate interacts perversely with expiry.
from math import erf, log, sqrt, exp

SIGMA = 4e-4
def implied_move_bp(price, tau):
    """|ln(S/K)| the underlying must travel for a share bought at `price` to win."""
    # price ~ P(win) = Phi(d) -> d = Phi^-1(price); required adverse move ~ d * sigma * sqrt(tau)
    lo, hi = -6.0, 6.0
    for _ in range(60):                        # invert Phi by bisection (numpy-free)
        mid = (lo + hi) / 2
        if 0.5 * (1 + erf(mid / sqrt(2))) < price: lo = mid
        else: hi = mid
    return abs(lo) * SIGMA * sqrt(tau) * 1e4

for tau in (240, 60, 15):
    print(f"share at 0.30 with tau={tau:>3}s: needs {implied_move_bp(0.30, tau):.1f}bp move to win")
print("-> identical entry price, ~4x different required move; no gate distinguishes them")
print("tau-blind decisions in poly_drift.py: entry (any tau, incl. seconds before expiry),")
print("DRIFT_Z_ALERT constant in tau, EVAL_H_S fixed, beta static while true gamma explodes,")
print("ENTRY_MAX_PX=0.45: near expiry the LOSING side is always 'cheap' - the gate inverts")
r = implied_move_bp(0.30, 240) / implied_move_bp(0.30, 15)
assert r > 2.5
print("DEMONSTRATED B7")
