"""Fail-closed validation. No model output is executable configuration."""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
import copy
import json
import math
import re
from .ai_contracts import SCHEMAS, SYMBOLS
from .models import canonical, utc

MAX_JSON_BYTES = 32768

class ValidationError(ValueError):
    pass


def _fail():
    raise ValidationError("Schema or evidence validation failed")


def strict_json(text: str, limit: int = MAX_JSON_BYTES):
    if not isinstance(text, str) or len(text.encode("utf-8")) > limit:
        _fail()
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: _fail())
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ValidationError("Invalid bounded JSON") from None


def validate_schema(value, schema):
    kind = schema["type"]
    if kind == "object":
        if type(value) is not dict or set(value) != set(schema["required"]):
            _fail()
        for key, spec in schema["properties"].items():
            validate_schema(value[key], spec)
    elif kind == "array":
        if type(value) is not list or not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 16):
            _fail()
        for item in value:
            validate_schema(item, schema["items"])
    elif kind == "string":
        if type(value) is not str or not schema.get("minLength", 1) <= len(value) <= schema.get("maxLength", 800):
            _fail()
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            _fail()
    elif kind == "number":
        if type(value) not in (int, float) or not math.isfinite(value):
            _fail()
        if not schema.get("minimum", -1e100) <= value <= schema.get("maximum", 1e100):
            _fail()
    else:
        _fail()
    if "enum" in schema and value not in schema["enum"]:
        _fail()


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names):
        _fail()


def timestamp(value):
    if type(value) is not str or len(value) > 40:
        _fail()
    try:
        return utc(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        _fail()


def numeric_string(value, lower, upper):
    if type(value) is not str or not re.fullmatch(r"-?\d{1,12}(?:\.\d{1,8})?", value):
        _fail()
    amount = Decimal(value)
    if not Decimal(lower) <= amount <= Decimal(upper):
        _fail()
    return amount


def validate_packet(packet, now, max_age_seconds=90, *, data_kind="synthetic"):
    # Round-trip freezes the supplied object and rejects non-JSON/oversized input.
    try:
        packet = strict_json(canonical(packet))
    except (TypeError, ValueError, RecursionError, OverflowError):
        _fail()
    _keys(packet, ("packet_id", "asof", "data_kind", "sources"))
    if type(packet["packet_id"]) is not str or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", packet["packet_id"]):
        _fail()
    if data_kind not in ("synthetic", "market") or packet["data_kind"] != data_kind:
        _fail()
    asof = timestamp(packet["asof"])
    if not 0 <= (utc(now) - asof).total_seconds() <= max_age_seconds:
        _fail()
    if type(packet["sources"]) is not list or not 2 <= len(packet["sources"]) <= 6:
        _fail()
    ids, kinds, times = set(), set(), {}
    for source in packet["sources"]:
        _keys(source, ("source_id", "symbol", "kind", "asof", "values"))
        sid, symbol, kind = source["source_id"], source["symbol"], source["kind"]
        if type(sid) is not str or not re.fullmatch(r"[a-z0-9_-]{1,64}", sid) or sid in ids:
            _fail()
        if type(symbol) is not str or symbol not in SYMBOLS or type(kind) is not str or kind not in ("quote", "summary"):
            _fail()
        if (symbol, kind) in kinds:
            _fail()
        observed = timestamp(source["asof"])
        if observed > asof or not 0 <= (utc(now) - observed).total_seconds() <= max_age_seconds:
            _fail()
        ids.add(sid)
        kinds.add((symbol, kind))
        times[(symbol, kind)] = observed
        values = source["values"]
        if kind == "quote":
            _keys(values, ("price",))
            numeric_string(values["price"], "0.00000001", "100000000000")
        else:
            _keys(values, ("samples", "momentum", "max_step"))
            if type(values["samples"]) is not int or not 4 <= values["samples"] <= 100000:
                _fail()
            numeric_string(values["momentum"], "-1", "100")
            numeric_string(values["max_step"], "0", "100")
    for symbol, kind in kinds:
        if (symbol, "quote") not in kinds or (symbol, "summary") not in kinds:
            _fail()
        if times[(symbol, "quote")] != times[(symbol, "summary")]:
            _fail()
    return packet


def output_schema(role, packet):
    schema = copy.deepcopy(SCHEMAS[role])
    schema["properties"]["packet_id"]["enum"] = [packet["packet_id"]]
    schema["properties"]["evidence_ids"]["items"]["enum"] = [s["source_id"] for s in packet["sources"]]
    return schema


def validate_output(role, raw, packet):
    output = strict_json(raw) if type(raw) is str else strict_json(canonical(raw))
    validate_schema(output, output_schema(role, packet))
    evidence = output["evidence_ids"]
    if len(evidence) != len(set(evidence)):
        _fail()
    if "symbol" in output:
        related = [s for s in packet["sources"] if s["source_id"] in evidence and s["symbol"] == output["symbol"]]
        # At least one relevant supplied source; packet validation independently
        # requires a fresh quote and summary for deterministic execution.
        if not related:
            _fail()
    return output


def validate_chain(records, packet):
    if type(records) is not dict or set(records) != set(SCHEMAS):
        _fail()
    results = {role: validate_output(role, records[role], packet) for role in SCHEMAS}
    analyst, critic, final = (results[r] for r in SCHEMAS)
    if analyst["symbol"] != final["symbol"]:
        _fail()
    if final["action"] != "abstain":
        expected = {"bullish": "buy", "bearish": "sell", "neutral": "abstain"}[analyst["bias"]]
        if final["action"] != expected or critic["verdict"] != "accept":
            _fail()
    # A critic cannot accept based solely on an unrelated symbol.
    cited = [s for s in packet["sources"] if s["source_id"] in critic["evidence_ids"]]
    if not any(s["symbol"] == analyst["symbol"] for s in cited):
        _fail()
    return results
