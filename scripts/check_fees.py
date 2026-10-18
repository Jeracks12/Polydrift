#!/usr/bin/env python
"""check_fees.py — read the LIVE fee and order constraints off Polymarket and compare them to
what CFG assumes. Run this before trusting any backtest number with real size.

Polymarket introduced `crypto_fees_v2` specifically to neutralise latency arbitrage on the
short-duration crypto markets, and has changed the schedule before. Every threshold in
poly_drift.py is calibrated against `FEE_RATE_TAKER`; if the live rate moves and CFG does not,
the bot silently trades a strategy whose largest cost is mis-modelled.

  python scripts/check_fees.py
  python scripts/check_fees.py --series eth-updown-5m
"""
import argparse, json, os, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from drift_math import taker_fee                                            # noqa: E402

GAMMA = 'https://gamma-api.polymarket.com/markets'


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'poly-drift/1.0',
                                               'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--series', default='btc-updown-5m', help='slug prefix to inspect')
    ap.add_argument('--cfg-rate', type=float, default=0.07,
                    help='the FEE_RATE_TAKER poly_drift.py currently assumes (0.07)')
    a = ap.parse_args()

    rows = get(f'{GAMMA}?active=true&closed=false&limit=300&order=endDate&ascending=true')
    hits = [m for m in rows if (m.get('slug') or '').startswith(a.series)]
    if not hits:
        sys.exit(f'no live market matching {a.series!r} (gamma returned {len(rows)} markets). '
                 f'Live-market queries need end_date_min/active=true; a resolved market needs '
                 f'closed=true.')
    m = hits[0]
    sched = m.get('feeSchedule') or {}
    rate = sched.get('rate')
    print(f"market            {m.get('slug')}")
    print(f"feeType           {m.get('feeType')}   feesEnabled={m.get('feesEnabled')}")
    print(f"feeSchedule       {sched}")
    print(f"tick size         {m.get('orderPriceMinTickSize')}   "
          f"min order {m.get('orderMinSize')} shares   negRisk={m.get('negRisk')}")
    print(f"rewardsMaxSpread  {m.get('rewardsMaxSpread')}   rewardsMinSize={m.get('rewardsMinSize')}"
          f"   -> liquidity rewards {'AVAILABLE' if m.get('rewardsMaxSpread') else 'NOT AVAILABLE'}")

    if rate is None:
        print('\n!! no rate in feeSchedule — cannot verify. Do NOT assume fee-free.')
        return 1
    exp = sched.get('exponent', 1)
    print(f"\ntaker fee per share = {rate} * (p*(1-p))**{exp}"
          f"   [makers exempt: takerOnly={sched.get('takerOnly')}]")
    print(f"{'price':>7}{'fee c/share':>14}{'% of capital':>15}{'round trip c':>14}")
    for p in (.10, .20, .30, .40, .50, .60, .70, .80, .90):
        f1 = taker_fee(p, rate, exp)
        print(f'{p:>7.2f}{f1*100:>13.3f}c{f1/p*100:>14.1f}%{2*f1*100:>13.3f}c')

    tick = float(m.get('orderPriceMinTickSize') or 0.01)
    mn = float(m.get('orderMinSize') or 0)
    print(f"\nMAKER SIZING REALITY: a limit order needs >= {mn:.0f} shares, so the minimum "
          f"capital at risk\nis {mn:.0f} x price -- ${mn*0.50:.2f} at 0.50 and ${mn*0.90:.2f} at "
          f"0.90. A --stake below that CANNOT rest a quote.")
    print(f"The tick is {tick} and the median observed spread is 1 tick, so there is usually NO "
          f"room\nto rest INSIDE the spread: a maker joins the touch and queues behind it.")

    if abs(float(rate) - a.cfg_rate) > 1e-9:
        print(f"\n*** MISMATCH: live rate {rate} != CFG FEE_RATE_TAKER {a.cfg_rate}. "
              f"Update CFG (or pass --fee-rate {rate}) before trading. ***")
        return 2
    print(f"\nOK: live rate {rate} matches CFG FEE_RATE_TAKER {a.cfg_rate}.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
