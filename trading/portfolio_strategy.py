"""Constrained monthly research proposal; tradability must be supplied explicitly."""
from datetime import datetime
from decimal import Decimal
from .models import utc
from .signals import StrategySignal


def portfolio_signal(features, now, *, enabled=False, tradable=False, already_rebalanced_month=None, cost_bps=None):
    now=utc(now);month=now.strftime('%Y-%m')
    action='ABSTAIN';reason='strategy_disabled'
    if enabled:
        if not tradable:reason='paper_tradability_unverified'
        elif features.timeframe not in {'1d','1w'}:reason='incompatible_timeframe'
        elif already_rebalanced_month==month:action='HOLD';reason='monthly_cadence'
        elif cost_bps is None:reason='costs_unknown'
        else:action='HOLD';reason='single_asset_universe_no_diversification'
    # Only BTC is verified in the existing code. Never invent a tradable universe
    # or call a one-asset weight an optimized portfolio.
    return StrategySignal.create(strategy_id='portfolio',symbol=features.symbol,horizon='months',as_of_utc=now,
        data_end_utc=features.data_end_utc,action=action,score=None,confidence=None,
        model_id='rules:monthly-constrained',model_version='v1',feature_version=features.feature_version,
        reason_codes=(reason,),estimated_cost_bps=cost_bps).validate_at(now)


def constrained_weights(symbols, *, max_concentration=Decimal('0.5')):
    """Explicit equal-weight baseline, residual weight is cash; no expected return."""
    from .signals import CRYPTO_SYMBOLS
    if not symbols or len(set(symbols))!=len(symbols) or any(s not in CRYPTO_SYMBOLS for s in symbols):
        raise ValueError('Unverified allocation universe')
    if not isinstance(max_concentration,Decimal) or not max_concentration.is_finite() or not 0<max_concentration<=1:
        raise ValueError('Invalid concentration')
    weight=min(Decimal(1)/len(symbols),max_concentration)
    return tuple((s,weight) for s in sorted(symbols))
