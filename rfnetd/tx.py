"""Transmisión APRS: mensajes (con ack y reintentos), estado y posición.

Construye tramas AX.25 UI y las entrega por KISS TCP a Direwolf, que gestiona CSMA y PTT.
Solo transmite si [tx] enabled = yes, hay indicativo propio y (si hay radio controlada) la radio
está en la frecuencia APRS y no está en scan.
"""
import re
import socket
import threading
import time
import unicodedata
from collections import deque

CALL_RE = re.compile(r"^([A-Z0-9]{1,6})(?:-(\d|1[0-5]))?$")
BLN_RE = re.compile(r"^(BLN|NWS)")
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
MAX_TEXT = 67


class TxError(ValueError):
    pass


def parse_call(text):
    m = CALL_RE.match(text.strip().upper())
    if not m:
        raise TxError("indicativo no válido: %r" % text[:20])
    return m[1], int(m[2] or 0)


def _addr(call, last):
    name, ssid = parse_call(call)
    return bytes([ord(c) << 1 for c in name.ljust(6)] + [0x60 | (ssid << 1) | (1 if last else 0)])


def ax25_ui(src, dest, digis, info):
    """Trama AX.25 UI (control 0x03, PID 0xF0), sin FCS."""
    addrs = [dest, src] + list(digis)
    out = b"".join(_addr(a, i == len(addrs) - 1) for i, a in enumerate(addrs))
    return out + b"\x03\xf0" + info


def kiss_encode(frame, port=0):
    body = bytes([port << 4]) + frame
    body = body.replace(bytes([FESC]), bytes([FESC, TFESC])).replace(bytes([FEND]), bytes([FESC, TFEND]))
    return bytes([FEND]) + body + bytes([FEND])


