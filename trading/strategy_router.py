"""Research-only arbitration. No calls to Ledger, submit or broker methods."""
from dataclasses import asdict
import threading
from .config import MultiHorizonConfig
from .risk_gate import assess
from .signals import RiskDecision


class StrategyRouter:
    def __init__(self, config=None, *, store=None):
        self.config=config or MultiHorizonConfig()
        self.store=store
        self.lock=threading.Lock()

    def route(self, signals, now, *, account=None, advisory=None, packet=None):
        """Fast path needs no LLM. Optional deep analysis never authorizes orders."""
        with self.lock:
            signals=tuple(signals)
            for s in signals:
                s.validate_at(now)
                if self.store is not None:self.store.record(s,now)
            directions={s.action for s in signals if s.action in {'BUY','SELL'}}
            if len(directions)>1:
                decisions=[RiskDecision(s.signal_id,False,('conflicting_horizons',)) for s in signals]
            else:
                decisions=[];chosen=False
                # Stable priority order, never sum independent strategy orders.
                ordered=sorted(signals,key=lambda s:({'minute':0,'swing':1,'portfolio':2}[s.strategy_id],s.signal_id))
                for s in ordered:
                    decision=assess(s,now,self.config,account)
                    if decision.allowed and chosen:
                        decision=RiskDecision(s.signal_id,False,('global_single_proposal',))
                    if decision.allowed:chosen=True
                    decisions.append(decision)
            deep=None
            if advisory is not None:
                if packet is None:raise ValueError('Deep analysis needs validated market evidence')
                deep=asdict(advisory.evaluate(packet,now,signals=signals))
                for index,decision in enumerate(decisions):
                    if decision.allowed:
                        chosen_signal=next(s for s in signals if s.signal_id==decision.signal_id)
                        final=deep.get('records',{}).get('synthesizer',{})
                        if (deep['status']!='proposed' or deep['action'].upper()!=chosen_signal.action or
                                final.get('symbol')!=chosen_signal.symbol):
                            decisions[index]=RiskDecision(decision.signal_id,False,('deep_analysis_veto',))
            return {'mode':'research_only','signals':[asdict(s) for s in signals],
                    'risk_decisions':[asdict(d) for d in decisions], 'deep_analysis':deep,
                    'execution':{'status':'disabled','reason':'paper_engine_missing','armed':False}}
