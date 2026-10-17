"""Read-only market data and bounded Hugging Face advisory calls. No broker access."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .ai_pipeline import AdvisoryPipeline
from .ai_validation import strict_json, timestamp, validate_packet
from .config import Config
from .models import ACCOUNTING_CONTEXT, canonical

SYMBOLS = ("AAPL", "MSFT", "BTC/USD")
MAX_AGE = 180  # minute-bar start timestamps, not a promise of tick freshness


class ConnectedError(RuntimeError):
    """Messages are safe for the UI: never include upstream bodies or headers."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_json(url, *, headers=None, payload=None, timeout=15):
    """Fixed HTTPS origins only; normal certificate verification; no retries."""
    if not (url.startswith("https://data.alpaca.markets/") or
            url == "https://router.huggingface.co/v1/chat/completions" or
            (url == "https://paper-api.alpaca.markets/v2/clock" and payload is None)):
        raise ConnectedError("Destination non autorisée")
    body = canonical(payload).encode() if payload is not None else None
    request = Request(url, data=body, headers={"Accept": "application/json",
                      "Content-Type": "application/json", **(headers or {})})
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(1_048_577)
        return strict_json(raw.decode("utf-8"), limit=1_048_576)
    except HTTPError as exc:
        raise ConnectedError(f"Accès distant refusé ou indisponible (HTTP {exc.code})") from None
    except (URLError, OSError, ValueError, UnicodeError):
        raise ConnectedError("Réponse distante indisponible ou invalide") from None


def credential(primary, alternate=None):
    return os.environ.get(primary) or (os.environ.get(alternate) if alternate else None)


def configuration_status():
    backend = os.environ.get("TRAD_AI_PROVIDER", "ollama")
    model = os.environ.get("TRAD_LOCAL_MODEL", "qwen2.5:1.5b") if backend == "ollama" else os.environ.get("TRAD_HF_MODEL", "")
    ready = bool(credential("TRAD_HF_TOKEN", "HF_TOKEN")) if backend == "huggingface" else False
    if backend == "ollama":
        try:
            ready = any(m.get("name") == model for m in local_json("/api/tags", timeout=1).get("models", []))
        except (ConnectedError, AttributeError, TypeError):
            ready = False
    return {
        "market_credentials": bool(credential("ALPACA_PAPER_KEY", "APCA_API_KEY_ID") and
                                   credential("ALPACA_PAPER_SECRET_", "APCA_API_SECRET_KEY")),
        "ai_credentials": ready,  # compatibility: local mode requires no credentials
        "ai_ready": ready,
        "ai_provider": backend,
        "ai_model": model,
        "analysis_only": True,
    }


