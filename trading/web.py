"""Local dashboard. Model calls require an explicit same-origin POST."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time

from .connected import AlpacaMarket, ConnectedError, analyze, configuration_status


class Dashboard:
    def __init__(self, market=None, advisor=None):
        self.market = market or AlpacaMarket()
        self.advisor = advisor or analyze
        self.lock = threading.Lock()
        self.snapshot = None
        self.last_fetch = float("-inf")
        self.last_analysis = float("-inf")
        self.result = None
        self.token = secrets.token_urlsafe(32)

    def refresh(self):
        if self.snapshot is None or time.monotonic() - self.last_fetch >= 15:
            self.snapshot = self.market.fetch()
            self.last_fetch = time.monotonic()
        return {"market": self.snapshot, "configuration": configuration_status(), "analysis": self.result}

    def evaluate(self):
        if time.monotonic() - self.last_analysis < 60:
            raise ConnectedError("Attendre 60 secondes entre deux demandes IA")
        if not configuration_status()["ai_credentials"] or not configuration_status()["ai_model"]:
            raise ConnectedError("IA indisponible : démarrer Ollama et installer le modèle local, ou configurer le fournisseur choisi")
        self.last_analysis = time.monotonic()
        self.result = None
        # A fresh acquisition is mandatory before a paid model call.
        self.snapshot = self.market.fetch()
        self.last_fetch = time.monotonic()
        self.result = self.advisor(self.snapshot)
        return {"market": self.snapshot, "configuration": configuration_status(), "analysis": self.result}


def make_server(port=8080, dashboard=None):
    state = dashboard or Dashboard()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No request headers, paths or model output in access logs.

        def reply(self, status, data, content_type="application/json; charset=utf-8"):
            raw = data.encode() if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def host_allowed(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def do_GET(self):
            if not self.host_allowed():
                self.reply(403, {"error": "Hôte refusé"})
                return
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path in assets:
                name, kind = assets[self.path]
                text = (Path(__file__).parent / "static" / name).read_text(encoding="utf-8")
                self.reply(200, text.replace("__CSRF_TOKEN__", state.token), kind)
            elif self.path == "/api/health":
                self.reply(200, {"status": "ok", "configuration": configuration_status()})
            elif self.path == "/api/market":
                self.run_locked(state.refresh)
            else:
                self.reply(404, {"error": "Page inconnue"})

        def run_locked(self, function):
            if not state.lock.acquire(blocking=False):
                self.reply(409, {"error": "Opération déjà en cours"})
                return
            try:
                self.reply(200, function())
            except ConnectedError as exc:
                self.reply(422, {"error": str(exc)})
            except Exception:
                self.reply(500, {"error": "Erreur interne ; aucune exécution d’ordre"})
            finally:
                state.lock.release()

        def do_POST(self):
            origins = (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")
            if (not self.host_allowed() or self.headers.get("Origin") not in origins or
                    not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), state.token)):
                self.reply(403, {"error": "Origine ou jeton de requête refusé"})
                return
            if self.path != "/api/analyze":
                self.reply(404, {"error": "Route inconnue"})
                return
            if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
                self.reply(400, {"error": "Cette commande n’accepte aucun contenu"})
                return
            self.run_locked(state.evaluate)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Tableau de bord local Alpaca + IA locale")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    with make_server(args.port) as server:
        print(f"Trad : interface locale sur le port {server.server_port}. Aucun ordre courtier.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
