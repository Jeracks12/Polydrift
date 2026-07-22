# D6: logging integrity — raw float repr in CSV, no session id, no config hash:
# outcomes pooled across DIFFERENT gate configurations are presented as one sample.
import csv, os

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sig_hdr = open(os.path.join(root, 'signals.csv')).readline().strip().split(',')
out_path = os.path.join(root, 'signals_outcomes.csv')
out_lines = open(out_path).read().splitlines()
out_hdr = out_lines[0].split(',')

ugly = [c for line in out_lines[1:] for c in line.split(',')
        if len(c) > 12 and ('00000' in c or '99999' in c)]
print(f"raw float repr in outcomes log: {len(ugly)} cells like {ugly[:2]}")
missing = [c for c in ('session_id', 'config_hash', 'cfg') if c not in sig_hdr + out_hdr]
print(f"provenance columns absent from BOTH logs: {missing}")
print("this session alone changed DRIFT_Z_ALERT / ENTRY_MAX_PX / REF_DRIVEN / CORR_SIGN "
      "between recorded signals - the pooled 'record NW/NL' spans different strategies "
      "with no way to separate them from the logs")
assert missing and len(ugly) > 0
print("DEMONSTRATED D6")
