"""Recibe líneas de los procesos: las guarda en bruto, las traduce y actualiza el mapa."""
import logging
import logging.handlers
import os
import threading
import time
from collections import deque

from . import aprs, humanize

SOURCES = ("rfnetd", "direwolf", "tncattach", "capture")


class Hub:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.lock = threading.Lock()
        n = cfg.int("logs", "buffer_lines")
        self.raw = {s: deque(maxlen=n) for s in SOURCES}
        self.raw_seq = {s: 0 for s in SOURCES}
        self.events = deque(maxlen=n)
        self.event_seq = 0
        self.counters = {"aprs_rx": 0, "aprs_tx": 0, "ip_packets": 0, "last_aprs": None}
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

    def _raw(self, src, text):
        with self.lock:
            self.raw_seq[src] += 1
            self.raw[src].append({"seq": self.raw_seq[src], "ts": time.time(), "text": text})
        if src in self.files:
            self.files[src].info(text)

    def event(self, src, level, text, **extra):
        with self.lock:
            self.event_seq += 1
            ev = {"seq": self.event_seq, "ts": time.time(), "src": src, "level": level, "text": text}
            ev.update(extra)
            self.events.append(ev)

    def log(self, text, level="info"):
        """Mensajes del propio servicio."""
        self._raw("rfnetd", text)
        self.event("rfnetd", level, text)

    def line(self, src, raw):
        text = humanize.clean(raw)
        if not text:
            return
        self._raw(src, text)
        handler = {"direwolf": self._direwolf, "tncattach": self._tncattach,
                   "capture": self._capture}.get(src)
        if handler:
            handler(text)

    def _direwolf(self, text):
        pkt = aprs.parse_line(text)
        if pkt:
            with self.lock:
                self.counters["aprs_tx" if pkt["dir"] == "tx" else "aprs_rx"] += 1
                self.counters["last_aprs"] = time.time()
            if pkt["dir"] != "tx":
                self.store.update(pkt)
            self.event("direwolf", "packet", humanize.aprs_packet(pkt), call=pkt["src"])
            return
        res = humanize.direwolf_line(text)
        if res:
            self.event("direwolf", res[0], res[1])

    def _tncattach(self, text):
        res = humanize.tncattach_line(text)
        if res:
            self.event("tncattach", res[0], res[1])

    def _capture(self, text):
        res = humanize.tcpdump_line(text)
        if res:
            with self.lock:
                self.counters["ip_packets"] += 1
            self.event("capture", res[0], res[1])

    # --- consultas para la web ---
    def get_raw(self, src, after=0, limit=500):
        with self.lock:
            rows = [r for r in self.raw.get(src, ()) if r["seq"] > after]
            return rows[-limit:], self.raw_seq.get(src, 0)

    def get_events(self, after=0, limit=500):
        with self.lock:
            rows = [e for e in self.events if e["seq"] > after]
            return rows[-limit:], self.event_seq
