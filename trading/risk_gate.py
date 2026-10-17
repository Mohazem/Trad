"""Deterministic research gate. No broker transport or arming capability."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from .models import decimal, utc, fixed_decimal
from .signals import CRYPTO_SYMBOLS, RiskDecision, StrategyAllocation, STRATEGIES, StrategySignal

GLOBAL_CAP=Decimal('50')
PURCHASE_CAP=Decimal('45')
MAX_POSITIONS=1


@dataclass(frozen=True)
class PaperAccountView:
    """Must be supplied by a future reconciled Paper adapter, never a local ledger.

    This contract alone cannot certify provenance. No such adapter exists here.
    All callers must treat a missing authoritative view as a veto.
    """
    observed_at_utc: datetime
    cash_usd: Decimal
    positions: tuple[tuple[str, Decimal], ...]
    reconciled: bool = False
    armed: bool = False
    halted: bool = True
    pending_orders: bool = False
    uncertain_order: bool = False
    source: str = 'unavailable'

    def __post_init__(self):
        object.__setattr__(self,'observed_at_utc',utc(self.observed_at_utc))
        if not isinstance(self.cash_usd,Decimal) or decimal(self.cash_usd)<0:
            raise ValueError('Invalid cash')
        if type(self.positions) is not tuple or len({s for s,_ in self.positions})!=len(self.positions):
            raise ValueError('Invalid positions')
        for symbol,value in self.positions:
            if symbol not in CRYPTO_SYMBOLS or not isinstance(value,Decimal) or decimal(value)<=0:
                raise ValueError('Unknown/invalid account position')
        for name in ('reconciled','armed','halted','pending_orders','uncertain_order'):
            if type(getattr(self,name)) is not bool:raise ValueError('Invalid account status')


@fixed_decimal
def assess(signal, now, config, account=None):
    if not isinstance(signal,StrategySignal):raise ValueError('Typed StrategySignal required')
    def deny(code):return RiskDecision(signal.signal_id,False,(code,))
    try:signal.validate_at(now)
    except (ValueError,TypeError):return deny('stale_or_invalid_signal')
    if not config.enabled or not getattr(config,signal.strategy_id+'_enabled'):
        return deny('strategy_disabled')
    if signal.action not in {'BUY','SELL'}:return deny('no_order_proposal')
    if signal.estimated_cost_bps is None:return deny('costs_unknown')
    if account is None:return deny('authoritative_account_unavailable')
    if account.source!='alpaca_paper_authoritative' or not account.reconciled:
        return deny('account_not_reconciled')
    if not 0<=(utc(now)-account.observed_at_utc).total_seconds()<=30:
        return deny('account_stale')
    if account.uncertain_order:return deny('uncertain_order_no_resubmit')
    if account.pending_orders:return deny('pending_order')
    if account.halted:return deny('account_halted')
    if not account.armed:return deny('paper_disarmed')
    if len(account.positions)>MAX_POSITIONS:return deny('position_limit')
    held=dict(account.positions)
    if signal.action=='SELL':
        if signal.symbol not in held:return deny('no_short')
        return RiskDecision(signal.signal_id,True,('research_gate_only',),held[signal.symbol])
    if account.positions:return deny('position_limit')
    budget=getattr(config,signal.strategy_id+'_budget')
    exposure=sum(held.values(),Decimal('0'))
    # Cash outlay includes the cost estimate, avoiding cost-created cap breaches.
    outlay=min(PURCHASE_CAP,GLOBAL_CAP-exposure,budget,account.cash_usd)
    if outlay<=0:return deny('budget_exhausted')
    notional=outlay/(1+Decimal(str(signal.estimated_cost_bps))/10000)
    return RiskDecision(signal.signal_id,True,('research_gate_only',),notional)