def local_json(path, *, payload=None, timeout=30):
    """Ollama loopback only. Never route local prompts through an egress proxy."""
    if path not in ("/api/tags", "/api/chat"):
        raise ConnectedError("Route locale non autorisée")
    body = canonical(payload).encode() if payload is not None else None
    request = Request("http://127.0.0.1:11434" + path, data=body,
                      headers={"Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(1_048_577)
        return strict_json(raw.decode("utf-8"), limit=1_048_576)
    except (URLError, OSError, ValueError, UnicodeError):
        raise ConnectedError("Ollama local indisponible : démarrer le serveur et installer le modèle") from None


class OllamaProvider:
    label = "ollama_local_inference"

    def __init__(self, model=None, transport=local_json):
        self.model = model or os.environ.get("TRAD_LOCAL_MODEL", "qwen2.5:1.5b")
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,150}", self.model):
            raise ConnectedError("Nom de modèle local invalide")
        self.transport = transport

    def complete(self, request, timeout_seconds):
        result = self.transport("/api/chat", payload={
            "model": self.model,
            "messages": [{"role": "system", "content": request.instructions},
                         {"role": "user", "content": request.input_json}],
            "format": strict_json(request.schema_json), "stream": False,
            "keep_alive": "30m", "options": {"temperature": 0, "num_predict": 512,
                                                "num_ctx": 4096, "num_thread": 4}},
            timeout=timeout_seconds)
        message = result.get("message", {})
        if (not result.get("done") or result.get("done_reason") != "stop" or
                message.get("tool_calls") or not isinstance(message.get("content"), str) or
                not message["content"]):
            raise ConnectedError("Réponse locale tronquée ou invalide")
        return message["content"]


def normalize_bars(raw, now):
    if not isinstance(raw, list) or len(raw) > 300:
        raise ConnectedError("Historique de marché invalide")
    result, seen = [], set()
    try:
        for bar in raw:
            at = timestamp(bar["t"])
            price = Decimal(str(bar["c"]))
            if not price.is_finite() or not 0 < price <= Decimal("100000000000"):
                raise ValueError()
            if at > now or at in seen:
                raise ValueError()
            seen.add(at)
            result.append({"asof": at.isoformat(), "close": format(price.quantize(Decimal("0.00000001")), "f")})
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise ConnectedError("Prix ou dates de marché invalides") from None
    return sorted(result, key=lambda b: b["asof"])[-30:]


class AlpacaMarket:
    def __init__(self, transport=request_json):
        self.transport = transport

    def fetch(self, now=None):
        now = now or datetime.now(timezone.utc)
        key = credential("ALPACA_PAPER_KEY", "APCA_API_KEY_ID")
        secret = credential("ALPACA_PAPER_SECRET_", "APCA_API_SECRET_KEY")
        if not key or not secret:
            raise ConnectedError("Accès Alpaca manquant : configurer les clés dans les paramètres sécurisés")
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        session = {"status": "unknown"}
        try:
            clock = self.transport("https://paper-api.alpaca.markets/v2/clock", headers=headers)
            if (type(clock.get("is_open")) is not bool or
                    abs((now - timestamp(clock["timestamp"])).total_seconds()) > MAX_AGE):
                raise ValueError()
            next_open = timestamp(clock["next_open"])
            if not clock["is_open"] and next_open <= now:
                raise ValueError()
            session = {"status": "open" if clock["is_open"] else "closed",
                       "next_open": next_open.isoformat()}
        except (ConnectedError, ValueError, KeyError, TypeError, AttributeError):
            pass  # Unknown session never makes an old observation eligible.
        assets = []
        for symbol in SYMBOLS:
            crypto = symbol == "BTC/USD"
            route = "/v1beta3/crypto/us/bars" if crypto else "/v2/stocks/bars"
            query = {"symbols": symbol, "timeframe": "1Min", "limit": 30, "sort": "desc",
                     "start": (now - timedelta(days=7)).isoformat()}
            if not crypto:
                query["feed"] = "iex"
            try:
                data = self.transport("https://data.alpaca.markets" + route + "?" + urlencode(query), headers=headers)
                bars = normalize_bars(data.get("bars", {}).get(symbol, []), now)
                age = (now - timestamp(bars[-1]["asof"])).total_seconds() if bars else None
                state = "fresh" if len(bars) >= 9 and age is not None and age <= MAX_AGE else "stale"
                if state == "stale" and not crypto and session["status"] == "closed":
                    state = "closed"
                assets.append({"symbol": symbol, "feed": "alpaca_crypto_us" if crypto else "alpaca_iex",
                               "status": state, "age_seconds": age, "bars": bars,
                               "session": session if not crypto else {"status": "continuous"}})
            except ConnectedError as exc:
                assets.append({"symbol": symbol, "status": "error", "error": str(exc), "bars": []})
            except (AttributeError, TypeError):
                assets.append({"symbol": symbol, "status": "error", "error": "Format Alpaca invalide", "bars": []})
        return {"provider": "Alpaca", "retrieved_at": now.isoformat(), "assets": assets,
                "analysis_only": True, "max_age_seconds": MAX_AGE}


def market_packet(market, now):
    sources = []
    with localcontext(ACCOUNTING_CONTEXT):
        for asset in market["assets"]:
            if asset["status"] != "fresh":
                continue
            bars = asset["bars"][-9:]
            if len(bars) < 9:
                continue
            at, symbol = bars[-1]["asof"], asset["symbol"]
            prices = [Decimal(b["close"]) for b in bars]
            base = symbol.lower().replace("/", "-")
            momentum = prices[-1] / prices[0] - 1
            max_step = max(abs(b / a - 1) for a, b in zip(prices, prices[1:]))
            values = {"samples": len(bars), "momentum": format(momentum.quantize(Decimal("0.00000001")), "f"),
                      "max_step": format(max_step.quantize(Decimal("0.00000001")), "f")}
            for kind, content in [("quote", {"price": bars[-1]["close"]}), ("summary", values)]:
                sources.append({"source_id": base + "-" + kind, "symbol": symbol,
                                "kind": kind, "asof": at, "values": content})
    if not sources:
        raise ConnectedError("Aucune série récente avec 9 observations : analyse IA non lancée")
    packet = {"packet_id": "market-" + hashlib.sha256(canonical(sources).encode()).hexdigest()[:40],
              "asof": now.isoformat(), "data_kind": "market", "sources": sources}
    try:
        return validate_packet(packet, now, MAX_AGE, data_kind="market")
    except ValueError:
        raise ConnectedError("Les données ne passent pas les contrôles de fraîcheur ou de format") from None


class HuggingFaceProvider:
    label = "huggingface_inference_live"

    def __init__(self, key, model, transport=request_json):
        if not key or not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,150}", model):
            raise ConnectedError("Configurer TRAD_HF_TOKEN et TRAD_HF_MODEL dans les paramètres")
        self._key, self.model, self.transport = key, model, transport

    def complete(self, request, timeout_seconds):
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": request.instructions},
                                {"role": "user", "content": request.input_json}],
                   "response_format": {"type": "json_schema", "json_schema": {
                       "name": "trading_" + request.role, "strict": True,
                       "schema": strict_json(request.schema_json)}},
                   "max_tokens": 1200, "stream": False}
        data = self.transport("https://router.huggingface.co/v1/chat/completions", headers={"Authorization": "Bearer " + self._key},
                              payload=payload, timeout=timeout_seconds)
        choices = data.get("choices")
        if data.get("error") or not isinstance(choices, list) or len(choices) != 1:
            raise ConnectedError("Réponse IA incomplète")
        choice = choices[0]
        message = choice.get("message", {})
        if (choice.get("finish_reason") != "stop" or message.get("tool_calls") or
                message.get("function_call") or message.get("refusal") or
                not isinstance(message.get("content"), str) or not message["content"]):
            raise ConnectedError("Réponse IA refusée, tronquée ou vide")
        return message["content"]


