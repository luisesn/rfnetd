import os
import socket
import tempfile
import threading
import time
import unittest

from rfnetd import tx as txmod
from rfnetd.config import Config
from rfnetd.history import History
from rfnetd.tx import Transmitter, TxError, ax25_ui, clean_text, kiss_encode


def decode_ax25(frame):
    """Inverso mínimo para comprobar las tramas: (dest, src, digis, info)."""
    calls, i = [], 0
    while True:
        a = frame[i:i + 7]
        name = "".join(chr(b >> 1) for b in a[:6]).strip()
        ssid = (a[6] >> 1) & 0x0F
        calls.append(name + ("-%d" % ssid if ssid else ""))
        i += 7
        if a[6] & 1:
            break
    assert frame[i:i + 2] == b"\x03\xf0"
    return calls[0], calls[1], calls[2:], frame[i + 2:].decode()


class FakeHub:
    def __init__(self):
        self.logs = []

    def log(self, text, level="info"):
        self.logs.append(text)

    log_src = lambda self, src, text, level="info": self.logs.append(text)


class FakeRadio:
    def __init__(self, enabled=False, **st):
        self.enabled = enabled
        self.st = dict(dict(connected=True, scan=False, freq=144.8), **st)

    def snapshot(self):
        return dict(self.st)


def make(radio=None, extra="", tmp=None):
    path = os.path.join(tmp, "c.conf")
    with open(path, "w") as f:
        f.write("[station]\ncallsign = EA1TST-5\nlat = 40.5\nlon = -3.5\n[tx]\nenabled = yes\n"
                + ("" if "min_interval" in extra else "min_interval = 0\n") + "retry_seconds = 1, 1\n" + extra)
    cfg = Config(path)
    hist = History(os.path.join(tmp, "h.db"))
    t = Transmitter(cfg, FakeHub(), hist, radio or FakeRadio())
    t.frames = []
    t._send_kiss = t.frames.append
    return t, hist


def unkiss(data):
    assert data[0] == 0xC0 and data[-1] == 0xC0 and data[1] == 0
    body = data[2:-1].replace(bytes([0xDB, 0xDC]), b"\xc0").replace(bytes([0xDB, 0xDD]), b"\xdb")
    return decode_ax25(body)


class Frames(unittest.TestCase):
    def test_ax25(self):
        f = ax25_ui("EA1TST-5", "APZRFN", ["WIDE1-1"], b"!x")
        self.assertEqual(decode_ax25(f), ("APZRFN", "EA1TST-5", ["WIDE1-1"], "!x"))
        self.assertEqual(f[6] & 1, 0)          # el destino no es la última dirección
        self.assertEqual(f[20] & 1, 1)         # el último digi sí
        self.assertEqual(f[:6], bytes(ord(c) << 1 for c in "APZRFN"))

    def test_kiss_escapes(self):
        k = kiss_encode(b"\xc0\xdb")
        self.assertEqual(k, bytes([0xC0, 0x00, 0xDB, 0xDC, 0xDB, 0xDD, 0xC0]))

    def test_clean_text(self):
        self.assertEqual(clean_text("Atención ñu"), "Atencion nu")
        for bad in ("a{b", "a|b", "a~b", "emoji \U0001F600", "", "   ", "x" * 68):
            with self.assertRaises(TxError):
                clean_text(bad)


