"""Ask the CLOB what it thinks your account can actually trade with. Read-only."""
import json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from execution import load_env

load_env()
key = os.environ.get('POLY_PRIVATE_KEY')
sig_type = int(os.environ.get('POLY_SIGNATURE_TYPE') or 0)
funder = os.environ.get('POLY_FUNDER') or None
if not key:
    sys.exit('no POLY_PRIVATE_KEY in .env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

client = ClobClient('https://clob.polymarket.com', key=key, chain_id=137,
                    signature_type=sig_type, funder=funder)
client.set_api_creds(client.create_or_derive_api_creds())

print("collateral (USDC) balance/allowance as the CLOB sees it:")
try:
    r = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                               signature_type=sig_type))
    print(json.dumps(r, indent=2))
    bal = float(r.get('balance', 0) or 0) / 1e6
    allow = r.get('allowances') or r.get('allowance')
    print(f"\n  -> balance ~ ${bal:,.2f}")
    print(f"  -> allowances: {allow}")
    if bal <= 0:
        print("  !! the CLOB sees NO collateral for this account -> a BUY cannot fill.")
except Exception as e:
    print("FAILED:", repr(e))
