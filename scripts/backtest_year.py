# scripts/backtest_year.py — 1-year model backtest on Binance 1m klines.
# Reconstructs every 5-minute up/down cycle, prices P(up) with the binary model
# (walk-forward recalibrated, no lookahead), applies a confidence gate, and
# scores $1-per-trade outcomes per day under two entry assumptions:
#   ev_stale: enter at the model price one checkpoint (60s) earlier — the
#             "market lags" premise; OPTIMISTIC bound (real poly lag is seconds).
#   ev_fair:  enter at current model price + 1c spread — market already caught
#             up; PESSIMISTIC bound. Truth lies between.
# Usage: python scripts/backtest_year.py [--days 365] [--symbol BTCUSDT] [--conf 0.60]
import argparse, json, math, os, time, urllib.request
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
os.makedirs(DATA, exist_ok=True)

def http_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'poly-drift-backtest/1.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

def fetch_day_1m(symbol, day_start):
    tag = time.strftime('%Y%m%d', time.gmtime(day_start))
    path = os.path.join(DATA, f'klines_{symbol}_1m_{tag}.parquet')
    if os.path.exists(path):
        return pd.read_parquet(path)
    rows = []
    t = int(day_start * 1000); end = int((day_start + 86400) * 1000)
    while t < end:
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m"
               f"&startTime={t}&endTime={end - 1}&limit=1000")
        for attempt in range(3):
            try:
                batch = http_json(url); break
            except Exception:
                if attempt == 2: raise
                time.sleep(2.0)
        if not batch: break
        rows.extend((b[0] // 1000, float(b[1]), float(b[4])) for b in batch)
        t = batch[-1][0] + 60_000
        time.sleep(0.05)
    df = pd.DataFrame(rows, columns=['ts', 'open', 'close'])
    df.to_parquet(path)
    return df

def load_year(symbol, days):
    now = time.time(); today0 = now - (now % 86400)
    frames = []
    for i in range(days, 0, -1):
        frames.append(fetch_day_1m(symbol, today0 - 86400 * i))
        if i % 30 == 0: print(f"  ...{i} days remaining fetched/cached")
    df = pd.concat(frames, ignore_index=True).drop_duplicates('ts').sort_values('ts')
    full = pd.DataFrame({'ts': np.arange(df.ts.iloc[0], df.ts.iloc[-1] + 60, 60)})
    df = full.merge(df, on='ts', how='left')
    df['close'] = df['close'].ffill(); df['open'] = df['open'].fillna(df['close'])
    return df

_phi = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))

def logit_np(p):
    p = np.clip(p, 1e-6, 1 - 1e-6); return np.log(p / (1 - p))

def fit_recal(p_raw, y, iters=30):
    """Logistic recalibration y ~ expit(a*logit(p_raw)+b) via Newton. Returns (a, b)."""
    x = logit_np(np.asarray(p_raw, float)); y = np.asarray(y, float)
    a, b = 1.0, 0.0
    for _ in range(iters):
        z = np.clip(a * x + b, -30, 30); p = 1 / (1 + np.exp(-z))
        W = np.maximum(p * (1 - p), 1e-9)
        ga, gb = np.sum((p - y) * x), np.sum(p - y)
        Haa, Hab, Hbb = np.sum(W * x * x), np.sum(W * x), np.sum(W)
        det = Haa * Hbb - Hab * Hab
        if det <= 1e-12: break
        da = (Hbb * ga - Hab * gb) / det
        db = (Haa * gb - Hab * ga) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-9: break
    if not (0.1 < a < 5.0): a, b = 1.0, 0.0   # degenerate fit: fall back to raw
    return a, b

