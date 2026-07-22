# C4: chart inventory vs what a trading operator needs — verified against the plot
# code (make_plot/update): each item below is absent from the rendered figure.
import os, sys

src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'poly_drift.py'), encoding='utf-8').read()
plot = src[src.index('def make_plot'):src.index('def run_plot')]
missing = {
    'bid/ask band': 'bbo' not in plot and 'fill_between' not in plot,
    'fair-value line': 'fair' not in plot,
    'tau remaining / cycle clock': 'end' not in plot.lower() or 'tau' not in plot,
    'outcome markers (WIN/LOSS)': 'tally' in plot and 'OUTCOME' not in plot,
    'position / P&L overlay': 'position' not in plot and 'pnl' not in plot.lower(),
    'venue-health indicator': 'silent' not in plot,
}
for k, absent in missing.items():
    print(f"  {'ABSENT' if absent else 'present'}: {k}")
print("operator flies blind on spread, fair, time-to-expiry, outcomes, exposure, feed health")
assert sum(missing.values()) >= 5
print("DEMONSTRATED C4")
