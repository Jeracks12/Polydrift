# D2: sample validity — n is far below the program's own >=100-signal bar, signals
# cluster within cycles (shared outcomes), and the naive binomial test overstates it.
import csv, math, os

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
rows = list(csv.DictReader(open(os.path.join(root, 'signals_outcomes.csv'))))
n = len(rows)
wins = sum(1 for r in rows if int(float(r['win'])) == 1)
slugs = {r['slug'] for r in rows}
p_naive = sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n if n else 1.0
print(f"n={n} scored signals, {wins} wins ({100*wins/max(n,1):.0f}%), "
      f"one-sided binomial vs 50%: p={p_naive:.3f}")
print(f"unique markets: {len(slugs)} -> effective independent sample ~{len(slugs)}, "
      f"not {n} (signals inside one cycle share the same resolution)")
k_eff = round(wins * len(slugs) / n) if n else 0
p_clust = (sum(math.comb(len(slugs), k) for k in range(k_eff, len(slugs) + 1))
           / 2 ** len(slugs)) if slugs else 1.0
print(f"cluster-collapsed test (~{k_eff}/{len(slugs)}): p={p_clust:.3f}")
print(f"program's own evidence bar: >=100 signals over >=3 days - not met (n={n})")
assert n < 100 and len(slugs) < n
print("DEMONSTRATED D2")
