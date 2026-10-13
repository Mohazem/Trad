from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from functools import wraps
import hashlib
import json

D = Decimal
ACCOUNTING_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


def fixed_decimal(function):
    """A caller's mutable Decimal precision must not change a financial result."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext(ACCOUNTING_CONTEXT):
            return function(*args, **kwargs)
    return wrapped

ASSETS = {"AAPL": "equity", "MSFT": "equity", "BTC/USD": "crypto"}


def decimal(value: object) -> Decimal:
    # Never accept binary floats or booleans in the accounting boundary.
    if isinstance(value, (bool, float)):
        raise ValueError("Decimal input must be a string, integer or Decimal")
    try:
        result = D(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("Non-finite decimal")
    return result


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware datetime is required")
    return value.astimezone(timezone.utc)


def canonical(value: object) -> str:
    def convert(item):
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError(type(item).__name__)
    return json.dumps(value, default=convert, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    asof: datetime

    def valid_at(self, now: datetime, max_age_seconds: int) -> bool:
        try:
            age = (utc(now) - utc(self.asof)).total_seconds()
            return isinstance(self.price, Decimal) and decimal(self.price) > 0 and 0 <= age <= max_age_seconds
        except (ValueError, TypeError):
            return False


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    symbol: str
    asset_class: str
    side: str
    qty: Decimal
    reference_price: Decimal
    created_at: datetime
    expires_at: datetime
    data_asof: datetime
    strategy: str
    rationale: str

    def validate(self) -> None:
        if not isinstance(self.intent_id, str) or not (1 <= len(self.intent_id) <= 96):
            raise ValueError("Invalid intent_id")
        if self.symbol not in ASSETS or ASSETS[self.symbol] != self.asset_class:
            raise ValueError("Unsupported asset or class mismatch")
        if self.side not in {"buy", "sell"}:
            raise ValueError("Only buy and sell are supported")
        if self.strategy not in {"intraday", "swing", "safety_probe"}:
            raise ValueError("Unknown strategy")
        if not isinstance(self.qty, Decimal) or not isinstance(self.reference_price, Decimal):
            raise ValueError("Order quantity and price must be Decimal")
        if decimal(self.qty) <= 0 or decimal(self.reference_price) <= 0:
            raise ValueError("Quantity and price must be positive")
        if self.qty.as_tuple().exponent < -8:
            raise ValueError("At most eight quantity decimals")
        if not isinstance(self.rationale, str) or len(self.rationale) > 1000:
            raise ValueError("Invalid rationale")
        if not utc(self.data_asof) <= utc(self.created_at) < utc(self.expires_at):
            raise ValueError("Invalid intent timestamps")

    def payload(self) -> dict:
        return asdict(self)

    def fingerprint(self) -> str:
        return hashlib.sha256(canonical(self.payload()).encode()).hexdigest()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    detail: str


@dataclass(frozen=True)
class OrderResult:
    intent_id: str
    status: str
    code: str
    detail: str
    fill_price: Decimal | None = None
    fee: Decimal | None = None
    replayed: bool = False


@dataclass(frozen=True)
class Snapshot:
    cash: Decimal
    positions: dict[str, Decimal]
    peak_equity: Decimal
    halted: bool
    halt_reason: str
