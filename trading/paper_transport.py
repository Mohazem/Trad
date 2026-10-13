"""Explicit Paper/data transport for the separate one-shot experiment.

Only protected Network placeholders are passed to the existing HTTPS proxy.
No constructor or import performs I/O. Never log requests, headers or bodies.
"""
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import signal
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

PAPER = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
SECRET_NAMES = ("ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET_")
MAX_BYTES = 1024 * 1024


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class PaperError(RuntimeError):
    """Only a fixed, non-sensitive code and optional HTTP status escape."""
    def __init__(self, code, status=None):
        super().__init__(code)
        self.code, self.status = code, status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@contextmanager
def deadline():
    if threading.current_thread() is not threading.main_thread():
        raise PaperError("main_thread_required")
    if signal.getitimer(signal.ITIMER_REAL)[0]:
        raise PaperError("existing_deadline_conflict")
    previous = signal.getsignal(signal.SIGALRM)
    def expired(*_):
        raise PaperError("request_timeout")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 20)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def permitted(host, policy_path="/etc/codex/network-policy.json"):
    try:
        policy = json.loads(Path(policy_path).read_text())["http_network_policy"]
        return policy.get("type") == "restricted" and any(
            r.get("host") == host for r in policy.get("egress_rules", []))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def validate_route(origin, method, path, params):
    if origin not in (PAPER, DATA):
        raise PaperError("origin_forbidden")
    if not isinstance(path, str) or any(c in path for c in "?#%\\"):
        raise PaperError("route_forbidden")
    if origin == PAPER:
        paths = {
            "/v2/account": set(), "/v2/clock": set(), "/v2/positions": set(),
            "/v2/orders": {"status", "limit", "nested"},
            "/v2/orders:by_client_order_id": {"client_order_id"},
            **{f"/v2/assets/{s}": set() for s in ("AAPL", "MSFT", "BTCUSD")},
        }
        if method == "GET" and path in paths and set(params) <= paths[path]:
            return
        if method == "POST" and path == "/v2/orders" and not params:
            return
        if method == "DELETE" and re.fullmatch(r"/v2/orders/[0-9a-fA-F-]{36}", path) and not params:
            return
    elif method == "GET":
        paths = {
            "/v2/stocks/quotes/latest": {"symbols", "feed"},
            "/v2/stocks/bars": {"symbols", "feed", "timeframe", "start", "end", "limit", "sort", "adjustment"},
            "/v1beta3/crypto/us/latest/quotes": {"symbols"},
            "/v1beta3/crypto/us/bars": {"symbols", "timeframe", "start", "end", "limit", "sort"},
        }
        if path in paths and set(params) <= paths[path]:
            return
    raise PaperError("route_forbidden")


class ProtectedTransport:
    """Destination confirmation comes from the operator, not guessed metadata.

    Default transport is read-only. A trusted caller may enable writes only
    after a separately approved exact order; no CLI exposes that switch.
    """
    def __init__(self, *, confirmed_secret_destinations=(), allow_write=False):
        self.destinations = frozenset(confirmed_secret_destinations)
        self.allow_write = allow_write is True

    def preflight(self, origin):
        if origin not in (PAPER, DATA):
            raise PaperError("origin_forbidden")
        host = urllib.parse.urlsplit(origin).hostname
        if not permitted(host):
            raise PaperError("network_host_not_permitted")
        if host not in self.destinations:
            raise PaperError("secret_destination_unconfirmed")
        if not all(n in os.environ for n in SECRET_NAMES):
            raise PaperError("missing_protected_placeholder")
        proxy = urllib.request.getproxies().get("https")
        if not proxy or urllib.request.proxy_bypass(host):
            raise PaperError("existing_proxy_required")
        return proxy

    def request(self, origin, method, path, *, params=None, body=None, before_send=None):
        params = params or {}
        validate_route(origin, method, path, params)
        if method != "GET" and not self.allow_write:
            raise PaperError("write_not_authorized")
        if method != "POST" and body is not None:
            raise PaperError("unexpected_body")
        proxy = self.preflight(origin)
        # No retries, auth retry handlers, redirects, netrc or alternate route.
        context = ssl.create_default_context()
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise PaperError("tls_verification_required")
        http.client.HTTPConnection.debuglevel = 0
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy}),
            urllib.request.HTTPSHandler(context=context, debuglevel=0), NoRedirect())
        url = origin + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        headers = {"APCA-API-KEY-ID": os.environ[SECRET_NAMES[0]],
                   "APCA-API-SECRET-KEY": os.environ[SECRET_NAMES[1]],
                   "Accept": "application/json", "Accept-Encoding": "identity",
                   "Connection": "close"}
        payload = None
        if body is not None:
            payload = json.dumps(body, allow_nan=False).encode()
            if len(payload) > 4096:
                raise PaperError("request_size_limit")
            headers["Content-Type"] = "application/json"
        try:
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)
            with deadline():
                if before_send is not None:
                    before_send()
                with opener.open(request, timeout=10) as response:
                    if response.status == 204:
                        return None
                    if response.status != 200:
                        raise PaperError("unexpected_http_status", response.status)
                    data = response.read(MAX_BYTES + 1)
                    if len(data) > MAX_BYTES:
                        raise PaperError("response_size_limit")
                    return json.loads(data, parse_float=str, object_pairs_hook=unique_object,
                                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise PaperError("http_error", status) from None
        except PaperError:
            raise
        except Exception:
            raise PaperError("transport_or_response_failure") from None
