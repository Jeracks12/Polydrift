# D1: outcome methodology — mark-to-mid at +30s books no exit spread or fee, and the
# outcomes log lacks the book fields (bid/ask at eval) needed to compute honest exits.
import csv, os, sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = os.path.join(root, 'signals_outcomes.csv')
rows = list(csv.DictReader(open(path)))
hdr = rows[0].keys() if rows else []
pnls = [float(r['pnl_c']) for r in rows]
n = len(pnls)
avg = sum(pnls) / n if n else 0.0
print(f"logged outcomes: n={n}, headline avg = {avg:+.1f}c/trade (mark-to-MID)")
for pen in (1.0, 2.0):
    adj = [p - pen for p in pnls]
    wins = sum(1 for p in adj if p > 0)
    print(f"  exit-at-touch proxy (-{pen:.0f}c spread/fee): avg {sum(adj)/n:+.1f}c, "
          f"hit {100*wins/n:.0f}%")
missing = [c for c in ('bb_eval', 'ba_eval', 'resolution') if c not in hdr]
print(f"columns absent from the log: {missing} -> true exit-at-touch and "
      f"resolution marking CANNOT be reconstructed; headline is an upper bound")
assert n > 0 and missing
print("DEMONSTRATED D1")
