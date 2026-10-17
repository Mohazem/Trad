"""Optional real-library smoke tests: explicitly skipped without native packages."""
import importlib.util
import tempfile
import unittest
from trading.backtest import walk_forward, run_model_backtest, benchmarks
from trading.training import dataset, fit_window
from trading.ml_models import predict, ModelUnavailable, ml_signal
from tests.test_history import fixtures

class NativeMLTests(unittest.TestCase):
    def check_family(self,family,timeframe):
        bars=fixtures(180,timeframe=timeframe)
        rows=dataset(bars,label_span=2,cost_bps=20.)
        w=walk_forward(len(rows),50,20,15,label_span=2,embargo=2)[0]
        with tempfile.TemporaryDirectory() as d:
            manifest,evaluation=fit_window(rows,w,family=family,directory=d+'/model',label_span=2,cost_bps=20.)
            loaded,prediction=predict(d+'/model',rows[w.test[0]].features)
            self.assertEqual(manifest,loaded)
            self.assertEqual(evaluation['test_samples'],len(w.test))
            self.assertIsInstance(prediction,float)
            with self.assertRaises(ModelUnavailable):predict(d+'/model',rows[w.train[-1]].features)
            feature=rows[w.test[0]].features
            result=ml_signal(manifest.strategy_id,feature,feature.as_of_utc,enabled=True,directory=d+'/model',cost_bps=25.)
            self.assertEqual(result.reason_codes,('costs_exceed_calibration',))
            research=run_model_backtest(bars,d+'/model',fee_bps=0.,spread_bps=0.,slippage_bps=0.)
            self.assertGreater(research.start_utc,manifest.validated_until_utc)
            idx=next(i for i,b in enumerate(bars) if b.end_utc==research.start_utc)
            comparison=benchmarks(bars[idx-8:],fee_bps=0.,spread_bps=0.,slippage_bps=0.)
            self.assertEqual(comparison['cash'].start_utc,research.start_utc)
            self.assertEqual(comparison['cash'].end_utc,research.end_utc)
            from pathlib import Path
            artifact=Path(d)/'model'/'model.json';artifact.write_text('corrupt')
            with self.assertRaises(ModelUnavailable):predict(d+'/model',feature)

    @unittest.skipUnless(importlib.util.find_spec('lightgbm'),'Optional LightGBM is not installed')
    def test_lightgbm_native_synthetic_training_inference(self):self.check_family('lightgbm','1m')

    @unittest.skipUnless(importlib.util.find_spec('xgboost'),'Optional XGBoost is not installed')
    def test_xgboost_native_synthetic_training_inference(self):self.check_family('xgboost','1d')
