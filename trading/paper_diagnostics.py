"""Bounded, fail-closed error-body projection. No response text is stored.

Runtime placeholders cannot identify a secret reflected after proxy injection.
Consequently regex masking alone is insufficient: only literal reviewed reason
fragments may leave this module. Unknown text, values, URLs and identifiers are
discarded. Recognized fragments are observations, never verified causes.
"""
from html.parser import HTMLParser
import json
import re

MAX_ERROR_BYTES = 4096
MAX_FRAGMENTS = 6
MAX_EXCERPT_CHARS = 512
REDACTED = "[REDACTED]"
MESSAGE_FIELDS = ("message", "error", "detail", "reason", "title")
# These are literal text observations, not an error-code-to-cause mapping.
REASON_FRAGMENTS = (
    "insufficient buying power", "not enough buying power",
    "buying power or shares is not sufficient", "insufficient funds",
    "insufficient balance", "insufficient qty available for order",
    "account is not authorized for trading", "account is not authorized to trade",
    "account is restricted", "account is blocked", "trading is blocked",
    "crypto trading is not enabled", "asset is not tradable", "asset is not active",
    "invalid api key", "invalid credentials", "unauthorized", "forbidden",
    "access denied", "permission denied", "proxy authentication required",
    "blocked by network policy", "not allowed by network policy",
    "not allowed by access control", "proxy access denied", "method not allowed",
    "domain is not allowed", "insufficient permission", "order not found",
    "order does not exist", "client_order_id must be unique",
    "duplicate client_order_id", "invalid limit_price", "sub-penny increment",
    "minimum order size", "too many requests", "rate limit exceeded",
    "service unavailable", "upstream connect error", "unsupported order type",
    "invalid time_in_force",
)
PROXY_FRAGMENTS = frozenset({"proxy authentication required", "proxy access denied",
                             "blocked by network policy", "not allowed by network policy",
                             "not allowed by access control", "upstream connect error"})
FORMATS = frozenset({"json", "text", "html", "unknown"})
STATES = frozenset({"captured", "empty", "truncated", "malformed_json",
                    "invalid_utf8", "unsupported_encoding", "unsupported_media_type",
                    "read_failed", "unsupported_json_shape"})


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class VisibleText(HTMLParser):
    """Ignore tags/attributes/comments and script, style or form content."""
    HIDDEN = frozenset({"script", "style", "form", "textarea", "select"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self.hidden.append(tag)

    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.hidden = self.hidden[:self.hidden.index(tag)]

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def fragments_from(texts, sensitive_values):
    matches = set()
    for text in texts:
        # Never search within a known supplied credential, URL, e-mail address
        # or reflected authentication/cookie line. Nothing unmatched is emitted.
        for value in sensitive_values:
            if type(value) is str and value:
                text = re.sub(re.escape(value), REDACTED, text, flags=re.I)
        text = re.sub(r"(?i)(?:[a-z][a-z0-9+.-]*://|www\.)\S+|\S+@\S+", REDACTED, text)
        text = re.sub(r"(?im)^.*(?:authorization|cookie|apca-api(?:-[\w-]+)?|api[-_ ]?key|"
                      r"secret|password|bearer|token)[ \t]*[:= ].*$", REDACTED, text)
        text = " ".join(text.lower().split())
        for fragment in REASON_FRAGMENTS:
            if re.search(r"(?<![\w/-])" + re.escape(fragment) + r"(?![\w/-])", text):
                matches.add(fragment)
    # Stable catalogue order; no arbitrary ordering or substrings from input.
    return [fragment for fragment in REASON_FRAGMENTS if fragment in matches][:MAX_FRAGMENTS]


def safe_response_detail(value):
    """Rebuild from finite constants even if the caller passes an unsafe dict."""
    if type(value) is not dict:
        return None
    fmt, state = value.get("format"), value.get("state")
    if type(fmt) is not str or fmt not in FORMATS or type(state) is not str or state not in STATES:
        return None
    supplied = value.get("recognized_fragments", [])
    fragments = [part for part in REASON_FRAGMENTS
                 if type(supplied) in (list, tuple) and part in supplied][:MAX_FRAGMENTS]
    excerpt = REDACTED + (" " + (" " + REDACTED + " ").join(fragments) + " " + REDACTED if fragments else "")
    result = {"format": fmt, "state": state, "truncated": value.get("truncated") is True,
              "source": "unverified", "recognized_fragments": fragments,
              "redacted_excerpt": excerpt[:MAX_EXCERPT_CHARS]}
    if any(fragment in PROXY_FRAGMENTS for fragment in fragments):
        result["source_hint"] = "proxy_or_network_policy_text"
    return result


def read_error_body(error, sensitive_values=()):
    """One bounded read inside the caller's deadline; no retry/decompression.

    Returns the safe detail and a candidate numeric code, never decoded values.
    Malformed/truncated JSON is not scanned as text. HTML uses visible text only.
    Even an apparent proxy page is not trusted provenance for the HTTP response.
    """
    detail = {"format": "unknown", "state": "read_failed", "truncated": False}
    code = None
    try:
        types = error.headers.get_all("Content-Type", [])
        encodings = error.headers.get_all("Content-Encoding", [])
        media = types[0].split(";", 1)[0].strip().lower() if len(types) == 1 else ""
        detail["format"] = {"application/json": "json", "application/problem+json": "json",
                            "text/plain": "text", "text/html": "html"}.get(media, "unknown")
        if len(encodings) > 1 or (encodings and encodings[0].strip().lower() != "identity"):
            detail["state"] = "unsupported_encoding"
        elif detail["format"] == "unknown":
            detail["state"] = "unsupported_media_type"
        else:
            raw = error.read(MAX_ERROR_BYTES + 1)
            detail["truncated"] = len(raw) > MAX_ERROR_BYTES
            detail["state"] = "truncated" if detail["truncated"] else "captured"
            if not raw:
                detail["state"] = "empty"
            elif not detail["truncated"] or detail["format"] == "text":
                try:
                    text = raw[:MAX_ERROR_BYTES].decode("utf-8")
                except UnicodeError:
                    detail["state"] = "invalid_utf8"
                else:
                    texts = []
                    if detail["format"] == "json":
                        try:
                            value = json.loads(text, object_pairs_hook=unique_object,
                                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                        except (ValueError, RecursionError):
                            detail["state"] = "malformed_json"
                        else:
                            if type(value) is dict:
                                code = value.get("code") if type(value.get("code")) is int else None
                                texts = [value[key] for key in MESSAGE_FIELDS if type(value.get(key)) is str]
                            else:
                                detail["state"] = "unsupported_json_shape"
                    elif detail["format"] == "html":
                        parser = VisibleText()
                        parser.feed(text)
                        parser.close()
                        texts = parser.parts
                    else:
                        texts = [text]
                    detail["recognized_fragments"] = fragments_from(texts, sensitive_values)
    except Exception:
        detail["state"] = "read_failed"
    return safe_response_detail(detail), code
