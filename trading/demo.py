from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
from .agents import CriticAgent, ResearchAgent, SignalAgent
from .config import Config
from .ledger import Ledger
from .models import D, Decision, OrderIntent, Quote, fixed_decimal

START = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)


def synthetic_feed(steps=72):
    """Made-up hourly observations, not market data or a realistic session model."""
    prices = {"AAPL": D("200"), "MSFT": D("400"), "BTC/USD": D("60000")}
    for step in range(steps):
        now = START + timedelta(hours=step)
        phase = step % 24
        for index, symbol in enumerate(prices):
            change = D("0.004") if phase < 10 else (D("-0.006") if phase < 17 else D("0.002"))
            change += D((step + index) % 3 - 1) / 10000
            if step == 41 and symbol == "BTC/USD":
                change = D("0.10")
            prices[symbol] = (prices[symbol] * (1 + change)).quantize(D("0.000001"))
        yield now, {symbol: Quote(symbol, price, now) for symbol, price in prices.items()}


@fixed_decimal
def run_demo(output, config=None):
    config = config or Config()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    ledger_path = output / "ledger.sqlite3"
    if ledger_path.exists():
        raise FileExistsError("This demo ledger already exists. Choose a new output directory; no ledger is overwritten.")
    research_agent, signal_agent, critic_agent = ResearchAgent(), SignalAgent(), CriticAgent()
    histories = {"AAPL": [], "MSFT": [], "BTC/USD": []}
    replay_cases, decisions = [], []
    with Ledger(ledger_path, config) as ledger:
        for step, (now, quotes) in enumerate(synthetic_feed()):
            ledger.mark(quotes, now)
            for symbol, quote in quotes.items():
                histories[symbol].append(quote)
                strategy = "intraday" if symbol == "AAPL" else "swing"
                research = research_agent.analyze(histories[symbol], now, 3 if strategy == "intraday" else 8)
                if research is None:
                    continue
                equity = ledger.mark(quotes, now)
                intent = signal_agent.propose(research, quote, ledger.snapshot(), equity, now, strategy, step)
                if intent is None:
                    continue
                critic = critic_agent.review(research, intent, now, config)
                result = ledger.submit(intent, quotes, now, critic)
                replay_cases.append((intent, quotes, now, critic))
                decisions.append({"intent_id": intent.intent_id, "research_momentum": str(research.momentum),
                                  "critic_code": critic.code, "order_status": result.status, "risk_code": result.code})
        q = quotes['AAPL']
        base = OrderIntent("probe-oversized", "AAPL", "equity", "buy", D("10000"), q.price,
                           now, now + timedelta(seconds=60), now, "safety_probe", "Explicit safety fixture")
        passing = Decision(True, "test_fixture", "Explicit deterministic safety probe")
        probes = [
            (base, quotes),
            (replace(base, intent_id="probe-expired", qty=D("1"), created_at=now-timedelta(minutes=2),
                     expires_at=now-timedelta(minutes=1), data_asof=now-timedelta(minutes=2)), quotes),
            (replace(base, intent_id="probe-stale", qty=D("1")),
             {s: replace(q, asof=now-timedelta(minutes=10)) for s, q in quotes.items()}),
            (replace(base, intent_id="probe-no-short", side="sell", qty=ledger.snapshot().positions.get("AAPL", D("0"))+D("1")), quotes),
        ]
        for intent, marks in probes:
            ledger.submit(intent, marks, now, passing)
            replay_cases.append((intent, marks, now, passing))
        before = ledger.report()
        for intent, marks, at, critic in replay_cases:
            if not ledger.submit(intent, marks, at, critic).replayed:
                raise AssertionError("Replay was not recognized")
        if before != ledger.report():
            raise AssertionError("Replay changed portfolio or audit")
        ledger.export(output)
        report = ledger.report() | {"replayed_without_mutation": len(replay_cases), "synthetic_steps": 72,
                                    "strategy_orders": len(decisions), "safety_probes": len(probes)}
    (output / "status.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (output / "agent_decisions.jsonl").write_text("".join(json.dumps(d, sort_keys=True) + "\n" for d in decisions))
    return report
