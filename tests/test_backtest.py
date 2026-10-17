from dataclasses import replace
from datetime import timedelta
import json
import tempfile
import unittest
from pathlib import Path
from tests.test_history import fixtures
from trading.backtest import benchmarks, run_backtest, save_report, walk_forward

class BacktestTests(unittest.TestCase):
    def test_purging_embargo_and_disjoint_tests(self):
        windows=walk_forward(150,30,15,10,label_span=5,embargo=3)
        seen=set()
        for w in windows:
            self.assertLess(max(w.train)+5,min(w.validation))
            self.assertLess(max(w.validation)+5,min(w.test))
            self.assertFalse(seen.intersection(w.test));seen.update(w.test)
        with self.assertRaises(ValueError):walk_forward(5,3,2,2)

    def test_next_open_costs_and_cash_benchmark(self):
        bars=fixtures()
        cash=run_backtest(bars,'cash');self.assertEqual(dict(cash.metrics)['net_return'],0)
        free=run_backtest(bars,'buy_and_hold',fee_bps=0.,spread_bps=0.,slippage_bps=0.)
        paid=run_backtest(bars,'buy_and_hold')
        self.assertLess(paid.equity_curve[-1],free.equity_curve[-1])
        # First decision sees 100..108, fills at bar 9 open=109, marks at 109.
        self.assertAlmostEqual(free.equity_curve[1],50.)
        self.assertIsNone(dict(paid.metrics)['sharpe'])
        self.assertEqual(paid.data_kind,'synthetic')

    def test_policy_cannot_see_future_and_prefix_invariance(self):
        seen=[]
        def policy(feature):
            seen.append(feature);return 'BUY'
        bars=fixtures();short=run_backtest(bars[:20],'custom',policy=policy)
        full=run_backtest(bars,'custom',policy=lambda f:'BUY')
        self.assertEqual(short.equity_curve,full.equity_curve[:len(short.equity_curve)])
        self.assertEqual(seen[0].data_end_utc,bars[8].end_utc)
        self.assertEqual(seen[-1].data_end_utc,bars[18].end_utc)

    def test_report_reproducible_no_overwrite(self):
        bars=fixtures()
        self.assertEqual(benchmarks(bars),benchmarks(bars))
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'report';save_report(benchmarks(bars),out)
            self.assertEqual(json.loads((out/'results.json').read_text())['cash']['data_kind'],'synthetic')
            with self.assertRaises(FileExistsError):save_report(benchmarks(bars),out)

class LiquidityTests(unittest.TestCase):
    def test_volume_cap_partial_fills_and_nonnegative_equity(self):
        bars=fixtures()
        result=run_backtest(bars,'buy_and_hold')
        self.assertGreater(dict(result.metrics)['partial_fills'],0)
        self.assertTrue(all(v>0 for v in result.equity_curve))
        empty=[replace(b,volume=b.volume*0) for b in bars]
        # Missing volume blocks feature calculation before fictitious fills.
        with self.assertRaises(ValueError):run_backtest(empty)
