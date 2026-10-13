from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import io
import json
from pathlib import Path
import runpy
import contextlib
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
import sqlite3
from unittest.mock import patch, Mock
import urllib.error

from trading.paper_once import (AttemptJournal, CLIENT_ORDER_ID, OneShotPaper, OrderSpec,
                                PaperBroker, checked_order, validate)
from trading.paper_transport import (DATA, PAPER, MAX_BYTES, NoRedirect, PaperError,
                                     ProtectedTransport, permitted, unique_object, validate_route)

NOW = datetime(2026, 10, 13, 15, 0, 30, tzinfo=timezone.utc)


def fixture():
    spec = OrderSpec("AAPL", "5", "100.00", "a" * 64, NOW.isoformat(), (NOW+timedelta(minutes=2)).isoformat())
    bars = [{"t": (NOW.replace(second=0)-timedelta(minutes=n)).isoformat(), "o": "100", "h": "100.1",
             "l": "99.9", "c": "100", "v": "500"} for n in range(30, 0, -1)]
    snapshot = {"fetched_at": NOW.isoformat(), "account": {"status": "ACTIVE", "currency": "USD",
                "cash": "100000", "non_marginable_buying_power": "100000", "short_market_value": "0", "long_market_value": "0",
                "trading_blocked": False, "account_blocked": False, "trade_suspended_by_user": False},
                "positions": [], "orders": [], "asset": {"symbol": "AAPL", "status": "active", "tradable": True, "class": "us_equity"},
                "clock": {"timestamp": NOW.isoformat(), "is_open": True, "next_close": (NOW+timedelta(hours=5)).isoformat()},
                "market": {"symbol": "AAPL", "source": DATA, "feed": "iex", "timeframe": "1Min", "bars": bars,
                           "quote": {"t": NOW.isoformat(), "bp": "99.99", "ap": "100.00", "bs": 100, "as": 100}}}
    return spec, snapshot


def order(spec=None, status="filled", filled="5"):
    spec = spec or fixture()[0]
    return {**spec.payload(), "id": "00000000-0000-0000-0000-000000000001", "status": status,
            "filled_qty": filled, "filled_avg_price": "100.00" if Decimal(filled) else None}


class FakeTransport:
    allow_write = True
    def __init__(self):
        self.calls = []
        self.post_result = order()
        self.cancel_result = None
    def request(self, origin, method, path, **kwargs):
        if kwargs.get('before_send') is not None:
            kwargs['before_send']()
        self.calls.append((origin, method, path, kwargs))
        result = self.post_result if method == "POST" else self.cancel_result
        if isinstance(result, Exception):
            raise result
        return result


