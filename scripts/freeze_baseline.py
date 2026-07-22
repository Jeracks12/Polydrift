# scripts/freeze_baseline.py — replay every fixture and freeze fixtures/baseline.json.
# The P1+ ratchet measures replay metrics against this file. Run once per baseline.
import glob, json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
os.environ.setdefault('MPLBACKEND', 'Agg')

fixtures = sorted(glob.glob(os.path.join('fixtures', 'session_*.jsonl.gz')))
if len(fixtures) < 2:
    sys.exit(f"need >=2 fixtures, found {len(fixtures)}")

per_file, tot = {}, dict(sigs=0, evaluated=0, wins=0, pnl_c=0.0)
for fx in fixtures:
    r = subprocess.run([sys.executable, 'replay.py', fx, '--quiet'], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"replay failed on {fx}:\n{r.stderr[-500:]}")
    m = json.loads(r.stdout.strip().splitlines()[-1])
    per_file[os.path.basename(fx)] = m
    tot['sigs'] += m['sigs']; tot['evaluated'] += m['evaluated']; tot['wins'] += m['wins']
    if m['avg_pnl_c'] is not None: tot['pnl_c'] += m['avg_pnl_c'] * m['evaluated']

baseline = {
    'frozen_ts': time.time(),
    'frozen_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    'files': per_file,
    'total': {
        'sigs': tot['sigs'], 'evaluated': tot['evaluated'], 'wins': tot['wins'],
        'hit_pct': round(100.0 * tot['wins'] / tot['evaluated'], 2) if tot['evaluated'] else None,
        'avg_pnl_c': round(tot['pnl_c'] / tot['evaluated'], 3) if tot['evaluated'] else None,
    },
}
path = os.path.join('fixtures', 'baseline.json')
with open(path, 'w') as f:
    json.dump(baseline, f, indent=2)
print('frozen', path, '->', json.dumps(baseline['total']))
