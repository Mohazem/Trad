"""Separate read-only CLI. Submission/cancellation are not CLI commands."""
import argparse
import json
from pathlib import Path

from .paper_once import OrderSpec, PaperBroker, validate
from .paper_transport import ProtectedTransport, PaperError


def main():
    parser = argparse.ArgumentParser(description="One-shot PAPER preparation; no broker writes")
    sub = parser.add_subparsers(dest="command", required=True)
    packet = sub.add_parser("market-packet", help="Read current broker metadata and market data only")
    packet.add_argument("--confirm-secret-destinations", action="store_true", required=True,
                        help="Operator confirms both protected secrets permit paper-api and data.alpaca.markets")
    packet.add_argument("--feed", choices=("iex", "sip"), default="iex")
    dry = sub.add_parser("dry-run", help="Validate a private local snapshot offline; not submission approval")
    dry.add_argument("--input", required=True)
    args = parser.parse_args()
    try:
        if args.command == "market-packet":
            transport = ProtectedTransport(confirmed_secret_destinations=("paper-api.alpaca.markets", "data.alpaca.markets"))
            result = PaperBroker(transport).candidate_packet(feed=args.feed)
        else:
            path = Path(args.input)
            if path.stat().st_size > 1024 * 1024:
                raise PaperError("input_size_limit")
            data = json.loads(path.read_text())
            result = validate(OrderSpec(**data["spec"]), data["snapshot"], data["now"])
            result["mode"] = "offline_snapshot_dry_run_not_submission_approval"
        print(json.dumps(result, indent=2))
    except PaperError as exc:
        print(json.dumps({"status": "stopped", "code": exc.code, "http_status": exc.status}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"status": "stopped", "code": "input_or_response_invalid"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
