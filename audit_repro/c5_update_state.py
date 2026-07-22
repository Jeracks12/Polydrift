# C5: z-histories are stored as attributes ON the update function object — they are
# never reset on market roll or feed reconnect, so the panel silently splices the
# z-series of DIFFERENT markets into one continuous-looking curve.
from collections import deque

def update(_):
    hist = getattr(update, '_z_spot', deque(maxlen=300))   # replicate update()'s pattern
    setattr(update, '_z_spot', hist)
    return hist

h = update(0); h.extend([1.0, 2.0, 3.0])                   # market A's z history
# --- market rolls: POLY_BOOK cleared, ROLL swapped, but the function object persists ---
h2 = update(0)
h2.append(9.9)                                             # market B's first z
print(f"after roll, z-history object is the same: {h is h2}; contents {list(h2)}")
print("market A's drift history flows straight into market B's curve - no reset hook exists")
assert h is h2 and list(h2)[:3] == [1.0, 2.0, 3.0]
print("DEMONSTRATED C5")
