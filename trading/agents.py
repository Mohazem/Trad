"""Four deterministic roles, not LLM agents and not validated trading models."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from .config import Config
from .models import ASSETS, D, Decision, OrderIntent, Quote, Snapshot, utc, fixed_decimal


@dataclass(frozen=True)
class Research:
    symbol: str
    momentum: Decimal
    max_step: Decimal
    samples: int
    asof: datetime


class ResearchAgent:
    @fixed_decimal
    def analyze(self, history: list[Quote], now: datetime, lookback: int) -> Research | None:
        # The runner supplies only the observed prefix; explicit filtering also
        # rejects future samples so a future bar cannot affect today's signal.
        past = [q for q in history if utc(q.asof) <= utc(now)]
        if len(past) < lookback + 1:
            return None
        window = past[-(lookback + 1):]
        if any(q.symbol != window[0].symbol or q.price <= 0 for q in window):
            raise ValueError("Invalid research window")
        if any(a.asof >= b.asof for a, b in zip(window, window[1:])):
            raise ValueError("History must be strictly chronological")
        changes = [abs(b.price / a.price - 1) for a, b in zip(window, window[1:])]
        return Research(window[-1].symbol, window[-1].price / window[0].price - 1,
                        max(changes), len(window), window[-1].asof)


class SignalAgent:
    @fixed_decimal
    def propose(self, research: Research, quote: Quote, snapshot: Snapshot,
                equity: Decimal, now: datetime, strategy: str, step: int) -> OrderIntent | None:
        held = snapshot.positions.get(quote.symbol, D("0"))
        if research.momentum > D("0.01") and held * quote.price < equity * D("0.12"):
            side, qty = "buy", (equity * D("0.03") / quote.price).quantize(D("0.00000001"), rounding=ROUND_DOWN)
        elif research.momentum < D("-0.015") and held > 0:
            # Small exits keep the same strict per-order bound as entries.
            side, qty = "sell", min(held, (equity * D("0.03") / quote.price).quantize(D("0.00000001"), rounding=ROUND_DOWN))
        else:
            return None
        if qty <= 0:
            return None
        return OrderIntent(f"demo-{step:03d}-{quote.symbol.replace('/', '_')}-{side}",
                           quote.symbol, ASSETS[quote.symbol], side, qty, quote.price,
                           now, now + timedelta(seconds=60), quote.asof, strategy,
                           f"Synthetic momentum={research.momentum:.6f}; deterministic rule")


class CriticAgent:
    @fixed_decimal
    def review(self, research: Research | None, intent: OrderIntent, now: datetime,
               config: Config) -> Decision:
        if research is None or research.samples < 4:
            return Decision(False, "critic_insufficient_history", "At least four observations required")
        if research.symbol != intent.symbol or research.asof != intent.data_asof:
            return Decision(False, "critic_mismatched_evidence", "Evidence does not match this intent")
        if not 0 <= (utc(now) - utc(research.asof)).total_seconds() <= config.max_quote_age_seconds:
            return Decision(False, "critic_stale_evidence", "Research evidence is stale or future-dated")
        if research.max_step > D("0.08"):
            return Decision(False, "critic_price_jump", "Synthetic one-step move exceeds 8%")
        return Decision(True, "critic_pass", "Deterministic evidence checks passed")


class RiskAgent:
    def __init__(self, config: Config):
        self.config = config

    @fixed_decimal
    def evaluate(self, intent: OrderIntent, snapshot: Snapshot, quotes: dict[str, Quote],
                 now: datetime, fill_price: Decimal, fee: Decimal) -> Decision:
        c = self.config
        def reject(code, detail):
            return Decision(False, code, detail)
        try:
            intent.validate()
        except (ValueError, TypeError, AttributeError):
            return reject("invalid_intent", "Intent schema validation failed")
        if snapshot.halted:
            return reject("kill_switch", snapshot.halt_reason)
        age = (utc(now) - utc(intent.created_at)).total_seconds()
        if age < 0 or age > c.max_intent_age_seconds or utc(now) >= utc(intent.expires_at):
            return reject("expired_intent", "Intent is future-dated, too old or expired")
        if not 0 <= (utc(now) - utc(intent.data_asof)).total_seconds() <= c.max_quote_age_seconds:
            return reject("stale_intent_data", "Intent references stale or future data")
        required = {intent.symbol} | {s for s, q in snapshot.positions.items() if q != 0}
        for symbol in sorted(required):
            quote = quotes.get(symbol)
            if not quote or quote.symbol != symbol or not quote.valid_at(now, c.max_quote_age_seconds):
                return reject("stale_or_missing_quote", f"No fresh valid quote for {symbol}")
        quote = quotes[intent.symbol]
        if utc(quote.asof) < utc(intent.data_asof):
            return reject("quote_older_than_intent", "Execution quote predates the research data")
        if abs(quote.price / intent.reference_price - 1) > c.max_reference_deviation:
            return reject("price_drift", "Quote moved too far from intent reference")
        equity = snapshot.cash + sum((q * quotes[s].price for s, q in snapshot.positions.items() if q != 0), D("0"))
        if equity <= 0:
            return reject("insolvent", "Portfolio equity is non-positive")
        drawdown = 1 - equity / max(snapshot.peak_equity, equity)
        if drawdown >= c.max_drawdown:
            return reject("drawdown", "Peak-to-trough drawdown threshold reached")
        notional = intent.qty * fill_price
        if notional > equity * c.max_order_fraction:
            return reject("order_size", "Order exceeds the configured fraction of shared equity")
        current = snapshot.positions.get(intent.symbol, D("0"))
        if intent.side == "sell" and intent.qty > current:
            return reject("short_forbidden", "Selling more than the held position is forbidden")
        signed = intent.qty if intent.side == "buy" else -intent.qty
        positions = dict(snapshot.positions)
        positions[intent.symbol] = current + signed
        cash = snapshot.cash - signed * fill_price - fee
        post_equity = cash + sum((q * quotes[s].price for s, q in positions.items() if q != 0), D("0"))
        if cash < 0 or post_equity <= 0:
            return reject("insufficient_cash", "Cash account cannot borrow")
        # Exits may reduce existing exposure even if a cap was breached by a
        # price move. Kill, freshness, no-short and per-order checks still apply.
        if intent.side == "sell":
            return Decision(True, "risk_pass", "Long-only position reduction")
        gross = sum((q * quotes[s].price for s, q in positions.items() if q != 0), D("0"))
        crypto = sum((q * quotes[s].price for s, q in positions.items() if q != 0 and ASSETS[s] == "crypto"), D("0"))
        if cash < post_equity * c.min_cash_fraction:
            return reject("cash_buffer", "Cash reserve would fall below the configured fraction")
        if gross > post_equity * c.max_gross_exposure:
            return reject("gross_exposure", "Shared equity and crypto gross cap exceeded")
        if crypto > post_equity * c.max_crypto_exposure:
            return reject("crypto_exposure", "Crypto allocation cap exceeded")
        if positions[intent.symbol] * quote.price > post_equity * c.max_symbol_exposure:
            return reject("symbol_exposure", "Single-symbol allocation cap exceeded")
        return Decision(True, "risk_pass", "All shared-portfolio gates passed")
