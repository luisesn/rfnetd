import os
import pty
import threading
import time
import unittest

from rfnetd import radio
from rfnetd.config import Config


class FakeHub:
    def __init__(self):
        self.lines = []

    def log_src(self, src, text, level="info"):
        self.lines.append((src, level, text))


class FakeRadio(threading.Thread):
    """Responde como un TK-780 sobre un pty."""

    def __init__(self, master):
        super().__init__(daemon=True)
        self.master, self.channel, self.busy, self.func = master, 3, False, {radio.SQUELCH: False, radio.SCAN: False}
        self.running = True
        self.mute = False

    def run(self):
        buf = b""
        while self.running:
            try:
                buf += os.read(self.master, 64)
            except OSError:
                return
            while b"\x03" in buf:
                raw, _, buf = buf.partition(b"\x03")
                body = raw[raw.rfind(b"\x02") + 1:]
                if self.mute:
                    continue
                if body[0] == radio.INTERROGATE and body[1] == radio.CHANNEL:
                    self.send([radio.CHANNEL, *radio.bcd_ascii(1), *radio.bcd_ascii(self.channel)])
                elif body[0] == radio.INTERROGATE and body[1] == radio.SQUELCH_STATUS:
                    self.send([radio.ACK_ACTIVE if self.busy else radio.ACK_INACTIVE])
                elif body[0] == radio.CYCLICAL:
                    self.func[body[1]] = not self.func[body[1]]
                    self.send([radio.ACK_ACTIVE if self.func[body[1]] else radio.ACK_INACTIVE])
                elif body[0] == radio.CHANNEL:
                    self.channel = radio._num(body[4:7])

    def send(self, content):
        try:
            os.write(self.master, radio.encode(content))
        except OSError:
            pass  # el test ya cerró el pty


class RadioTests(unittest.TestCase):
    def setUp(self):
        self.master, slave = pty.openpty()
        cfg = Config()
        cfg.cp.set("radio", "port", os.ttyname(slave))
        cfg.cp.set("radio", "channels", "CH ZERO | 0\nDATA | 144.950\nVHF | 145.500\nAPRS | 144.800")
        cfg.cp.set("radio", "channel_delay_ms", "10")
        self.hub, self.fake = FakeHub(), FakeRadio(self.master)
        self.fake.start()
        self.r = radio.Radio(cfg, self.hub)
        self.r._open()

    def tearDown(self):
        self.fake.running = False
        self.r._close()

    def test_codec(self):
        self.assertEqual(radio.bcd_ascii(7), [48, 48, 55])
        self.assertEqual(radio.bcd_ascii(42), [48, 52, 50])
        self.assertEqual(radio.decode(b"\x02L001003"), {"kind": "channel", "group": 1, "channel": 3})
        self.assertEqual(radio.decode(b"noise\x02" + bytes([radio.ACK_ACTIVE]))["kind"], "active")
        self.assertIsNone(radio.decode(b"sin stx"))

    def test_get_channel_uses_table(self):
        self.assertEqual(self.r.get_channel()["channel"], 3)
        self.assertEqual((self.r.state["name"], self.r.state["freq"]), ("APRS", 144.8))

    def test_set_channel(self):
        self.r.set_channel(1)
        self.assertEqual((self.r.state["channel"], self.r.state["name"]), (1, "DATA"))

    def test_busy(self):
        self.assertFalse(self.r.query_busy())
        self.fake.busy = True
        self.assertTrue(self.r.query_busy())

    def test_set_function_reaches_desired_state(self):
        self.assertTrue(self.r.set_scan(True))
        self.assertTrue(self.fake.func[radio.SCAN])
        self.assertFalse(self.r.set_scan(False))
        self.assertFalse(self.fake.func[radio.SCAN])
        self.assertFalse(self.r.set_scan(False))  # ya estaba apagado: conmuta dos veces y acaba apagado
        self.assertFalse(self.fake.func[radio.SCAN])

    def test_timeout_returns_none(self):
        self.fake.mute = True
        self.r.timeout = 0.1
        self.assertIsNone(self.r.query_busy())

    def test_unplugged_port_raises(self):
        os.close(self.master)
        with self.assertRaises(OSError):
            self.r.query_busy()

    def test_parse_channels(self):
        ch = radio.parse_channels("A | 1.5\n# nota\n\nB sin freq")
        self.assertEqual(ch, [{"n": "A", "f": 1.5}, {"n": "B sin freq", "f": 0.0}])


if __name__ == "__main__":
    unittest.main()
