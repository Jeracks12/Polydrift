# scripts/backtest_model.py — backtest the binary-option fair value P(up)=Phi(ln(S/K)/sigma*sqrt(tau))
# against thousands of REAL resolved 5-minute cycles, using Binance 1s klines.
#
# What this validates: the model-pricer leg (P1 step 10) — calibration, Brier, sigma choice.
# What it CANNOT validate: the seconds-scale poly-lag drift signal (needs recorded poly ticks).
#
# Usage: python scripts/backtest_model.py [--days 3] [--symbol BTCUSDT] [--coin btc]
import argparse, json, math, os, sys, time, urllib.request
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
os.makedirs(DATA, exist_ok=True)

def http_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'poly-drift-backtest/1.0'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

# ---------- 1s klines, cached per UTC day ----------
def fetch_day(symbol, day_start):
    """One UTC day of 1s klines -> DataFrame(ts, open, close). Cached to parquet."""
    tag = time.strftime('%Y%m%d', time.gmtime(day_start))
    path = os.path.join(DATA, f'klines_{symbol}_1s_{tag}.parquet')
    if os.path.exists(path):
        return pd.read_parquet(path)
    rows = []
    t = int(day_start * 1000)
    end = int((day_start + 86400) * 1000)
    while t < end:
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1s"
               f"&startTime={t}&endTime={end - 1}&limit=1000")
        batch = http_json(url)
        if not batch: break
        rows.extend((b[0] // 1000, float(b[1]), float(b[4])) for b in batch)
        t = batch[-1][0] + 1000
        time.sleep(0.05)
    df = pd.DataFrame(rows, columns=['ts', 'open', 'close'])
    df.to_parquet(path)
    print(f"  fetched {tag}: {len(df)} rows -> {os.path.basename(path)}")
    return df

def load_klines(symbol, days):
    now = time.time()
    today0 = now - (now % 86400)
    frames = [fetch_day(symbol, today0 - 86400 * (i + 1)) for i in range(days)][::-1]
    df = pd.concat(frames, ignore_index=True).drop_duplicates('ts').sort_values('ts')
    # dense 1s grid, ffill gaps
    full = pd.DataFrame({'ts': np.arange(df.ts.iloc[0], df.ts.iloc[-1] + 1)})
    df = full.merge(df, on='ts', how='left')
    df['close'] = df['close'].ffill()
    df['open'] = df['open'].fillna(df['close'])
    return df

# ---------- model ----------
_phi = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))

def model_p(S, K, sigma, tau):
    """P(end > K) for driftless GBM: Phi(ln(S/K) / (sigma*sqrt(tau)))."""
    d = np.log(S / K) / np.maximum(sigma * np.sqrt(np.maximum(tau, 1e-9)), 1e-12)
    return np.clip(_phi(d), 1e-6, 1 - 1e-6)

