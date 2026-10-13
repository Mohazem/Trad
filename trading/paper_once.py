"""One bounded PAPER experiment; not imported by the offline simulator.

No strategy or model selects an order here. A trusted operator must supply an
exact, separately approved specification. All numerical safety checks are local.
"""
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .paper_transport import DATA, PAPER, PaperError

CLIENT_ORDER_ID = "trad-paper-20261013-one-shot-01"
MAX_NOTIONAL = Decimal("1000")
TERMINAL = {"filled", "canceled", "expired", "rejected"}
OPEN = {"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "accepted_for_bidding"}
SYMBOLS = {"AAPL", "MSFT", "BTC/USD"}


def number(value, *, positive=True):
    if type(value) not in (str, int, Decimal) or len(str(value)) > 40:
        raise PaperError("invalid_number")
    if not re.fullmatch(r"-?\d+(?:\.\d{1,12})?", str(value)):
        raise PaperError("invalid_number")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise PaperError("invalid_number") from None
    if not result.is_finite() or abs(result) > Decimal("1e12") or (positive and result <= 0):
        raise PaperError("invalid_number")
    return result


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise PaperError("invalid_timestamp") from None


def fresh(value, now, seconds):
    age = (timestamp(now) - timestamp(value)).total_seconds()
    if not 0 <= age <= seconds:
        raise PaperError("stale_or_future_data")


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class OrderSpec:
    symbol: str
    qty: str
    limit_price: str
    analysis_digest: str
    approved_at: str
    expires_at: str

    def payload(self):
        if self.symbol not in SYMBOLS:
            raise PaperError("symbol_not_permitted")
        qty, price = number(self.qty), number(self.limit_price)
        if not isinstance(self.analysis_digest, str) or len(self.analysis_digest) != 64 or any(
                c not in "0123456789abcdef" for c in self.analysis_digest):
            raise PaperError("analysis_digest_required")
        with localcontext() as context:
            context.prec = 80
            if qty * price > MAX_NOTIONAL:
                raise PaperError("notional_limit")
        return {"symbol": self.symbol, "qty": format(qty, "f"),
                "limit_price": format(price, "f"), "side": "buy", "type": "limit",
                "time_in_force": "ioc", "extended_hours": False,
                "order_class": "simple", "client_order_id": CLIENT_ORDER_ID}

    def approval_digest(self):
        return digest({"payload": self.payload(), "analysis_digest": self.analysis_digest,
                       "approved_at": self.approved_at, "expires_at": self.expires_at})


def validate(spec, snapshot, now):
    """Pure dry-run: no network, no credentials and no journal mutation."""
    with localcontext() as context:
        context.prec = 80
        payload = spec.payload()
        fresh(spec.approved_at, now, 300)
        expiry = timestamp(spec.expires_at)
        if not timestamp(now) < expiry <= timestamp(spec.approved_at) + timedelta(minutes=5):
            raise PaperError("approval_expired")
        fresh(snapshot["fetched_at"], now, 15)
        symbol, qty, price = spec.symbol, number(spec.qty), number(spec.limit_price)
        account = snapshot["account"]
        if account.get("status") != "ACTIVE" or account.get("currency") != "USD" or any(
            account.get(k) is not False for k in ("trading_blocked", "account_blocked", "trade_suspended_by_user")):
            raise PaperError("account_not_tradeable")
        if number(account["short_market_value"], positive=False) != 0:
            raise PaperError("existing_short_exposure")
        if number(account["long_market_value"], positive=False) != 0:
            raise PaperError("existing_long_exposure")
        notional = qty * price
        # Cash, not leveraged buying_power. A 1% cash reserve is an additional
        # guard, not a broker fee estimate. Any existing order/position abstains.
        if (notional * Decimal("1.01") > number(account["cash"]) or
                notional * Decimal("1.01") > number(account["non_marginable_buying_power"])):
            raise PaperError("cash_only_limit")
        if snapshot["orders"] != [] or snapshot["positions"] != []:
            raise PaperError("existing_orders_or_positions")
        asset = snapshot["asset"]
        crypto = symbol == "BTC/USD"
        if crypto and account.get("crypto_status") != "ACTIVE":
            raise PaperError("crypto_account_not_active")
        if (asset.get("symbol") != symbol or asset.get("status") != "active" or
                asset.get("tradable") is not True or asset.get("class") != ("crypto" if crypto else "us_equity")):
            raise PaperError("asset_not_tradeable")
        if crypto:
            if qty < number(asset["min_order_size"]) or qty % number(asset["min_trade_increment"]) or price % number(asset["price_increment"]):
                raise PaperError("asset_increment_violation")
        elif qty != qty.to_integral_value() or price % Decimal("0.01") or price < 1:
            raise PaperError("whole_share_or_price_tick_required")
        clock = snapshot["clock"]
        fresh(clock["timestamp"], now, 15)
        if not crypto and (clock.get("is_open") is not True or timestamp(clock["next_close"]) <= timestamp(now) + timedelta(seconds=30)):
            raise PaperError("stock_session_closed_or_closing")
        market = snapshot["market"]
        if market.get("symbol") != symbol or market.get("source") != DATA:
            raise PaperError("market_source_mismatch")
        if market.get("feed") not in (("us",) if crypto else ("iex", "sip")):
            raise PaperError("unsupported_or_delayed_feed")
        quote = market["quote"]
        fresh(quote["t"], now, 15)
        bid, ask = number(quote["bp"]), number(quote["ap"])
        if bid > ask or (ask - bid) / ask > Decimal("0.005"):
            raise PaperError("crossed_or_wide_quote")
        if number(quote["bs"]) <= 0 or number(quote["as"]) <= 0:
            raise PaperError("empty_quote")
        if not bid <= price <= ask:
            raise PaperError("limit_outside_current_quote")
        bars = market["bars"]
        if not isinstance(bars, list) or not 20 <= len(bars) <= 60 or market.get("timeframe") != "1Min":
            raise PaperError("insufficient_bars")
        prior = None
        for bar in bars:
            at = timestamp(bar["t"])
            # Completed one-minute bars; sparse bars may not cover enough time.
            if at + timedelta(minutes=1) > timestamp(now) or (prior and not 0 < (at-prior).total_seconds() <= 120):
                raise PaperError("invalid_bar_sequence")
            op, high, low, close = (number(bar[k]) for k in ("o", "h", "l", "c"))
            if not low <= min(op, close) <= max(op, close) <= high or number(bar["v"], positive=False) < 0:
                raise PaperError("invalid_bar")
            prior = at
        fresh(bars[-1]["t"], now, 180)
        if abs(ask / number(bars[-1]["c"]) - 1) > Decimal("0.01"):
            raise PaperError("quote_bar_price_dislocation")
        return {"mode": "paper_dry_run", "order": payload, "max_notional_usd": str(notional),
                "approval_digest": spec.approval_digest(), "broker_write": False}


class PaperBroker:
    def __init__(self, transport):
        self.transport = transport

    def lookup(self):
        try:
            return self.transport.request(PAPER, "GET", "/v2/orders:by_client_order_id",
                                          params={"client_order_id": CLIENT_ORDER_ID})
        except PaperError as exc:
            if exc.status == 404:
                return None
            raise

    def market(self, symbol, *, feed="iex", now=None):
        if symbol not in SYMBOLS or feed not in {"iex", "sip"}:
            raise PaperError("market_request_not_permitted")
        now = timestamp(now or datetime.now(timezone.utc))
        end = now.replace(second=0, microsecond=0) - timedelta(microseconds=1)
        params = {"symbols": symbol, "timeframe": "1Min", "start": (end-timedelta(minutes=60)).isoformat(),
                  "end": end.isoformat(), "limit": "60", "sort": "desc"}
        crypto = symbol == "BTC/USD"
        bars_path = "/v1beta3/crypto/us/bars" if crypto else "/v2/stocks/bars"
        quotes_path = "/v1beta3/crypto/us/latest/quotes" if crypto else "/v2/stocks/quotes/latest"
        if not crypto:
            params.update(feed=feed, adjustment="raw")
        bars = self.transport.request(DATA, "GET", bars_path, params=params)
        quotes = self.transport.request(DATA, "GET", quotes_path,
                                        params={"symbols": symbol, **({} if crypto else {"feed": feed})})
        selected = bars.get("bars", {}).get(symbol, [])
        if not isinstance(selected, list) or len(selected) > 60:
            raise PaperError("invalid_market_response")
        # The deliberately bounded last 60 bars can have a continuation token.
        # Record truncation, never automatically paginate or substitute feeds.
        return {"symbol": symbol, "source": DATA, "feed": "us" if crypto else feed,
                "coverage": "Alpaca crypto US" if crypto else ("IEX only; not consolidated NBBO" if feed == "iex" else "SIP"),
                "timeframe": "1Min", "more_history_available": bool(bars.get("next_page_token")),
                "bars": [{k: b[k] for k in ("t", "o", "h", "l", "c", "v")} for b in reversed(selected)],
                "quote": {k: quotes["quotes"][symbol][k] for k in ("t", "bp", "ap", "bs", "as")}}

    def snapshot(self, symbol, *, feed="iex"):
        # Gate BOTH destinations before any account or data request.
        self.transport.preflight(PAPER)
        self.transport.preflight(DATA)
        if symbol not in SYMBOLS:
            raise PaperError("symbol_not_permitted")
        fetched_at = datetime.now(timezone.utc).isoformat()
        account = self.transport.request(PAPER, "GET", "/v2/account")
        # Retain only risk inputs in memory; never return raw account data.
        safe_account = {k: account[k] for k in ("status", "currency", "trading_blocked", "account_blocked",
                       "trade_suspended_by_user", "cash", "non_marginable_buying_power", "short_market_value", "long_market_value")}
        safe_account["crypto_status"] = account.get("crypto_status")
        positions = self.transport.request(PAPER, "GET", "/v2/positions")
        orders = self.transport.request(PAPER, "GET", "/v2/orders", params={"status": "open", "limit": "1", "nested": "false"})
        asset = self.transport.request(PAPER, "GET", "/v2/assets/" + symbol.replace("/", ""))
        clock = self.transport.request(PAPER, "GET", "/v2/clock")
        market = self.market(symbol, feed=feed)
        return {"fetched_at": fetched_at, "account": safe_account, "positions": positions,
                "orders": orders, "asset": asset, "clock": clock, "market": market}

    def candidate_packet(self, symbols=("AAPL", "MSFT", "BTC/USD"), *, feed="iex"):
        self.transport.preflight(PAPER)
        self.transport.preflight(DATA)
        if len(symbols) > 3 or len(set(symbols)) != len(symbols) or any(s not in SYMBOLS for s in symbols):
            raise PaperError("candidate_limit")
        clock = self.transport.request(PAPER, "GET", "/v2/clock")
        candidates = []
        for symbol in symbols:
            asset = self.transport.request(PAPER, "GET", "/v2/assets/" + symbol.replace("/", ""))
            metadata = {k: asset.get(k) for k in ("symbol", "class", "status", "tradable", "min_order_size", "min_trade_increment", "price_increment")}
            market = self.market(symbol, feed=feed)
            now = datetime.now(timezone.utc)
            quote_age = (now - timestamp(market["quote"]["t"])).total_seconds()
            bar_age = (now - timestamp(market["bars"][-1]["t"])).total_seconds() if market["bars"] else None
            candidates.append({"asset": metadata, **market, "quote_age_seconds": quote_age,
                               "last_bar_age_seconds": bar_age, "quote_fresh": 0 <= quote_age <= 15})
        # Ages for all symbols refer to packet completion, not an earlier read.
        completed = datetime.now(timezone.utc)
        for candidate in candidates:
            age = (completed - timestamp(candidate["quote"]["t"])).total_seconds()
            candidate["quote_age_seconds"] = age
            candidate["quote_fresh"] = 0 <= age <= 15
            candidate["last_bar_age_seconds"] = ((completed - timestamp(candidate["bars"][-1]["t"])).total_seconds()
                                                   if candidate["bars"] else None)
        return {"source": "Alpaca market data", "retrieved_at": completed.isoformat(),
                "clock": {k: clock[k] for k in ("timestamp", "is_open", "next_open", "next_close")}, "candidates": candidates}


class AttemptJournal:
    """One durable slot. Explicit initialization; missing/corrupt recovery stops.

    Records only the approval digest and minimal order reconciliation state,
    never credentials or full accounts. No automatic migration or reset.
    """
    STATES = TERMINAL | OPEN | {"reserved_unknown", "post_attempt_unknown", "preflight_stopped", "unknown"}

    def __init__(self, path, *, create=False):
        self.path = Path(path).absolute()
        # This is a process-local permission, not durable anti-rollback proof.
        # Reopening an existing journal is always reconciliation-only, including
        # a valid empty database restored from an old backup. It cannot submit.
        self._initial_submission_available = create is True
        try:
            if create:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if create else 0)
            fd = os.open(self.path, flags, 0o600)
            try:
                stat = os.fstat(fd)
                self.identity = (stat.st_dev, stat.st_ino)
                if create:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.connect() as db:
                        db.execute("CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, client_id TEXT NOT NULL, claimed INTEGER NOT NULL CHECK(claimed IN (0,1)))")
                        db.execute("INSERT INTO meta VALUES (1,2,?,0)", (CLIENT_ORDER_ID,))
                        db.execute("CREATE TABLE attempt (id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT NOT NULL, state TEXT NOT NULL, broker_id TEXT, filled_qty TEXT NOT NULL, observed_state TEXT)")
                    # Persist the new directory entry as well as SQLite data.
                    parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
                with self.connect() as db:
                    self._verify(db)
            finally:
                os.close(fd)
        except (OSError, sqlite3.Error):
            raise PaperError("journal_unavailable_or_corrupt") from None

    def _identity_check(self):
        stat = self.path.stat(follow_symlinks=False)
        if (stat.st_dev, stat.st_ino) != self.identity:
            raise PaperError("journal_replaced")

    def _verify(self, db):
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise PaperError("journal_unavailable_or_corrupt")
        meta = db.execute("SELECT id,version,client_id,claimed FROM meta").fetchall()
        if len(meta) != 1 or meta[0][:3] != (1, 2, CLIENT_ORDER_ID):
            raise PaperError("journal_metadata_invalid")
        rows = db.execute("SELECT id,digest,state,broker_id,filled_qty,observed_state FROM attempt").fetchall()
        if len(rows) != meta[0][3] or len(rows) > 1:
            raise PaperError("journal_attempt_missing")
        if rows:
            ident, fingerprint, state, broker_id, filled, observed = rows[0]
            if ident != 1 or not re.fullmatch(r"[a-f0-9]{64}", fingerprint) or state not in self.STATES:
                raise PaperError("journal_attempt_invalid")
            if number(filled, positive=False) < 0:
                raise PaperError("journal_attempt_invalid")
            if observed is not None and observed not in OPEN | TERMINAL:
                raise PaperError("journal_attempt_invalid")
            if (broker_id is None) != (observed is None) or (broker_id is None and number(filled, positive=False) != 0):
                raise PaperError("journal_attempt_invalid")
            if broker_id is not None and str(uuid.UUID(broker_id)) != broker_id:
                raise PaperError("journal_attempt_invalid")
        return rows

    @contextmanager
    def locked(self):
        """Fail immediately on another local submit/reconcile/cancel operation."""
        fd = None
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW)
            stat = os.fstat(fd)
            if (stat.st_dev, stat.st_ino) != self.identity:
                raise PaperError("journal_replaced")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PaperError("experiment_busy") from None
            with self.connect() as db:
                self._verify(db)
            yield
        except OSError:
            raise PaperError("journal_unavailable_or_corrupt") from None
        finally:
            if fd is not None:
                os.close(fd)

    @contextmanager
    def connect(self):
        db = None
        try:
            self._identity_check()
            # mode=rw must never silently recreate a deleted database.
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0.2)
            self._identity_check()
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
                self._identity_check()
        except (OSError, sqlite3.Error, ValueError):
            raise PaperError("journal_unavailable_or_corrupt") from None
        finally:
            if db is not None:
                db.close()

    def claim(self, fingerprint):
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise PaperError("invalid_approval_digest")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify(db)
            row = db.execute("SELECT digest FROM attempt WHERE id=1").fetchone()
            if row:
                if row[0] != fingerprint:
                    raise PaperError("experiment_spec_conflict")
                return False
            db.execute("INSERT INTO attempt VALUES (1,?,?,NULL,'0',NULL)", (fingerprint, "reserved_unknown"))
            db.execute("UPDATE meta SET claimed=1 WHERE id=1")
            return True

    def _take_initial_submission_permission(self):
        """Consume once while locked; never recreate permission on recovery."""
        available = self._initial_submission_available
        self._initial_submission_available = False
        return available

    def set_state(self, state):
        if state not in self.STATES:
            raise PaperError("invalid_journal_state")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify(db)
            if db.execute("UPDATE attempt SET state=? WHERE id=1", (state,)).rowcount != 1:
                raise PaperError("journal_attempt_missing")

    def require_attempt(self, fingerprint):
        with self.connect() as db:
            rows = self._verify(db)
            if not rows or rows[0][1] != fingerprint:
                raise PaperError("experiment_spec_conflict")
            return rows[0]

    def observe(self, fingerprint, broker_id, result):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._verify(db)
            if not rows or rows[0][1] != fingerprint:
                raise PaperError("experiment_spec_conflict")
            _, _, _, previous_id, previous_filled, observed = rows[0]
            if previous_id is not None and previous_id != broker_id:
                raise PaperError("broker_order_id_changed")
            if number(result["filled_qty"], positive=False) < number(previous_filled, positive=False):
                raise PaperError("filled_quantity_regressed")
            if observed in TERMINAL and (result["state"] != observed or number(result["filled_qty"], positive=False) != number(previous_filled, positive=False)):
                raise PaperError("terminal_state_regressed")
            db.execute("UPDATE attempt SET state=?,broker_id=?,filled_qty=?,observed_state=? WHERE id=1",
                       (result["state"], broker_id, result["filled_qty"], result["state"]))


