"""Recibe líneas de los procesos: las guarda en bruto, las traduce y actualiza mapa e histórico."""
import logging
import logging.handlers
import os
import threading
import time
from collections import deque

from . import aprs, humanize

SOURCES = ("rfnetd", "direwolf", "tncattach", "capture", "radio", "audio")


class Hub:
    def __init__(self, cfg, store, history):
        self.cfg = cfg
        self.store = store
        self.history = history
        self.lock = threading.Lock()
        n = cfg.int("logs", "buffer_lines")
        self.raw = {s: deque(maxlen=n) for s in SOURCES}
        self.events = deque(maxlen=n)
        # los ids continúan donde lo dejó el histórico, así son únicos y crecientes entre reinicios
        self.raw_id, self.event_id = history.max_ids()
        self.tx = None   # Transmitter, se asigna desde __main__
        self.counters = {"aprs_rx": 0, "aprs_ig": 0, "aprs_tx": 0, "ip_packets": 0, "last_aprs": None}
        self.files = {}
        self._open_files()

    def _open_files(self):
        d = self.cfg.get("logs", "dir")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return
        for s in SOURCES:
            lg = logging.getLogger("rfnetd.raw." + s)
            lg.propagate = False
            lg.setLevel(logging.INFO)
            lg.handlers[:] = []
            h = logging.handlers.RotatingFileHandler(
                os.path.join(d, s + ".log"), maxBytes=self.cfg.int("logs", "max_bytes"),
                backupCount=self.cfg.int("logs", "backups"))
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            lg.addHandler(h)
            self.files[s] = lg

    def _raw(self, src, text, ts=None, replay=False):
        ts = ts or time.time()
        with self.lock:
            self.raw_id += 1
            row = {"seq": self.raw_id, "ts": ts, "text": text}
            self.raw[src].append(row)
        self.history.add_raw(row["seq"], ts, src, text)
        if src in self.files and not replay:
            self.files[src].info(text)

    def event(self, src, level, text, ts=None, **extra):
        ts = ts or time.time()
        with self.lock:
            self.event_id += 1
            ev = {"seq": self.event_id, "ts": ts, "src": src, "level": level, "text": text}
            ev.update(extra)
            self.events.append(ev)
        self.history.add_event(ev["seq"], ts, src, level, text, extra.get("call"))

    def log(self, text, level="info"):
        """Mensajes del propio servicio."""
        self.log_src("rfnetd", text, level)

    def log_src(self, src, text, level="info", ts=None, replay=False):
        """Mensaje propio de un módulo (rfnetd, radio, audio…): log en bruto + evento."""
        self._raw(src, text, ts, replay)
        self.event(src, level, text, ts)

    def line(self, src, raw):
        self.ingest(src, raw)

    def ingest(self, src, raw, ts=None, replay=False):
        text = humanize.clean(raw)
        if not text:
            return
        if src in ("rfnetd", "radio", "audio"):
            if replay:
                self.log_src(src, text, "info", ts, True)
            return
        self._raw(src, text, ts, replay)
        handler = {"direwolf": self._direwolf, "tncattach": self._tncattach, "capture": self._capture}.get(src)
        if handler:
            handler(text, ts, replay)

    def _direwolf(self, text, ts, replay):
        pkt = aprs.parse_line(text)
        if pkt:
            if not replay and pkt["dir"] != "digi":
                with self.lock:
                    self.counters[{"tx": "aprs_tx", "ig": "aprs_ig"}.get(pkt["dir"], "aprs_rx")] += 1
                    self.counters["last_aprs"] = time.time()
            # tx = lo emitimos nosotros; digi = copia de un paquete que ya se registró al recibirlo
            if pkt["dir"] not in ("tx", "digi"):
                self.store.update(pkt, ts)
            if pkt.get("type") == "message" and pkt["dir"] != "digi":
                self.history.add_message(ts or time.time(), pkt)
            # El flujo de APRS-IS (decenas por hora) no va a Eventos; sí los mensajes para esta estación.
            # Sigue en el log de Direwolf, en el mapa y en el histórico de mensajes.
            mine = pkt.get("type") == "message" and (pkt.get("to") or "").upper() == self.cfg.get("station", "callsign").upper()
            if pkt["dir"] != "ig" or mine:
                self.event("direwolf", "packet", humanize.aprs_packet(pkt), ts, call=pkt["src"])
            if self.tx and not replay:
                self.tx.on_packet(pkt)
            return
        res = humanize.direwolf_line(text)
        if res:
            self.event("direwolf", res[0], res[1], ts)

    def _tncattach(self, text, ts, replay):
        res = humanize.tncattach_line(text)
        if res:
            self.event("tncattach", res[0], res[1], ts)

    def _capture(self, text, ts, replay):
        res = humanize.tcpdump_line(text)
        if res:
            if not replay:
                with self.lock:
                    self.counters["ip_packets"] += 1
            self.event("capture", res[0], res[1], ts)

    # --- consultas en vivo para la web (el histórico va por self.history) ---
    def get_raw(self, src, after=0, limit=500):
        with self.lock:
            rows = [r for r in self.raw.get(src, ()) if r["seq"] > after]
            return rows[-limit:], self.raw_id

    def get_events(self, after=0, limit=500):
        with self.lock:
            rows = [e for e in self.events if e["seq"] > after]
            return rows[-limit:], self.event_id
