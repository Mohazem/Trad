import concurrent.futures
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from trading.agents import CriticAgent, Research, ResearchAgent, RiskAgent
from trading.alpaca import AlpacaPaperSkeleton, NetworkTradingDisabled, PAPER_BASE_URL, validate_paper_url
from trading.config import Config
from trading.demo import run_demo
from trading.ledger import IntentConflict, InvalidMarketData, Ledger
from trading.models import D, Decision, OrderIntent, Quote, Snapshot, decimal

NOW = datetime(2026, 1, 5, 14, tzinfo=timezone.utc)
PASS = Decision(True, "test_pass", "Unit test fixture")


def intent(**changes):
    base = OrderIntent("test-1", "AAPL", "equity", "buy", D("0.4"), D("100"),
                       NOW, NOW + timedelta(seconds=60), NOW, "intraday", "Test fixture")
    return replace(base, **changes)


def quotes(now=NOW, aapl="100", btc="100"):
    return {"AAPL": Quote("AAPL", D(aapl), now), "MSFT": Quote("MSFT", D("100"), now),
            "BTC/USD": Quote("BTC/USD", D(btc), now)}


class NumericTests(unittest.TestCase):
    def test_decimal_boundary_rejects_floats_bool_nonfinite(self):
        for value in [0.1, True, "NaN", "Infinity", "-Infinity", object()]:
            with self.subTest(value=str(value)), self.assertRaises(ValueError):
                decimal(value)

    def test_quote_rejects_bad_prices_and_time(self):
        for value in [D("NaN"), D("Infinity"), D("0"), D("-1"), 100.0, "100"]:
            self.assertFalse(Quote("AAPL", value, NOW).valid_at(NOW, 90))
        self.assertFalse(Quote("AAPL", D("100"), NOW.replace(tzinfo=None)).valid_at(NOW, 90))
        self.assertFalse(Quote("AAPL", D("100"), NOW + timedelta(seconds=1)).valid_at(NOW, 90))
        self.assertTrue(Quote("AAPL", D("100"), NOW - timedelta(seconds=90)).valid_at(NOW, 90))
        self.assertFalse(Quote("AAPL", D("100"), NOW - timedelta(seconds=91)).valid_at(NOW, 90))

    def test_config_normalizes_and_forbids_unsafe_fields(self):
        self.assertEqual(Config(initial_cash="1000").initial_cash, D("1000"))
        for changes in [dict(mode="paper"), dict(mode="live"), dict(max_drawdown="NaN"),
                        dict(initial_cash=1.2), dict(max_gross_exposure="1"),
                        dict(max_quote_age_seconds=True), dict(fee_bps="-1"),
                        dict(paper_base_url="https://api.alpaca.markets")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Config(**changes)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"config.json"
            path.write_text('{"enable_network": true}')
            with self.assertRaises(ValueError):
                Config.load(path)

    def test_strict_order_schema(self):
        for changes in [dict(side="short"), dict(qty=D("0")), dict(qty=D("-1")),
                        dict(qty=D("NaN")), dict(qty=0.4), dict(qty="0.4"),
                        dict(qty=D("0.000000001")), dict(asset_class="crypto"),
                        dict(symbol="EUR/USD"), dict(reference_price=D("Infinity")),
                        dict(created_at=NOW.replace(tzinfo=None)), dict(expires_at=NOW)]:
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                intent(**changes).validate()


class AdapterTests(unittest.TestCase):
    def test_exact_paper_allowlist(self):
        for url in [PAPER_BASE_URL, PAPER_BASE_URL + "/"]:
            self.assertEqual(validate_paper_url(url), PAPER_BASE_URL)
        for url in ["https://api.alpaca.markets", "http://paper-api.alpaca.markets",
                    PAPER_BASE_URL + ".evil.test", PAPER_BASE_URL + ":443", PAPER_BASE_URL + "/v2/orders",
                    PAPER_BASE_URL + "?redirect=https://api.alpaca.markets", PAPER_BASE_URL + "#live",
                    "https://evil.test@paper-api.alpaca.markets", "https://paper-api.alpaca.markets@evil.test",
                    " " + PAPER_BASE_URL, PAPER_BASE_URL.upper(), PAPER_BASE_URL + "\\evil.test"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                AlpacaPaperSkeleton(url)

    def test_submit_cannot_reach_network_even_with_options(self):
        with patch.object(socket, "socket", side_effect=AssertionError("Network used")):
            adapter = AlpacaPaperSkeleton()
            self.assertEqual(adapter.prepare_payload(intent())["type"], "limit")
            with self.assertRaises(NetworkTradingDisabled):
                adapter.submit(intent(), enable_network=True, live=True)

    def test_payload_crypto_time_in_force(self):
        p = AlpacaPaperSkeleton().prepare_payload(intent(symbol="BTC/USD", asset_class="crypto"))
        self.assertEqual(p["time_in_force"], "gtc")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"ledger.sqlite3"
        self.config = Config(initial_cash=D("1000"))
        self.ledger = Ledger(self.path, self.config)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def submit(self, order=None, marks=None, at=NOW, critic=PASS):
        return self.ledger.submit(order or intent(), marks if marks is not None else quotes(at), at, critic)

    def test_accounting_ignores_callers_decimal_context(self):
        with localcontext() as context:
            context.prec = 4
            result = self.submit()
        self.assertEqual(result.fee, D("0.02001"))
        self.assertEqual(self.ledger.snapshot().cash, D("959.95999"))

    def test_fill_costs_and_position_are_exact(self):
        result = self.submit()
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.fill_price, D("100.05"))
        self.assertEqual(result.fee, D("0.02001"))
        self.assertEqual(self.ledger.snapshot().cash, D("959.95999"))
        self.assertEqual(self.ledger.snapshot().positions, {"AAPL": D("0.4")})
        self.assertEqual(self.ledger.report()["audit_events"], 1)

    def test_critic_is_required_and_veto_audited(self):
        self.assertEqual(self.submit(critic=None).code, "critic_required")
        self.assertEqual(self.submit(intent(intent_id="veto"), critic=Decision(False,"veto","test")).code, "veto")
        self.assertEqual(self.ledger.snapshot().cash, D("1000"))
        self.assertEqual(self.ledger.report()["orders"], {"blocked": 2})

    def test_invalid_intents_are_audited(self):
        for index, changes in enumerate([dict(qty=D("NaN")), dict(qty=D("-1")), dict(symbol="UNKNOWN"),
                                        dict(qty=0.1), dict(created_at=NOW.replace(tzinfo=None))]):
            result = self.submit(intent(intent_id=f"bad-{index}", **changes))
            self.assertEqual(result.code, "invalid_intent")
        self.assertEqual(self.ledger.report()["orders"], {"blocked": 5})

    def test_duplicate_fill_and_blocked_intent_are_idempotent(self):
        self.submit()
        self.submit(intent(intent_id="blocked", qty=D("999")))
        before = self.ledger.report()
        self.assertTrue(self.submit().replayed)
        self.assertTrue(self.submit(intent(intent_id="blocked", qty=D("999"))).replayed)
        self.assertEqual(self.ledger.report(), before)

    def test_same_id_changed_payload_is_rejected_and_audited(self):
        self.submit()
        state = self.ledger.snapshot()
        with self.assertRaises(IntentConflict):
            self.submit(intent(qty=D("0.3")))
        self.assertEqual(self.ledger.snapshot(), state)
        self.assertEqual(self.ledger.report()["audit_events"], 2)

    def test_replay_after_restart(self):
        self.submit()
        self.ledger.close()
        self.ledger = Ledger(self.path, self.config)
        self.assertTrue(self.submit().replayed)
        self.assertEqual(self.ledger.snapshot().positions["AAPL"], D("0.4"))

    def test_two_connections_serialize_same_intent(self):
        def worker():
            with Ledger(self.path, self.config) as ledger:
                return ledger.submit(intent(), quotes(), NOW, PASS)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(sum(r.replayed for r in results), 1)
        self.assertEqual(self.ledger.report()["orders"], {"filled": 1})

    def test_atomic_rollback_on_audit_failure(self):
        before = self.ledger.snapshot()
        with patch.object(self.ledger, "_audit", side_effect=RuntimeError("Simulated disk failure")):
            with self.assertRaises(RuntimeError):
                self.submit()
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.ledger.report()["orders"], {})
        self.assertEqual(self.submit().status, "filled")

    def test_stale_execution_quote(self):
        self.assertEqual(self.submit(marks=quotes(NOW-timedelta(seconds=91))).code, "stale_or_missing_quote")
        self.assertEqual(self.ledger.snapshot().cash, D("1000"))

    def test_missing_or_stale_held_asset_blocks_other_asset(self):
        self.submit()
        for index, marks in enumerate([{"BTC/USD": quotes()["BTC/USD"]},
                    quotes() | {"AAPL": Quote("AAPL", D("100"), NOW-timedelta(minutes=5))}]):
            order = intent(intent_id=f"btc-{index}", symbol="BTC/USD", asset_class="crypto")
            self.assertEqual(self.submit(order, marks).code, "stale_or_missing_quote")
        self.assertNotIn("BTC/USD", self.ledger.snapshot().positions)

    def test_future_quote_and_price_drift(self):
        self.assertEqual(self.submit(marks=quotes(NOW+timedelta(seconds=1))).code, "stale_or_missing_quote")
        self.assertEqual(self.submit(intent(intent_id="drift"), quotes(aapl="103")).code, "price_drift")

    def test_old_and_future_intents(self):
        old = intent(created_at=NOW-timedelta(minutes=2), expires_at=NOW-timedelta(minutes=1), data_asof=NOW-timedelta(minutes=2))
        self.assertEqual(self.submit(old).code, "expired_intent")
        future = intent(intent_id="future", created_at=NOW+timedelta(seconds=1), expires_at=NOW+timedelta(minutes=1))
        self.assertEqual(self.submit(future).code, "expired_intent")
        stale = intent(intent_id="stale", data_asof=NOW-timedelta(minutes=2))
        self.assertEqual(self.submit(stale).code, "stale_intent_data")

    def test_short_and_oversize_are_blocked(self):
        self.assertEqual(self.submit(intent(side="sell")).code, "short_forbidden")
        self.assertEqual(self.submit(intent(intent_id="size",qty=D("1"))).code, "order_size")

    def test_full_exit_removes_position(self):
        self.submit()
        self.assertEqual(self.submit(intent(intent_id="sell", side="sell")).status, "filled")
        self.assertEqual(self.ledger.snapshot().positions, {})
        self.ledger.mark({}, NOW+timedelta(seconds=1))

    def test_config_cannot_change_on_restart(self):
        with self.assertRaises(ValueError):
            Ledger(self.path, replace(self.config, max_drawdown=D("0.10")))

    def test_clock_rewind_is_blocked_but_replay_works(self):
        self.submit()
        self.ledger.mark(quotes(NOW+timedelta(minutes=1)), NOW+timedelta(minutes=1))
        self.assertEqual(self.submit(intent(intent_id="rewind")).code, "stale_or_missing_quote")
        self.assertTrue(self.submit().replayed)

    def test_manual_halt_persists_and_blocks(self):
        self.ledger.halt(NOW)
        self.assertEqual(self.submit().code, "kill_switch")
        self.ledger.close()
        self.ledger = Ledger(self.path, self.config)
        self.assertTrue(self.ledger.snapshot().halted)

    def test_mark_no_order_latches_drawdown_and_recovery_does_not_reset(self):
        # Deposit no external funds: acquire max-size lots until about 28% invested.
        for n in range(7):
            self.submit(intent(intent_id=f"buy-{n}"))
        later = NOW+timedelta(hours=1)
        self.ledger.mark(quotes(later, aapl="500"), later)
        peak = self.ledger.snapshot().peak_equity
        later += timedelta(hours=1)
        self.ledger.mark(quotes(later, aapl="100"), later)
        self.assertTrue(self.ledger.snapshot().halted)
        self.assertGreater(peak, D("2000"))
        self.assertEqual(self.submit(intent(intent_id="halted", created_at=later, expires_at=later+timedelta(seconds=60), data_asof=later), quotes(later), later).code, "kill_switch")
        later += timedelta(hours=1)
        self.ledger.mark(quotes(later, aapl="600"), later)
        self.assertTrue(self.ledger.snapshot().halted)
        self.ledger.close()
        self.ledger = Ledger(self.path, self.config)
        self.assertTrue(self.ledger.snapshot().halted)

    def test_post_fill_costs_can_latch_kill(self):
        # Engineering fixture with deliberately tiny threshold to exercise costs.
        with Ledger(Path(self.tmp.name)/"tiny.sqlite3", replace(self.config,max_drawdown=D("0.00001"))) as ledger:
            result = ledger.submit(intent(), quotes(), NOW, PASS)
            self.assertEqual(result.status, "filled")
            self.assertTrue(ledger.snapshot().halted)
            self.assertEqual(ledger.submit(intent(intent_id="next"),quotes(),NOW,PASS).code,"kill_switch")


