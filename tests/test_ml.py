from dataclasses import replace
from datetime import timedelta
import tempfile
import unittest
from unittest.mock import patch
from trading.backtest import walk_forward, WalkForwardWindow
from trading.features import compute_features
from trading.ml_models import ModelUnavailable, backend, ml_signal
from trading.training import dataset, fit_window
from tests.test_history import fixtures

class MLTests(unittest.TestCase):
    def test_missing_optional_dependencies_no_install(self):
        with patch('trading.ml_models.importlib.import_module',side_effect=ImportError):
            with self.assertRaises(ModelUnavailable):backend('lightgbm')
        with self.assertRaises(ModelUnavailable):backend('unknown')

    def test_no_model_and_disabled_abstain_without_backend(self):
        bars=fixtures();now=bars[-1].end_utc;feature=compute_features(bars,now)
        with patch('trading.ml_models.backend',side_effect=AssertionError('No implicit load')):
            self.assertEqual(ml_signal('minute',feature,now).reason_codes,('strategy_disabled',))
            self.assertEqual(ml_signal('minute',feature,now,enabled=True).action,'ABSTAIN')
            self.assertEqual(ml_signal('minute',feature,now,enabled=True,directory='/missing').reason_codes,('costs_unknown',))
            self.assertEqual(ml_signal('minute',feature,now,enabled=True,directory='/missing',cost_bps=20.).action,'ABSTAIN')

    def test_stale_features_do_not_load_any_model(self):
        bars=fixtures();now=bars[-1].end_utc;feature=compute_features(bars,now)
        with patch('trading.ml_models.predict',side_effect=AssertionError('No inference on stale data')):
            with self.assertRaises(ValueError):
                ml_signal('minute',feature,now+timedelta(minutes=4),enabled=True,directory='unused',cost_bps=20.)

    def test_labels_use_next_open_and_net_costs(self):
        bars=fixtures(100);rows=dataset(bars,label_span=3,cost_bps=20.)
        self.assertAlmostEqual(rows[0].target_net_return,float(bars[11].close/bars[9].open)-1-.002)
        self.assertEqual(rows[0].label_end_utc,bars[11].end_utc)
        self.assertEqual(rows[0].features,compute_features(bars[:9],bars[8].end_utc))

    def test_training_refuses_label_leakage_before_loading_library(self):
        rows=dataset(fixtures(100),label_span=5,cost_bps=20.)
        w=WalkForwardWindow(tuple(range(20)),tuple(range(20,30)),tuple(range(30,40)))
        with tempfile.TemporaryDirectory() as d,patch('trading.training.backend',side_effect=AssertionError('No training')):
            with self.assertRaises(ValueError):fit_window(rows,w,family='lightgbm',directory=d+'/model',label_span=5)
