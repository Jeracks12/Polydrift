# recorder.py — continuous session recorder: one jsonl.gz per session.
# Line kinds: hdr (session meta), t (venue trade), q (venue quote),
#             praw (raw polymarket ws message), roll (market roll).
import gzip, json, os, time


class Recorder:
    def __init__(self, out_dir='data', tag=''):
        os.makedirs(out_dir, exist_ok=True)
        name = time.strftime('session_%Y%m%d_%H%M%S') + (f'_{tag}' if tag else '') + '.jsonl.gz'
        self.path = os.path.join(out_dir, name)
        self.f = gzip.open(self.path, 'at', encoding='utf-8')
        self._n = 0

    def rec(self, obj):
        if self.f is None: return
        self.f.write(json.dumps(obj, separators=(',', ':')) + '\n')
        self._n += 1
        if self._n % 500 == 0: self.f.flush()

    def close(self):
        f, self.f = self.f, None
        if f is not None:
            try: f.flush(); f.close()
            except Exception: pass
