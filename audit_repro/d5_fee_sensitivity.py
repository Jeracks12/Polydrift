# D5: fee/size realism — outcomes assume full fill at touch, zero fee. Break-even
# hit-rates under fee and half-depth slippage scenarios, vs the strategy's entry range.
print(f"{'entry':>6} {'fee':>5} {'slip':>5} {'break-even hit%':>16}")
for entry in (0.20, 0.30, 0.40):
    for fee in (0.00, 0.01, 0.02):
        for slip in (0.0, 0.005):                  # half the fill one level deeper
            e = entry + slip
            be = (e + fee) * 100                   # $1 payoff: win prob must exceed cost
            print(f"{entry:>6.2f} {fee:>5.2f} {slip:>5.3f} {be:>15.1f}%")
be_clean = 0.30 * 100
be_worst = (0.30 + 0.005 + 0.02) * 100
print(f"\nat the 0.30 entry band: break-even hit moves {be_clean:.1f}% -> {be_worst:.1f}% "
      f"(+{be_worst - be_clean:.1f}pts) under 2c fee + half-depth slip")
print("the fast-regime cheap-band result (win 26.7% at avg entry 0.23) survives 0 fee "
      "comfortably but the margin narrows sharply with costs; fee is asserted, not verified")
assert be_worst - be_clean > 2.0
print("DEMONSTRATED D5")
