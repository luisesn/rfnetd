"""Control serie de la radio Kenwood TK-780 (protocolo portado de tk780.js).

Trama: STX(0x02) <comando...> ETX(0x03). Se habla a 9600 baudios, 8N2.
"""
import os
import select
import termios
import threading
import time

STX, ETX = 0x02, 0x03
ACK, NACK, ACK_ACTIVE, ACK_INACTIVE = 0x30, 0x31, 0x32, 0x33
INTERROGATE, CYCLICAL = 0x4A, 0x5B
VOLUME_LEVEL, CHANNEL, SQUELCH, SQUELCH_LEVEL, SCAN, SQUELCH_STATUS = 0x4B, 0x4C, 0x49, 0x47, 0x3E, 0x53


def encode(content):
    return bytes([STX, *content, ETX])


def bcd_ascii(n):
    """Número 0-99 -> 3 bytes ASCII ('0', decena, unidad), como pide la radio."""
    return [48, n // 10 + 48, n % 10 + 48]


def _num(b):
    return (b[0] - 48) * 100 + (b[1] - 48) * 10 + (b[2] - 48)


def decode(frame):
    """Bytes de una trama (sin ETX) -> dict {'kind': ..., ...} o None."""
    i = frame.rfind(bytes([STX]))
    if i < 0 or i + 1 >= len(frame):
        return None
    body = frame[i + 1:]
    cmd = body[0]
    if cmd == ACK:
        return {"kind": "ack"}
    if cmd == NACK:
        return {"kind": "nack"}
    if cmd == ACK_ACTIVE:
        return {"kind": "active"}
    if cmd == ACK_INACTIVE:
        return {"kind": "inactive"}
    if cmd == CHANNEL and len(body) >= 7:
        return {"kind": "channel", "group": _num(body[1:4]), "channel": _num(body[4:7])}
    if cmd == SQUELCH_STATUS and len(body) >= 2:
        return {"kind": "active" if body[1] == 0x31 else "inactive"}
    if cmd == SQUELCH_LEVEL and len(body) >= 2:
        return {"kind": "squelch_level", "level": body[1]}
    return {"kind": "other", "cmd": cmd}


def parse_channels(text):
    """Líneas 'NOMBRE | 144.800' -> lista indexada por número de canal."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, freq = line.rpartition("|")
        try:
            out.append({"n": name.strip(), "f": float(freq)})
        except ValueError:
            out.append({"n": line, "f": 0.0})
    return out


class Radio(threading.Thread):
    def __init__(self, cfg, hub):
        super().__init__(daemon=True, name="radio")
        self.hub = hub
        self.enabled = cfg.bool("radio", "enabled")
        self.path = cfg.get("radio", "port")
        self.timeout = cfg.int("radio", "command_timeout_ms") / 1000.0
        self.channel_delay = cfg.int("radio", "channel_delay_ms") / 1000.0
        self.poll = cfg.int("radio", "poll_ms") / 1000.0
        self.reconnect = cfg.int("radio", "reconnect_delay_ms") / 1000.0
        self.channels = parse_channels(cfg.get("radio", "channels"))
        self.fd = None
        self.buf = b""
        self.io = threading.Lock()
        self.stop_evt = threading.Event()
        self.state = {"connected": False, "error": None, "group": None, "channel": None,
                      "name": None, "freq": None, "busy": None, "monitor": None, "scan": None,
                      "updated": None}
        self._last_channel_poll = 0

    # --- puerto serie ---
    def _open(self):
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(fd)
            cflag = termios.CS8 | termios.CSTOPB | termios.CREAD | termios.CLOCAL | termios.B9600
            termios.tcsetattr(fd, termios.TCSANOW, [0, 0, cflag, 0, termios.B9600, termios.B9600, cc])
            termios.tcflush(fd, termios.TCIOFLUSH)
        except Exception:
            os.close(fd)
            raise
        self.fd, self.buf = fd, b""

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.state["connected"] = False

    def _pump(self, wait):
        """Lee lo disponible y devuelve las tramas completas decodificadas."""
        r, _, _ = select.select([self.fd], [], [], wait)
        if r:
            chunk = os.read(self.fd, 256)
            if not chunk:
                raise OSError("puerto cerrado")
            self.buf += chunk
        frames = []
        while bytes([ETX]) in self.buf:
            raw, _, self.buf = self.buf.partition(bytes([ETX]))
            f = decode(raw)
            if f:
                frames.append(f)
        return frames

    def _apply(self, f):
        if f["kind"] == "channel":
            ch = f["channel"]
            info = self.channels[ch] if f["group"] == 1 and ch < len(self.channels) else {"n": "CH%d" % ch, "f": 0.0}
            changed = (self.state["group"], self.state["channel"]) != (f["group"], ch)
            self.state.update(group=f["group"], channel=ch, name=info["n"], freq=info["f"])
            if changed:
                self.hub.log_src("radio", "Canal %d/%d «%s» %s" % (f["group"], ch, info["n"],
                                 ("%.3f MHz" % info["f"]) if info["f"] else "(sin frecuencia en la tabla)"))

    def _xfer(self, content, want, timeout=None):
        """Envía un comando y espera una trama de tipo `want` (o NACK)."""
        with self.io:
            if self.fd is None:
                return None
            for f in self._pump(0):
                self._apply(f)
            os.write(self.fd, encode(content))
            end = time.time() + (timeout or self.timeout)
            while time.time() < end:
                for f in self._pump(0.05):
                    self._apply(f)
                    if f["kind"] in want or f["kind"] == "nack":
                        return f
        return None

    # --- operaciones ---
    def query_busy(self):
        f = self._xfer([INTERROGATE, SQUELCH_STATUS], {"active", "inactive"})
        return None if f is None or f["kind"] == "nack" else f["kind"] == "active"

    def get_channel(self):
        time.sleep(self.channel_delay)
        return self._xfer([INTERROGATE, CHANNEL], {"channel"})

    def _toggle(self, fn):
        f = self._xfer([CYCLICAL, fn], {"active", "inactive"})
        return None if f is None or f["kind"] == "nack" else f["kind"] == "active"

    def _set_function(self, fn, key, want):
        """La radio solo ofrece 'conmutar': se conmuta y, si queda al revés, otra vez."""
        st = self._toggle(fn)
        if st is not None and st != want:
            time.sleep(self.channel_delay)
            st = self._toggle(fn)
        if st is not None:
            self.state[key] = st
            self.hub.log_src("radio", "%s %s" % ("Squelch (función)" if key == "monitor" else "Scan",
                                                 "activado" if st else "desactivado"))
        return st

    def set_monitor(self, want):
        return self._set_function(SQUELCH, "monitor", bool(want))

    def set_scan(self, want):
        return self._set_function(SCAN, "scan", bool(want))

    def set_channel(self, channel, group=1):
        if not (0 <= int(channel) <= 99 and 0 <= int(group) <= 99):
            raise ValueError("canal/grupo fuera de rango")
        with self.io:
            if self.fd is None:
                raise OSError("radio no conectada")
            os.write(self.fd, encode([CHANNEL, *bcd_ascii(int(group)), *bcd_ascii(int(channel))]))
        self.get_channel()

    def snapshot(self):
        s = dict(self.state)
        s["enabled"] = self.enabled
        s["port"] = self.path
        s["channels"] = self.channels
        return s

    # --- hilo ---
    def run(self):
        if not self.enabled:
            return
        while not self.stop_evt.is_set():
            if self.fd is None:
                try:
                    self._open()
                    self.state.update(connected=True, error=None)
                    self.hub.log_src("radio", "Radio conectada (%s)" % self.path)
                except OSError as e:
                    if self.state["error"] != str(e):
                        self.hub.log_src("radio", "No se puede abrir %s: %s" % (self.path, e), "error")
                    self.state.update(connected=False, error=str(e))
                    self.stop_evt.wait(self.reconnect)
                    continue
            try:
                busy = self.query_busy()
                if busy is not None:
                    if busy != self.state["busy"]:
                        self.state["busy"] = busy
                    self.state["updated"] = time.time()
                if time.time() - self._last_channel_poll > 10:
                    self._last_channel_poll = time.time()
                    self.get_channel()
            except OSError as e:
                self.hub.log_src("radio", "Error de puerto serie: %s" % e, "error")
                self._close()
                self.state["error"] = str(e)
                self.stop_evt.wait(self.reconnect)
                continue
            self.stop_evt.wait(self.poll)
        self._close()

    def shutdown(self):
        self.stop_evt.set()