def run(symbol, coin, days, halflives=(60, 300, 900), offsets=(30, 60, 120, 180, 240)):
    df = load_klines(symbol, days)
    ts = df.ts.to_numpy()
    close = df.close.to_numpy()
    lr = np.diff(np.log(close), prepend=np.log(close[0]))

    # per-second EWMA sigma for each halflife (past-only)
    sigmas = {hl: np.sqrt(pd.Series(lr ** 2).ewm(halflife=hl).mean().to_numpy()) for hl in halflives}

    # cycles: [T, T+300), K = open at T, outcome = close(T+299) vs K
    t0 = int(math.ceil(ts[0] / 300) * 300)
    cycles = []
    idx0 = ts[0]
    for T in range(t0, int(ts[-1]) - 300, 300):
        i = int(T - idx0)
        K = df.open.iloc[i]
        end = close[i + 299]
        if not (np.isfinite(K) and np.isfinite(end)): continue
        cycles.append((T, i, K, end > K, end == K))
    n_tie = sum(c[4] for c in cycles)
    print(f"\ncycles reconstructed: {len(cycles)} ({days}d {symbol}), ties: {n_tie}")

    # samples at fixed offsets into each cycle
    recs = []
    for T, i, K, up, tie in cycles:
        if tie: continue
        for off in offsets:
            j = i + off
            S = close[j]
            for hl in halflives:
                p = float(model_p(S, K, sigmas[hl][j], 300 - off))
                recs.append((T, off, hl, p, int(up)))
    r = pd.DataFrame(recs, columns=['cycle', 'offset', 'hl', 'p', 'up'])

    print(f"samples: {len(r)} ({len(r) // len(halflives)} per sigma variant)")
    print("\n--- Brier score by sigma halflife (lower=better; 0.25 = know-nothing p=0.5) ---")
    for hl in halflives:
        s = r[r.hl == hl]
        brier = ((s.p - s.up) ** 2).mean()
        print(f"  hl={hl:>4}s: brier={brier:.4f}  (n={len(s)})")

    best_hl = min(halflives, key=lambda hl: ((r[r.hl == hl].p - r[r.hl == hl].up) ** 2).mean())
    s = r[r.hl == best_hl].copy()

    print(f"\n--- calibration, sigma hl={best_hl}s (predicted P(up) vs realized frequency) ---")
    s['bin'] = np.clip((s.p * 10).astype(int), 0, 9)
    cal = s.groupby('bin').agg(n=('up', 'size'), pred=('p', 'mean'), real=('up', 'mean'))
    print(f"  {'bin':<10}{'n':>7}{'predicted':>11}{'realized':>10}")
    for b, row in cal.iterrows():
        print(f"  {b/10:.1f}-{(b+1)/10:.1f}{row.n:>8.0f}{row.pred:>11.3f}{row.real:>10.3f}")
    slope = np.polyfit(s.p, s.up, 1)[0]
    print(f"  calibration slope: {slope:.3f} (1.0 = perfect)")

    print(f"\n--- Brier by time-into-cycle (hl={best_hl}s) ---")
    for off in offsets:
        o = s[s.offset == off]
        print(f"  t+{off:>3}s (tau={300-off:>3}s): brier={((o.p - o.up) ** 2).mean():.4f}")

    out = os.path.join(DATA, f'backtest_calibration_{coin}_{days}d.csv')
    s.to_csv(out, index=False)
    print(f"\nsaved samples -> {out}")

    # ---------- verify Binance-derived outcomes against real Polymarket resolutions ----------
    print("\n--- oracle basis check: Binance-derived vs actual Polymarket resolution ---")
    rng = np.random.default_rng(1)
    sample = rng.choice(len(cycles), size=min(40, len(cycles)), replace=False)
    agree = disagree = missing = 0
    for k in sample:
        T, i, K, up, tie = cycles[k]
        try:
            # closed=true is required: gamma's slug lookup silently omits resolved markets without it
            m = http_json(f"https://gamma-api.polymarket.com/markets?slug={coin}-updown-5m-{T}&closed=true")
            if not m: missing += 1; continue
            op = m[0].get('outcomePrices')
            if isinstance(op, str): op = json.loads(op)
            if not op or float(op[0]) not in (0.0, 1.0): missing += 1; continue
            poly_up = float(op[0]) == 1.0
            if poly_up == up: agree += 1
            else: disagree += 1
        except Exception:
            missing += 1
        time.sleep(0.1)
    tot = agree + disagree
    if tot:
        print(f"  checked {tot} resolved cycles: agree {agree} ({100*agree/tot:.0f}%), "
              f"disagree {disagree}, unresolved/missing {missing}")
    else:
        print(f"  no resolvable cycles found in sample (missing={missing})")

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=3)
    ap.add_argument('--symbol', default='BTCUSDT')
    ap.add_argument('--coin', default='btc')
    a = ap.parse_args()
    run(a.symbol, a.coin, a.days)