class SharedRiskTests(unittest.TestCase):
    def evaluate(self, state, config, order=None):
        return RiskAgent(config).evaluate(order or intent(qty=D("1")), state, quotes(), NOW, D("100"), D("0"))

    def test_gross_equity_and_crypto_share_one_cap(self):
        c=Config(max_order_fraction=D("0.2"),max_symbol_exposure=D("0.5"),max_crypto_exposure=D("0.5"))
        s=Snapshot(D("400"),{"AAPL":D("3"),"BTC/USD":D("3")},D("1000"),False,"")
        self.assertEqual(self.evaluate(s,c).code,"gross_exposure")

    def test_crypto_cap(self):
        c=Config(max_order_fraction=D("0.2"))
        s=Snapshot(D("850"),{"BTC/USD":D("1.5")},D("1000"),False,"")
        self.assertEqual(self.evaluate(s,c,intent(symbol="BTC/USD",asset_class="crypto",qty=D("1"))).code,"crypto_exposure")

    def test_symbol_cap(self):
        c=Config(max_order_fraction=D("0.2"))
        s=Snapshot(D("750"),{"AAPL":D("2.5")},D("1000"),False,"")
        self.assertEqual(self.evaluate(s,c).code,"symbol_exposure")

    def test_cash_reserve(self):
        c=Config(max_order_fraction=D("0.2"),min_cash_fraction=D("0.8"))
        s=Snapshot(D("850"),{"AAPL":D("1.5")},D("1000"),False,"")
        self.assertEqual(self.evaluate(s,c).code,"cash_buffer")

    def test_zero_positions_do_not_require_quotes(self):
        c=Config(initial_cash=D("1000"))
        s=Snapshot(D("1000"),{"MSFT":D("0")},D("1000"),False,"")
        self.assertTrue(RiskAgent(c).evaluate(intent(),s,{"AAPL":quotes()["AAPL"]},NOW,D("100"),D("0")).allowed)

    def test_reduction_allowed_after_exposure_drift(self):
        c=Config(max_order_fraction=D("0.2"))
        s=Snapshot(D("500"),{"AAPL":D("5")},D("1000"),False,"")
        self.assertTrue(self.evaluate(s,c,intent(side="sell",qty=D("1"))).allowed)


