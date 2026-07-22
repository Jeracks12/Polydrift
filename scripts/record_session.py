# scripts/record_session.py — headless live run that records a session (no GUI).
# Usage: python scripts/record_session.py --duration 400 [--out-dir data|fixtures]
import os, sys, time, threading, asyncio, types, argparse

os.environ.setdefault('MPLBACKEND', 'Agg')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import poly_drift as pdft
from recorder import Recorder


def main(duration, series, out_dir):
    pdft.CFG.update(pdft.PRESET_5M)
    pdft.RECORDER = Recorder(out_dir)
    pdft.RECORDER.rec({'k': 'hdr', 'ts': time.time(), 'symbol': 'btcusdt', 'series': series})
    pdft.AN = pdft.Analytics(siglog=None)
    args = types.SimpleNamespace(symbol='btcusdt', series=series, token=None)
    threading.Thread(target=lambda: asyncio.run(pdft.amain(args)), daemon=True).start()
    t0 = time.time()
    while time.time() - t0 < duration:
        time.sleep(10)
        sz = os.path.getsize(pdft.RECORDER.path) if os.path.exists(pdft.RECORDER.path) else 0
        print(f"[rec] t={int(time.time() - t0)}s size={sz}B lines={pdft.RECORDER._n}")
    pdft.RECORDER.close()
    print(f"recorded {pdft.RECORDER.path} ({pdft.RECORDER._n} lines)")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--duration', type=int, default=400)
    ap.add_argument('--series', default='btc-updown-5m')
    ap.add_argument('--out-dir', default='data')
    a = ap.parse_args()
    main(a.duration, a.series, a.out_dir)