def analyze(market, provider=None, now=None):
    wall_clock = now is None
    now = now or datetime.now(timezone.utc)
    packet = market_packet(market, now)
    if provider is None:
        backend = os.environ.get("TRAD_AI_PROVIDER", "ollama")
        if backend == "ollama":
            provider = OllamaProvider()
        elif backend == "huggingface":
            provider = HuggingFaceProvider(credential("TRAD_HF_TOKEN", "HF_TOKEN"), os.environ.get("TRAD_HF_MODEL", ""))
        else:
            raise ConnectedError("TRAD_AI_PROVIDER doit être ollama ou huggingface")
    result = AdvisoryPipeline(provider, timeout_seconds=30 if isinstance(provider, OllamaProvider) else 20, data_kind="market").evaluate(
        packet, now, replace(Config(), max_quote_age_seconds=MAX_AGE))
    # Do not show a proposal as current if a source aged out during inference.
    finished = datetime.now(timezone.utc) if wall_clock else now
    try:
        validate_packet(packet, finished, MAX_AGE, data_kind="market")
    except ValueError:
        raise ConnectedError("Données devenues périmées pendant l’analyse : résultat non publié") from None
    included = sorted({s["symbol"] for s in packet["sources"]})
    return {"mode": "market_advisory_only", "model": getattr(provider, "model", provider.label),
            "analyzed_at": now.isoformat(), "symbols_analyzed": included,
            "symbols_excluded": [s for s in SYMBOLS if s not in included],
            "result": asdict(result), "execution": "disabled", "packet": packet}


def main():
    parser = argparse.ArgumentParser(description="Données Alpaca et avis IA local (ou Hugging Face explicite) ; aucun ordre")
    parser.add_argument("--analyze", action="store_true", help="Effectue au plus 3 appels IA, locaux par défaut, sans retry")
    parser.add_argument("--output", help="Nouveau fichier JSON de résultat")
    args = parser.parse_args()
    try:
        market = AlpacaMarket().fetch()
        report = {"market": market, "configuration": configuration_status()}
        if args.analyze:
            report["analysis"] = analyze(market)
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            with Path(args.output).open("x", encoding="utf-8") as file:
                file.write(text + "\n")
        print(text)
        if all(a["status"] == "error" for a in market["assets"]):
            raise ConnectedError("Aucun instrument accessible")
    except (ConnectedError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
