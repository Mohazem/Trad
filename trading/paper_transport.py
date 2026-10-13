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
MAX_ERROR_BYTES = 4096
# Deliberately narrow documented examples, not inferred code ranges. See the
# official source links in PAPER_README.md. Unknown codes are omitted.
PROVIDER_ERROR_CODES = {403: frozenset({40310000}), 422: frozenset({42210000})}


def safe_request_id(value):
    # Alpaca documents a 32-hex X-Request-ID. No whitespace, URLs, prose,
    # alternate headers or loosely shaped tokens are retained.
    return value if type(value) is str and re.fullmatch(r"[0-9a-fA-F]{32}", value) else None


def safe_provider_code(status, value):
    return value if type(value) is int and value in PROVIDER_ERROR_CODES.get(status, ()) else None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class PaperError(RuntimeError):
    """Fixed local code plus a small validated HTTP diagnostic projection."""
    def __init__(self, code, status=None, *, request_id=None, provider_error_code=None):
        super().__init__(code)
        self.code = code
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.request_id = safe_request_id(request_id)
        self.provider_error_code = safe_provider_code(self.status, provider_error_code)

    def safe_details(self):
        result = {"code": self.code, "http_status": self.status}
        if self.request_id is not None:
            result["request_id"] = self.request_id
        if self.provider_error_code is not None:
            result["provider_error_code"] = self.provider_error_code
        return result


def http_error_details(error, sensitive_values=()):
    """Read a bounded error inside the request deadline; retain no raw text.

    Response headers/body are untrusted. Even an accepted ID/code cannot prove
    whether the provider or proxy produced the response. Only a small projection
    leaves this function; errors while extracting diagnostics are ignored.
    """
    request_id = provider_code = None
    try:
        ids = error.headers.get_all("X-Request-ID", [])
        if len(ids) == 1:
            request_id = safe_request_id(ids[0])
    except Exception:
        pass
    try:
        content_type = error.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        encoding = error.headers.get("Content-Encoding", "identity").lower()
        if content_type == "application/json" and encoding == "identity":
            raw = error.read(MAX_ERROR_BYTES + 1)
            if len(raw) <= MAX_ERROR_BYTES:
                value = json.loads(raw, object_pairs_hook=unique_object,
                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if isinstance(value, dict):
                    provider_code = safe_provider_code(error.code, value.get("code"))
    except Exception:
        pass
    # Reject reflected values already supplied to this request. In production
    # these are protected placeholders; the proxy owns the actual credentials.
    for value in sensitive_values:
        if type(value) is str and value:
            if request_id is not None and value.casefold() in request_id.casefold():
                request_id = None
            if provider_code is not None and value in str(provider_code):
                provider_code = None
    return {"request_id": request_id, "provider_error_code": provider_code}


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
                failure = None
                try:
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
                    details = http_error_details(exc, (headers["APCA-API-KEY-ID"], headers["APCA-API-SECRET-KEY"]))
                    failure = PaperError("http_error", exc.code, **details)
                    try:
                        exc.close()
                    except Exception:
                        pass
                # Raise outside the HTTPError handler: do not attach the raw
                # response exception (headers/body/URL) as exception context.
                if failure is not None:
                    raise failure from None
        except PaperError:
            raise
        except Exception:
            raise PaperError("transport_or_response_failure") from None
