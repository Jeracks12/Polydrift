# replay.py — deterministic re-run of a recorded session through Analytics on a virtual clock.
# Emits the exact signal rows + one JSON metrics line to stdout. Never touches signals*.csv.
# Determinism contract: same recording -> byte-identical stdout (the gate asserts this).
import argparse, contextlib, gzip, io, json, os, sys

os.environ.setdefault('MPLBACKEND', 'Agg')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poly_drift as pdft

SIG_COLS = ['ts', 'side', 'exec_px', 'fair_px', 'edge_c', 'z', 'lag', 'corr', 'beta',
            'ref', 'vol_z', 'bb', 'ba', 'slug']

# A FIXED, deterministic loosened profile — for validation only. It exercises the full
# signal + scoring path (so the gate can check strategy behaviour and economics) without
# claiming production thresholds. Keep it constant so replays stay byte-reproducible.
LOOSEN_PROFILE = dict(DRIFT_Z_ALERT=1.0, VOL_Z_MIN=-9.0, MIN_CORR=0.10,
                      ENTRY_MAX_PX=1.0, SCENARIO_MODE='both', MIN_MODEL_P=0.0, ENTRY_CUTOFF_S=0,
                      POLY_STALE_S=9999, STRIKE_MAX_LAG_S=0,   # research profile: replay old recordings that predate ROLL['open']
                      MIN_EDGE=0.0, RT_GATE=0)   # path coverage only: gates are unit-tested


# Captured at import, BEFORE anything mutates CFG. Without this a --loosen replay permanently
# contaminated every later replay in the same process (gate.py runs several): MIN_EDGE stayed 0
# and POLY_STALE_S stayed 9999, so a "strict" run silently used loosened gates.
_CFG_DEFAULTS = dict(pdft.CFG)


def reset_state(loosen=False):
    """Return the module to a cold-start state. EVERY cross-cycle global must be listed here —
    a leftover ROLL['end']/['strike'] from a previous replay makes the binary model price the
    new recording against the old recording's strike, which is both wrong and non-deterministic
    (the result depends on what ran before it)."""
    pdft.CFG.clear(); pdft.CFG.update(_CFG_DEFAULTS)
    pdft.FEEDS.clear()
    pdft.POLY_BOOK.bids.clear(); pdft.POLY_BOOK.asks.clear()
    pdft.ROLL.update(token=None, token_no=None, slug=None, end=None, open=None,
                     strike=None, strike_ref=None)
    pdft.ROLLS.clear()
    pdft.EXECUTOR = None                 # never carry an executor (or its P&L) between replays
    if loosen: pdft.CFG.update(LOOSEN_PROFILE)
    for g in pdft.GROUPS.values():
        for n in g: pdft.FEEDS[n] = pdft.Feed(n)
    pdft.FEEDS['poly'] = pdft.Feed('poly')
    an = pdft.Analytics(siglog=None)
    pdft.AN = an
    return an


def replay(path, quiet=True, loosen=False):
    an = reset_state(loosen=loosen)
    sink = contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext()
    last_compute = None
    with sink:
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            for line in f:
                r = json.loads(line)
                k = r.get('k')
                if k == 'hdr':
                    if r.get('series'): pdft.CFG.update(pdft.PRESET_5M)
                    continue
                ts = r['ts']
                if k == 't':
                    if r['f'] != 'poly': pdft.FEEDS[r['f']].on_trade(ts, r['p'], r['n'])
                elif k == 'q':
                    if r['f'] != 'poly': pdft.FEEDS[r['f']].on_quote(ts, r['p'])
                elif k == 'praw':
                    m = r['ev']
                    for ev in (m if isinstance(m, list) else [m]):
                        pdft.apply_poly_event(ev, pdft.FEEDS['poly'], pdft.POLY_BOOK, ts)
                elif k == 'roll':
                    pdft.POLY_BOOK.bids.clear(); pdft.POLY_BOOK.asks.clear()
                    # restore end/token_no so tau replays faithfully; reset strike so compute()
                    # recaptures it (older recordings lack 'end' -> None). 'open' is the
                    # previous cycle's end: recordings made before it existed fall back to the
                    # live end we last held, which is exactly what the live path computes.
                    prev_end = pdft.ROLL['end']
                    pdft.ROLL.update(token=r['token'], token_no=r.get('token_no'), slug=r['slug'],
                                     end=r.get('end'), open=r.get('open', prev_end),
                                     strike=None, strike_ref=None)
                    pdft.ROLLS.append(ts)
                # virtual clock: one compute per elapsed wall second
                vt = int(ts)
                if last_compute is None: last_compute = vt
                while vt > last_compute:
                    last_compute += 1
                    an.compute(now=float(last_compute))
    return an


def emit(an):
    out = [','.join(SIG_COLS)]
    for s in an.signals:
        out.append(','.join(str(s[c]) for c in SIG_COLS))
    # tally is flat since the favorite strategy was removed
    t = an.tally
    metrics = {
        'sigs': len(an.signals),
        'evaluated': t['n'],
        'wins': t['win'],
        'hit_pct': round(100.0 * t['win'] / t['n'], 2) if t['n'] else None,
        'avg_pnl_c': round(t['pnl_c'] / t['n'], 3) if t['n'] else None,
    }
    print('\n'.join(out))
    print(json.dumps(metrics, sort_keys=True))
    return metrics


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('recording')
    ap.add_argument('--quiet', action='store_true', help='suppress live-style SIGNAL/OUTCOME prints')
    ap.add_argument('--loosen', action='store_true',
                    help='apply the fixed loosened validation profile (exercises both strategies)')
    a = ap.parse_args()
    emit(replay(a.recording, quiet=a.quiet, loosen=a.loosen))
