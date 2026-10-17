from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import tempfile
import unittest
from unittest.mock import patch
from trading.config import Config, MultiHorizonConfig
from trading.risk_gate import PaperAccountView, assess
from trading.strategy_router import StrategyRouter
from trading.portfolio_strategy import portfolio_signal, constrained_weights
from trading.features import compute_features
from tests.test_history import START, fixtures, signal

class RiskTests(unittest.TestCase):
    def setUp(self):
        self.cfg=MultiHorizonConfig(enabled=True,minute_enabled=True,swing_enabled=True)
        self.account=PaperAccountView(START,D(100),(),True,True,False,source='alpaca_paper_authoritative')

    def test_defaults_and_no_live_or_additive_budgets(self):
        self.assertFalse(MultiHorizonConfig().enabled)
        for kw in [dict(paper_only=False),dict(minute_budget=D(50)),dict(enabled='true')]:
            with self.assertRaises(ValueError):MultiHorizonConfig(**kw)
        self.assertEqual(Config().initial_cash,D(100000))

    def test_disarmed_uncertain_pending_halted_stale_and_missing_account(self):
        s=signal(START)
        self.assertEqual(assess(s,START,self.cfg).reason_codes,('authoritative_account_unavailable',))
        for kw,code in [(dict(armed=False),'paper_disarmed'),(dict(uncertain_order=True),'uncertain_order_no_resubmit'),
                        (dict(pending_orders=True),'pending_order'),(dict(halted=True),'account_halted'),
                        (dict(observed_at_utc=START-timedelta(seconds=31)),'account_stale')]:
            self.assertEqual(assess(s,START,self.cfg,replace(self.account,**kw)).reason_codes,(code,))

    def test_global_budget_single_position_no_short(self):
        decision=assess(signal(START),START,self.cfg,self.account)
        self.assertTrue(decision.allowed);self.assertLessEqual(decision.approved_notional,D(15))
        held=replace(self.account,positions=(('BTC/USD',D(30)),))
        self.assertFalse(assess(signal(START),START,self.cfg,held).allowed)
        self.assertFalse(assess(signal(START,action='SELL'),START,self.cfg,self.account).allowed)

    def test_router_conflicts_and_fast_path_without_ollama(self):
        buy=signal(START);sell=signal(START,strategy_id='swing',horizon='days',action='SELL')
        with patch('socket.socket',side_effect=AssertionError('No network')):
            result=StrategyRouter(self.cfg).route([buy,sell],START,account=self.account)
            self.assertTrue(all(d['reason_codes']==('conflicting_horizons',) for d in result['risk_decisions']))
            result=StrategyRouter(self.cfg).route([buy],START,account=self.account)
            self.assertEqual(result['execution']['reason'],'paper_engine_missing')
            self.assertFalse(result['execution']['armed'])

    def test_portfolio_never_invents_verified_assets_or_allocations(self):
        b=fixtures(timeframe='1d');now=b[-1].end_utc;f=compute_features(b,now)
        self.assertEqual(portfolio_signal(f,now,enabled=True).reason_codes,('paper_tradability_unverified',))
        self.assertEqual(constrained_weights(['BTC/USD']),(('BTC/USD',D('.5')),))
        with self.assertRaises(ValueError):constrained_weights(['ETH/USD'])


class DeepVetoTests(unittest.TestCase):
    setUp = RiskTests.setUp
    def test_deep_abstention_vetoes_research_proposal(self):
        from trading.ai_pipeline import PipelineResult
        class Abstain:
            def evaluate(self,*args,**kw):
                return PipelineResult('abstained','inconsistent_roles','abstain','fixture')
        result=StrategyRouter(self.cfg).route([signal(START)],START,account=self.account,advisory=Abstain(),packet={})
        self.assertEqual(result['risk_decisions'][0]['reason_codes'],('deep_analysis_veto',))
        self.assertEqual(result['execution']['status'],'disabled')
