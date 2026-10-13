"""Non-network adapter skeleton. It cannot connect, authenticate or place orders."""
from .models import OrderIntent

PAPER_BASE_URL = "https://paper-api.alpaca.markets"


class NetworkTradingDisabled(RuntimeError):
    pass


def validate_paper_url(url: str) -> str:
    # Exact match deliberately rejects redirects, userinfo, ports, suffixes,
    # URL paths, query strings, whitespace, lookalike hosts and live endpoints.
    if url not in {PAPER_BASE_URL, PAPER_BASE_URL + "/"}:
        raise ValueError("Only the exact Alpaca PAPER origin is allowlisted")
    return PAPER_BASE_URL


class AlpacaPaperSkeleton:
    def __init__(self, base_url: str = PAPER_BASE_URL):
        self.base_url = validate_paper_url(base_url)

    def prepare_payload(self, intent: OrderIntent) -> dict[str, str]:
        """Illustrative shape only: not proof of broker acceptance or tick validity.

        The simulator uses a separate slippage fill model; it does not execute
        these limit payloads. No endpoint receives this dictionary.
        """
        intent.validate()
        return {
            "client_order_id": intent.intent_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "qty": str(intent.qty),
            "type": "limit",
            "limit_price": str(intent.reference_price),
            "time_in_force": "gtc" if intent.asset_class == "crypto" else "day",
        }

    def submit(self, *args, **kwargs):
        # There is no transport, credentials parameter, environment-variable
        # lookup or runtime switch. Implementing transport requires new code.
        raise NetworkTradingDisabled("Network trading is absent from this prototype")