class FakeBroker:
    def __init__(self):
        self.transport = FakeTransport()
        self.snapshot_data = fixture()[1]
        self.lookups = [None]
    def lookup(self):
        value = self.lookups.pop(0) if self.lookups else None
        if isinstance(value, Exception):
            raise value
        return value
    def snapshot(self, *args, **kwargs):
        return deepcopy(self.snapshot_data)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.spec, self.snapshot = fixture()
    def check_bad(self, *, spec=None, snapshot=None):
        with self.assertRaises((PaperError, KeyError, TypeError, ValueError)):
            validate(spec or self.spec, snapshot or self.snapshot, NOW)
    def test_valid_dry_run_and_fixed_client_id(self):
        with patch('socket.socket', side_effect=AssertionError('offline')):
            result = validate(self.spec, self.snapshot, NOW)
        self.assertEqual(result['max_notional_usd'], '500.00')
        self.assertEqual(result['order']['client_order_id'], CLIENT_ORDER_ID)
        self.assertEqual(result['order']['time_in_force'], 'ioc')
        self.assertFalse(result['broker_write'])
    def test_thousand_limit_and_decimal_context(self):
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(validate(replace(self.spec, qty='10'), self.snapshot, NOW)['max_notional_usd'], '1000.00')
            self.check_bad(spec=replace(self.spec, qty='11'))
    def test_invalid_prices_quantities_and_exponents(self):
        for bad in ('NaN', 'Infinity', '-1', '0', '1e999999999', '0.0000000000001', True, 1.5):
            for field in ('qty', 'limit_price'):
                with self.subTest(field=field, value=bad):
                    self.check_bad(spec=replace(self.spec, **{field:bad}))
    def test_whole_share_and_tick_rules(self):
        self.check_bad(spec=replace(self.spec, qty='0.5'))
        self.check_bad(spec=replace(self.spec, limit_price='99.999'))
    def test_cash_and_unleveraged_buying_power(self):
        for field in ('cash', 'non_marginable_buying_power'):
            self.snapshot['account'][field] = '500'
            self.check_bad()
            self.snapshot['account'][field] = '100000'
    def test_existing_orders_positions_and_short_exposure(self):
        for field in ('orders', 'positions'):
            self.snapshot[field] = [{}]
            self.check_bad()
            self.snapshot[field] = []
        self.snapshot['account']['short_market_value'] = '-100'
        self.check_bad()
    def test_exposure_on_other_symbols_and_all_pending_orders_blocks_buy(self):
        # The policy is stricter than an aggregate sum: ANY prior position or
        # open order forbids the experiment, including foreign-symbol orders.
        for field, values in [
            ('positions',[{'symbol':'MSFT','market_value':'1','qty':'1','side':'long'}]),
            ('orders',[{'symbol':'BTC/USD','side':'buy','qty':'1','filled_qty':'0','limit_price':'1'}]),
            ('orders',[{'symbol':'MSFT','side':'buy','qty':'5','filled_qty':'2','limit_price':'100','status':'pending_cancel'}]),
            ('orders',[{'symbol':'MSFT','side':'buy','qty':'1','limit_price':'100'}]*500),
        ]:
            snap=deepcopy(self.snapshot)
            snap[field]=values
            self.check_bad(snapshot=snap)
        self.snapshot['account']['long_market_value']='1'
        self.check_bad()
    def test_huge_margin_buying_power_cannot_override_low_cash(self):
        self.snapshot['account']['cash']='5'
        self.snapshot['account']['buying_power']='10000000'
        self.snapshot['account']['regt_buying_power']='10000000'
        self.snapshot['account']['daytrading_buying_power']='10000000'
        self.check_bad()
    def test_account_and_asset_blocks(self):
        for section, field, value in [('account','trading_blocked',True), ('account','status','CLOSED'),
                                     ('account','currency','EUR'), ('asset','tradable',False), ('asset','status','inactive')]:
            snapshot = deepcopy(self.snapshot)
            snapshot[section][field] = value
            self.check_bad(snapshot=snapshot)
    def test_closed_and_closing_stock_session(self):
        self.snapshot['clock']['is_open'] = False
        self.check_bad()
        self.snapshot['clock']['is_open'] = True
        self.snapshot['clock']['next_close'] = (NOW+timedelta(seconds=20)).isoformat()
        self.check_bad()
    def test_quote_stale_future_crossed_wide_and_empty(self):
        for field, value in [('t',(NOW-timedelta(seconds=16)).isoformat()), ('t',(NOW+timedelta(seconds=1)).isoformat()),
                             ('bp','101'), ('bp','90'), ('as',0), ('bp','NaN')]:
            snapshot = deepcopy(self.snapshot)
            snapshot['market']['quote'][field] = value
            self.check_bad(snapshot=snapshot)
    def test_snapshot_and_clock_staleness(self):
        self.snapshot['fetched_at'] = (NOW-timedelta(seconds=16)).isoformat()
        self.check_bad()
        self.snapshot = fixture()[1]
        self.snapshot['clock']['timestamp'] = (NOW-timedelta(seconds=16)).isoformat()
        self.check_bad()
    def test_bars_stale_future_unordered_gaps_and_invalid_ohlc(self):
        for kind in ('stale','future','unordered','gap','ohlc','few','dislocated'):
            snap = deepcopy(self.snapshot)
            bars = snap['market']['bars']
            if kind == 'stale':
                for bar in bars: bar['t'] = (datetime.fromisoformat(bar['t'])-timedelta(minutes=5)).isoformat()
            elif kind == 'future': bars[-1]['t'] = NOW.isoformat()
            elif kind == 'unordered': bars.reverse()
            elif kind == 'gap': del bars[-5:-2]
            elif kind == 'ohlc': bars[-1]['l'] = '101'
            elif kind == 'few': del bars[10:]
            elif kind == 'dislocated':
                for k in ('o','h','l','c'): bars[-1][k] = '90'
            with self.subTest(kind=kind): self.check_bad(snapshot=snap)
    def test_source_symbol_feed_and_limit_mismatches(self):
        for field, value in [('source','https://evil.invalid'),('symbol','MSFT'),('feed','delayed_sip')]:
            snap = deepcopy(self.snapshot)
            snap['market'][field] = value
            self.check_bad(snapshot=snap)
        self.check_bad(spec=replace(self.spec, limit_price='100.01'))
    def test_expired_or_future_approval_and_no_analysis(self):
        self.check_bad(spec=replace(self.spec, expires_at=NOW.isoformat()))
        self.check_bad(spec=replace(self.spec, approved_at=(NOW+timedelta(seconds=1)).isoformat()))
        self.check_bad(spec=replace(self.spec, analysis_digest=''))
    def test_crypto_increments_and_closed_equity_clock(self):
        spec = replace(self.spec, symbol='BTC/USD', qty='0.01', limit_price='100')
        self.snapshot['asset'] = {'symbol':'BTC/USD','class':'crypto','tradable':True,'status':'active',
                                   'min_order_size':'0.001','min_trade_increment':'0.001','price_increment':'0.01'}
        self.snapshot['market'].update(symbol='BTC/USD', feed='us')
        self.snapshot['clock']['is_open'] = False
        self.snapshot['account']['crypto_status'] = 'ACTIVE'
        validate(spec, self.snapshot, NOW)
        self.check_bad(spec=replace(spec, qty='0.0001'))
        self.check_bad(spec=replace(spec, qty='0.0105'))
        self.snapshot['account']['crypto_status'] = 'INACTIVE'
        self.check_bad(spec=spec)

    def test_cli_dry_run_offline_without_secret_or_network_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'synthetic-test-only.json'
            path.write_text(json.dumps({'spec':asdict(self.spec),'snapshot':self.snapshot,'now':NOW.isoformat()}))
            output = io.StringIO()
            with patch('sys.argv',['paper_cli','dry-run','--input',str(path)]),patch('socket.socket',side_effect=AssertionError('network forbidden')),patch('os.getenv',side_effect=AssertionError('secrets forbidden')),contextlib.redirect_stdout(output):
                runpy.run_module('trading.paper_cli',run_name='__main__')
            result = json.loads(output.getvalue())
            self.assertFalse(result['broker_write'])
            self.assertEqual(result['mode'],'offline_snapshot_dry_run_not_submission_approval')

    def test_cli_exposes_no_submit_or_cancel_command(self):
        for command in ('submit','cancel','live','run-forever'):
            with patch('sys.argv',['paper_cli',command]),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                runpy.run_module('trading.paper_cli',run_name='__main__')


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'attempt.sqlite3'
        self.journal = AttemptJournal(self.path, create=True)
        self.broker = FakeBroker()
        self.runner = OneShotPaper(self.broker, self.journal, mode="manual_one_shot", clock=lambda: NOW)
        self.spec = fixture()[0]
    def tearDown(self):
        self.tmp.cleanup()
    def submit(self):
        return self.runner.submit_approved(self.spec, approved_digest=self.spec.approval_digest())
    def posts(self):
        return [c for c in self.broker.transport.calls if c[1] == 'POST']
    def reconcile(self, *, cancel_open=False):
        self.journal.claim(self.spec.approval_digest())
        return self.runner.reconcile(self.spec,cancel_open=cancel_open,
                                     approved_digest=self.spec.approval_digest() if cancel_open else None)
    def test_separate_exact_approval_and_write_gate(self):
        with self.assertRaises(PaperError): self.runner.submit_approved(self.spec, approved_digest='not-approved')
        self.broker.transport.allow_write = False
        with self.assertRaises(PaperError): self.submit()
        self.assertEqual(self.posts(), [])
    def test_default_read_only_mode_blocks_writes_before_broker_access(self):
        runner = OneShotPaper(self.broker, self.journal, clock=lambda: NOW)
        with self.assertRaisesRegex(PaperError, 'manual_one_shot_mode_required'):
            runner.submit_approved(self.spec, approved_digest=self.spec.approval_digest())
        with self.assertRaisesRegex(PaperError, 'manual_one_shot_mode_required'):
            runner.reconcile(self.spec, cancel_open=True, approved_digest=self.spec.approval_digest())
        self.assertEqual(self.broker.lookups, [None])
        self.assertEqual(self.broker.transport.calls, [])
    def test_unattended_and_recurring_modes_are_explicitly_forbidden(self):
        for mode in ('unattended', 'recurring', 'continuous', 'run_forever', 'automatic'):
            with self.subTest(mode=mode), self.assertRaisesRegex(PaperError, 'unattended_or_recurring_execution_forbidden'):
                OneShotPaper(self.broker, self.journal, mode=mode)
        self.assertEqual(self.broker.lookups, [None])
        self.assertEqual(self.broker.transport.calls, [])
    def test_single_submission_and_restart_deduplication(self):
        self.assertEqual(self.submit()['state'], 'filled')
        self.broker.lookups = [order()]
        self.runner = OneShotPaper(self.broker, AttemptJournal(self.path), mode="manual_one_shot", clock=lambda: NOW)
        self.assertEqual(self.submit()['state'], 'filled')
        self.assertEqual(len(self.posts()), 1)
    def test_different_second_spec_rejected_even_after_fill(self):
        self.submit()
        self.spec = replace(self.spec, symbol='MSFT')
        with self.assertRaises(PaperError): self.submit()
        self.assertEqual(len(self.posts()), 1)
    def test_broker_existing_id_never_posts(self):
        self.broker.lookups = [order()]
        self.assertTrue(self.submit()['duplicate_submission_avoided'])
        self.assertEqual(self.posts(), [])
    def test_timeout_reconciles_without_resubmission(self):
        self.broker.transport.post_result = PaperError('transport_or_response_failure')
        self.broker.lookups = [None, order()]
        self.assertEqual(self.submit()['state'], 'filled')
        self.assertEqual(len(self.posts()), 1)
    def test_unknown_result_404_keeps_attempt_consumed(self):
        self.broker.transport.post_result = PaperError('request_timeout')
        self.assertEqual(self.submit()['state'], 'unknown')
        self.assertEqual(self.submit()['state'], 'unknown')
        self.assertEqual(len(self.posts()), 1)
    def test_duplicate_error_and_malformed_response_reconcile(self):
        for response in (PaperError('http_error',422), {'unexpected':'raw'}, None):
            with self.subTest(response=type(response).__name__):
                journal = AttemptJournal(Path(self.tmp.name)/str(len(list(Path(self.tmp.name).iterdir()))), create=True)
                broker = FakeBroker()
                broker.transport.post_result = response
                broker.lookups = [None, order()]
                runner = OneShotPaper(broker, journal, mode="manual_one_shot", clock=lambda: NOW)
                self.assertEqual(runner.submit_approved(self.spec,approved_digest=self.spec.approval_digest())['state'],'filled')
                self.assertEqual(sum(c[1]=='POST' for c in broker.transport.calls),1)
    def test_lookup_auth_error_stops_without_post(self):
        self.broker.lookups = [PaperError('http_error',401)]
        with self.assertRaises(PaperError): self.submit()
        self.assertEqual(self.posts(), [])
    def test_preflight_rechecks_account_before_post(self):
        self.broker.snapshot_data['orders'] = [{}]
        with self.assertRaises(PaperError): self.submit()
        self.assertEqual(self.posts(), [])
    def test_cancel_partial_fill_reconciles_terminal_state(self):
        self.broker.lookups = [order(status='partially_filled',filled='2'), order(status='canceled',filled='2')]
        result = self.reconcile(cancel_open=True)
        self.assertEqual(result['state'],'canceled')
        self.assertEqual(result['filled_qty'],'2')
        self.assertEqual([c[1] for c in self.broker.transport.calls],['DELETE'])
    def test_cancel_ack_is_not_cancellation_confirmation(self):
        self.broker.lookups = [order(status='new',filled='0'),order(status='pending_cancel',filled='0')]
        result = self.reconcile(cancel_open=True)
        self.assertEqual(result['state'],'pending_cancel')
        self.assertIn('manual_reconciliation',result['action'])
    def test_cancel_error_never_replaces_or_liquidates(self):
        self.broker.lookups = [order(status='new',filled='0')]
        self.broker.transport.cancel_result = PaperError('request_timeout')
        self.assertEqual(self.reconcile(cancel_open=True)['state'],'unknown')
        self.assertEqual([c[1] for c in self.broker.transport.calls],['DELETE'])
    def test_reconcile_order_mismatch_unknown_state_and_fill_price(self):
        for change in ({'symbol':'MSFT'},{'status':'unrecognized'},{'filled_qty':'6'},{'filled_avg_price':'101'}):
            self.broker.lookups = [{**order(),**change}]
            self.assertEqual(self.reconcile()['state'],'unknown')
        self.assertEqual(self.posts(),[])
    def test_journal_claim_is_atomic_and_spec_bound(self):
        self.assertTrue(self.journal.claim('a'*64))
        self.assertFalse(AttemptJournal(self.path).claim('a'*64))
        with self.assertRaises(PaperError): self.journal.claim('b'*64)
    def test_reopened_unused_journal_is_reconciliation_only(self):
        self.runner = OneShotPaper(self.broker, AttemptJournal(self.path), mode="manual_one_shot", clock=lambda: NOW)
        with patch.object(self.broker, 'snapshot') as snapshot:
            result = self.submit()
            snapshot.assert_not_called()
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['action'], 'manual_reconciliation_required_no_resubmit')
        self.assertEqual(self.broker.lookups, [])
        self.assertEqual(self.posts(), [])
        self.assertFalse(AttemptJournal(self.path).claim(self.spec.approval_digest()))
    def test_restored_empty_backup_does_not_enable_post_after_restart(self):
        empty_backup = self.path.read_bytes()
        self.journal.claim(self.spec.approval_digest())
        restored = Path(self.tmp.name) / 'restored.sqlite3'
        restored.write_bytes(empty_backup)
        restored.replace(self.path)
        # A new instance cannot detect this structurally valid rollback. Its
        # submission permission is absent, so 404/absence remains unknown.
        self.runner = OneShotPaper(self.broker, AttemptJournal(self.path), mode="manual_one_shot", clock=lambda: NOW)
        self.assertEqual(self.submit()['state'], 'unknown')
        self.assertEqual(self.submit()['state'], 'unknown')
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.spec.payload()['client_order_id'], CLIENT_ORDER_ID)
    def test_restored_empty_backup_reconciles_matching_broker_order(self):
        empty_backup = self.path.read_bytes()
        self.journal.claim(self.spec.approval_digest())
        self.path.write_bytes(empty_backup)
        self.broker.lookups = [order()]
        self.runner = OneShotPaper(self.broker, AttemptJournal(self.path), mode="manual_one_shot", clock=lambda: NOW)
        result = self.submit()
        self.assertEqual(result['state'], 'filled')
        self.assertEqual(result['filled_qty'], '5')
        self.assertEqual(self.posts(), [])
    def test_reopened_empty_journal_lookup_errors_remain_unknown(self):
        self.runner = OneShotPaper(self.broker, AttemptJournal(self.path), mode="manual_one_shot", clock=lambda: NOW)
        for response in (PaperError('http_error', 401), PaperError('request_timeout'), {'ambiguous': True}):
            self.broker.lookups = [response]
            self.assertEqual(self.submit()['state'], 'unknown')
            self.assertEqual(self.posts(), [])
    def test_same_instance_does_not_regain_permission_after_inplace_restore(self):
        empty_backup = self.path.read_bytes()
        self.assertEqual(self.submit()['state'], 'filled')
        self.path.write_bytes(empty_backup)
        self.broker.lookups = [None]
        self.assertEqual(self.submit()['state'], 'unknown')
        self.assertEqual(len(self.posts()), 1)
    def test_concurrent_claims_reserve_one_slot(self):
        second = AttemptJournal(self.path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda journal: journal.claim('a'*64), [self.journal, second]))
        self.assertEqual(sorted(results), [False, True])
    def test_missing_journal_never_auto_initializes(self):
        missing=Path(self.tmp.name)/'missing.sqlite3'
        with self.assertRaises(PaperError): AttemptJournal(missing)
        self.assertFalse(missing.exists())
        with self.assertRaises(PaperError): AttemptJournal(self.path,create=True)
    def test_deleted_or_replaced_journal_stops_before_broker_access(self):
        self.path.unlink()
        with self.assertRaises(PaperError): self.submit()
        self.assertFalse(self.path.exists())
        replacement=AttemptJournal(self.path,create=True)
        # Inode can be immediately reused after unlink on some filesystems;
        # replace an existing file atomically to ensure a different inode.
        other=Path(self.tmp.name)/'other.sqlite3'
        AttemptJournal(other,create=True)
        other.replace(self.path)
        with self.assertRaises(PaperError): self.submit()
        self.assertEqual(self.broker.lookups,[None])
        self.assertEqual(self.posts(),[])
    def test_empty_corrupt_or_old_schema_journal_stops(self):
        for name,data in [('empty',b''),('corrupt',b'not a database')]:
            path=Path(self.tmp.name)/name
            path.write_bytes(data)
            with self.assertRaises(PaperError): AttemptJournal(path)
        path=Path(self.tmp.name)/'old'
        with sqlite3.connect(path) as db: db.execute('CREATE TABLE attempt (id INTEGER PRIMARY KEY,digest TEXT,state TEXT)')
        with self.assertRaises(PaperError): AttemptJournal(path)
    def test_deleted_consumed_attempt_row_cannot_rearm(self):
        self.journal.claim(self.spec.approval_digest())
        with self.journal.connect() as db: db.execute('DELETE FROM attempt')
        with self.assertRaises(PaperError): AttemptJournal(self.path)
        with self.assertRaises(PaperError): self.submit()
        self.assertEqual(self.posts(),[])
    def test_journal_write_failure_before_post_stops(self):
        with patch.object(self.journal,'set_state',side_effect=PaperError('disk_failure')):
            with self.assertRaises(PaperError): self.submit()
        self.assertEqual(self.posts(),[])
        self.assertFalse(AttemptJournal(self.path).claim(self.spec.approval_digest()))
    def test_process_crash_after_reserved_attempt_cannot_resubmit(self):
        with patch.object(self.broker.transport,'request',side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt): self.submit()
        restarted=OneShotPaper(self.broker,AttemptJournal(self.path),mode="manual_one_shot",clock=lambda:NOW)
        self.assertEqual(restarted.submit_approved(self.spec,approved_digest=self.spec.approval_digest())['state'],'unknown')
        self.assertEqual(self.posts(),[])
    def test_operation_lock_blocks_submit_reconcile_and_cancel(self):
        self.journal.claim(self.spec.approval_digest())
        second=OneShotPaper(self.broker,AttemptJournal(self.path),mode="manual_one_shot",clock=lambda:NOW)
        with self.journal.locked():
            for call in [lambda:second.submit_approved(self.spec,approved_digest=self.spec.approval_digest()),
                         lambda:second.reconcile(self.spec),
                         lambda:second.reconcile(self.spec,cancel_open=True,approved_digest=self.spec.approval_digest())]:
                with self.assertRaisesRegex(PaperError,'experiment_busy'): call()
        self.assertEqual(self.broker.lookups,[None])
        self.assertEqual(self.posts(),[])
    def test_database_write_lock_failure_never_posts(self):
        with self.journal.connect() as db:
            db.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(PaperError): self.submit()
        self.assertEqual(self.posts(),[])
    def test_lock_is_held_through_post_and_released_afterwards(self):
        other=AttemptJournal(self.path)
        original=self.broker.transport.request
        def during_request(*args,**kwargs):
            with self.assertRaisesRegex(PaperError,'experiment_busy'):
                with other.locked(): pass
            return original(*args,**kwargs)
        with patch.object(self.broker.transport,'request',side_effect=during_request):
            self.assertEqual(self.submit()['state'],'filled')
        with other.locked(): pass
    def test_stale_data_at_last_send_gate_prevents_post(self):
        self.runner.clock=Mock(side_effect=[NOW,NOW+timedelta(seconds=16)])
        self.assertEqual(self.submit()['state'],'unknown')
        self.assertEqual(self.posts(),[])
    def test_cancel_requires_matching_approval_before_reads(self):
        self.journal.claim(self.spec.approval_digest())
        with self.assertRaisesRegex(PaperError,'exact_cancel_approval_required'):
            self.runner.reconcile(self.spec,cancel_open=True)
        self.assertEqual(self.broker.lookups,[None])
    def test_cancel_racing_full_fill_reconciles_after_422(self):
        self.broker.lookups=[order(status='partially_filled',filled='2'),order()]
        self.broker.transport.cancel_result=PaperError('http_error',422)
        result=self.reconcile(cancel_open=True)
        self.assertEqual(result['state'],'filled')
        self.assertEqual(result['filled_qty'],'5')
        self.assertEqual([c[1] for c in self.broker.transport.calls],['DELETE'])
    def test_cancel_timeout_preserves_confirmed_partial_fill(self):
        self.broker.lookups=[order(status='partially_filled',filled='2'),PaperError('request_timeout')]
        self.broker.transport.cancel_result=PaperError('request_timeout')
        result=self.reconcile(cancel_open=True)
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(result['confirmed_filled_qty'],'2')
        self.assertFalse(self.journal.claim(self.spec.approval_digest()))
    def test_reconcile_rejects_order_id_change_after_cancel(self):
        changed={**order(status='canceled',filled='2'),'id':'00000000-0000-0000-0000-000000000002'}
        self.broker.lookups=[order(status='partially_filled',filled='2'),changed]
        self.assertEqual(self.reconcile(cancel_open=True)['state'],'unknown')
    def test_fills_and_terminal_observations_cannot_regress(self):
        self.broker.lookups=[order(status='partially_filled',filled='2')]
        self.assertEqual(self.reconcile()['filled_qty'],'2')
        self.broker.lookups=[order(status='partially_filled',filled='1')]
        self.assertEqual(self.reconcile()['confirmed_filled_qty'],'2')
        self.broker.lookups=[order()]
        self.assertEqual(self.reconcile()['state'],'filled')
        for _ in range(2):
            self.broker.lookups=[order(status='canceled',filled='5')]
            result=self.reconcile()
            self.assertEqual(result['state'],'unknown')
            self.assertEqual(result['last_observed_state'],'filled')
    def test_contradictory_fill_status_is_unknown(self):
        for status,filled in [('filled','2'),('new','2'),('rejected','2'),('partially_filled','5'),('partially_filled','0')]:
            self.broker.lookups=[order(status=status,filled=filled)]
            self.assertEqual(self.reconcile()['state'],'unknown')