def checked_order(raw, payload):
    if not isinstance(raw, dict) or any(raw.get(k) != payload[k] for k in ("client_order_id", "symbol", "side", "type", "time_in_force")):
        raise PaperError("broker_order_mismatch")
    if number(raw.get("qty")) != number(payload["qty"]) or number(raw.get("limit_price")) != number(payload["limit_price"]):
        raise PaperError("broker_order_mismatch")
    try:
        if str(uuid.UUID(raw["id"])) != raw["id"]:
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise PaperError("invalid_broker_order_id") from None
    status = raw.get("status")
    if status not in TERMINAL | OPEN:
        raise PaperError("unknown_broker_order_state")
    filled = number(raw.get("filled_qty"), positive=False)
    if not 0 <= filled <= number(payload["qty"]):
        raise PaperError("invalid_filled_quantity")
    if ((status == "filled" and filled != number(payload["qty"])) or
            (status == "partially_filled" and not 0 < filled < number(payload["qty"])) or
            (status in {"new", "accepted", "pending_new", "accepted_for_bidding", "rejected"} and filled != 0)):
        raise PaperError("inconsistent_fill_status")
    if raw.get("extended_hours") is not False or raw.get("order_class") not in {"", "simple"}:
        raise PaperError("broker_order_mismatch")
    if filled:
        average = number(raw.get("filled_avg_price"))
        if average > number(payload["limit_price"]):
            raise PaperError("fill_above_limit")
    else:
        average = None
    return {"state": status, "filled_qty": str(filled), "filled_avg_price": str(average) if average else None}


