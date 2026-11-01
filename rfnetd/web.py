"""Servidor HTTP de solo lectura: página estática + API JSON."""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATIC = os.path.join(os.path.dirname(__file__), "static")


def make_server(cfg, hub, store, supervisor):
    class Handler(BaseHTTPRequestHandler):
        server_version = "rfnetd"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(200, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}

            def num(key, default=0):
                try:
                    return int(q.get(key, default))
                except ValueError:
                    return default

            if u.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/status":
                st = supervisor.status()
                with hub.lock:
                    st["counters"] = dict(hub.counters)
                st["station"] = {"callsign": cfg.get("station", "callsign"),
                                 "lat": cfg.float("station", "lat"), "lon": cfg.float("station", "lon")}
                st["now"] = time.time()
                return self._json(st)
            if u.path == "/api/logs":
                rows, last = hub.get_raw(q.get("src", "direwolf"), num("after"), min(num("limit", 500), 2000))
                return self._json({"rows": rows, "last": last})
            if u.path == "/api/events":
                rows, last = hub.get_events(num("after"), min(num("limit", 500), 2000))
                return self._json({"rows": rows, "last": last})
            if u.path == "/api/stations":
                return self._json({"stations": store.snapshot(), "now": time.time()})
            self._send(404, b"not found", "text/plain")

    return ThreadingHTTPServer((cfg.get("web", "bind"), cfg.int("web", "port")), Handler)
