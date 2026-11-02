"""Servidor HTTP: página estática + API JSON + audio.

Seguridad: con [web] password todo exige Basic auth; sin contraseña la web es de solo lectura
(sin control de radio/Direwolf ni audio). Las peticiones POST exigen además la cabecera
X-Requested-With: rfnetd y JSON, que un sitio ajeno no puede enviar (anti-CSRF).
"""
import base64
import hmac
import json
import os
import queue
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import dwconf
from .tx import TxError

STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_BODY = 16384


def make_server(cfg, hub, store, supervisor, radio, audio, controls, history, tx):
    password = cfg.get("web", "password")
    user = cfg.get("web", "user")
    control = bool(password)

    def dw_state():
        base_path = cfg.get("direwolf", "base_conf")
        out = {"managed": False, "chain_on": supervisor.chain_on, "state": supervisor.state,
               "procs": supervisor.status()["procs"]}
        if not base_path:
            return out
        try:
            with open(base_path) as f:
                vals, avail = dwconf.parse_base(f.read())
        except OSError as e:
            out["error"] = str(e)
            return out
        saved = controls.get("dw_opts")
        for k, v in saved.items():
            vals[k] = v
        out.update(managed=True, values=vals, schema=dwconf.schema(avail), saved=sorted(saved))
        return out

    class Handler(BaseHTTPRequestHandler):
        server_version = "rfnetd"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode())

        def _err(self, code, msg):
            self._json({"error": msg}, code)

        def _authed(self):
            if not password:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    u, _, p = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                    ok_u = hmac.compare_digest(u.encode(), user.encode())
                    ok_p = hmac.compare_digest(p.encode(), password.encode())
                    if ok_u and ok_p:
                        return True
                except (ValueError, UnicodeDecodeError):
                    pass
            self._send(401, b"autenticacion requerida", "text/plain",
                       {"WWW-Authenticate": 'Basic realm="rfnetd", charset="UTF-8"'})
            return False

        # ---------- GET ----------
        def do_GET(self):
            if not self._authed():
                return
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
            m = re.fullmatch(r"/symbols/(aprs-symbols-48-[012]\.png)", u.path)
            if m:   # hojas de iconos APRS (aprs.fi, ver static/symbols/COPYRIGHT.md)
                with open(os.path.join(STATIC, "symbols", m[1]), "rb") as f:
                    return self._send(200, f.read(), "image/png", {"Cache-Control": "max-age=86400"})
            if u.path == "/api/status":
                st = supervisor.status()
                with hub.lock:
                    st["counters"] = dict(hub.counters)
                st["station"] = {"callsign": cfg.get("station", "callsign"),
                                 "lat": cfg.float("station", "lat"), "lon": cfg.float("station", "lon")}
                st["control"] = control
                st["radio"] = {k: v for k, v in radio.snapshot().items() if k != "channels"}
                st["audio"] = audio.status()
                st["now"] = time.time()
                return self._json(st)
            if u.path == "/api/logs":
                rows, last = hub.get_raw(q.get("src", "direwolf"), num("after"), min(num("limit", 500), 2000))
                return self._json({"rows": rows, "last": last})
            if u.path == "/api/events":
                rows, last = hub.get_events(num("after"), min(num("limit", 500), 2000))
                return self._json({"rows": rows, "last": last})
            if u.path == "/api/history/raw":
                src = q.get("src", "direwolf")
                if src not in hub.raw:
                    return self._err(400, "fuente desconocida")
                return self._json({"rows": history.raw(src, num("before"), num("until"), q.get("q", "")[:100],
                                                       min(num("limit", 200), 1000))})
            if u.path == "/api/history/events":
                return self._json({"rows": history.events(q.get("src", ""), num("before"), num("until"),
                                                          q.get("q", "")[:100], min(num("limit", 200), 1000))})
            if u.path == "/api/conversations":
                return self._json({"rows": history.conversations()})
            if u.path == "/api/tx":
                return self._json(dict(tx.status(), control=control))
            if u.path == "/api/messages":
                if q.get("ids"):
                    ids = [i for i in q["ids"].split(",") if i.isdigit()]
                    return self._json({"rows": history.messages_by_ids(ids)})
                return self._json({"rows": history.messages(q.get("conv", ""), num("before"), num("after"),
                                                            q.get("q", "")[:100], min(num("limit", 100), 500), q.get("to", "")[:12]),
                                   "me": cfg.get("station", "callsign").upper()})
            if u.path == "/api/stations":
                return self._json({"stations": store.snapshot(), "now": time.time()})
            if u.path == "/api/radio":
                return self._json(dict(radio.snapshot(), control=control))
            if u.path == "/api/direwolf":
                return self._json(dict(dw_state(), control=control))
            if u.path == "/api/network":
                return self._json(supervisor.iface_info())
            if u.path == "/api/audio":
                return self._json(dict(audio.status(), control=control))
            if u.path == "/stream":
                return self._stream()
            self._send(404, b"not found", "text/plain")

        def _stream(self):
            if not control or not audio.enabled:
                return self._send(403, b"audio desactivado", "text/plain")
            q = audio.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while True:
                    try:
                        chunk = q.get(timeout=10)
                    except queue.Empty:
                        continue
                    if chunk is None:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                audio.unsubscribe(q)

        # ---------- POST ----------
        def do_POST(self):
            if not control:
                return self._err(403, "control desactivado: define [web] password en rfnetd.conf")
            if not self._authed():
                return
            if self.headers.get("X-Requested-With") != "rfnetd" or \
                    not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._err(400, "falta X-Requested-With o Content-Type JSON")
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n <= MAX_BODY:
                    raise ValueError
                body = json.loads(self.rfile.read(n))
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._err(400, "cuerpo JSON no válido")
            route = urlparse(self.path).path
            try:
                if route == "/api/radio":
                    return self._radio(body)
                if route == "/api/direwolf":
                    return self._direwolf(body)
                if route == "/api/tx":
                    return self._tx(body)
            except TxError as e:
                return self._err(409, str(e))
            except (ValueError, KeyError, TypeError) as e:
                return self._err(400, str(e))
            except OSError as e:
                return self._err(503, str(e))
            self._err(404, "no existe")

        def _radio(self, b):
            if not radio.enabled or not radio.state["connected"]:
                return self._err(503, "radio no conectada")
            act = b.get("action")
            if act == "channel":
                radio.set_channel(int(b["channel"]), int(b.get("group", 1)))
            elif act == "monitor":
                radio.set_monitor(bool(b["on"]))
            elif act == "scan":
                radio.set_scan(bool(b["on"]))
            else:
                raise ValueError("acción desconocida")
            hub.log_src("radio", "Orden desde la web: %s %s" % (act, {k: v for k, v in b.items() if k != "action"}))
            self._json(radio.snapshot())

        def _tx(self, b):
            act = b.get("action")
            who = self.client_address[0]
            if act == "message":
                r = tx.send_message(str(b.get("to", "")), str(b.get("text", "")))
            elif act == "status":
                r = tx.send_status(str(b.get("text", "")))
            elif act == "position":
                r = tx.send_position(str(b.get("comment", "")))
            else:
                raise ValueError("acción desconocida")
            hub.log("Transmisión pedida desde la web (%s): %s" % (who, act))
            self._json(dict(tx.status(), result=r))

        def _direwolf(self, b):
            act = b.get("action")
            if act == "start":
                supervisor.set_chain(True)
            elif act == "stop":
                supervisor.set_chain(False)
            elif act == "restart":
                supervisor.restart()
            elif act == "apply":
                if not cfg.get("direwolf", "base_conf"):
                    raise ValueError("base_conf no configurado")
                opts = dwconf.validate(b.get("options") or {})
                saved = dict(controls.get("dw_opts"))
                saved.update(opts)
                controls.set("dw_opts", saved)
                hub.log("Opciones de Direwolf cambiadas desde la web: %s" % ", ".join(sorted(opts)))
                if supervisor.chain_on:
                    supervisor.restart()
            else:
                raise ValueError("acción desconocida")
            self._json(dw_state())

    return ThreadingHTTPServer((cfg.get("web", "bind"), cfg.int("web", "port")), Handler)