class AgentAndIntegrationTests(unittest.TestCase):
    def test_research_ignores_future_observation(self):
        history=[Quote("AAPL",D(100+n),NOW+timedelta(hours=n)) for n in range(6)]
        r=ResearchAgent().analyze(history,NOW+timedelta(hours=3),3)
        self.assertEqual(r.samples,4)
        self.assertEqual(r.momentum,D("0.03"))

    def test_critic_rejects_shock_and_mismatch(self):
        r=Research("AAPL",D("0.1"),D("0.1"),4,NOW)
        self.assertEqual(CriticAgent().review(r,intent(),NOW,Config()).code,"critic_price_jump")
        self.assertEqual(CriticAgent().review(replace(r,symbol="MSFT"),intent(),NOW,Config()).code,"critic_mismatched_evidence")

    def test_full_demo_reproducible_and_network_free(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(socket,"socket",side_effect=AssertionError("Network used")):
            first,second=Path(tmp)/"one",Path(tmp)/"two"
            a,b=run_demo(first),run_demo(second)
            self.assertEqual(a,b)
            self.assertEqual(a["replayed_without_mutation"],sum(a["orders"].values()))
            self.assertEqual(a["orders"],{"blocked":12,"filled":71})
            for name in ["status.json","orders.jsonl","audit.jsonl","equity_curve.jsonl","agent_decisions.jsonl"]:
                self.assertEqual((first/name).read_bytes(),(second/name).read_bytes())
            with self.assertRaises(FileExistsError):
                run_demo(first)
            with sqlite3.connect(first/"ledger.sqlite3") as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")


if __name__ == "__main__":
    unittest.main()