def clean_text(text, limit=MAX_TEXT):
    """ASCII imprimible sin los caracteres reservados de APRS ({ | ~); quita acentos."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).strip()
    if not text:
        raise TxError("el texto está vacío")
    bad = sorted({c for c in text if not 0x20 <= ord(c) <= 0x7E or c in "{|~"})
    if bad:
        raise TxError("caracteres no permitidos en APRS: " + " ".join(bad[:6]))
    if len(text) > limit:
        raise TxError("texto demasiado largo (%d > %d)" % (len(text), limit))
    return text


def _lat(v):
    h = "N" if v >= 0 else "S"
    v = abs(v)
    d = int(v)
    return "%02d%05.2f%s" % (d, (v - d) * 60, h)


def _lon(v):
    h = "E" if v >= 0 else "W"
    v = abs(v)
    d = int(v)
    return "%03d%05.2f%s" % (d, (v - d) * 60, h)


class Transmitter:
    def __init__(self, cfg, hub, history, radio, controls=None):
        self.cfg, self.hub, self.history, self.radio = cfg, hub, history, radio
        self.controls = controls
        self.enabled = cfg.bool("tx", "enabled")
        self.me = cfg.get("station", "callsign").upper()
        self.tocall = cfg.get("tx", "tocall").upper()
        self.path = [p for p in re.split(r"[,\s]+", cfg.get("tx", "path").upper()) if p]
        self.host, self.port = cfg.get("direwolf", "kiss_host"), cfg.int("direwolf", "kiss_port")
        self.min_interval = cfg.float("tx", "min_interval")
        self.max_per_hour = cfg.int("tx", "max_per_hour")
        self.waits = [int(x) for x in re.split(r"[,\s]+", cfg.get("tx", "retry_seconds").strip()) if x]
        self.require_freq = cfg.float("tx", "require_freq")
        self.allow_channels = [x.strip().upper() for x in cfg.get("tx", "allow_channels").split(",") if x.strip()]
        self.auto_ack = cfg.bool("tx", "auto_ack")
        try:   # lo elegido en la web manda sobre rfnetd.conf
            self.auto_ack = bool(controls.get("auto_ack"))
        except (AttributeError, KeyError):
            pass
        self.symbol = (cfg.get("tx", "symbol") + "/-")[:2]
        self.lock = threading.Lock()
        self.sent = deque()          # marcas de tiempo de las transmisiones (límite por hora)
        self.last_tx = 0.0
        self.pending = {}            # (destino, msgno) -> estado del reintento
        self.acked = {}              # (origen, msgno) -> instante del último ack que enviamos
        self.msgno = int(time.time()) % 90000
        self.stop_evt = threading.Event()
        if self.enabled:
            try:
                parse_call(self.me)
                for p in [self.tocall] + self.path:
                    parse_call(p)
            except TxError as e:
                self.enabled = False
                hub.log("TX desactivado: %s" % e, "warn")
            if self.me.startswith("N0CALL"):
                self.enabled = False
                hub.log("TX desactivado: define [station] callsign", "warn")
            n = history.fail_pending()
            if n:
                hub.log("TX: %d mensaje(s) pendientes de un arranque anterior marcados como fallidos" % n, "warn")

    # --- comprobaciones ---
    def blocked(self):
        """None si se puede transmitir; si no, el motivo."""
        if not self.enabled:
            return "transmisión desactivada: define [tx] enabled = yes e indicativo propio"
        if getattr(self.hub, "ptt_problem", None):
            return "PTT de Direwolf: " + self.hub.ptt_problem
        if self.radio.enabled:
            st = self.radio.snapshot()
            if not st["connected"]:
                return "radio no conectada: no se sabe en qué canal está"
            if st.get("scan"):
                return "la radio está en scan"
            if self.require_freq:
                freq_ok = abs((st.get("freq") or 0) - self.require_freq) <= 0.0005
                chan_ok = (st.get("name") or "").strip().upper() in self.allow_channels
                if not (freq_ok or chan_ok):
                    extra = (" ni en " + " / ".join(self.allow_channels)) if self.allow_channels else ""
                    now = st.get("name") or (("%.3f" % st["freq"]) if st.get("freq") else "desconocido")
                    return "la radio no está en el canal APRS (%.3f MHz)%s; ahora %s%s" % (
                        self.require_freq, extra, now,
                        (" (%.3f MHz)" % st["freq"]) if st.get("freq") and st.get("name") else "")
        return None

    def _reserve(self, now, force=False):
        with self.lock:
            while self.sent and self.sent[0] < now - 3600:
                self.sent.popleft()
            if len(self.sent) >= self.max_per_hour:
                raise TxError("límite de %d transmisiones por hora alcanzado" % self.max_per_hour)
            if not force and now - self.last_tx < self.min_interval:
                raise TxError("espera %d s entre transmisiones" % max(1, round(self.min_interval - (now - self.last_tx))))
            self.sent.append(now)
            self.last_tx = now

    # --- envío ---
    def _send_kiss(self, data):
        with socket.create_connection((self.host, self.port), timeout=5) as s:
            s.sendall(data)
            time.sleep(0.2)   # deja a Direwolf leer la trama antes de cerrar

    def _transmit(self, info, dest=None, force=False):
        why = self.blocked()
        if why:
            raise TxError(why)
        self._reserve(time.time(), force)
        frame = ax25_ui(self.me, dest or self.tocall, self.path, info.encode("ascii"))
        try:
            self._send_kiss(kiss_encode(frame))
        except OSError as e:
            raise TxError("no se pudo hablar con Direwolf (%s:%s): %s" % (self.host, self.port, e))
        self.hub.log_src("rfnetd", "TX APRS %s>%s%s:%s" % (self.me, self.tocall, "".join("," + p for p in self.path), info))

    def send_message(self, to, text):
        to = to.strip().upper()
        parse_call(to)
        if to == self.me:
            raise TxError("no puedes enviarte un mensaje a ti mismo")
        text = clean_text(text)
        bulletin = bool(BLN_RE.match(to))
        msgno = None
        if not bulletin:
            self.msgno = self.msgno % 99999 + 1
            msgno = str(self.msgno)
        info = ":%-9s:%s%s" % (to, text, ("{" + msgno) if msgno else "")
        self._transmit(info)
        row = self.history.add_message(time.time(), {
            "src": self.me, "to": to, "text": text, "msgno": msgno, "dir": "tx", "gateway": None,
            "path": self.path}, status="pending" if msgno else "sent", tries=1)
        if msgno and row:
            with self.lock:
                self.pending[(to, msgno)] = {"row": row, "info": info, "tries": 1,
                                             "next": time.time() + (self.waits[0] if self.waits else 30)}
        return {"id": row, "msgno": msgno}

    def send_status(self, text):
        self._transmit(">" + clean_text(text, 62))

    def send_position(self, comment=""):
        lat, lon = self.cfg.float("station", "lat"), self.cfg.float("station", "lon")
        if not (lat or lon):
            raise TxError("define [station] lat y lon para enviar la posición")
        comment = clean_text(comment, 43) if comment.strip() else ""
        self._transmit("!%s%s%s%s%s" % (_lat(lat), self.symbol[0], _lon(lon), self.symbol[1], comment))

    # --- recepción: ack de nuestros mensajes y ack automático de los recibidos ---
    def on_packet(self, pkt):
        if pkt.get("type") != "message" or pkt.get("dir") not in ("rx", "ig") or \
                (pkt.get("to") or "").upper() != self.me or pkt["src"] == self.me:
            return
        text = pkt.get("text") or ""
        low = text[:3].lower()
        if low in ("ack", "rej"):
            msgno = text[3:].strip()
            with self.lock:
                self.pending.pop((pkt["src"], msgno), None)
            rid = self.history.settle_tx(pkt["src"], msgno, "acked" if low == "ack" else "rejected")
            if rid:
                self.hub.log("TX: %s confirmó el mensaje #%s" % (pkt["src"], msgno) if low == "ack"
                             else "TX: %s rechazó el mensaje #%s" % (pkt["src"], msgno))
            return
        if self.auto_ack and pkt.get("msgno") and self.blocked() is None:
            key, now = (pkt["src"], pkt["msgno"]), time.time()
            with self.lock:
                if now - self.acked.get(key, 0) < 20:
                    return
                self.acked[key] = now
                for k in [k for k, t in self.acked.items() if t < now - 600]:
                    del self.acked[k]
            try:
                self._transmit(":%-9s:ack%s" % (pkt["src"], pkt["msgno"]), force=True)
            except TxError as e:
                self.hub.log("TX: no se pudo enviar el ack a %s: %s" % (pkt["src"], e), "warn")

    # --- reintentos ---
    def _retry_loop(self):
        while not self.stop_evt.wait(1):
            now = time.time()
            with self.lock:
                due = [(k, v) for k, v in self.pending.items() if v["next"] <= now]
            for key, st in due:
                if st["tries"] > len(self.waits):
                    self._settle(key, "failed", "sin confirmación de %s tras %d envíos" % (key[0], st["tries"]))
                    continue
                try:
                    self._transmit(st["info"])
                except TxError as e:
                    self._settle(key, "failed", "no se pudo reintentar a %s: %s" % (key[0], e))
                    continue
                st["tries"] += 1
                st["next"] = time.time() + (self.waits[st["tries"] - 1] if st["tries"] - 1 < len(self.waits) else self.waits[-1])
                self.history.set_tries(st["row"], st["tries"])
                self.hub.log("TX: reintento %d del mensaje #%s a %s" % (st["tries"], key[1], key[0]))

    def _settle(self, key, status, why):
        with self.lock:
            st = self.pending.pop(key, None)
        if st:
            self.history.set_status(st["row"], status)
            self.hub.log("TX: " + why, "warn")

    def start(self):
        if self.enabled:
            threading.Thread(target=self._retry_loop, daemon=True).start()

    def set_auto_ack(self, on):
        self.auto_ack = bool(on)
        if self.controls:
            self.controls.set("auto_ack", self.auto_ack)

    def shutdown(self):
        self.stop_evt.set()

    def status(self):
        with self.lock:
            pend = [{"to": k[0], "msgno": k[1], "tries": v["tries"], "next": v["next"]} for k, v in self.pending.items()]
            n = len([t for t in self.sent if t > time.time() - 3600])
        return {"enabled": self.enabled, "blocked": self.blocked(), "me": self.me, "path": self.path,
                "tocall": self.tocall, "pending": pend, "sent_last_hour": n, "max_per_hour": self.max_per_hour,
                "auto_ack": self.auto_ack, "max_text": MAX_TEXT, "require_freq": self.require_freq,
                "allow_channels": self.allow_channels}
