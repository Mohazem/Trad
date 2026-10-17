"""Explicit offline research commands; imports only configured local datasets."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from .backtest import benchmarks, save_report, run_model_backtest
from .config import MultiHorizonConfig
from .features import compute_features
from .market_history import MarketObservation
from .ml_models import ModelUnavailable, ml_signal
from .models import canonical
from .portfolio_strategy import portfolio_signal
from .strategy_router import StrategyRouter
from .training import train_walk_forward
from .ai_validation import strict_json


def load_history(path):
    with open(path,encoding='utf-8') as stream:
        return tuple(MarketObservation.from_dict(strict_json(line)) for line in stream if line.strip())


def research_signal(bars, strategy, now, config, *, model_directory=None, cost_bps=None):
    enabled=config.enabled and getattr(config,strategy+'_enabled')
    try:
        features=compute_features(bars,now)
        if strategy=='portfolio':
            # No authoritative Paper tradability adapter exists in this checkout.
            signal=portfolio_signal(features,now,enabled=enabled,cost_bps=cost_bps)
        else:
            signal=ml_signal(strategy,features,now,enabled=enabled,directory=model_directory,cost_bps=cost_bps)
        result=StrategyRouter(config).route([signal],now)
        result['data_kind']=features.data_kind
        result['source']=features.source
        return result
    except ValueError:
        # No forged "current" data timestamp when sources are stale or incomplete.
        return {'mode':'research_only','data_kind':bars[0].data_kind if bars else 'unknown',
                'signals':[],'risk_decisions':[],'reason':'data_or_model_validation_failed',
                'execution':{'status':'disabled','reason':'paper_engine_missing','armed':False}}


def main():
    parser=argparse.ArgumentParser(description='Offline multi-horizon research; never sends orders')
    commands=parser.add_subparsers(dest='command',required=True)
    fixture=commands.add_parser('fixture',help='Generate explicitly synthetic OHLCV for software checks')
    fixture.add_argument('--output',required=True);fixture.add_argument('--bars',type=int,default=240)
    fixture.add_argument('--timeframe',choices=['1m','4h','1d'],default='1m')
    for name in ('backtest','train','signal'):
        p=commands.add_parser(name);p.add_argument('--input',required=True);p.add_argument('--output',required=True)
        if name=='backtest':
            p.add_argument('--fee-bps',type=float,default=5.);p.add_argument('--spread-bps',type=float,default=10.)
            p.add_argument('--slippage-bps',type=float,default=5.);p.add_argument('--periods-per-year',type=float)
            p.add_argument('--model-dir',help='Explicit frozen native model; evaluate only after validation')
        elif name=='train':
            p.add_argument('--family',choices=['lightgbm','xgboost'],required=True)
            p.add_argument('--label-span',type=int,default=1);p.add_argument('--embargo',type=int,default=1)
            p.add_argument('--train-size',type=int,default=60);p.add_argument('--validation-size',type=int,default=20)
            p.add_argument('--test-size',type=int,default=20);p.add_argument('--seed',type=int,default=7)
            p.add_argument('--cost-bps',type=float,required=True)
        else:
            p.add_argument('--strategy',choices=['minute','swing','portfolio'],required=True)
            p.add_argument('--config');p.add_argument('--model-dir');p.add_argument('--cost-bps',type=float)
            p.add_argument('--signal-store',help='Explicit SQLite research audit file')
            p.add_argument('--as-of',help='Explicit historical decision time (aware ISO8601); defaults to current UTC')
    args=parser.parse_args()
    try:
        if args.command=='fixture':
            if not 11<=args.bars<=10000:raise ValueError('Fixture size must be 11..10000')
            from .market_history import TIMEFRAMES
            span=timedelta(seconds=TIMEFRAMES[args.timeframe]);start=datetime(2026,1,1,tzinfo=timezone.utc)
            rows=[]
            for i in range(args.bars):
                price=Decimal('100')+Decimal(i%40)/10
                rows.append(MarketObservation('BTC/USD',args.timeframe,start+i*span,start+(i+1)*span,
                    price,price+1,price-1,price,Decimal(10+i%7),'synthetic-cli-v1',data_kind='synthetic'))
            with open(args.output,'x',encoding='utf-8') as output:
                for row in rows:output.write(canonical(asdict(row))+'\n')
            print(f'{len(rows)} synthetic observations written')
            return
        bars=load_history(args.input)
        if args.command=='backtest':
            kwargs=dict(fee_bps=args.fee_bps,spread_bps=args.spread_bps,
                slippage_bps=args.slippage_bps,periods_per_year=args.periods_per_year)
            results=benchmarks(bars,**kwargs)
            if args.model_dir:
                ml=run_model_backtest(bars,args.model_dir,**kwargs)
                # Benchmarks must cover the exact same held-out interval as ML.
                first=next(i for i,b in enumerate(bars) if b.end_utc==ml.start_utc)
                results=benchmarks(bars[first-8:],**kwargs)
                results[ml.strategy_id]=ml
            save_report(results,args.output)
        elif args.command=='train':
            train_walk_forward(bars,family=args.family,directory=args.output,train_size=args.train_size,
                validation_size=args.validation_size,test_size=args.test_size,label_span=args.label_span,
                embargo=args.embargo,cost_bps=args.cost_bps,seed=args.seed)
        else:
            cfg=MultiHorizonConfig.load(args.config) if args.config else MultiHorizonConfig()
            now=datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc)
            report=research_signal(bars,args.strategy,now,cfg,model_directory=args.model_dir,cost_bps=args.cost_bps)
            if args.signal_store:
                from .signals import SignalStore
                from .strategy_status import parse_signal
                store=SignalStore(args.signal_store)
                try:
                    for raw in report['signals']:store.record(parse_signal(raw),now)
                finally:store.close()
            with open(args.output,'x',encoding='utf-8') as output:output.write(canonical(report)+'\n')
        print('Research output written; execution disabled')
    except (OSError,ValueError,ModelUnavailable):
        parser.exit(1,'Research failed: check data quality, configuration, output path and optional dependencies. No order sent.\n')


if __name__=='__main__':main()
