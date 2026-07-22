# scripts/audit_gate.py — audit-loop gate (spec frozen in the AUDIT LOOP doc).
# Prints exactly: RECEIPT audit checks=X/Y findings=N high=H ; exit 0 iff all checks pass.
import hashlib, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
IDS = ([f'A{i}' for i in range(1, 8)] + [f'B{i}' for i in range(1, 8)]
       + [f'C{i}' for i in range(1, 6)] + [f'D{i}' for i in range(1, 7)])
SEV = {'high': 3, 'med': 2, 'low': 1}
LEDGER = os.path.join('docs', 'audit.md')
STATE = os.path.join('docs', '.audit_state.json')
BASE = os.path.join('docs', '.audit_baseline.json')
PROD = ['poly_drift.py', 'drift_math.py', 'execution.py', 'recorder.py', 'replay.py',
        'scripts/gate.py', 'scripts/backtest_year.py', 'scripts/build_dataset.py',
        'FINDINGS.md', 'signals.csv', 'signals_outcomes.csv']

checks, tail, rows = {}, '', {}

# 1) ledger parses: one row per id, verdict valid
try:
    text = open(LEDGER, encoding='utf-8').read()
    pat = r'^\|\s*([ABCD]\d)\s*\|\s*(\w+)\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|'
    for m in re.finditer(pat, text, re.M):
        rows[m.group(1)] = dict(verdict=m.group(2).strip(), severity=m.group(3).strip(),
                                loc=m.group(4).strip(), repro=m.group(5).strip(),
                                impact=m.group(6).strip())
except FileNotFoundError:
    pass
checks['ledger_complete'] = all(i in rows and rows[i]['verdict'] in ('PASS', 'FINDING') for i in IDS)
if not checks['ledger_complete']:
    missing = [i for i in IDS if i not in rows or rows.get(i, {}).get('verdict') not in ('PASS', 'FINDING')]
    tail += f'unresolved cells: {missing}\n'

# 2) FINDING rows carry all fields with valid severity
ok = True
for i in IDS:
    r = rows.get(i)
    if r and r['verdict'] == 'FINDING':
        if r['severity'] not in SEV or not r['loc'] or not r['repro'] or not r['impact']:
            ok = False; tail += f'{i}: incomplete finding fields\n'
checks['finding_fields'] = ok

# 3) every referenced repro runs and prints DEMONSTRATED <id>
ok = True
for i in IDS:
    r = rows.get(i)
    if r and r['verdict'] == 'FINDING' and r['repro']:
        path = r['repro']
        if not os.path.exists(path):
            ok = False; tail += f'{i}: missing {path}\n'; continue
        try:
            p = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            ok = False; tail += f'{i}: {path} timeout\n'; continue
        if p.returncode != 0 or f'DEMONSTRATED {i}' not in p.stdout:
            ok = False
            tail += f'{i}: {path} rc={p.returncode} out=...{p.stdout[-150:]} err=...{p.stderr[-200:]}\n'
checks['repros_run'] = ok

# 4) anti-gaming: production files untouched since baseline
hashes = {f: hashlib.sha256(open(f, 'rb').read()).hexdigest() for f in PROD if os.path.exists(f)}
if not os.path.exists(BASE):
    os.makedirs('docs', exist_ok=True)
    json.dump(hashes, open(BASE, 'w'))
    checks['no_prod_edits'] = True
else:
    base = json.load(open(BASE))
    bad = [f for f, h in hashes.items() if base.get(f) != h]
    checks['no_prod_edits'] = not bad
    if bad: tail += f'production files modified: {bad}\n'

# 5) monotonicity: findings never deleted or downgraded
cur = {i: rows[i]['severity'] for i in rows if rows[i]['verdict'] == 'FINDING'}
if os.path.exists(STATE):
    prev = json.load(open(STATE))
    mono = all(i in cur and SEV.get(cur[i], 0) >= SEV.get(s, 0) for i, s in prev.items())
    if not mono: tail += 'finding deleted or downgraded vs prior ledger\n'
else:
    mono = True
checks['monotone'] = mono
if mono:
    json.dump(cur, open(STATE, 'w'))

n = len(cur); h = sum(1 for s in cur.values() if s == 'high')
x, y = sum(checks.values()), len(checks)
print(f"RECEIPT audit checks={x}/{y} findings={n} high={h}")
if x != y:
    sys.stderr.write(json.dumps(checks) + '\n' + tail[-1000:] + '\n')
sys.exit(0 if x == y else 1)
