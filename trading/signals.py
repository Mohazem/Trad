"""Versioned research contracts. A signal is never an order."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import math
import re
import sqlite3
from .models import canonical, decimal, utc

CRYPTO_SYMBOLS = frozenset({'BTC/USD'})  # Only the existing verified code universe.
STRATEGIES = {'minute': 'minutes', 'swing': 'days', 'portfolio': 'months'}
MAX_AGES = {'minute': 180, 'swing': 86400, 'portfolio': 2678400}


def identifier(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,150}', value):
        raise ValueError('Invalid identifier')
    return value


def finite(value, *, lower=-1e100, upper=1e100, optional=False):
    if value is None and optional:
        return
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError('Invalid finite number')


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class StrategySignal:
    signal_id: str
    strategy_id: str
    symbol: str
    horizon: str
    as_of_utc: datetime
    data_end_utc: datetime
    action: str
    score: float | None
    confidence: float | None
    model_id: str
    model_version: str
    feature_version: str
    reason_codes: tuple[str, ...]
    estimated_cost_bps: float | None

    def __post_init__(self):
        for name in ('signal_id', 'model_id', 'model_version', 'feature_version'):
            identifier(getattr(self, name))
        if self.strategy_id not in STRATEGIES or self.horizon != STRATEGIES[self.strategy_id]:
            raise ValueError('Invalid strategy/horizon')
        if self.symbol not in CRYPTO_SYMBOLS or self.action not in {'BUY', 'SELL', 'HOLD', 'ABSTAIN'}:
            raise ValueError('Invalid symbol/action')
        for name in ('as_of_utc', 'data_end_utc'):
            object.__setattr__(self, name, utc(getattr(self, name)))
        if self.data_end_utc > self.as_of_utc:
            raise ValueError('Future data')
        finite(self.score, optional=True)
        finite(self.confidence, lower=0, upper=1, optional=True)
        finite(self.estimated_cost_bps, lower=0, upper=10000, optional=True)
        if type(self.reason_codes) is not tuple or not 1 <= len(self.reason_codes) <= 16:
            raise ValueError('Reason codes required')
        for reason in self.reason_codes:
            identifier(reason)

    def validate_at(self, now):
        now = utc(now)
        if not (0 <= (now-self.as_of_utc).total_seconds() <= MAX_AGES[self.strategy_id] and
                0 <= (now-self.data_end_utc).total_seconds() <= MAX_AGES[self.strategy_id]):
            raise ValueError('Stale or future signal')
        return self

    @classmethod
    def create(cls, **values):
        return cls(signal_id='sig-' + fingerprint(values), **values)


@dataclass(frozen=True)
class RiskDecision:
    signal_id: str
    allowed: bool
    reason_codes: tuple[str, ...]
    approved_notional: Decimal = Decimal('0')

    def __post_init__(self):
        identifier(self.signal_id)
        if type(self.allowed) is not bool or type(self.reason_codes) is not tuple or not self.reason_codes:
            raise ValueError('Invalid risk decision')
        for reason in self.reason_codes:
            identifier(reason)
        if not isinstance(self.approved_notional, Decimal) or decimal(self.approved_notional) < 0:
            raise ValueError('Invalid notional')
        if not self.allowed and self.approved_notional != 0:
            raise ValueError('Rejected decision cannot allocate money')


@dataclass(frozen=True)
class StrategyAllocation:
    strategy_id: str
    budget_usd: Decimal

    def __post_init__(self):
        if self.strategy_id not in STRATEGIES or not isinstance(self.budget_usd, Decimal):
            raise ValueError('Invalid allocation')
        if not 0 <= decimal(self.budget_usd) <= 50:
            raise ValueError('Budget outside global cap')


class SignalStore:
    """Explicit local SQLite audit. Conflicting identities never overwrite."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS signals (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def record(self, signal, now):
        signal.validate_at(now)
        payload = canonical(asdict(signal))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO signals VALUES (?, ?)', (signal.signal_id, payload))
            if self.db.execute('SELECT payload FROM signals WHERE id=?', (signal.signal_id,)).fetchone()[0] != payload:
                raise ValueError('Conflicting signal identity')

    def close(self):
        self.db.close()
