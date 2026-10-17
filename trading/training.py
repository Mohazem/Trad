"""Explicit offline training with purged train/validation/test partitions."""
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import importlib.metadata
from pathlib import Path
from .backtest import walk_forward
from .features import FEATURE_NAMES, FEATURE_VERSION, compute_features
from .market_history import available_series
from .ml_models import ModelManifest, backend
from .models import canonical
from .signals import finite, fingerprint


@dataclass(frozen=True)
class TrainingRow:
    features: object
    label_end_utc: datetime
    target_net_return: float
    label_span: int
    cost_bps: float

    def __post_init__(self):
        from .models import utc
        if utc(self.label_end_utc) <= self.features.as_of_utc:
            raise ValueError('Label must end after decision')
        finite(self.target_net_return)
        finite(self.cost_bps,lower=0,upper=10000)
        if type(self.label_span) is not int or self.label_span<1:
            raise ValueError('Invalid label horizon')


def dataset(bars, *, label_span, cost_bps):
    if type(label_span) is not int or label_span<1:raise ValueError('Invalid label span')
    finite(cost_bps,lower=0,upper=10000)
    bars=tuple(bars)
    if not bars:raise ValueError('No training history')
    bars=available_series(bars,max(b.received_at_utc for b in bars))
    rows=[]
    for i in range(8,len(bars)-label_span-1):
        f=compute_features(bars[:i+1],bars[i].end_utc)
        if f.data_end_utc!=bars[i].end_utc:continue
        # Entry at next open; exit after label_span complete holding bars.
        target=float(bars[i+label_span].close/bars[i+1].open)-1-cost_bps/10000
        rows.append(TrainingRow(f,bars[i+label_span].end_utc,target,label_span,cost_bps))
    return tuple(rows)


def fit_window(rows, window, *, family, directory, seed=7, cost_bps=20., label_span=1):
    """Fits train only, calibrates on validation only, reports untouched test.

    Library parameters are fixed here; a new experiment/version is required to
    tune them. Caller must not select experiments by their test score.
    """
    if not rows:raise ValueError('Empty dataset')
    if family not in {'lightgbm','xgboost'}:raise ValueError('Unknown family')
    finite(cost_bps,lower=0,upper=10000)
    if type(seed) is not int or not 0<=seed<=2**31-1:raise ValueError('Invalid seed')
    blocks=[tuple(getattr(window,k)) for k in ('train','validation','test')]
    if any(not b or tuple(sorted(set(b)))!=b or b[0]<0 or b[-1]>=len(rows) for b in blocks):
        raise ValueError('Invalid split indices')
    train,val,test=([rows[i] for i in b] for b in blocks)
    if (train[-1].label_end_utc>=val[0].features.as_of_utc or
            val[-1].label_end_utc>=test[0].features.as_of_utc):
        raise ValueError('Label leakage across partitions')
    first=rows[0].features
    if any((r.features.symbol,r.features.source,r.features.timeframe,r.features.data_kind,r.features.feature_version)!=
           (first.symbol,first.source,first.timeframe,first.data_kind,first.feature_version) for r in rows):
        raise ValueError('Mixed training provenance')
    if any(r.label_span!=label_span or r.cost_bps!=cost_bps for r in rows):
        raise ValueError('Manifest label/cost assumptions mismatch')
    if any(a.features.as_of_utc>=b.features.as_of_utc for a,b in zip(rows,rows[1:])):
        raise ValueError('Training data not strictly chronological')
    module=backend(family)
    import numpy as np
    x=lambda block:np.asarray([[v for _,v in r.features.values] for r in block],dtype=np.float64)
    y=lambda block:[r.target_net_return for r in block]
    if family=='lightgbm':
        params={'objective':'regression','seed':seed,'num_threads':1,'num_leaves':7,'min_data_in_leaf':3,
                'deterministic':True,'force_col_wise':True,'verbosity':-1}
        model=module.train(params,module.Dataset(x(train),label=y(train),feature_name=list(FEATURE_NAMES)),num_boost_round=20)
        forecast=lambda block:model.predict(x(block))
    else:
        params={'objective':'reg:squarederror','seed':seed,'nthread':1,'max_depth':3,'eta':.1}
        matrix=lambda block:module.DMatrix(x(block),label=y(block),feature_names=list(FEATURE_NAMES))
        model=module.train(params,matrix(train),num_boost_round=20)
        forecast=lambda block:model.predict(matrix(block))
    validation_predictions=[float(v) for v in forecast(val)]
    test_predictions=[float(v) for v in forecast(test)]
    for value in validation_predictions+test_predictions:finite(value)
    # Threshold selected ONLY on validation net targets; includes no-trade option.
    candidates=sorted({0.,*(max(0.,p) for p in validation_predictions)})
    threshold=max(candidates,key=lambda t:(sum(r.target_net_return for r,p in zip(val,validation_predictions) if p>t),t))
    out=Path(directory);out.mkdir(parents=True,exist_ok=False)
    artifact=out/'model.json'
    model.save_model(str(artifact))
    digest=fingerprint([asdict(r) for r in rows])
    versions={family:module.__version__}
    for name in ('numpy','scipy'):
        try:versions[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:pass
    strategy='minute' if family=='lightgbm' else 'swing'
    manifest=ModelManifest('model-'+digest[:20],'v1',family,strategy,first.timeframe,FEATURE_VERSION,FEATURE_NAMES,
        max(r.label_end_utc for r in train),max(r.label_end_utc for r in val),digest,first.data_kind,first.source,
        hashlib.sha256(artifact.read_bytes()).hexdigest(),seed,
        tuple(sorted((k,canonical(v)) for k,v in {**params,'num_boost_round':20}.items())),
        tuple(sorted(versions.items())),threshold,label_span,cost_bps,len(val))
    (out/'manifest.json').write_text(canonical(asdict(manifest))+'\n')
    evaluation={'data_kind':first.data_kind,'threshold_selected_on':'validation_only',
        'test_samples':len(test),'test_mse':sum((p-r.target_net_return)**2 for p,r in zip(test_predictions,test))/len(test),
        'test_predictions':test_predictions,'warning':'Regression error is not trading performance; test set must not select models.'}
    (out/'evaluation.json').write_text(canonical(evaluation)+'\n')
    return manifest,evaluation


def train_walk_forward(bars, *, family, directory, train_size=60, validation_size=20, test_size=20,
                       label_span=1, embargo=1, cost_bps=20., seed=7):
    rows=dataset(bars,label_span=label_span,cost_bps=cost_bps)
    windows=walk_forward(len(rows),train_size,validation_size,test_size,label_span=label_span,embargo=embargo)
    output=Path(directory);output.mkdir(parents=True,exist_ok=False)
    return tuple(fit_window(rows,w,family=family,directory=output/f'window-{i:03}',seed=seed,
                            cost_bps=cost_bps,label_span=label_span) for i,w in enumerate(windows))