def run(symbol, days, conf, hl_min=15):
    print(f"loading {days}d of {symbol} 1m klines...")
    df = load_year(symbol, days)
    ts = df.ts.to_numpy(); close = df.close.to_numpy(); openp = df.open.to_numpy()
    lr = np.diff(np.log(close), prepend=np.log(close[0]))
    sig = np.sqrt(pd.Series(lr ** 2).ewm(halflife=hl_min).mean().to_numpy())  # per-minute sigma

    t0 = int(math.ceil(ts[0] / 300) * 300)
    starts = np.arange(t0, ts[-1] - 300, 300)
    i0 = ((starts - ts[0]) // 60).astype(int)
    K = openp[i0]; endp = close[i0 + 4]   # close of the cycle's last 1m bar ~ price at T+300
    up = endp > K; tie = endp == K
    day = ((starts - starts[0]) // 86400).astype(int)
    date = np.array([time.strftime('%Y-%m-%d', time.gmtime(s)) for s in starts])

    # raw model p at checkpoints m=1..4 (tau = 5-m minutes)
    p_raw = {}
    for m in (1, 2, 3, 4):
        S = close[i0 + m]; s_ = np.maximum(sig[i0 + m], 1e-8)
        d = np.log(S / K) / (s_ * np.sqrt(5 - m))
        p_raw[m] = np.clip(_phi(d), 1e-6, 1 - 1e-6)

    # walk-forward recalibration: block = 30 days, fit on ALL prior days, per checkpoint
    p_cal = {m: p_raw[m].copy() for m in (1, 2, 3, 4)}
    blocks = int(np.ceil((day.max() + 1) / 30))
    for b in range(blocks):
        lo, hi = b * 30, (b + 1) * 30
        mask_fit = (day < lo) & ~tie
        mask_app = (day >= lo) & (day < hi)
        for m in (1, 2, 3, 4):
            if mask_fit.sum() >= 500:
                a_, b_ = fit_recal(p_raw[m][mask_fit], up[mask_fit].astype(float))
            else:
                a_, b_ = 1.0, 0.0
            z = np.clip(a_ * logit_np(p_raw[m][mask_app]) + b_, -30, 30)
            p_cal[m][mask_app] = 1 / (1 + np.exp(-z))

    # one trade per cycle: first checkpoint whose calibrated side-prob >= conf
    n = len(starts)
    trade_m = np.zeros(n, int)           # 0 = no trade
    for m in (1, 2, 3, 4):
        side_p = np.maximum(p_cal[m], 1 - p_cal[m])
        hit = (trade_m == 0) & (side_p >= conf) & ~tie
        trade_m[hit] = m
    traded = trade_m > 0
    pm = np.array([p_cal[m_][i] if m_ else 0.5 for i, m_ in enumerate(trade_m)])
    side_up = pm >= 0.5
    win = np.where(side_up, up, ~up)
    conf_taken = np.maximum(pm, 1 - pm)

    # entries
    prev_p = np.array([p_cal[m_ - 1][i] if m_ >= 2 else 0.5 for i, m_ in enumerate(trade_m)])
    entry_stale = np.clip(np.where(side_up, prev_p, 1 - prev_p), 0.02, 0.98)
    entry_fair = np.clip(conf_taken + 0.01, 0.02, 0.98)
    pnl_stale = np.where(win, (1 - entry_stale) / entry_stale, -1.0)
    pnl_fair = np.where(win, (1 - entry_fair) / entry_fair, -1.0)

    # accuracy without gate, checkpoint 2
    acc_all = ((p_cal[2] >= 0.5) == up) & ~tie

    d = pd.DataFrame({
        'date': date, 'tie': tie, 'traded': traded, 'win': win & traded,
        'conf': np.where(traded, conf_taken, np.nan),
        'pnl_stale': np.where(traded, pnl_stale, np.nan),
        'pnl_fair': np.where(traded, pnl_fair, np.nan),
        'acc': acc_all,
    })
    g = d.groupby('date')
    daily = pd.DataFrame({
        'cycles': g.size(),
        'trades': g['traded'].sum().astype(int),
        'wins': g['win'].sum().astype(int),
        'win_rate_pct': (100 * g['win'].sum() / g['traded'].sum().clip(lower=1)).round(1),
        'accuracy_pct': (100 * g['acc'].mean()).round(1),
        'avg_conf': g['conf'].mean().round(3),
        'ev_stale_usd': g['pnl_stale'].mean().round(4),
        'ev_fair_usd': g['pnl_fair'].mean().round(4),
    }).reset_index()
    out = os.path.join(DATA, f'backtest_daily_{symbol}_{days}d.csv')
    daily.to_csv(out, index=False)

    # monthly rollup + totals
    dd = d[d.traded]
    print(f"\n=== {symbol} {days}d | conf gate {conf} | walk-forward recal (30d blocks, first block raw) ===")
    print(f"cycles={n}  ties={int(tie.sum())}  trades={int(traded.sum())} "
          f"({100 * traded.sum() / n:.1f}% of cycles)")
    print(f"win rate={100 * dd.win.mean():.2f}%  avg conf={dd.conf.mean():.3f}  "
          f"accuracy(all cycles, t+120s)={100 * d.acc.mean():.2f}%")
    print(f"EV per $1 trade: stale-entry={dd.pnl_stale.mean():+.4f}  fair-entry={dd.pnl_fair.mean():+.4f}")
    print(f"total P&L at $1/trade: stale={dd.pnl_stale.sum():+.0f}  fair={dd.pnl_fair.sum():+.0f}")
    d['month'] = d.date.str[:7]
    gm = d[d.traded].groupby('month')
    print(f"\n{'month':<9}{'trades':>7}{'win%':>7}{'conf':>7}{'EVstale$':>9}{'EVfair$':>9}")
    for mo, r in gm.agg(t=('traded', 'size'), w=('win', 'mean'), c=('conf', 'mean'),
                        s=('pnl_stale', 'mean'), f=('pnl_fair', 'mean')).iterrows():
        print(f"{mo:<9}{r.t:>7.0f}{100 * r.w:>7.1f}{r.c:>7.3f}{r.s:>+9.4f}{r.f:>+9.4f}")
    print(f"\ndaily table -> {out}")
    return daily

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=365)
    ap.add_argument('--symbol', default='BTCUSDT')
    ap.add_argument('--conf', type=float, default=0.60)
    a = ap.parse_args()
    run(a.symbol, a.days, a.conf)