class Sending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_message_and_ack(self):
        t, h = make(tmp=self.tmp)
        r = t.send_message("ea1abc-7", "Hola Éxito")
        d, s, digis, info = unkiss(t.frames[0])
        self.assertEqual((d, s, digis), ("APZRFN", "EA1TST-5", ["WIDE1-1"]))
        self.assertEqual(info, ":EA1ABC-7 :Hola Exito{" + r["msgno"])
        row = h.messages_by_ids([r["id"]])[0]
        self.assertEqual((row["status"], row["direction"], row["dest"]), ("pending", "tx", "EA1ABC-7"))
        # el eco [0L] de Direwolf no duplica ni cuenta como copia
        self.assertIsNone(h.add_message(time.time(), {"src": "EA1TST-5", "to": "EA1ABC-7", "text": "Hola Exito",
                          "msgno": r["msgno"], "dir": "tx", "gateway": None, "path": []}))
        self.assertEqual(h.messages_by_ids([r["id"]])[0]["heard"], 1)
        t.on_packet({"type": "message", "dir": "rx", "src": "EA1ABC-7", "to": "EA1TST-5", "text": "ack" + r["msgno"]})
        self.assertEqual(h.messages_by_ids([r["id"]])[0]["status"], "acked")
        self.assertFalse(t.pending)

    def test_ack_from_other_station_ignored(self):
        t, h = make(tmp=self.tmp)
        r = t.send_message("EA1ABC-7", "hola")
        t.on_packet({"type": "message", "dir": "rx", "src": "EA1XYZ", "to": "EA1TST-5", "text": "ack" + r["msgno"]})
        self.assertEqual(h.messages_by_ids([r["id"]])[0]["status"], "pending")

    def test_retries_then_fail(self):
        t, h = make(tmp=self.tmp)
        r = t.send_message("EA1ABC-7", "hola")
        t.start()
        deadline = time.time() + 10
        while time.time() < deadline and h.messages_by_ids([r["id"]])[0]["status"] == "pending":
            time.sleep(0.2)
        t.shutdown()
        row = h.messages_by_ids([r["id"]])[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(len(t.frames), 3)               # envío inicial + 2 reintentos
        self.assertEqual(len({unkiss(f)[3] for f in t.frames}), 1)   # mismo msgno en todos

    def test_bulletin_has_no_msgno(self):
        t, h = make(tmp=self.tmp)
        r = t.send_message("BLN1", "aviso")
        self.assertEqual(unkiss(t.frames[0])[3], ":BLN1     :aviso")
        self.assertIsNone(r["msgno"])
        self.assertFalse(t.pending)

    def test_status_and_position(self):
        t, _ = make(tmp=self.tmp)
        t.send_status("Operativo")
        self.assertEqual(unkiss(t.frames[0])[3], ">Operativo")
        t.send_position("QRV")
        self.assertEqual(unkiss(t.frames[1])[3], "!4030.00N/00330.00W-QRV")

    def test_validation(self):
        t, _ = make(tmp=self.tmp)
        for to in ("", "TOOLONGCALL", "EA1 ABC", "EA1ABC-16", "EA1TST-5"):
            with self.assertRaises(TxError):
                t.send_message(to, "x")
        self.assertEqual(t.frames, [])

    def test_blocked_by_radio(self):
        for st, word in ((dict(connected=False), "no conectada"), (dict(scan=True), "scan"), (dict(freq=146.5), "canal APRS")):
            t, _ = make(FakeRadio(True, **st), "require_freq = 144.8\n", self.tmp)
            with self.assertRaises(TxError) as e:
                t.send_status("x")
            self.assertIn(word, str(e.exception))
            self.assertEqual(t.frames, [])
        t, _ = make(FakeRadio(True), "require_freq = 144.8\n", self.tmp)
        t.send_status("ok")
        self.assertEqual(len(t.frames), 1)

    def test_disabled_and_n0call(self):
        for conf in ("[tx]\nenabled = no\n", "[station]\ncallsign = N0CALL\n[tx]\nenabled = yes\n"):
            path = os.path.join(self.tmp, "d.conf")
            open(path, "w").write(conf)
            t = Transmitter(Config(path), FakeHub(), History(os.path.join(self.tmp, "d.db")), FakeRadio())
            t._send_kiss = lambda d: self.fail("no debe transmitir")
            with self.assertRaises(TxError):
                t.send_status("x")

    def test_rate_limits(self):
        t, _ = make(tmp=self.tmp, extra="max_per_hour = 2\n")
        t.send_status("1"); t.send_status("2")
        with self.assertRaises(TxError):
            t.send_status("3")
        t, _ = make(tmp=self.tmp, extra="min_interval = 60\n")
        t.send_status("1")
        with self.assertRaises(TxError):
            t.send_status("2")

    def test_auto_ack(self):
        t, _ = make(tmp=self.tmp, extra="auto_ack = yes\n")
        pkt = {"type": "message", "dir": "rx", "src": "EA1ABC-7", "to": "EA1TST-5", "text": "hola", "msgno": "42"}
        t.on_packet(pkt)
        t.on_packet(pkt)                                   # copia por otro digipeater: no se repite el ack
        self.assertEqual([unkiss(f)[3] for f in t.frames], [":EA1ABC-7 :ack42"])
        t.on_packet(dict(pkt, dir="digi"))                 # copia nuestra repetida: nada
        t.on_packet(dict(pkt, to="EA1ZZD"))                # no es para nosotros
        t.on_packet(dict(pkt, text="ack7", msgno=None))    # nunca se hace ack de un ack
        self.assertEqual(len(t.frames), 1)

    def test_no_auto_ack_by_default(self):
        t, _ = make(tmp=self.tmp)
        t.on_packet({"type": "message", "dir": "rx", "src": "EA1ABC-7", "to": "EA1TST-5", "text": "hola", "msgno": "42"})
        self.assertEqual(t.frames, [])

    def test_auto_ack_switch_persists(self):
        from rfnetd.dwconf import Controls
        ctl = Controls(os.path.join(self.tmp, "c.json"))
        t, _ = make(tmp=self.tmp)
        t.controls = ctl
        t.set_auto_ack(True)
        self.assertTrue(t.auto_ack)
        pkt = {"type": "message", "dir": "rx", "src": "EA1ABC-7", "to": "EA1TST-5", "text": "hola", "msgno": "42"}
        t.on_packet(pkt)
        self.assertEqual(len(t.frames), 1)
        self.assertTrue(Controls(os.path.join(self.tmp, "c.json")).get("auto_ack"))
        t.set_auto_ack(False)
        self.assertFalse(Controls(os.path.join(self.tmp, "c.json")).get("auto_ack"))

    def test_real_socket(self):
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        got = []
        def serve():
            c, _ = srv.accept(); got.append(c.recv(4096)); c.close()
        threading.Thread(target=serve, daemon=True).start()
        t, _ = make(tmp=self.tmp)
        t.port = srv.getsockname()[1]; t.host = "127.0.0.1"
        del t._send_kiss
        t.send_status("por socket")
        time.sleep(0.5)
        self.assertEqual(unkiss(got[0])[3], ">por socket")

    def test_direwolf_down(self):
        t, _ = make(tmp=self.tmp)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        t.port = port; del t._send_kiss
        with self.assertRaises(TxError) as e:
            t.send_status("x")
        self.assertIn("Direwolf", str(e.exception))


if __name__ == "__main__":
    unittest.main()
