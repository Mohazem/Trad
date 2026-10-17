"""Optional native model adapters. No pickle, training, network or fallback."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime
import importlib
from pathlib import Path
import json
import re
from .features import FEATURE_NAMES, FEATURE_VERSION
from .models import canonical, utc
from .signals import STRATEGIES, MAX_AGES, StrategySignal, finite, fingerprint, identifier

PINNED = {'lightgbm':'4.6.0','xgboost':'3.0.5'}


class ModelUnavailable(ValueError):
    pass


def backend(name):
    if name not in PINNED:
        raise ModelUnavailable('Unsupported model family')
    try:
        module=importlib.import_module(name)
    except Exception:
        raise ModelUnavailable('Install optional requirements-ml.txt explicitly') from None
    if module.__version__ != PINNED[name]:
        raise ModelUnavailable('Incompatible model library version')
    return module


@dataclass(frozen=True)
class ModelManifest:
    model_id: str
    model_version: str
    family: str
    strategy_id: str
    timeframe: str
    feature_version: str
    feature_names: tuple[str, ...]
    trained_until_utc: datetime
    validated_until_utc: datetime
    data_digest: str
    data_kind: str
    source: str
    artifact_sha256: str
    seed: int
    hyperparameters: tuple[tuple[str, str], ...]
    library_versions: tuple[tuple[str, str], ...]
    threshold: float
    label_span: int
    target_cost_bps: float
    validation_samples: int
    code_version: str = 'research-v1'

    def __post_init__(self):
        for name in ('model_id','model_version','source','feature_version','code_version'):
            identifier(getattr(self,name))
        if (self.family,self.strategy_id,self.timeframe) not in {('lightgbm','minute','1m'),('xgboost','swing','1d'),('xgboost','swing','4h')}:
            raise ValueError('Incompatible strategy/model/timeframe')
        if self.feature_version != FEATURE_VERSION or self.feature_names != FEATURE_NAMES:
            raise ValueError('Incompatible features')
        for name in ('trained_until_utc','validated_until_utc'):
            object.__setattr__(self,name,utc(getattr(self,name)))
        if self.trained_until_utc >= self.validated_until_utc:
            raise ValueError('Training/validation overlap')
        for value in (self.data_digest,self.artifact_sha256):
            if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):
                raise ValueError('Invalid artifact/data hash')
        if self.data_kind not in {'synthetic','historical'}:
            raise ValueError('Invalid data provenance')
        if type(self.seed) is not int or not 0 <= self.seed <= 2**31-1:
            raise ValueError('Invalid seed')
        if any(type(v) is not int or v<1 for v in (self.label_span,self.validation_samples)):
            raise ValueError('Invalid label/validation count')
        finite(self.threshold,lower=0,upper=1e6)
        finite(self.target_cost_bps,lower=0,upper=10000)
        if type(self.hyperparameters) is not tuple or type(self.library_versions) is not tuple:
            raise ValueError('Immutable manifest metadata required')
        for k,v in self.hyperparameters+self.library_versions:
            identifier(k)
            if type(v) is not str or len(v)>1000:raise ValueError('Invalid metadata')
        if dict(self.library_versions).get(self.family) != PINNED[self.family]:
            raise ValueError('Library version missing/mismatch')

    @classmethod
    def load(cls,path):
        from .ai_validation import strict_json
        raw=strict_json(Path(path).read_text())
        for key in ('trained_until_utc','validated_until_utc'):
            raw[key]=datetime.fromisoformat(raw[key])
        raw['feature_names']=tuple(raw['feature_names'])
        for key in ('hyperparameters','library_versions'):
            raw[key]=tuple(tuple(p) for p in raw[key])
        return cls(**raw)


class ModelPredictor:
    """Load a verified native artifact once; validate every point-in-time input."""
    def __init__(self, directory):
        directory=Path(directory)
        try:
            self.manifest=ModelManifest.load(directory/'manifest.json')
            artifact=directory/'model.json'
            import hashlib
            if hashlib.sha256(artifact.read_bytes()).hexdigest()!=self.manifest.artifact_sha256:
                raise ModelUnavailable('Artifact integrity mismatch')
            self.module=backend(self.manifest.family)
            if self.manifest.family=='lightgbm':
                self.native=self.module.Booster(model_file=str(artifact))
            else:
                self.native=self.module.Booster();self.native.load_model(str(artifact))
        except ModelUnavailable:raise
        except Exception:raise ModelUnavailable('Model absent, invalid or incompatible') from None

    def predict(self, features):
        m=self.manifest
        try:
            if (features.feature_version!=m.feature_version or features.timeframe!=m.timeframe or
                    features.data_kind!=m.data_kind or features.source!=m.source or
                    features.data_end_utc<=m.validated_until_utc):
                raise ModelUnavailable('Manifest incompatible or prediction not out of sample')
            import numpy as np
            row=np.asarray([[v for _,v in features.values]],dtype=np.float64)
            if m.family=='lightgbm':result=float(self.native.predict(row)[0])
            else:result=float(self.native.predict(self.module.DMatrix(row,feature_names=list(FEATURE_NAMES)))[0])
            finite(result)
            return result
        except ModelUnavailable:raise
        except Exception:raise ModelUnavailable('Inference unavailable') from None


def predict(directory, features):
    loaded=ModelPredictor(directory)
    return loaded.manifest,loaded.predict(features)


def action_from_prediction(score, threshold):
    finite(score);finite(threshold,lower=0)
    return 'BUY' if score>threshold else ('SELL' if score<0 else 'HOLD')


def ml_signal(strategy, features, now, *, enabled=False, directory=None, cost_bps=None):
    if strategy not in {'minute','swing'}:raise ValueError('Unsupported ML strategy')
    now=utc(now)
    if not 0 <= (now-features.data_end_utc).total_seconds() <= MAX_AGES[strategy]:
        raise ValueError('Stale or future model input')
    reason='strategy_disabled';action='ABSTAIN';score=None;model_id='unavailable';version='none'
    if enabled:
        if directory is None:reason='model_missing'
        elif cost_bps is None:reason='costs_unknown'
        else:
            finite(cost_bps,lower=0,upper=10000)
            try:
                manifest,score=predict(directory,features)
                model_id,version=manifest.model_id,manifest.model_version
                if manifest.strategy_id!=strategy:raise ModelUnavailable('Wrong strategy model')
                if cost_bps>manifest.target_cost_bps:reason='costs_exceed_calibration'
                else:
                    action=action_from_prediction(score,manifest.threshold);reason='net_prediction_threshold'
            except ModelUnavailable:reason='model_unavailable'
    signal=StrategySignal.create(strategy_id=strategy,symbol=features.symbol,horizon=STRATEGIES[strategy],
        as_of_utc=now,data_end_utc=features.data_end_utc,action=action,score=score,confidence=None,
        model_id=model_id,model_version=version,feature_version=features.feature_version,
        reason_codes=(reason,),estimated_cost_bps=cost_bps)
    signal.validate_at(now)
    return signal
