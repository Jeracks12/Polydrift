# tests/test_menu.py — interactive launcher fills args correctly (no network, no tty).
import os, sys, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('MPLBACKEND', 'Agg')
import poly_drift as pdft

ENV_KEYS = ('POLY_PRIVATE_KEY', 'POLY_SIGNATURE_TYPE', 'POLY_FUNDER', 'POLY_DRIFT_ARM')


def run_menu(answers, coins=('btc', 'eth', 'sol')):
    args = types.SimpleNamespace(token=None, series=None, symbol='btcusdt', record=True,
                                 mode='off', order='market', stake=1.0, max_daily_loss=10.0,
                                 exit_mgmt=None, exit_tp=None,
                                 drift_z=None, min_corr=None, corr_sign=None, min_edge=None,
                                 fair_source=None, scenario=None)
    it = iter(answers)
    orig_coins = pdft._live_updown_coins
    pdft._live_updown_coins = lambda: list(coins)
    import builtins
    saved = builtins.input
    builtins.input = lambda prompt='': next(it)
    try:
        pdft.interactive_menu(args)
    finally:
        builtins.input = saved
        pdft._live_updown_coins = orig_coins
    return args


def test_menu_pick_live_series():
    a = run_menu(['2', '', 'n', ''])            # ETH, record default, mode off
    assert a.series == 'eth-updown-5m' and a.symbol == 'ethusdt'
    assert a.record is True and a.mode == 'off'


def test_menu_default_is_btc():
    a = run_menu(['', 'n', 'n', ''])            # Enter=BTC, record off, mode off
    assert a.series == 'btc-updown-5m' and a.record is False


def test_menu_custom_series():
    a = run_menu(['4', 'doge-updown-5m', 'dogeusdt', 'y', 'n', ''])
    assert a.series == 'doge-updown-5m' and a.symbol == 'dogeusdt'


def test_menu_fixed_token():
    a = run_menu(['5', '123456789', '', '', 'n', ''])
    assert a.token == '123456789' and a.series is None and a.symbol == 'btcusdt'


def test_menu_rejects_bad_choice_then_accepts():
    a = run_menu(['99', 'x', '1', '', 'n', ''])
    assert a.series == 'btc-updown-5m'


def test_menu_paper_mode_settings():
    # BTC, record default, paper, limit orders, $2.50 stake, $5 kill switch, no exit mgmt
    a = run_menu(['1', '', 'n', '2', '2', '2.5', '5', 'n'])
    assert a.mode == 'paper' and a.order == 'limit'
    assert a.stake == 2.5 and a.max_daily_loss == 5.0
    assert a.exit_mgmt is False


def test_menu_paper_with_exit_management():
    # ...paper, market, $1, $10, exit=yes, tp=3
    a = run_menu(['1', '', 'n', '2', '1', '1', '10', 'y', '3'])
    assert a.mode == 'paper' and a.exit_mgmt is True and a.exit_tp == 3.0


TEST_KEY = '0x' + '11' * 32        # well-formed dummy key (never funded, test-only)


def _with_fake_key(answers, key=TEST_KEY):
    import getpass as gp
    import execution
    for k in ENV_KEYS: os.environ.pop(k, None)
    saved, saved_le = gp.getpass, execution.load_env
    gp.getpass = lambda prompt='': key
    execution.load_env = lambda path='.env': None   # hermetic: ignore any real .env on disk
    try:
        args = run_menu(answers)
        # snapshot the resolved credentials before cleanup so tests can assert on them
        args.env = {k: os.environ.get(k) for k in ENV_KEYS}
        return args
    finally:
        gp.getpass, execution.load_env = saved, saved_le
        for k in ENV_KEYS: os.environ.pop(k, None)


def test_menu_live_armed():
    # BTC, record default, live, market, $1, $10, no-exit, EOA type, don't save, correct arm phrase
    a = _with_fake_key(['1', '', 'n', '3', '1', '1', '10', 'n', '0', 'n', 'YES-LIVE'])
    assert a.mode == 'live'


def test_menu_rejects_signer_address_as_funder():
    """The real-world failure: pasting the Signer Address into the funder field."""
    from eth_account import Account
    signer = Account.from_key(TEST_KEY).address
    # a syntactically valid but obviously-fake proxy: the real one is the user's
    # on-chain trading wallet and does not belong in a public repo
    proxy = '0x00000000000000000000000000000000000000c1'
    # BTC, record default, live, market, $1, $10, type=1(email), signer(REJECTED), proxy, no save, arm
    a = _with_fake_key(['1', '', 'n', '3', '1', '1', '10', 'n', '1', signer, proxy, 'n', 'YES-LIVE'])
    assert a.mode == 'live'
    assert a.env['POLY_FUNDER'] == proxy               # kept re-prompting until a valid one


def test_menu_plain_wallet_clears_funder():
    a = _with_fake_key(['1', '', 'n', '3', '1', '1', '10', 'n', '0', 'n', 'YES-LIVE'])
    assert a.mode == 'live'
    assert not a.env.get('POLY_FUNDER')                # type 0 must not carry a funder


def test_menu_live_wrong_arm_phrase_falls_back_to_paper():
    a = _with_fake_key(['1', '', 'n', '3', '1', '1', '10', 'n', '0', 'n', 'nope'])
    assert a.mode == 'paper'


def test_main_publishes_executor_for_the_signal_engine(monkeypatch):
    """Live/paper startup must set the module global read by Analytics._signal."""
    import execution

    class FakeExecutor:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

    class FakeThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass

    monkeypatch.setattr(execution, 'Executor', FakeExecutor)
    monkeypatch.setattr(pdft.threading, 'Thread', FakeThread)
    monkeypatch.setattr(pdft, 'run_plot', lambda: None)
    monkeypatch.setattr(pdft, 'EXECUTOR', None)
    monkeypatch.setattr(sys, 'argv', ['poly_drift.py', '--token', 'yes-token',
                                      '--mode', 'paper', '--no-record'])
    pdft.main()
    assert isinstance(pdft.EXECUTOR, FakeExecutor)


def test_menu_tunes_reward_risk_and_vol_spike_guard():
    """The indicator branch must expose the reward:risk and vol-spike knobs at runtime, so
    tuning them never means editing the source."""
    a = run_menu(['1', '', 'y', 'drift', 'fade', '2.5', '0.2', '1', '0.04', '0.12', '0', 'n', ''])
    assert a.fair_source == 'drift' and a.scenario == 'fade'
    assert a.drift_z == 2.5 and a.min_corr == 0.2 and a.corr_sign == 1
    assert a.min_edge == 0.04 and a.min_edge_roi == 0.12 and a.vol_spike_guard == 0
    assert a.mode == 'off'


def test_menu_indicator_defaults_pass_through_as_none():
    a = run_menu(['1', '', 'y', '', '', '', '', '', '', '', '', 'n', ''])
    assert a.min_edge_roi is None and a.vol_spike_guard is None   # None = keep CFG default
    assert a.fair_source is None and a.scenario is None
