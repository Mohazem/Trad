"""Offline mock/replay runner. No API credentials or network CLI exists."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .ai_contracts import SCHEMAS, sample_packet
from .ai_pipeline import AdvisoryPipeline, simulate_snapshot
from .ai_providers import OfflineMockProvider, RecordedReplayProvider
from .ai_validation import strict_json, timestamp, ValidationError
from .config import Config
from .ledger import Ledger
from .models import canonical


def load_replay(path):
    path = Path(path)
    if path.stat().st_size > 131072:
        raise ValidationError("Replay file exceeds size limit")
    data = strict_json(path.read_text(encoding="utf-8"), limit=131072)
    if type(data) is not dict or set(data) != {"format_version", "provenance", "packet", "responses"}:
        raise ValidationError("Invalid replay envelope")
    if type(data["format_version"]) is not int or data["format_version"] != 1:
        raise ValidationError("Unknown replay version")
    if data["provenance"] not in ("recorded_llm", "offline_mock", "user_supplied_recording"):
        raise ValidationError("Invalid provenance label")
    provider = RecordedReplayProvider(data["packet"], data["responses"], data["provenance"])
    return data["packet"], provider, data["provenance"]


def run(output, packet=None, provider=None, provenance="offline_mock", config=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    packet = packet if packet is not None else sample_packet()
    provider = provider if provider is not None else OfflineMockProvider()
    with Ledger(output / "ledger.sqlite3", config or Config()) as ledger:
        # Explicit historical synthetic clock. Never wall-clock/live execution.
        now = timestamp(packet["asof"])
        result = simulate_snapshot(AdvisoryPipeline(provider), packet, ledger, now)
        ledger.export(output)
        report = {"mode": "synthetic_ai_local_simulation", "clock": "packet_asof_replay_clock",
                  "provider": provider.label, "declared_provenance": provenance,
                  "provenance_verified_by_code": False, "network_trading": "not_implemented",
                  "result": result, "ledger": ledger.report()}
    (output / "ai_trace.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    (output / "ai_input.json").write_text(canonical(packet) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description="Bounded AI advisory pipeline; local synthetic simulation only")
    sub = parser.add_subparsers(dest="command", required=True)
    mock = sub.add_parser("mock", help="Deterministic mock: no actual model inference")
    mock.add_argument("--output", required=True)
    replay = sub.add_parser("replay", help="Validate and replay recorded role outputs; no new inference")
    replay.add_argument("--input", required=True)
    replay.add_argument("--output", required=True)
    sub.add_parser("contracts", help="Print exact sample packet and role JSON schemas")
    args = parser.parse_args()
    try:
        if args.command == "contracts":
            report = {"packet": sample_packet(), "schemas": SCHEMAS}
        elif args.command == "mock":
            report = run(args.output)
        else:
            packet, provider, provenance = load_replay(args.input)
            report = run(args.output, packet, provider, provenance)
    except (ValueError, OSError, KeyError, TypeError):
        parser.error("Input, output directory or replay validation failed; no remote order was attempted")
    print(json.dumps(report, indent=2, default=str))

if __name__ == "__main__":
    main()
