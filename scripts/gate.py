# scripts/gate.py — the executable gate. Runs all P0 checks and prints exactly one line:
#   RECEIPT verified|failed checks=X/Y replay_sigs=N hit=H% netEdge=Ec
# Exit 0 iff verified. On failure, the failing-check tail goes to stderr (<=2K chars).
import glob, json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
os.environ.setdefault('MPLBACKEND', 'Agg')

checks, fail_tail = {}, ''

# 1) modules import
r = subprocess.run([sys.executable, '-c', 'import drift_math, poly_drift, recorder, replay'],
                   capture_output=True, text=True)
checks['imports'] = r.returncode == 0
if not checks['imports']: fail_tail += r.stderr[-600:]

# 2) unit tests
r = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'tests'], capture_output=True, text=True)
checks['pytest'] = r.returncode == 0
if not checks['pytest']: fail_tail += r.stdout[-1000:]

# 3) replay determinism on the newest fixture (fallback: newest data/ recording)
fixtures = sorted(glob.glob(os.path.join('fixtures', 'session_*.jsonl.gz')))
rec_pool = fixtures or sorted(glob.glob(os.path.join('data', 'session_*.jsonl.gz')))
metrics = {}
if rec_pool:
    fx = rec_pool[-1]
    outs = []
    ok = True
    for _ in range(2):
        r = subprocess.run([sys.executable, 'replay.py', fx, '--quiet'], capture_output=True, text=True)
        if r.returncode != 0:
            ok = False; fail_tail += r.stderr[-600:]; break
        outs.append(r.stdout)
    if ok and len(outs) == 2:
        ok = outs[0] == outs[1]
        if not ok: fail_tail += 'replay outputs differ between two runs of ' + fx
        try: metrics = json.loads(outs[0].strip().splitlines()[-1])
        except Exception as e: ok = False; fail_tail += f'metrics parse: {e}'
    checks['replay_determinism'] = ok
else:
    checks['replay_determinism'] = False
    fail_tail += 'no recordings found in fixtures/ or data/'

# 4) fixtures recorded (>=2 sessions) and baseline frozen
checks['fixtures_baseline'] = (len(fixtures) >= 2
                               and os.path.exists(os.path.join('fixtures', 'baseline.json')))
if not checks['fixtures_baseline']:
    fail_tail += f' fixtures={len(fixtures)} baseline={os.path.exists(os.path.join("fixtures", "baseline.json"))}'

# 5) strategy reproduction: under the FIXED loosened profile both strategies must actually
# fire and the full signal->scoring path must be deterministic. This validates present
# strategy behaviour/economics (strict-gate replay can legitimately fire nothing).
repro = {}
if rec_pool:
    fx = rec_pool[-1]
    outs = []
    ok = True
    for _ in range(2):
        r = subprocess.run([sys.executable, 'replay.py', fx, '--quiet', '--loosen'],
                           capture_output=True, text=True)
        if r.returncode != 0:
            ok = False; fail_tail += r.stderr[-600:]; break
        outs.append(r.stdout)
    if ok and len(outs) == 2:
        if outs[0] != outs[1]:
            ok = False; fail_tail += 'loosened replay non-deterministic\n'
        else:
            try: repro = json.loads(outs[0].strip().splitlines()[-1])
            except Exception as e: ok = False; fail_tail += f'repro metrics parse: {e}'
    if ok and repro.get('sigs', 0) <= 0:
        ok = False; fail_tail += 'loosened replay produced 0 signals (strategy path not exercised)\n'
    # Signals alone do not prove the SCORING path ran. A change that quietly stopped every
    # signal from ever being evaluated slipped through here once with sigs>0 and hit_pct=None.
    if ok and not repro.get('evaluated', 0):
        ok = False; fail_tail += 'loosened replay evaluated 0 signals (scoring path not exercised)\n'
    checks['strategy_repro'] = ok
else:
    checks['strategy_repro'] = False

n_ok, n = sum(checks.values()), len(checks)
status = 'verified' if n_ok == n else 'failed'
print(f"RECEIPT {status} checks={n_ok}/{n} repro_sigs={repro.get('sigs', 0)} "
      f"repro_hit={('%.0f%%' % repro['hit_pct']) if repro.get('hit_pct') is not None else 'NA'}")
if status == 'failed' and fail_tail:
    sys.stderr.write(fail_tail[-2000:] + '\n')
sys.exit(0 if status == 'verified' else 1)
