"""Explicit historical import, immutable observations and point-in-time reads."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import json
import sqlite3
from .models import canonical, decimal, utc
from .signals import CRYPTO_SYMBOLS, identifier

TIMEFRAMES = {'1m': 60, '5m': 300, '15m': 900, '4h': 14400, '1d': 86400, '1w': 604800}


@dataclass(frozen=True)
class MarketObservation:
    symbol: str
    timeframe: str
    start_utc: datetime
    received_at_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str
    quality: str = 'validated'
    data_kind: str = 'historical'

    def __post_init__(self):
        if self.symbol not in CRYPTO_SYMBOLS or self.timeframe not in TIMEFRAMES:
            raise ValueError('Unknown symbol/timeframe')
        for key in ('start_utc', 'received_at_utc'):
            object.__setattr__(self, key, utc(getattr(self, key)))
        if self.start_utc.microsecond or int(self.start_utc.timestamp()) % TIMEFRAMES[self.timeframe]:
            raise ValueError('Unaligned candle')
        if self.received_at_utc < self.end_utc:
            raise ValueError('Incomplete candle')
        for key in ('open', 'high', 'low', 'close', 'volume'):
            value = getattr(self, key)
            if not isinstance(value, Decimal) or not 0 <= decimal(value) <= Decimal('1e15'):
                raise ValueError('Invalid market value')
        if not 0 < self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError('Invalid OHLC')
        identifier(self.source)
        if self.quality != 'validated' or self.data_kind not in {'historical', 'synthetic'}:
            raise ValueError('Invalid provenance/quality')

    @property
    def end_utc(self):
        return self.start_utc + timedelta(seconds=TIMEFRAMES[self.timeframe])

    @classmethod
    def from_dict(cls, raw):
        values = dict(raw)
        for key in ('start_utc', 'received_at_utc'):
            values[key] = datetime.fromisoformat(values[key])
        for key in ('open', 'high', 'low', 'close', 'volume'):
            values[key] = decimal(values[key])
        return cls(**values)


def available_series(bars, now, *, contiguous=True):
    now = utc(now)
    bars = list(bars)
    if not bars:
        raise ValueError('No observations')
    keys = {(b.symbol, b.timeframe, b.source, b.data_kind) for b in bars}
    if len(keys) != 1:
        raise ValueError('Mixed series/provenance')
    ordered = sorted(bars, key=lambda b: b.start_utc)
    if len({b.start_utc for b in ordered}) != len(ordered):
        raise ValueError('Duplicate observation')
    # Future arrivals are invisible, rather than retroactively usable.
    visible = [b for b in ordered if b.end_utc <= now and b.received_at_utc <= now]
    if not visible:
        raise ValueError('No closed observations available at decision time')
    if contiguous and any(a.end_utc != b.start_utc for a, b in zip(visible, visible[1:])):
        raise ValueError('Gap in series')
    return tuple(visible)


class HistoryStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS bars (symbol TEXT, timeframe TEXT, source TEXT, start TEXT, payload TEXT NOT NULL, PRIMARY KEY(symbol,timeframe,source,start))')

    def ingest(self, bars):
        bars = tuple(bars)
        with self.db:
            for bar in bars:
                if not isinstance(bar, MarketObservation):
                    raise ValueError('Validated observations required')
                key = (bar.symbol, bar.timeframe, bar.source, bar.start_utc.isoformat())
                payload = canonical(asdict(bar))
                self.db.execute('INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?)', (*key, payload))
                if self.db.execute('SELECT payload FROM bars WHERE symbol=? AND timeframe=? AND source=? AND start=?', key).fetchone()[0] != payload:
                    raise ValueError('Conflicting history; corrections require a new source/version')

    def read(self, symbol, timeframe, source, now):
        rows = self.db.execute('SELECT payload FROM bars WHERE symbol=? AND timeframe=? AND source=? ORDER BY start', (symbol, timeframe, source))
        return available_series((MarketObservation.from_dict(json.loads(r[0])) for r in rows), now)

    def close(self):
        self.db.close()


def import_jsonl(path, store):
    """No implicit provider/network: input must carry full provenance."""
    with open(path, encoding='utf-8') as stream:
        bars = [MarketObservation.from_dict(json.loads(line)) for line in stream if line.strip()]
    store.ingest(bars)
    return len(bars)
