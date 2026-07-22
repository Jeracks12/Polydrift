# C3: the drift-z panel appends one point per ANIMATION FRAME, x-axis pretends 1s
# spacing — frame drops compress time; intermittent feeds leave different-length curves.
from collections import deque

frames = [0, 1, 2, 3, 8, 9, 10, 11, 12, 13, 14]   # GUI stalled 4s between frames 3 and 8
hist_spot = deque(maxlen=300)
hist_perp = deque(maxlen=300)
for f in frames:
    hist_spot.append(f)                            # spot present every frame
    if f not in (9, 10):                           # perp dropped from stats for 2 frames
        hist_perp.append(f)
elapsed = frames[-1] - frames[0]
print(f"real elapsed: {elapsed}s; spot curve length: {len(hist_spot)} points "
      f"-> x-axis (-len+1..0) claims {len(hist_spot) - 1}s: time compressed by "
      f"{elapsed - (len(hist_spot) - 1)}s")
print(f"perp curve length {len(hist_perp)} vs spot {len(hist_spot)}: "
      f"curves end-aligned at x=0 but represent different time spans -> misaligned overlay")
assert len(hist_spot) - 1 != elapsed and len(hist_perp) != len(hist_spot)
print("DEMONSTRATED C3")
