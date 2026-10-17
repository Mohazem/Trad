from __future__ import annotations
from dataclasses import dataclass, fields
from decimal import Decimal
import json
from pathlib import Path
from .models import D, decimal


@dataclass(frozen=True)
class Config:
    mode: str = "offline_demo"
    initial_cash: Decimal = D("100000")
    max_drawdown: Decimal = D("0.20")
    max_gross_exposure: Decimal = D("0.65")
    max_crypto_exposure: Decimal = D("0.20")
    max_symbol_exposure: Decimal = D("0.30")
    max_order_fraction: Decimal = D("0.05")
    min_cash_fraction: Decimal = D("0.10")
    max_reference_deviation: Decimal = D("0.02")
    fee_bps: Decimal = D("5")
    slippage_bps: Decimal = D("5")
    max_quote_age_seconds: int = 90
    max_intent_age_seconds: int = 60
    paper_base_url: str = "https://paper-api.alpaca.markets"

    def __post_init__(self):
        for field in fields(self):
            if field.type == "Decimal":
                object.__setattr__(self, field.name, decimal(getattr(self, field.name)))
        if self.mode != "offline_demo":
            raise ValueError("Only offline_demo mode is implemented")
        if decimal(self.initial_cash) <= 0:
            raise ValueError("Initial cash must be positive")
        for name in ("max_drawdown", "max_gross_exposure", "max_crypto_exposure",
                     "max_symbol_exposure", "max_order_fraction", "min_cash_fraction",
                     "max_reference_deviation"):
            value = decimal(getattr(self, name))
            if not 0 < value < 1:
                raise ValueError(f"{name} must be strictly between zero and one")
        if self.max_crypto_exposure > self.max_gross_exposure:
            raise ValueError("Crypto cap cannot exceed the gross cap")
        if self.max_symbol_exposure > self.max_gross_exposure:
            raise ValueError("Symbol cap cannot exceed the gross cap")
        for name in ("fee_bps", "slippage_bps"):
            if not 0 <= decimal(getattr(self, name)) <= 100:
                raise ValueError(f"{name} must be between zero and 100")
        for name in ("max_quote_age_seconds", "max_intent_age_seconds"):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 3600:
                raise ValueError(f"Invalid {name}")
        from .alpaca import validate_paper_url
        validate_paper_url(self.paper_base_url)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        raw = json.loads(Path(path).read_text())
        if not isinstance(raw, dict):
            raise ValueError("Configuration must be an object")
        names = {f.name for f in fields(cls)}
        unknown = set(raw) - names
        if unknown:
            raise ValueError(f"Unknown configuration fields: {sorted(unknown)}")
        default = cls()
        for name in raw:
            if isinstance(getattr(default, name), Decimal):
                raw[name] = decimal(raw[name])
        return cls(**raw)


@dataclass(frozen=True)
class MultiHorizonConfig:
    """Separate research configuration: never changes legacy Ledger fingerprints."""
    enabled: bool = False
    paper_only: bool = True
    minute_enabled: bool = False
    swing_enabled: bool = False
    portfolio_enabled: bool = False
    routing_enabled: bool = False
    analyst_model: str = 'qwen3:8b'
    critic_model: str = 'qwen3:14b'
    synthesizer_model: str = 'qwen3:8b'
    minute_budget: Decimal = D('15')
    swing_budget: Decimal = D('15')
    portfolio_budget: Decimal = D('20')

    def __post_init__(self):
        from .signals import identifier
        for key in ('enabled','paper_only','minute_enabled','swing_enabled','portfolio_enabled','routing_enabled'):
            if type(getattr(self,key)) is not bool:
                raise ValueError('Boolean configuration required')
        if not self.paper_only:
            raise ValueError('Only paper-only research is supported')
        budgets=[]
        for key in ('minute_budget','swing_budget','portfolio_budget'):
            value=decimal(getattr(self,key))
            if not 0<=value<=50:raise ValueError('Invalid suballocation')
            object.__setattr__(self,key,value);budgets.append(value)
        from decimal import localcontext
        from .models import ACCOUNTING_CONTEXT
        with localcontext(ACCOUNTING_CONTEXT):
            if sum(budgets)>50:raise ValueError('Suballocations exceed global 50 USD ceiling')
        for key in ('analyst_model','critic_model','synthesizer_model'):
            identifier(getattr(self,key))

    @classmethod
    def load(cls,path):
        from .ai_validation import strict_json
        raw=strict_json(Path(path).read_text())
        if type(raw) is not dict or set(raw)!={'multi_horizon'} or type(raw['multi_horizon']) is not dict:
            raise ValueError('Expected separate multi_horizon object')
        try:
            return cls(**raw['multi_horizon'])
        except TypeError:
            raise ValueError('Unknown or invalid multi-horizon configuration fields') from None
