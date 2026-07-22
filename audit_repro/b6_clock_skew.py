# B6: venue trades carry EXCHANGE timestamps; poly quotes carry LOCAL time.time().
# Any local clock offset shifts every measured lead by that offset - the "lead(s)"
# column and the lag>=0 gate are only as good as the PC clock.
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drift_math as dm

rng = np.random.default_rng(9)
TRUE_LAG = 3
base = rng.normal(0, 1e-3, 400)
poly_r = 0.6 * np.roll(base, TRUE_LAG); poly_r[:TRUE_LAG] = 0
poly_r += rng.normal(0, 1e-4, 400)

for clock_off in (0, 1, 2):
    # local clock fast by clock_off s -> poly rows stamped LATER relative to exchange ts,
    # equivalent to shifting the poly series by +clock_off on the shared grid
    pshift = np.roll(poly_r, clock_off); pshift[:clock_off] = 0
    lag, c = dm.xcorr_best(base[-90:], pshift[-90:], 15)
    print(f"local clock offset {clock_off:+d}s: measured lead = {lag}s (true {TRUE_LAG}s), corr {c:.2f}")
print("measured lead = true lead + clock offset: a 2s NTP error doubles the believed edge window")
lag2, _ = dm.xcorr_best(base[-90:], np.roll(poly_r, 2)[-90:], 15)
assert lag2 != TRUE_LAG
print("DEMONSTRATED B6")
