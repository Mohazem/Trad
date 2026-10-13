"""Public wire contracts for bounded, unprivileged LLM proposals."""
from __future__ import annotations
import copy

ROLES = ("analyst", "critic", "synthesizer")
SYMBOLS = ("AAPL", "MSFT", "BTC/USD")

def _object(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}

BASE = {
    "packet_id": {"type": "string", "minLength": 1, "maxLength": 80},
    "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
}
RATIONALE = {"type": "string", "minLength": 1, "maxLength": 800}
CONFIDENCE = {"type": "number", "minimum": 0, "maximum": 1}
SCHEMAS = {
    "analyst": _object({"role": {"type": "string", "enum": ["analyst"]}, **BASE,
        "symbol": {"type": "string", "enum": list(SYMBOLS)},
        "bias": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "confidence": CONFIDENCE, "rationale": RATIONALE}),
    "critic": _object({"role": {"type": "string", "enum": ["critic"]}, **BASE,
        "verdict": {"type": "string", "enum": ["accept", "reject"]},
        "concerns": {"type": "array", "items": RATIONALE, "minItems": 0, "maxItems": 8}}),
    "synthesizer": _object({"role": {"type": "string", "enum": ["synthesizer"]}, **BASE,
        "symbol": {"type": "string", "enum": list(SYMBOLS)},
        "action": {"type": "string", "enum": ["buy", "sell", "abstain"]},
        "confidence": CONFIDENCE, "rationale": RATIONALE}),
}

# Entirely invented, public fixture: not a news feed, market history, or forecast.
_SAMPLE_PACKET = {
    "packet_id": "synthetic-ai-20261013-140000",
    "asof": "2026-10-13T14:00:00+00:00",
    "data_kind": "synthetic",
    "sources": [
        {"source_id": "aapl-quote", "symbol": "AAPL", "kind": "quote", "asof": "2026-10-13T14:00:00+00:00", "values": {"price": "100.00"}},
        {"source_id": "aapl-summary", "symbol": "AAPL", "kind": "summary", "asof": "2026-10-13T14:00:00+00:00", "values": {"samples": 9, "momentum": "0.025", "max_step": "0.009"}},
        {"source_id": "msft-quote", "symbol": "MSFT", "kind": "quote", "asof": "2026-10-13T14:00:00+00:00", "values": {"price": "200.00"}},
        {"source_id": "msft-summary", "symbol": "MSFT", "kind": "summary", "asof": "2026-10-13T14:00:00+00:00", "values": {"samples": 9, "momentum": "-0.005", "max_step": "0.008"}},
        {"source_id": "btc-quote", "symbol": "BTC/USD", "kind": "quote", "asof": "2026-10-13T14:00:00+00:00", "values": {"price": "50000.00"}},
        {"source_id": "btc-summary", "symbol": "BTC/USD", "kind": "summary", "asof": "2026-10-13T14:00:00+00:00", "values": {"samples": 9, "momentum": "0.04", "max_step": "0.035"}}
    ]
}

def sample_packet():
    return copy.deepcopy(_SAMPLE_PACKET)
