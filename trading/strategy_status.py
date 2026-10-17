"""Read-only dashboard projection of explicitly produced research artifacts."""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from .ai_validation import strict_json
from .config import MultiHorizonConfig
from .signals import STRATEGIES, StrategySignal
from .ml_models import ModelManifest


def parse_signal(raw):
    raw=dict(raw)
    for key in ('as_of_utc','data_end_utc'):
        if isinstance(raw[key],str):raw[key]=datetime.fromisoformat(raw[key])
    raw['reason_codes']=tuple(raw['reason_codes'])
    return StrategySignal(**raw)


def bounded_read(path):
    with open(path,'rb') as stream:
        return strict_json(stream.read(1048577).decode('utf-8'),limit=1048576)


class StrategyStatus:
    def __init__(self, config=None, directory='outputs/multi-horizon'):
        self.config=config or MultiHorizonConfig()
        self.directory=Path(directory)

    def read(self,now=None):
        now=now or datetime.now(timezone.utc)
        rows=[]
        for strategy,horizon in STRATEGIES.items():
            row={'strategy_id':strategy,'horizon':horizon,
                'enabled':self.config.enabled and getattr(self.config,strategy+'_enabled'),
                'candidate_model':{'minute':'lightgbm','swing':'xgboost','portfolio':'rules:monthly-constrained'}[strategy],
                'model_version':None,'last_signal':None,'last_data':None,'out_of_sample_metrics':None,
                'budget_ceiling_usd':str(getattr(self.config,strategy+'_budget')),'alerts':[],
                'mode':'research_only'}
            path=self.directory/(strategy+'.json')
            if path.exists():
                try:
                    report=bounded_read(path)
                    if report.get('mode')!='research_only' or report.get('data_kind') not in {'synthetic','historical'}:
                        raise ValueError('Invalid provenance')
                    row['data_kind']=report['data_kind']
                    signals=report.get('signals',[])
                    if signals:
                        s=parse_signal(signals[-1])
                        if s.strategy_id!=strategy:raise ValueError('Wrong horizon report')
                        row['last_signal']=asdict(s);row['last_data']=s.data_end_utc.isoformat()
                        row['model_version']=s.model_version
                        row['alerts'].extend(s.reason_codes)
                        try:s.validate_at(now)
                        except ValueError:row['alerts'].append('signal_stale_for_current_time')
                    else:row['alerts'].append('no_valid_signal')
                except (OSError,ValueError,TypeError,KeyError,AttributeError):
                    row['alerts'].append('invalid_research_artifact')
            else:row['alerts'].append('no_research_report')
            model_dir=self.directory/(strategy+'-model')
            if (model_dir/'manifest.json').exists():
                try:
                    manifest=ModelManifest.load(model_dir/'manifest.json')
                    evaluation=bounded_read(model_dir/'evaluation.json')
                    if (manifest.strategy_id!=strategy or evaluation.get('data_kind')!=manifest.data_kind or
                            evaluation.get('threshold_selected_on')!='validation_only'):
                        raise ValueError('Invalid model evaluation provenance')
                    from .signals import finite
                    mse=evaluation['test_mse'];finite(mse,lower=0)
                    count=evaluation['test_samples']
                    if type(count) is not int or count<1:raise ValueError('Missing held-out observations')
                    row['model_version']=manifest.model_version
                    row['out_of_sample_metrics']={'metric':'regression_mse_not_trading_return','test_mse':mse,
                        'test_samples':count,'data_kind':manifest.data_kind,'model_id':manifest.model_id,
                        'warning':'Held-out regression error; no profitability claim.'}
                except (OSError,ValueError,TypeError,KeyError,AttributeError):row['alerts'].append('invalid_model_evaluation')
            # Backtest benchmarks are separate from true ML out-of-sample results.
            backtests=self.directory/(strategy+'-backtest')/'results.json'
            row['backtest_benchmarks']=None
            if backtests.exists():
                try:
                    report=bounded_read(backtests)
                    allowed={}
                    for name in ('cash','buy_and_hold','causal_momentum'):
                        r=report[name]
                        if r['data_kind'] not in {'synthetic','historical'}:raise ValueError('Bad provenance')
                        from .backtest import BacktestResult
                        validated=BacktestResult(r['strategy_id'],r['data_kind'],r['source'],datetime.fromisoformat(r['start_utc']),
                            datetime.fromisoformat(r['end_utc']),r['observations'],tuple(tuple(p) for p in r['metrics']),
                            tuple(r['equity_curve']),tuple(r['assumptions']))
                        allowed[name]={'data_kind':validated.data_kind,'metrics':dict(validated.metrics),'source':validated.source}
                    row['backtest_benchmarks']=allowed
                except (OSError,ValueError,TypeError,KeyError):row['alerts'].append('invalid_backtest_artifact')
            rows.append(row)
        return {'mode':'research_only','strategies':rows,'enabled':self.config.enabled,
            'paper':{'supported':False,'armed':False,'positions':None,'remaining_budget_usd':None,
                     'global_cap_usd':'50','purchase_cap_usd':'45','max_positions':1,
                     'reason':'paper_engine_missing'},'live':{'supported':False},
            'alerts':['PaperEngine/PaperBroker absent: no order submission is implemented.',
                      'baseline_rules crypto EMA/RSI absent from this checkout.']}
