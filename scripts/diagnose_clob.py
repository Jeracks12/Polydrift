# scripts/diagnose_clob.py — pinpoint the "invalid order version" rejection WITHOUT trading.
# Read-only by default: prints how your credentials resolve and what the CLOB says about the
# live market. Only attempts a real order if you pass --try-order (that is YOUR decision).
#
#   python scripts/diagnose_clob.py                 # safe, no orders
#   python scripts/diagnose_clob.py --try-order     # places ONE $1 order (real money)
import argparse, json, os, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from execution import load_env


def http_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'poly-drift/1.0'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def live_btc_token():
    url = ("https://gamma-api.polymarket.com/markets?active=true&closed=false"
           "&order=endDate&ascending=true&limit=100&end_date_min="
           + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    for m in http_json(url):
        if 'btc-updown-5m' in m.get('slug', ''):
            toks = m.get('clobTokenIds')
            if isinstance(toks, str): toks = json.loads(toks)
            return m['slug'], toks[0]
    return None, None


def main(try_order):
    load_env()
    key = os.environ.get('POLY_PRIVATE_KEY')
    sig_type = int(os.environ.get('POLY_SIGNATURE_TYPE') or 0)
    funder = os.environ.get('POLY_FUNDER') or None
    print(f"signature_type = {sig_type}  (0=EOA, 1=email/Magic proxy, 2=browser-wallet proxy)")
    print(f"funder         = {funder}")
    print(f"private key    = {'present' if key else 'MISSING'}")
    if not key:
        sys.exit("no POLY_PRIVATE_KEY in .env — nothing to diagnose")

    from py_clob_client_v2 import ClobClient          # v1 orders are rejected by the CLOB
    client = ClobClient(host='https://clob.polymarket.com', chain_id=137, key=key,
                        signature_type=sig_type, funder=funder or None)
    signer_addr = client.get_address()
    print(f"signer address = {signer_addr}")
    print("\n--> CRITICAL CHECK: for signature_type 1 or 2, POLY_FUNDER must be your Polymarket")
    print("    PROFILE (proxy) address, and it must NOT equal the signer address above.")
    if funder and signer_addr and funder.lower() == signer_addr.lower():
        print("    !! funder == signer. For a proxy account that is WRONG and will be rejected.")

    # NOTE: do NOT try to validate the funder by balance. Polymarket cash does not show up as
    # plain USDC held by the proxy on Polygon (verified: both addresses read $0 on-chain while
    # the UI showed cash), and the positions endpoint reports position value, not cash.
    # The funder is simply the address on your Polymarket PROFILE page:
    #   polymarket.com -> avatar -> profile URL = polymarket.com/profile/0x<PROXY>
    # The only authoritative test that the pair is right is a real order (--try-order).
    if sig_type in (1, 2) and funder:
        print(f"\n    funder set to {funder}")
        print("    (verify this matches your polymarket.com profile URL; balance cannot confirm it)")
    if sig_type == 0 and funder:
        print("    !! signature_type 0 (EOA) should normally have NO funder set.")
    if sig_type in (1, 2) and not funder:
        print("    !! proxy signature type requires POLY_FUNDER.")

    try:
        client.set_api_creds(client.create_or_derive_api_key())
        print("\nAPI creds: derived OK (level-2 auth works)")
    except Exception as e:
        print(f"\nAPI creds FAILED: {e}")
        print("-> if this fails, the key/signature-type pair is wrong; fix that first.")
        return

    slug, token = live_btc_token()
    print(f"\nlive market: {slug}\ntoken:       {token}")
    if not token:
        return
    try:
        print(f"tick_size  = {client.get_tick_size(token)}")
        print(f"neg_risk   = {client.get_neg_risk(token)}")
    except Exception as e:
        print(f"market config fetch FAILED: {e}")

    if not try_order:
        print("\n(read-only mode — no order attempted. Re-run with --try-order to place ONE $1 order.)")
        return

    print("\n!! placing ONE real $1 market order — this spends real money !!")
    from py_clob_client_v2 import MarketOrderArgs, OrderType, Side
    try:
        resp = client.create_and_post_market_order(
            order_args=MarketOrderArgs(token_id=token, amount=1.0, side=Side.BUY),
            order_type=OrderType.FOK)
        print("RESPONSE:", resp)
    except Exception as e:
        print("ORDER FAILED:", repr(e))
        print("\nIf this still says 'invalid order version', the signature_type/funder pair is the")
        print("most likely culprit — try the other proxy type (1 <-> 2), or 0 with no funder.")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--try-order', action='store_true', help='place ONE real $1 order (real money)')
    a = ap.parse_args()
    main(a.try_order)
