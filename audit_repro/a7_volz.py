# A7: vol z-gate — dead-window hair trigger: near-zero-volume windows make any tiny
# trade a huge "volume z", passing the VOL_Z_MIN=1.5 gate that exists to demand real flow.
import numpy as np

w, H = 90, 5
vol = np.zeros(200)
vol[[20, 60, 100, 140]] = 100.0                  # four tiny $100 prints in a dead market
vol[-1] = 500.0                                  # current second: one small $500 trade
vh = np.convolve(vol, np.ones(H), 'full')[:len(vol)]   # rolling H-sum (as compute() does)
win = vh[-w:]
vz = (win[-1] - win.mean()) / win.std()
print(f"dead market: window mean=${win.mean():.0f}, one $500 print -> vol_z = {vz:.1f} "
      f"(gate needs only 1.5)")

# heavy-tail calibration: lognormal burst flow vs gaussian assumption
rng = np.random.default_rng(4)
flow = np.where(rng.random(20000) < 0.3, rng.lognormal(6, 1.6, 20000), 0.0)
fh = np.convolve(flow, np.ones(H), 'full')[:len(flow)]
zs = []
for i in range(w, len(fh)):
    s = fh[i - w:i]
    if s.std() > 0: zs.append((fh[i] - s.mean()) / s.std())
zs = np.array(zs)
print(f"burst-flow |vol_z|>=1.5 rate: {100 * (zs >= 1.5).mean():.1f}% "
      f"(gaussian intent: ~6.7%) - the gate's meaning varies with flow regime")
assert vz > 3.0, "expected hair-trigger z in dead window"
print("DEMONSTRATED A7")
