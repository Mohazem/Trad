import argparse
import json
import sqlite3
from pathlib import Path
from .config import Config
from .demo import run_demo


def main():
    parser = argparse.ArgumentParser(description="OFFLINE synthetic paper-trading prototype. No network trading.")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run a deterministic synthetic demo into a new directory")
    demo.add_argument("--output", default="outputs/demo")
    demo.add_argument("--config", default=None)
    status = sub.add_parser("status", help="Inspect a ledger read-only")
    status.add_argument("--ledger", required=True)
    args = parser.parse_args()
    if args.command == "demo":
        config = Config.load(args.config) if args.config else Config()
        try:
            report = run_demo(args.output, config)
        except FileExistsError as exc:
            parser.error(str(exc))
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        path = Path(args.ledger).resolve()
        if not path.is_file():
            parser.error("Ledger does not exist")
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            account = dict(db.execute("SELECT * FROM account WHERE id=1").fetchone())
            counts = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM orders GROUP BY status")}
            print(json.dumps({"account": account, "orders": counts}, indent=2))


if __name__ == "__main__":
    main()