class BrokerReadTests(unittest.TestCase):
    def test_lookup_uses_stable_experiment_client_order_id(self):
        transport = Mock()
        transport.request.side_effect = PaperError('http_error', 404)
        self.assertIsNone(PaperBroker(transport).lookup())
        transport.request.assert_called_once_with(PAPER, 'GET', '/v2/orders:by_client_order_id',
                                                 params={'client_order_id': CLIENT_ORDER_ID})
    def test_market_request_is_bounded_and_records_exact_feed(self):
        spec, snapshot = fixture()
        transport = Mock()
        market = snapshot['market']
        transport.request.side_effect = [
            {'bars':{'AAPL':list(reversed(market['bars']))},'next_page_token':'more'},
            {'quotes':{'AAPL':market['quote']}}]
        result = PaperBroker(transport).market('AAPL',feed='iex',now=NOW)
        self.assertEqual(result['bars'],market['bars'])
        self.assertEqual(result['feed'],'iex')
        self.assertTrue(result['more_history_available'])
        self.assertEqual(transport.request.call_count,2)
        params = transport.request.call_args_list[0].kwargs['params']
        self.assertEqual(params['limit'],'60')
        self.assertEqual(params['timeframe'],'1Min')
        self.assertEqual(params['sort'],'desc')
        self.assertLess(datetime.fromisoformat(params['end']), NOW.replace(second=0))
    def test_auth_failure_does_not_fallback_or_request_quote(self):
        transport = Mock()
        transport.request.side_effect = PaperError('http_error',403)
        with self.assertRaises(PaperError): PaperBroker(transport).market('AAPL',feed='sip',now=NOW)
        self.assertEqual(transport.request.call_count,1)
    def test_crypto_market_routes_use_alpaca_us_location(self):
        transport = Mock()
        transport.request.side_effect = [{'bars':{'BTC/USD':[]}}, {'quotes':{'BTC/USD':fixture()[1]['market']['quote']}}]
        result = PaperBroker(transport).market('BTC/USD',now=NOW)
        self.assertEqual(result['feed'],'us')
        self.assertEqual(transport.request.call_args_list[0].args[2],'/v1beta3/crypto/us/bars')
        self.assertEqual(transport.request.call_args_list[1].args[2],'/v1beta3/crypto/us/latest/quotes')
    def test_snapshot_projects_account_fields_and_timestamps_start(self):
        spec, snapshot = fixture()
        transport = Mock()
        account = {**snapshot['account'],'id':'must-not-escape','account_number':'must-not-escape'}
        transport.request.side_effect = [account,[],[],snapshot['asset'],snapshot['clock'],
            {'bars':{'AAPL':list(reversed(snapshot['market']['bars']))}},
            {'quotes':{'AAPL':snapshot['market']['quote']}}]
        result = PaperBroker(transport).snapshot('AAPL')
        self.assertNotIn('id',result['account'])
        self.assertNotIn('account_number',result['account'])
        self.assertEqual(transport.preflight.call_count,2)
        self.assertEqual(transport.request.call_count,7)


