# logbook.py — split, Notepad-friendly logging. One plain-text file per category per day:
#   logs/YYYY-MM-DD/{errors,warnings,trades,signals,data,session}.log
# Every entry also prints to the console, so nothing about the live UX changes; the files
# are a persistent, categorized copy you can open in Notepad and read cold.
import os, sys, time, datetime

CATEGORIES = ('errors', 'warnings', 'trades', 'signals', 'data', 'session')

_state = {'dir': None, 'day': None, 'files': {}, 'base': 'logs', 'echo': True}


def init_logs(base='logs', echo=True, tag=None):
    """Enable file logging under `base`. Safe to call once at startup. echo=False silences
    the console mirror (files still written).

    `tag` (e.g. the symbol/series) gets its own sub-directory. This MATTERS: two runs on
    different markets sharing one data.log interleave their price rows, and any later
    per-cycle analysis silently mixes assets (observed: a BTC cycle inheriting a 1939
    'strike' from a concurrent run, producing +330000bp nonsense)."""
    _state['base'] = base
    _state['echo'] = echo
    _state['tag'] = tag
    _roll_day()


def _roll_day():
    day = datetime.date.today().isoformat()
    if day == _state['day'] and _state['files']:
        return
    # close yesterday's handles
    for f in _state['files'].values():
        try: f.close()
        except Exception: pass
    d = os.path.join(_state['base'], day, _state.get('tag') or '')
    try:
        os.makedirs(d, exist_ok=True)
        _state['files'] = {c: open(os.path.join(d, c + '.log'), 'a', encoding='utf-8', buffering=1)
                           for c in CATEGORIES}
        _state['dir'], _state['day'] = d, day
    except Exception as e:                       # never let logging kill the bot
        sys.stderr.write(f'[logbook] cannot open log dir {d}: {e}\n')
        _state['files'] = {}


def log(category, msg, echo=None):
    """Write one line to the given category's file (and console). category not in
    CATEGORIES is coerced to 'data'. Returns msg so callers can `print(log(...))` if wanted."""
    cat = category if category in CATEGORIES else 'data'
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.') + f'{int(time.time() % 1 * 1000):03d}'
    line = f'{ts} | {msg}'
    if _state['base']:
        _roll_day()
        fh = _state['files'].get(cat)
        if fh is not None:
            try: fh.write(line + '\n')
            except Exception: pass
    if (_state['echo'] if echo is None else echo):
        print(msg)
    return msg


# convenience wrappers (category is explicit in the call site so it reads well)
def err(msg):      return log('errors', msg)
def warn(msg):     return log('warnings', msg)
def trade(msg):    return log('trades', msg)
def signal(msg):   return log('signals', msg)
def data(msg):     return log('data', msg)
def session(msg):  return log('session', msg)


# ---------------- per-second market state (CSV, for inefficiency research) ----------------
_state_csv = {'f': None, 'path': None, 'day': None}

STATE_COLS = ['ts', 'slug', 'tau_s', 'strike', 'spot', 'perp', 'ref_bp_vs_strike',
              'poly_last', 'poly_bid', 'poly_ask', 'poly_bid_sz', 'poly_ask_sz', 'spread',
              # --- the drift thesis, one row per second ---
              'fair',        # exchange-implied fair price for YES (no strike involved)
              'dev',         # residual: poly's move minus what the exchange justified (logit)
              'gap_c',       # (fair - mid) in cents: the dislocation, in tradeable units
              'scenario',    # catchup = exchange moved first | fade = poly moved first
              'overreact',   # |poly_move| / |ref_move|: >1 poly overreacted, <1 poly lagging
              'beta',
              'p_up',        # strike binary model, kept for cross-checking (may be blank)
              'edge_up', 'edge_dn', 'req_up', 'req_dn', 'roi_up', 'roi_dn',
              'walk_px_yes', 'walk_px_no',
              'z', 'corr', 'lag', 'vol_z', 'ref_move', 'poly_move',
              'tradable', 'gate', 'n_signals', 'n_positions']


def state_row(row):
    """Append one per-second snapshot of EVERYTHING (exchange, poly book, model, gates) so a
    dislocation can be reconstructed after the fact. One CSV per day per market."""
    day = datetime.date.today().isoformat()
    if _state_csv['f'] is None or _state_csv['day'] != day:
        if _state_csv['f']:
            try: _state_csv['f'].close()
            except Exception: pass
        d = os.path.join(_state['base'], day, _state.get('tag') or '')
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, 'market_state.csv')
            # SCHEMA GUARD: appending new columns under an old header silently misaligns every
            # later row (observed: 2596 of 4528 rows in one file parsed with `gate` reading a
            # number). If the existing header is not ours, retire that file and start a fresh
            # one rather than corrupt it.
            if os.path.exists(p):
                try:
                    with open(p, encoding='utf-8') as old:
                        stale = old.readline().strip() != ','.join(STATE_COLS)
                except Exception:
                    stale = True
                if stale:
                    keep = p.replace('.csv', time.strftime('_%Y%m%d_%H%M%S.csv'))
                    os.rename(p, keep)
                    sys.stderr.write(f'[logbook] state csv schema changed; kept old rows as {keep}\n')
            new = not os.path.exists(p)
            f = open(p, 'a', encoding='utf-8', buffering=1)
            if new: f.write(','.join(STATE_COLS) + '\n')
            _state_csv.update(f=f, path=p, day=day)
        except Exception as e:
            sys.stderr.write(f'[logbook] state csv: {e}\n'); return
    def fmt(v):
        if v is None: return ''
        if isinstance(v, float): return f'{v:.6g}'
        return str(v).replace(',', ';')
    try:
        _state_csv['f'].write(','.join(fmt(row.get(c)) for c in STATE_COLS) + '\n')
    except Exception:
        pass
