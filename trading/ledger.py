"""Atomic, single-account SQLite simulator with persistent risk state."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from decimal import localcontext
from functools import wraps
from pathlib import Path
import json
import sqlite3
from .agents import RiskAgent
from .config import Config
from .models import D, Decision, OrderIntent, OrderResult, Quote, Snapshot, canonical, utc, ACCOUNTING_CONTEXT


class IntentConflict(ValueError):
    pass


class InvalidMarketData(ValueError):
    pass


def consistent_read(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with self.read_transaction():
            return function(self, *args, **kwargs)
    return wrapped


class Ledger:
    def __init__(self, path, config: Config):
        self.path, self.config = Path(path), config
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK(id=1), cash TEXT NOT NULL,
                peak TEXT NOT NULL, halted INTEGER NOT NULL, halt_reason TEXT NOT NULL, marked_at TEXT);
            CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, qty TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                intent_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                decided_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('filled', 'blocked')),
                code TEXT NOT NULL, detail TEXT NOT NULL, fill_price TEXT, fee TEXT);
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL, intent_id TEXT, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS equity_curve (
                asof TEXT PRIMARY KEY, equity TEXT NOT NULL, peak TEXT NOT NULL,
                drawdown TEXT NOT NULL, halted INTEGER NOT NULL);
        """)
        expected = canonical(asdict(config))
        try:
            with self.transaction():
                existing = self.db.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
                if existing and existing[0] != expected:
                    raise ValueError("Ledger configuration is immutable; use a new ledger")
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('config', ?)", (expected,))
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')")
                self.db.execute("INSERT OR IGNORE INTO account VALUES (1, ?, ?, 0, '', NULL)",
                                (str(config.initial_cash), str(config.initial_cash)))
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            with localcontext(ACCOUNTING_CONTEXT):
                yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    @contextmanager
    def read_transaction(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    @consistent_read
    def snapshot(self):
        row = self.db.execute("SELECT * FROM account WHERE id=1").fetchone()
        return Snapshot(D(row['cash']), {r['symbol']: D(r['qty']) for r in self.db.execute("SELECT * FROM positions")},
                        D(row['peak']), bool(row['halted']), row['halt_reason'])

    def _audit(self, at, event, intent_id, details):
        self.db.execute("INSERT INTO audit(at,event,intent_id,details) VALUES(?,?,?,?)",
                        (utc(at).isoformat(), event, intent_id, canonical(details)))

    def _mark(self, quotes, now):
        now = utc(now)
        row = self.db.execute("SELECT marked_at FROM account WHERE id=1").fetchone()
        if row[0] and now < datetime.fromisoformat(row[0]):
            raise InvalidMarketData("Portfolio time cannot move backwards")
        s = self.snapshot()
        for symbol, qty in s.positions.items():
            q = quotes.get(symbol)
            if qty != 0 and (not q or q.symbol != symbol or not q.valid_at(now, self.config.max_quote_age_seconds)):
                raise InvalidMarketData(f"No fresh valid mark for held position {symbol}")
        equity = s.cash + sum((qty * quotes[symbol].price for symbol, qty in s.positions.items() if qty != 0), D("0"))
        peak = max(s.peak_equity, equity)
        dd = 1 - equity / peak
        halted = s.halted or dd >= self.config.max_drawdown
        reason = s.halt_reason or ("drawdown_threshold" if halted else "")
        if halted and not s.halted:
            self._audit(now, "kill_switch_latched", None, {"drawdown": dd, "threshold": self.config.max_drawdown})
        self.db.execute("UPDATE account SET peak=?, halted=?, halt_reason=?, marked_at=? WHERE id=1",
                        (str(peak), int(halted), reason, now.isoformat()))
        self.db.execute("INSERT OR REPLACE INTO equity_curve VALUES(?,?,?,?,?)",
                        (now.isoformat(), str(equity), str(peak), str(dd), int(halted)))
        return equity

    def mark(self, quotes, now):
        with self.transaction():
            return self._mark(quotes, now)

    def halt(self, now, reason="manual_halt"):
        with self.transaction():
            self.db.execute("UPDATE account SET halted=1, halt_reason=? WHERE id=1", (reason,))
            self._audit(now, "manual_halt", None, {"reason": reason})

    @staticmethod
    def _result(row, replayed=False):
        return OrderResult(row['intent_id'], row['status'], row['code'], row['detail'],
                           D(row['fill_price']) if row['fill_price'] else None,
                           D(row['fee']) if row['fee'] else None, replayed)

    def submit(self, intent, quotes, now, critic=None):
        """Local fill only: risk gates, cash, positions and audit commit atomically.

        A passing critic decision is required. This is trusted internal code,
        not a public API or a cryptographically authenticated critic service.
        """
        now = utc(now)
        fingerprint = intent.fingerprint()
        conflict = False
        with self.transaction():
            old = self.db.execute("SELECT * FROM orders WHERE intent_id=?", (intent.intent_id,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    self._audit(now, "intent_conflict", intent.intent_id, {"supplied_fingerprint": fingerprint})
                    conflict = True
                else:
                    return self._result(old, replayed=True)
            if not conflict:
                fill_price = fee = None
                try:
                    intent.validate()
                except (ValueError, TypeError, AttributeError):
                    decision = Decision(False, "invalid_intent", "Intent schema validation failed")
                else:
                    try:
                        self._mark(quotes, now)
                    except InvalidMarketData as exc:
                        decision = Decision(False, "stale_or_missing_quote", str(exc))
                    else:
                        if critic is None or not critic.allowed:
                            decision = critic or Decision(False, "critic_required", "No critic approval supplied")
                        else:
                            quote = quotes.get(intent.symbol)
                            if quote is None or quote.symbol != intent.symbol or not quote.valid_at(now, self.config.max_quote_age_seconds):
                                decision = Decision(False, "stale_or_missing_quote", "No fresh valid execution quote")
                            else:
                                sign = D("1") if intent.side == "buy" else D("-1")
                                fill_price = quote.price * (1 + sign * self.config.slippage_bps / 10000)
                                fee = intent.qty * fill_price * self.config.fee_bps / 10000
                                decision = RiskAgent(self.config).evaluate(intent, self.snapshot(), quotes, now, fill_price, fee)
                status = "filled" if decision.allowed else "blocked"
                if decision.allowed:
                    s = self.snapshot()
                    signed = intent.qty if intent.side == "buy" else -intent.qty
                    cash = s.cash - signed * fill_price - fee
                    qty = s.positions.get(intent.symbol, D("0")) + signed
                    self.db.execute("UPDATE account SET cash=? WHERE id=1", (str(cash),))
                    if qty == 0:
                        self.db.execute("DELETE FROM positions WHERE symbol=?", (intent.symbol,))
                    else:
                        self.db.execute("INSERT OR REPLACE INTO positions VALUES(?,?)", (intent.symbol, str(qty)))
                    self._mark(quotes, now)
                else:
                    fill_price = fee = None
                self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)",
                    (intent.intent_id, fingerprint, canonical(intent.payload()), now.isoformat(), status,
                     decision.code, decision.detail, str(fill_price) if fill_price is not None else None,
                     str(fee) if fee is not None else None))
                self._audit(now, f"order_{status}", intent.intent_id,
                            {"code": decision.code, "detail": decision.detail, "fingerprint": fingerprint})
                result = OrderResult(intent.intent_id, status, decision.code, decision.detail, fill_price, fee)
        if conflict:
            raise IntentConflict("Intent ID was previously used with a different payload")
        return result

    @consistent_read
    def report(self):
        s = self.snapshot()
        last = self.db.execute("SELECT * FROM equity_curve ORDER BY asof DESC LIMIT 1").fetchone()
        counts = {r[0]: r[1] for r in self.db.execute("SELECT status,COUNT(*) FROM orders GROUP BY status")}
        reasons = {r[0]: r[1] for r in self.db.execute("SELECT code,COUNT(*) FROM orders WHERE status='blocked' GROUP BY code")}
        return {
            "mode": "offline_demo", "network_trading": "not_implemented", "data": "synthetic",
            "cash": str(s.cash), "positions": {k: str(v) for k, v in sorted(s.positions.items())},
            "equity": last['equity'] if last else str(s.cash), "peak_equity": str(s.peak_equity),
            "drawdown": last['drawdown'] if last else "0", "halted": s.halted, "halt_reason": s.halt_reason,
            "orders": counts, "blocked_reasons": reasons,
            "audit_events": self.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
            "max_drawdown_interpretation": "provisional_peak_to_trough_requires_confirmation",
            "configured_drawdown_threshold": str(self.config.max_drawdown),
        }

    @consistent_read
    def export(self, directory):
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / "status.json").write_text(json.dumps(self.report(), ensure_ascii=False, indent=2) + "\n")
        for table, order in (("orders", "intent_id"), ("audit", "seq"), ("equity_curve", "asof")):
            with (target / f"{table}.jsonl").open("w") as out:
                for row in self.db.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                    out.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
