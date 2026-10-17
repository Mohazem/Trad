from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from trading.config import MultiHorizonConfig
from trading.strategy_status import StrategyStatus, parse_signal
from trading.models import canonical
from tests.test_history import START, signal
from tests import test_connected

class StrategyViewTests(unittest.TestCase):
    def test_signal_projection_accepts_typed_and_serialized_dates(self):
        s=signal(START)
        self.assertEqual(parse_signal(asdict(s)),s)
        self.assertEqual(parse_signal(json.loads(canonical(asdict(s)))),s)

    def test_read_does_not_create_files_or_infer_account(self):
        with tempfile.TemporaryDirectory() as d:
            view=StrategyStatus(directory=d+'/nonexistent')
            state=view.read(START)
            self.assertFalse(Path(d+'/nonexistent').exists())
            self.assertFalse(state['enabled']);self.assertFalse(state['paper']['armed'])
            self.assertIsNone(state['paper']['remaining_budget_usd'])
            self.assertIsNone(state['paper']['positions'])
            self.assertEqual(len(state['strategies']),3)

    def test_provenance_and_stale_report_are_visible(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'minute.json'
            report={'mode':'research_only','data_kind':'synthetic','signals':[asdict(signal(START))]}
            p.write_text(canonical(report))
            state=StrategyStatus(directory=d).read(START+timedelta(minutes=4))
            row=state['strategies'][0]
            self.assertEqual(row['data_kind'],'synthetic')
            self.assertIn('signal_stale_for_current_time',row['alerts'])
            p.write_text('{"api_key":"secret"}')
            text=canonical(StrategyStatus(directory=d).read(START))
            self.assertNotIn('secret',text)
            self.assertIn('invalid_research_artifact',text)

    def test_configuration_preserves_original_config_and_rejects_live(self):
        self.assertFalse(MultiHorizonConfig.load('multi_horizon.example.json').enabled)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'cfg.json';p.write_text('{"multi_horizon":{"paper_only":false}}')
            with self.assertRaises(ValueError):MultiHorizonConfig.load(p)

class StrategyHTTPTests(unittest.TestCase):
    setUp = test_connected.WebTests.setUp
    close = test_connected.WebTests.close
    get = test_connected.WebTests.get

    def test_strategy_get_does_not_fetch_market_or_invoke_model(self):
        from unittest.mock import patch
        with patch.object(self.dashboard.market,'fetch',side_effect=AssertionError('No market request')):
            page=self.get('/strategies');self.assertIn('Minutes',page)
            response=json.loads(self.get('/api/strategies'))
            self.assertFalse(response['paper']['armed']);self.assertIsNone(response['paper']['positions'])
            self.assertIn('fetch("/api/strategies")',self.get('/strategies.js'))
        self.assertEqual(self.calls,0)
