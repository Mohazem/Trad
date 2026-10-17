"""Chronological long-only research replay; never a broker or fill guarantee."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime
import math
from pathlib import Path
from .features import compute_features
from .market_history import available_series
from .models import canonical, utc
from .signals import finite, identifier


@dataclass(frozen=True)
class WalkForwardWindow:
    train: tuple[int, ...]
    validation: tuple[int, ...]
    test: tuple[int, ...]


def walk_forward(size, train, validation, test, *, label_span=1, embargo=1):
    """Index split; purge label_span bars before each held-out block.

    Targets at i use i+label_span. Embargo adds unused bars between partitions.
    Each test block has a disjoint index interval; expanding train uses past only.
    """
    if any(type(x) is not int or x < 1 for x in (size, train, validation, test, label_span)) or type(embargo) is not int or embargo < 0:
        raise ValueError('Invalid chronological partition')
    gap = label_span + embargo
    windows = []
    start = train + gap + validation + gap
    while start + test <= size:
        val_end = start-gap
        val_start = val_end-validation
        train_end = val_start-gap
        windows.append(WalkForwardWindow(tuple(range(train_end)), tuple(range(val_start,val_end)), tuple(range(start,start+test))))
        start += test
    if not windows:
        raise ValueError('Insufficient data for purged chronological splits')
    return tuple(windows)


@dataclass(frozen=True)
class BacktestResult:
    strategy_id: str
    data_kind: str
    source: str
    start_utc: datetime
    end_utc: datetime
    observations: int
    metrics: tuple[tuple[str, float | None], ...]
    equity_curve: tuple[float, ...]
    assumptions: tuple[str, ...]

    def __post_init__(self):
        identifier(self.strategy_id)
        identifier(self.source)
        if self.data_kind not in {'synthetic', 'historical'}:
            raise ValueError('Invalid backtest provenance')
        if utc(self.start_utc) >= utc(self.end_utc) or type(self.observations) is not int or self.observations < 2:
            raise ValueError('Invalid backtest range')
        for _, value in self.metrics:
            finite(value, optional=True)
        for value in self.equity_curve:
            finite(value, lower=0)


def _metrics(curve, fees, turnover, trades, attempts, exposure, periods_per_year, partials):
    changes = [b/a-1 for a,b in zip(curve,curve[1:])]
    avg = sum(changes)/len(changes) if changes else 0.
    var = sum((r-avg)**2 for r in changes)/len(changes) if changes else 0.
    std = math.sqrt(var)
    downside = math.sqrt(sum(min(0,r)**2 for r in changes)/len(changes)) if changes else 0.
    peak, drawdown = curve[0],0.
    for value in curve:
        peak=max(peak,value);drawdown=max(drawdown,1-value/peak)
    scale = math.sqrt(periods_per_year) if periods_per_year else None
    values = dict(net_return=curve[-1]/curve[0]-1,max_drawdown=drawdown,
        volatility=std,annualized_volatility=std*scale if scale else None,sharpe=avg/std*scale if scale and std else None,
        sortino=avg/downside*scale if scale and downside else None,turnover=turnover/curve[0],
        fees_usd=fees,execution_rate=trades/attempts if attempts else 0.,exposure=exposure/max(1,len(changes)),
        trades=float(trades),partial_fills=float(partials),observations=float(len(curve)),
        first_half_return=curve[len(curve)//2]/curve[0]-1,
        second_half_return=curve[-1]/curve[len(curve)//2]-1)
    return tuple(values.items())


def run_backtest(bars, strategy='causal_momentum', *, fee_bps=5., spread_bps=10.,
                 slippage_bps=5., initial_cash=50., periods_per_year=None, policy=None,
                 max_volume_participation=.01, min_notional=1.):
    """Decide at closed bar t; execute only at next bar open.

    policy receives FeatureSnapshot only, never future bars. SELL closes the long.
    No book data: fills are an explicit approximation, no partial-fill simulation.
    """
    bars=tuple(bars)
    if not bars:
        raise ValueError('No history')
    bars=available_series(bars,max(b.received_at_utc for b in bars))
    if len(bars)<11 or strategy not in {'cash','buy_and_hold','causal_momentum','custom'}:
        raise ValueError('Invalid backtest request')
    if strategy=='custom' and policy is None:
        raise ValueError('Explicit policy required')
    for v in (fee_bps,spread_bps,slippage_bps):finite(v,lower=0,upper=1000)
    finite(initial_cash,lower=.01,upper=50)
    if periods_per_year is not None:finite(periods_per_year,lower=1,upper=1e7)
    cash, qty, fees, turnover = float(initial_cash),0.,0.,0.
    trades,attempts,exposure,partials=0,0,0.,0
    finite(max_volume_participation,lower=.000001,upper=.1)
    finite(min_notional,lower=.00000001,upper=45)
    curve=[cash]
    fee=fee_bps/10000;impact=(spread_bps/2+slippage_bps)/10000
    for i in range(8,len(bars)-1):
        decision_time=bars[i].end_utc
        features=compute_features(bars[:i+1],decision_time)
        if features.data_end_utc != decision_time:
            action='ABSTAIN'  # last candle not yet received
        elif strategy=='cash':action='HOLD'
        elif strategy=='buy_and_hold':action='BUY' if not qty else 'HOLD'
        elif strategy=='causal_momentum':action='BUY' if dict(features.values)['return_8']>0 else 'SELL'
        else:action=policy(features)
        if action not in {'BUY','SELL','HOLD','ABSTAIN'}:raise ValueError('Invalid strategy output')
        nxt=bars[i+1]
        price=float(nxt.open)
        if (action=='BUY' and not qty) or (action=='SELL' and qty):
            attempts+=1
            if nxt.volume>0:
                fill_price=price*(1+impact if action=='BUY' else 1-impact)
                requested=min(45.,cash)/(1+fee)/fill_price if action=='BUY' else qty
                quantity=min(requested,float(nxt.volume)*max_volume_participation)
                quantity=math.floor(quantity*1e8)/1e8
                notional=quantity*fill_price
                if notional>=min_notional:
                    if action=='BUY':cash-=notional*(1+fee);qty+=quantity
                    else:cash+=notional*(1-fee);qty-=quantity
                    fees+=notional*fee;turnover+=notional;trades+=1
                    if quantity<requested-1e-8:partials+=1
        value=cash+qty*float(nxt.close)
        curve.append(value);exposure+=qty*float(nxt.close)/value if value else 0
    # No artificial liquidation: open inventory is marked at the final close.
    assumptions=('Next-open execution after closed-bar signal; long-only; starting cash <=50 USD.',
        'Purchase cash outlay <=45 USD; no leverage; one instrument.',
        f'fee_bps={fee_bps}; full_spread_bps={spread_bps}; slippage_bps={slippage_bps}.',
        f'Conservative bar-volume proxy: participation <= {max_volume_participation}; partial fills; min_notional={min_notional}; 8 quantity decimals. Not broker lot rules.',
        'No order-book evidence; no claim that observed candle volume was executable liquidity.',
        'No forced final liquidation; open inventory marked to last close.',
        f'Annual periods={periods_per_year}; zero risk-free rate; population volatility; downside target zero.',
        'Fixed input universe; no survivorship-free universe claim; synthetic data is not market validation.')
    return BacktestResult(strategy,bars[0].data_kind,bars[0].source,bars[8].end_utc,bars[-1].end_utc,
        len(bars),_metrics(curve,fees,turnover,trades,attempts,exposure,periods_per_year,partials),tuple(curve),assumptions)


def benchmarks(bars, **kwargs):
    return {name:run_backtest(bars,name,**kwargs) for name in ('cash','buy_and_hold','causal_momentum')}


def save_report(results, directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    (directory/'results.json').write_text(canonical({k:asdict(v) for k,v in results.items()})+'\n')
    lines=['# Research replay', '', 'Not a profitability claim. Provenance and approximations follow.', '']
    for name,result in results.items():
        lines += [f'## {name} ({result.data_kind}, {result.source})','',canonical(dict(result.metrics)), '']
        lines += [f'- {a}' for a in result.assumptions]+['']
    (directory/'report.md').write_text('\n'.join(lines))



def run_model_backtest(bars, model_directory, **kwargs):
    """Frozen model only, evaluated strictly after its validation label end."""
    from dataclasses import replace
    from .ml_models import ModelPredictor, action_from_prediction
    loaded=ModelPredictor(model_directory)
    costs=2*kwargs.get('fee_bps',5.)+kwargs.get('spread_bps',10.)+2*kwargs.get('slippage_bps',5.)
    if costs>loaded.manifest.target_cost_bps:
        raise ValueError('Backtest costs exceed model calibration assumptions')
    bars=tuple(bars)
    eligible=[i for i,b in enumerate(bars) if b.end_utc>loaded.manifest.validated_until_utc]
    if not eligible or eligible[0]<8:
        raise ValueError('Insufficient point-in-time context or out-of-sample data')
    subset=bars[eligible[0]-8:]
    def policy(features):
        value=loaded.predict(features)
        return action_from_prediction(value,loaded.manifest.threshold)
    result=run_backtest(subset,'custom',policy=policy,**kwargs)
    return replace(result,strategy_id=loaded.manifest.strategy_id+'_ml',
        assumptions=result.assumptions+(f'Frozen model {loaded.manifest.model_id}, version {loaded.manifest.model_version}; evaluation begins after validation labels.',))