class OneShotPaper:
    def __init__(self, broker, journal, *, mode="read_only", clock=lambda: datetime.now(timezone.utc)):
        if mode not in {"read_only", "manual_one_shot"}:
            raise PaperError("unattended_or_recurring_execution_forbidden")
        self.broker, self.journal, self.clock = broker, journal, clock
        self.mode = mode

    def dry_run(self, spec, *, feed="iex"):
        return validate(spec, self.broker.snapshot(spec.symbol, feed=feed), self.clock())

    def reconcile(self, spec, *, cancel_open=False, approved_digest=None):
        """One GET; optional single cancel and follow-up GET. Never submit again.

        A cancellation acknowledgement is NOT proof of cancellation. Partial
        fills are retained; no liquidation or replacement order is attempted.
        """
        if cancel_open and self.mode != "manual_one_shot":
            raise PaperError("manual_one_shot_mode_required")
        if cancel_open and (not self.broker.transport.allow_write or approved_digest != spec.approval_digest()):
            raise PaperError("exact_cancel_approval_required")
        with self.journal.locked():
            return self._reconcile_locked(spec, cancel_open=cancel_open)

    def _unknown(self, spec):
        # Preserve the last observed fill and terminal receipt separately from
        # uncertainty. Never release/resubmit the reserved order budget.
        self.journal.set_state("unknown")
        row = self.journal.require_attempt(spec.approval_digest())
        return {"state": "unknown", "confirmed_filled_qty": row[4], "last_observed_state": row[5],
                "action": "manual_reconciliation_required_no_resubmit"}

    def _record(self, spec, raw):
        result = checked_order(raw, spec.payload())
        self.journal.observe(spec.approval_digest(), raw["id"], result)
        if result["state"] in OPEN:
            result["action"] = "manual_reconciliation_required_no_resubmit"
        return result

    def _reconcile_locked(self, spec, *, cancel_open=False):
        self.journal.require_attempt(spec.approval_digest())
        errors = {}
        try:
            raw = self.broker.lookup()
            if raw is None:
                return self._unknown(spec)
            result = self._record(spec, raw)
            if result["state"] in OPEN and cancel_open:
                try:
                    self.broker.transport.request(PAPER, "DELETE", "/v2/orders/" + raw["id"])
                except Exception as exc:
                    # A fill can race with DELETE (including 422). One GET
                    # resolves that race; there is no second DELETE or POST.
                    if isinstance(exc, PaperError):
                        errors["cancellation_error"] = exc.safe_details()
                raw = self.broker.lookup()
                if raw is None:
                    return {**self._unknown(spec), **errors}
                result = self._record(spec, raw)
            return {**result, **errors}
        except Exception as exc:
            if isinstance(exc, PaperError):
                errors["reconciliation_error"] = exc.safe_details()
            return {**self._unknown(spec), **errors}

    def submit_approved(self, spec, *, approved_digest, feed="iex"):
        # This method is deliberately absent from the CLI. Call only after the
        # operator separately authorizes the exact analysis/order specification.
        if self.mode != "manual_one_shot":
            raise PaperError("manual_one_shot_mode_required")
        if not self.broker.transport.allow_write or approved_digest != spec.approval_digest():
            raise PaperError("exact_order_approval_required")
        with self.journal.locked():
            return self._submit_locked(spec, feed=feed)

    def _submit_locked(self, spec, *, feed):
        if not self.journal.claim(spec.approval_digest()):
            return self._reconcile_locked(spec)
        if not self.journal._take_initial_submission_permission():
            # An empty recovered/restored database cannot prove that no prior
            # request happened. Query the same experiment client ID once, record
            # a confirmed matching order, or remain unknown. Never POST here.
            return self._reconcile_locked(spec)
        try:
            existing = self.broker.lookup()
            if existing is not None:
                result = self._record(spec, existing)
                return {**result, "duplicate_submission_avoided": True}
            snapshot = self.broker.snapshot(spec.symbol, feed=feed)
            plan = validate(spec, snapshot, self.clock())
        except Exception as exc:
            self.journal.set_state("preflight_stopped")
            if isinstance(exc, PaperError):
                raise PaperError("preflight_stopped_no_post", exc.status,
                                 request_id=exc.request_id, provider_error_code=exc.provider_error_code,
                                 response_detail=exc.response_detail) from None
            raise PaperError("preflight_stopped_no_post") from None
        # Freshness is checked after all GETs; submission follows immediately.
        self.journal.set_state("post_attempt_unknown")
        try:
            raw = self.broker.transport.request(PAPER, "POST", "/v2/orders", body=plan["order"],
                                               before_send=lambda: validate(spec, snapshot, self.clock()))
            return self._record(spec, raw)
        except Exception as exc:
            # 4xx, duplicate response, malformed response, timeout and any crash
            # are all treated conservatively. No automatic second POST, ever.
            result = self._reconcile_locked(spec)
            if isinstance(exc, PaperError):
                result["submission_error"] = exc.safe_details()
            return result
