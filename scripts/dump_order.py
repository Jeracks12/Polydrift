"""Build + sign an order LOCALLY and print the payload. Submits NOTHING - no money moves.
Shows exactly what the CLOB is being asked to accept, so we can see what it objects to."""
import json, os, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from execution import load_env, apply_exchange_override


def http_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'poly-drift/1.0'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def live_token():
    url = ("https://gamma-api.polymarket.com/markets?active=true&closed=false"
           "&order=endDate&ascending=true&limit=100&end_date_min="
           + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    for m in http_json(url):
        if 'updown-5m' in m.get('slug', ''):
            t = m.get('clobTokenIds')
            if isinstance(t, str): t = json.loads(t)
            return m['slug'], t[0]
    return None, None


load_env()
key = os.environ.get('POLY_PRIVATE_KEY')
sig_type = int(os.environ.get('POLY_SIGNATURE_TYPE') or 0)
funder = os.environ.get('POLY_FUNDER') or None
if not key:
    sys.exit('no POLY_PRIVATE_KEY in .env')

ovr = apply_exchange_override()
if ovr:
    print(f"exchange override: {ovr}")

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.utilities import order_to_json

client = ClobClient('https://clob.polymarket.com', key=key, chain_id=137,
                    signature_type=sig_type, funder=funder)
client.set_api_creds(client.create_or_derive_api_creds())

slug, token = live_token()
print(f"market: {slug}\ntoken:  {token}")
print(f"exchange contract: {client.get_exchange_address(client.get_neg_risk(token))}")

signed = client.create_market_order(MarketOrderArgs(token_id=token, amount=1.0, side=BUY))
body = order_to_json(signed, client.creds.api_key, OrderType.FOK, False)

# redact the signature (a signed order is a bearer instrument) but show everything else
red = json.loads(json.dumps(body))
if isinstance(red.get('order'), dict) and 'signature' in red['order']:
    red['order']['signature'] = '<redacted>'
print("\n--- ORDER PAYLOAD (signature redacted, NOTHING submitted) ---")
print(json.dumps(red, indent=2))