class TransportTests(unittest.TestCase):
    def test_duplicate_json_fields_rejected(self):
        with self.assertRaises(ValueError): json.loads('{"status":"new","status":"filled"}',object_pairs_hook=unique_object)
    def test_send_gate_runs_after_setup_before_any_network(self):
        transport=ProtectedTransport(allow_write=True)
        with patch.object(transport,'preflight',return_value='http://proxy:8080'),patch('os.environ',{'ALPACA_PAPER_KEY':'test','ALPACA_PAPER_SECRET_':'test'}),patch('urllib.request.build_opener') as build:
            with self.assertRaises(PaperError):
                transport.request(PAPER,'POST','/v2/orders',body={},before_send=Mock(side_effect=PaperError('stale_or_future_data')))
            build.return_value.open.assert_not_called()
    def test_live_or_lookalike_origin_and_unapproved_routes_rejected(self):
        for origin in ('https://api.alpaca.markets',PAPER+'/',PAPER+':443',PAPER+'.evil.invalid','http://paper-api.alpaca.markets'):
            with self.assertRaises(PaperError): validate_route(origin,'GET','/v2/account',{})
        for method,path in [('POST','/v2/account'),('DELETE','/v2/orders'),('GET','/v2/orders/../account')]:
            with self.assertRaises(PaperError): validate_route(PAPER,method,path,{})
    def test_redirect_never_constructs_second_request(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid'))
    def test_default_transport_forbids_broker_writes_before_secrets(self):
        with patch('os.environ',{}):
            with self.assertRaises(PaperError): ProtectedTransport().request(PAPER,'POST','/v2/orders',body={})
    def test_network_policy_denial_stops_before_secret_or_proxy_access(self):
        with patch('trading.paper_transport.permitted',return_value=False), patch('urllib.request.getproxies',side_effect=AssertionError('no proxy read')):
            with self.assertRaisesRegex(PaperError,'network_host_not_permitted'):
                ProtectedTransport().preflight(DATA)
    def test_missing_destination_confirmation_stops(self):
        with patch('trading.paper_transport.permitted',return_value=True):
            with self.assertRaisesRegex(PaperError,'secret_destination_unconfirmed'):
                ProtectedTransport().preflight(DATA)
    def test_missing_or_bypassed_proxy_stops(self):
        env={'ALPACA_PAPER_KEY':'test-placeholder','ALPACA_PAPER_SECRET_':'test-placeholder'}
        with patch('trading.paper_transport.permitted',return_value=True),patch.dict('os.environ',env,clear=True):
            for proxy,bypass in (({},False),({'https':'http://proxy:8080'},True)):
                with patch('urllib.request.getproxies',return_value=proxy),patch('urllib.request.proxy_bypass',return_value=bypass):
                    with self.assertRaises(PaperError): ProtectedTransport(confirmed_secret_destinations=('data.alpaca.markets',)).preflight(DATA)
    def test_policy_exact_host_no_suffix_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'policy.json'
            path.write_text(json.dumps({'http_network_policy':{'type':'restricted','egress_rules':[{'host':'paper-api.alpaca.markets'}]}}))
            self.assertTrue(permitted('paper-api.alpaca.markets',path))
            self.assertFalse(permitted('data.alpaca.markets',path))
    def test_http_failure_body_is_not_logged_or_exposed(self):
        transport=ProtectedTransport()
        error=urllib.error.HTTPError(PAPER+'/v2/account',401,'sensitive',{},io.BytesIO(b'sensitive body'))
        with patch.object(transport,'preflight',return_value='http://proxy:8080'),patch('os.environ',{'ALPACA_PAPER_KEY':'test','ALPACA_PAPER_SECRET_':'test'}),patch('urllib.request.build_opener') as build:
            build.return_value.open.side_effect=error
            with self.assertRaises(PaperError) as caught: transport.request(PAPER,'GET','/v2/account')
            self.assertEqual(str(caught.exception),'http_error')
            self.assertEqual(caught.exception.status,401)
            self.assertEqual(build.return_value.open.call_count,1)
    def test_response_size_is_bounded_and_tls_verified(self):
        transport=ProtectedTransport()
        with patch.object(transport,'preflight',return_value='http://proxy:8080'),patch('os.environ',{'ALPACA_PAPER_KEY':'test','ALPACA_PAPER_SECRET_':'test'}),patch('urllib.request.build_opener') as build:
            response=build.return_value.open.return_value.__enter__.return_value
            response.status=200
            response.read.return_value=b'x'*(MAX_BYTES+1)
            with self.assertRaisesRegex(PaperError,'response_size_limit'): transport.request(PAPER,'GET','/v2/account')
            response.read.assert_called_once_with(MAX_BYTES+1)
            handlers=build.call_args.args
            self.assertTrue(handlers[1]._context.check_hostname)
    def test_candidate_preflight_blocks_all_reads_when_data_denied(self):
        transport=ProtectedTransport()
        with patch.object(transport,'preflight',side_effect=[None,PaperError('network_host_not_permitted')]),patch.object(transport,'request') as request:
            with self.assertRaises(PaperError): PaperBroker(transport).candidate_packet()
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
