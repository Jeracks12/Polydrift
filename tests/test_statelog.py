# tests/test_statelog.py — the per-tick research telemetry must capture enough to reconstruct
# a dislocation after it closed, and must name WHICH gate declined the trade.
#
# Motivating episode (2026-07-21 15:03, BTC 5m): poly UP printed 0.785 -> 0.455 while BTC spot
# moved 4.6bp, then recovered to 0.855 within ~45s. The text logs could not tell us how much
# size rested at the 0.55 ask, nor which gate blocked the entry. This file guards against that
# blindness returning.
import csv, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('MPLBACKEND', 'Agg')
import logbook
import poly_drift as pdft


def _fresh_logs(tmp_path):
    logbook._state.update(dir=None, day=None, files={}, base=str(tmp_path), echo=False, tag='t')
    logbook._state_csv.update(f=None, path=None, day=None)
    logbook.init_logs(base=str(tmp_path), echo=False, tag='t')


def _read_csv(tmp_path):
    import datetime
    p = os.path.join(str(tmp_path), datetime.date.today().isoformat(), 't', 'market_state.csv')
    with open(p) as f:
        return list(csv.DictReader(f))


def _seed_book(bids, asks):
    pdft.POLY_BOOK.bids = dict(bids)
    pdft.POLY_BOOK.asks = dict(asks)


def test_state_row_captures_book_depth_and_gate(tmp_path, monkeypatch):
    _fresh_logs(tmp_path)
    monkeypatch.setitem(pdft.CFG, 'STATE_LOG', 1)
    monkeypatch.setitem(pdft.ROLL, 'slug', 'btc-updown-5m-x')
    monkeypatch.setitem(pdft.ROLL, 'strike', 65940.0)
    monkeypatch.setitem(pdft.ROLL, 'end', 1000.0)
    _seed_book({0.54: 800.0}, {0.55: 1200.0})

    an = pdft.Analytics()
    an.snap = {'px': {'spot': [65920.90], 'perp': [65921.5], 'poly': [0.455]}}
    an.last_gate = 'price_cap'
    an.fair = dict(fair=0.72, dev=-0.4, scenario='fade', ref='spot', model_p=0.51,
                   st=dict(beta=900.0, ref_move=0.1, poly_move=0.9))
    monkeypatch.setattr(an, '_model_p_win', lambda side, now: 0.51)
    monkeypatch.setattr(an, '_paid_effective', lambda side, bb, ba: ba if side == 'BUY' else 1 - bb)
    an._write_state(now=960.0, stats={}, drift={})

    r = _read_csv(tmp_path)[-1]
    assert r['gate'] == 'price_cap'                 # the blocker is named, not inferred
    assert float(r['poly_ask']) == 0.55 and float(r['poly_ask_sz']) == 1200.0
    assert float(r['poly_bid']) == 0.54 and float(r['poly_bid_sz']) == 800.0
    assert float(r['tau_s']) == 40.0
    assert abs(float(r['ref_bp_vs_strike']) - -2.897) < 0.01   # spot vs strike, basis points
    assert abs(float(r['edge_up']) - (0.72 - 0.55)) < 1e-6
    assert abs(float(r['edge_dn']) - (0.28 - 0.46)) < 1e-6
    # the drift columns are what makes a dislocation reconstructable later
    assert float(r['fair']) == 0.72 and r['scenario'] == 'fade'
    assert abs(float(r['gap_c']) - (0.72 - 0.545) * 100) < 1e-6
    assert abs(float(r['overreact']) - 9.0) < 1e-6      # poly moved 9x what the exchange did


def test_state_row_written_even_when_no_ref_is_live(tmp_path, monkeypatch):
    """A blackout is itself a finding: rows must not silently stop when a feed dies."""
    _fresh_logs(tmp_path)
    monkeypatch.setitem(pdft.CFG, 'STATE_LOG', 1)
    _seed_book({}, {})
    an = pdft.Analytics()
    an.snap = {'px': {}}
    an.last_gate = 'no_ref'
    an._write_state(now=time.time(), stats={}, drift={})
    assert _read_csv(tmp_path)[-1]['gate'] == 'no_ref'


def test_state_log_can_be_switched_off(tmp_path, monkeypatch):
    _fresh_logs(tmp_path)
    monkeypatch.setitem(pdft.CFG, 'STATE_LOG', 0)
    an = pdft.Analytics()
    an.snap = {'px': {}}
    an._write_state(now=time.time(), stats={}, drift={})
    import datetime
    p = os.path.join(str(tmp_path), datetime.date.today().isoformat(), 't', 'market_state.csv')
    assert not os.path.exists(p)


def test_every_signal_exit_path_sets_a_gate(monkeypatch):
    """_signal must never return without recording why — an unlabeled tick is an invisible
    lost trade, which is exactly how the 15:03 dislocation went unexplained."""
    import inspect, re
    src = inspect.getsource(pdft.Analytics._signal)
    body = src.split('\n')
    unlabeled = []
    for i, ln in enumerate(body):
        if not re.search(r'(^|[;:]\s*)return\s*$', ln):
            continue
        # a skip is often `self.last_gate = ...` then a multi-line L_sig(...); return,
        # so scan back over the whole statement rather than a single line
        if any('last_gate' in b for b in body[max(0, i - 4):i + 1]):
            continue
        unlabeled.append(ln.strip())
    assert not unlabeled, f'unlabeled early return(s) in _signal: {unlabeled}'


def test_schema_change_retires_the_old_file_instead_of_corrupting_it(tmp_path, monkeypatch):
    """Observed live: 2596 of 4528 rows were written with 32 fields under a 29-field header,
    so `gate` parsed as a number and every column after edge_dn was shifted."""
    import datetime
    _fresh_logs(tmp_path)
    monkeypatch.setitem(pdft.CFG, 'STATE_LOG', 1)
    d = os.path.join(str(tmp_path), datetime.date.today().isoformat(), 't')
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, 'market_state.csv')
    with open(p, 'w') as f:                      # a file from an older build
        f.write('ts,slug,gate\n1,old,edge\n')
    logbook._state_csv.update(f=None, path=None, day=None)

    an = pdft.Analytics()
    an.snap = {'px': {}}
    an.last_gate = 'no_ref'
    an._write_state(now=time.time(), stats={}, drift={})

    with open(p) as f:
        assert f.readline().strip() == ','.join(logbook.STATE_COLS)
    kept = [n for n in os.listdir(d) if n.startswith('market_state_') and n.endswith('.csv')]
    assert kept, 'old rows must be preserved, not overwritten'
    assert 'old' in open(os.path.join(d, kept[0])).read()
