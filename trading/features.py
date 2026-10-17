"""Causal features from closed, contiguous, available observations only."""
from dataclasses import dataclass
from datetime import datetime
import math
from .market_history import TIMEFRAMES, MarketObservation, available_series
from .models import utc
from .signals import CRYPTO_SYMBOLS, finite, identifier

FEATURE_VERSION = 'causal-v1'
FEATURE_NAMES = ('return_1', 'return_8', 'volatility_8', 'relative_volume_8')


@dataclass(frozen=True)
class FeatureSnapshot:
    symbol: str
    timeframe: str
    as_of_utc: datetime
    data_end_utc: datetime
    feature_version: str
    values: tuple[tuple[str, float], ...]
    source: str
    data_kind: str

    def __post_init__(self):
        if self.symbol not in CRYPTO_SYMBOLS or self.timeframe not in TIMEFRAMES:
            raise ValueError('Invalid feature series')
        for name in ('as_of_utc', 'data_end_utc'):
            object.__setattr__(self, name, utc(getattr(self, name)))
        if self.data_end_utc > self.as_of_utc:
            raise ValueError('Future feature')
        identifier(self.feature_version)
        identifier(self.source)
        if self.data_kind not in {'historical', 'synthetic'} or type(self.values) is not tuple:
            raise ValueError('Invalid feature provenance')
        if tuple(k for k, _ in self.values) != FEATURE_NAMES:
            raise ValueError('Feature schema mismatch')
        for _, value in self.values:
            finite(value)


def compute_features(bars, now):
    bars = available_series(bars, now)
    if len(bars) < 9:
        raise ValueError('Insufficient history')
    last = bars[-9:]
    prices = [float(b.close) for b in last]
    returns = [b/a-1 for a, b in zip(prices, prices[1:])]
    mean = sum(returns)/len(returns)
    vol = math.sqrt(sum((r-mean)**2 for r in returns)/len(returns))
    base_volume = sum(float(b.volume) for b in last[:-1])/8
    if base_volume <= 0:
        raise ValueError('Missing volume/liquidity evidence')
    values = (returns[-1], prices[-1]/prices[0]-1, vol, float(last[-1].volume)/base_volume)
    b = bars[-1]
    return FeatureSnapshot(b.symbol, b.timeframe, utc(now), b.end_utc, FEATURE_VERSION,
                           tuple(zip(FEATURE_NAMES, values)), b.source, b.data_kind)


def aggregate(bars, target, now):
    bars = available_series(bars, now)
    source = TIMEFRAMES[bars[0].timeframe]
    if target not in TIMEFRAMES or TIMEFRAMES[target] <= source or TIMEFRAMES[target] % source:
        raise ValueError('Invalid aggregation')
    span, groups = TIMEFRAMES[target], {}
    for b in bars:
        key = int(b.start_utc.timestamp()) // span
        groups.setdefault(key, []).append(b)
    result = []
    for group in groups.values():
        if len(group) != span//source or int(group[0].start_utc.timestamp()) % span:
            continue  # Partial buckets are never treated as closed candles.
        first, last = group[0], group[-1]
        result.append(MarketObservation(first.symbol, target, first.start_utc,
            max(b.received_at_utc for b in group), first.open, max(b.high for b in group),
            min(b.low for b in group), last.close, sum(b.volume for b in group),
            first.source, data_kind=first.data_kind))
    return tuple(result)
