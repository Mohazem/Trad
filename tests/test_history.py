from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import tempfile
import unittest
from trading.market_history import MarketObservation, HistoryStore, available_series
from trading.features import compute_features, aggregate
from trading.signals import StrategySignal, SignalStore

START = datetime(2026, 1, 1, tzinfo=timezone.utc)

def fixtures(n=40, timeframe='1m'):
    from trading.market_history import TIMEFRAMES
    span = timedelta(seconds=TIMEFRAMES[timeframe])
    return [MarketObservation('BTC/USD', timeframe, START+i*span, START+(i+1)*span,
            D(100+i), D(101+i), D(99+i), D(100+i), D('10'), 'fixture-v1', data_kind='synthetic') for i in range(n)]


def signal(now, **kw):
    values = dict(strategy_id='minute', symbol='BTC/USD', horizon='minutes', as_of_utc=now,
        data_end_utc=now, action='BUY', score=.1, confidence=None, model_id='rules:test',
        model_version='v1', feature_version='causal-v1', reason_codes=('fixture',), estimated_cost_bps=10.)
    values.update(kw)
    return StrategySignal.create(**values)

class HistoryTests(unittest.TestCase):
    def test_reject_invalid_utc_ohlc_incomplete(self):
        b=fixtures(1)[0]
        for kw in [dict(start_utc=START.replace(tzinfo=None)), dict(close=D('NaN')),
                   dict(low=D('0')),dict(symbol='UNKNOWN'),dict(received_at_utc=START)]:
            with self.subTest(kw=kw), self.assertRaises(ValueError): replace(b,**kw)

    def test_gap_duplicate_and_future_arrival(self):
        bars=fixtures(); now=bars[-1].end_utc
        for bad in [bars[:3]+bars[4:],bars+[bars[0]]]:
            with self.assertRaises(ValueError): available_series(bad, now)
        self.assertEqual(len(available_series(bars, bars[8].end_utc)),9)
        delayed=replace(bars[-1],received_at_utc=now+timedelta(minutes=1))
        self.assertEqual(len(available_series(bars[:-1]+[delayed],now)),39)

    def test_features_prefix_invariance_and_partial_buckets(self):
        bars=fixtures(); now=bars[14].end_utc
        self.assertEqual(compute_features(bars,now),compute_features(bars[:15],now))
        self.assertEqual(len(aggregate(bars,'5m',bars[12].end_utc)),2)
        with self.assertRaises(ValueError): compute_features(bars[:8],now)

    def test_storage_idempotent_conflict_atomic_and_point_in_time(self):
        with tempfile.TemporaryDirectory() as d:
            store=HistoryStore(d+'/history.sqlite3'); self.addCleanup(store.close)
            bars=fixtures(); store.ingest(bars); store.ingest(bars)
            self.assertEqual(len(store.read('BTC/USD','1m','fixture-v1',bars[8].end_utc)),9)
            with self.assertRaises(ValueError): store.ingest([replace(bars[0],volume=D(20))])
            self.assertEqual(store.read('BTC/USD','1m','fixture-v1',bars[-1].end_utc)[0].volume,D(10))

    def test_signals_are_finite_aware_fresh_and_idempotent(self):
        s=signal(START)
        self.assertEqual(s.signal_id, signal(START).signal_id)
        for kw in [dict(score=float('nan')),dict(confidence=2),dict(symbol='DOGE/USD'),dict(as_of_utc=START.replace(tzinfo=None))]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):signal(START,**kw)
        with self.assertRaises(ValueError):s.validate_at(START+timedelta(minutes=4))
        with tempfile.TemporaryDirectory() as d:
            store=SignalStore(d+'/signals.sqlite3');self.addCleanup(store.close)
            store.record(s,START);store.record(s,START)
            with self.assertRaises(ValueError):store.record(replace(s,action='SELL'),START)
