# B4: cooldown is per-SIDE only — one persistent oscillating dislocation refires
# repeatedly and can alternate BUY/SELL, multiplying correlated signals in the tally.
SIG_COOLDOWN_S = 10
last = {'BUY': -999, 'SELL': -999}
fires = []
for t in range(60):                       # one 60s dislocation episode
    # mid oscillates around fair: fair-vs-touch flips sign every 4s
    side = 'BUY' if (t // 4) % 2 == 0 else 'SELL'
    if t - last[side] >= SIG_COOLDOWN_S:  # replicate _signal cooldown logic exactly
        fires.append((t, side)); last[side] = t
buys = sum(1 for _, s in fires if s == 'BUY'); sells = len(fires) - buys
print(f"one 60s oscillating episode -> {len(fires)} signals ({buys} BUY / {sells} SELL): {fires}")
print("stats layer counts each as independent; executor caps positions but the tally does not")
assert len(fires) >= 8
print("DEMONSTRATED B4")
