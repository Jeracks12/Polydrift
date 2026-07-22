#!/usr/bin/env python
"""collect_signals.py — one expensive replay pass over recordings, producing a compact JSONL of
every CANDIDATE signal plus the forward poly path after it.

Thresholds are deliberately permissive here: this builds the superset so that fade/catchup
thresholds and exit rules can be swept offline (cheaply, and without re-replaying gigabytes).
Nothing in this file decides a strategy — it only records what was available to decide on.

  python scripts/collect_signals.py data/session_2026072*.jsonl.gz -o scratch/cands.jsonl
"""
import argparse, glob, json, os, sys, time

os.environ.setdefault('MPLBACKEND', 'Agg')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import replay                                  # noqa: E402
import poly_drift as pdft                      # noqa: E402

# permissive: capture candidates, filter later
COLLECT_PROFILE = dict(POLY_STALE_S=9999, STRIKE_MAX_LAG_S=0, ENTRY_CUTOFF_S=40,
                       FAIR_SOURCE='drift', SCENARIO_MODE='both',
                       DRIFT_Z_ALERT=1.0, VOL_Z_MIN=-9.0, MIN_CORR=0.10,
                       MIN_EDGE=0.005, MIN_EDGE_ROI=0.0, RT_GATE=0,
                       MIN_MODEL_P=0.0, ENTRY_MAX_PX=1.0, SIG_COOLDOWN_S=10)

HORIZONS = [5, 10, 15, 20, 30, 40, 50, 60, 80, 100, 120, 150, 180]


def collect(path):
    replay.LOOSEN_PROFILE = dict(COLLECT_PROFILE)
    an = replay.replay(path, quiet=True, loosen=True)
    out = []
    for s in an.signals:
        cost = s['exec_px'] if s['side'] == 'BUY' else 1 - s['exec_px']
        if cost <= 0:
            continue
        end = s.get('cycle_end')
        path_pts = {}
        for h in HORIZONS:
            t = s['ts'] + h
            if end is not None and t > end - 1:       # never look past the cycle close
                break
            m = an._poly_mid_at(t)
            if m is None:
                continue
            # per-share P&L in the traded direction, YES-space
            path_pts[h] = (m - s['exec_px']) if s['side'] == 'BUY' else (s['exec_px'] - m)
        rm = (s.get('beta') or 0) and None
        out.append(dict(
            src=os.path.basename(path), ts=s['ts'], slug=s['slug'], side=s['side'],
            scenario=s.get('scenario'), entry=s['exec_px'], cost=cost,
            fair=s.get('fair_px'), mid=s.get('mid_at_entry'), dev=s.get('dev'),
            edge_c=s.get('edge_c'), z=s.get('z'), corr=s.get('corr'), lag=s.get('lag'),
            vol_z=s.get('vol_z'), model_p=s.get('model_p'),
            tau=None if s.get('cycle_end') is None else s['cycle_end'] - s['ts'],
            resolved_pnl_c=s.get('pnl_c'), won=s.get('win'),
            path={str(k): v for k, v in path_pts.items()}))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('recordings', nargs='+')
    ap.add_argument('-o', '--out', default='scratch/candidates.jsonl')
    ap.add_argument('--force', action='store_true',
                    help='overwrite a non-empty output file instead of refusing')
    a = ap.parse_args()
    files = []
    for pat in a.recordings:
        files += sorted(glob.glob(pat))
    files = [f for f in files if os.path.getsize(f) > 100_000]
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    # Two collectors sharing one output file truncate it and then write at INDEPENDENT offsets,
    # so each silently overwrites parts of the other. This happened once (3 malformed lines and
    # 396 duplicated rows to clean up afterwards) and it is invisible: the surviving lines parse
    # fine, so nothing looks wrong. Refuse both cases rather than produce quiet garbage.
    if os.path.exists(a.out) and os.path.getsize(a.out) > 0 and not a.force:
        sys.exit(f'{a.out} exists and is non-empty. Use a fresh -o path (preferred - one file '
                 f'per run stays attributable) or pass --force to overwrite.')
    lock = a.out + '.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        sys.exit(f'another collector already holds {lock}. Wait for it to finish, or delete the '
                 f'lock if that process is gone.')
    n = 0
    t0 = time.time()
    try:
        with open(a.out, 'w', encoding='utf-8') as fh:
            for i, f in enumerate(files, 1):
                try:
                    rows = collect(f)
                except Exception as e:
                    sys.stderr.write(f'[{i}/{len(files)}] {f}: {e}\n')
                    continue
                for r in rows:
                    fh.write(json.dumps(r) + '\n')
                n += len(rows)
                sys.stderr.write(f'[{i}/{len(files)}] {os.path.basename(f)} '
                                 f'-> {len(rows)} cands (total {n}, {time.time()-t0:.0f}s)\n')
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
    print(f'{n} candidate signals -> {a.out}')


if __name__ == '__main__':
    main()
