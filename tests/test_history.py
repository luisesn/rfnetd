import os
import tempfile
import time
import unittest

from rfnetd import aprs
from rfnetd.config import Config
from rfnetd.history import History, import_logs
from rfnetd.pipeline import Hub
from rfnetd.store import StationStore


def make(tmp):
    cfg = Config()
    cfg.cp.set("logs", "dir", os.path.join(tmp, "logs"))
    cfg.cp.set("station", "callsign", "N0CALL-5")
    hist = History(os.path.join(tmp, "h.db"))
    store = StationStore(os.path.join(tmp, "s.json"))
    return cfg, hist, store, Hub(cfg, store, hist)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg, self.hist, self.store, self.hub = make(self.tmp)

    def test_pagination_backwards_and_search(self):
        for i in range(250):
            self.hub.ingest("capture", "line %d" % i)
        newest = self.hist.raw("capture", limit=100)
        self.assertEqual([r["text"] for r in newest][-1], "line 249")
        self.assertEqual(len(newest), 100)
        older = self.hist.raw("capture", before=newest[0]["seq"], limit=100)
        self.assertEqual(older[-1]["seq"], newest[0]["seq"] - 1)  # sin huecos ni solapes
        self.assertEqual(len(self.hist.raw("capture", q="line 24")), 11)  # 24, 240..249
        self.assertEqual(len(self.hist.raw("capture", q="100%_")), 0)   # comodines escapados

    def test_ids_continue_after_restart(self):
        self.hub.ingest("capture", "a")
        self.hist.flush()
        last = self.hub.raw_id
        hub2 = Hub(self.cfg, self.store, self.hist)
        self.assertEqual(hub2.raw_id, last)

    def test_messages_dedup_and_conversations(self):
        pkt = aprs.parse_line("[0.3] EA1AAA>APRS,EA1D*::EA1BBB   :hola{1")
        self.assertIsNotNone(self.hist.add_message(time.time(), pkt))
        self.assertIsNone(self.hist.add_message(time.time() + 5, pkt))  # misma copia digipeada
        rep = aprs.parse_line("[0.3] EA1BBB>APRS::EA1AAA   :ack1")
        self.hist.add_message(time.time(), rep)
        bln = aprs.parse_line("[0.3] EA1CCC>APRS::BLN1     :aviso a todos")
        self.hist.add_message(time.time(), bln)
        msgs = self.hist.messages("EA1AAA|EA1BBB")
        self.assertEqual([m["kind"] for m in msgs], ["msg", "ack"])
        self.assertEqual(msgs[0]["heard"], 2)
        convs = {c["conv"]: c for c in self.hist.conversations()}
        self.assertEqual(set(convs), {"EA1AAA|EA1BBB", "BLN:BLN1"})  # los ack no cuentan como conversación
        self.assertTrue(convs["BLN:BLN1"]["bulletin"])
        self.assertEqual(self.hist.messages("EA1AAA|EA1BBB", after=msgs[0]["id"])[0]["kind"], "ack")

    def test_replay_logs_rebuilds_stations_correctly(self):
        d = os.path.join(self.tmp, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "direwolf.log"), "w") as f:
            f.write("2026-11-01 18:05:42,004 [0.2] EA1DGP-3>APDW15,WIDE1-1,WIDE2-1:}EA1DGP-3>APDW15,TCPIP,EA1DGP-3*:!/8TNuMR(4#  !IGATE\n")
            f.write("2026-11-01 18:05:42,056 [0H] EA1DGP-3>APDW15,N0CALL-5*,WIDE2-1:}EA1DGP-3>APDW15,TCPIP,EA1DGP-3*:!/8TNuMR(4#  !IGATE\n")
            f.write("2026-11-01 18:07:19,875 [0.2] EA1DGP-3>APDW15,WIDE1-1,WIDE2-1:}EB1XYZ-11>APLRG1,TCPIP,EA1DGP-3*:!L8WO>MH%^_  G.../...g...t358h00b-148-4DIGI/IGATE/METEO LoRa\n")
            f.write("linea sin fecha\n")
        n = import_logs(self.hub, self.cfg, ("direwolf",))
        self.assertEqual(n, 3)
        st = {s["call"]: s for s in self.store.snapshot()}
        self.assertAlmostEqual(st["EB1XYZ-11"]["lon"], -4.215, places=2)   # Cabezón de la Sal
        self.assertAlmostEqual(st["EA1DGP-3"]["lon"], -3.78, places=2)     # cerro del IGate: ya no se pisa
        self.assertEqual(len(st["EA1DGP-3"]["track"]), 1)
        self.assertEqual(st["EB1XYZ-11"]["route"], ["EB1XYZ-11", "EA1DGP-3"])
        ev = self.hist.events(src="direwolf")
        self.assertEqual(len(ev), 3)
        self.assertLess(ev[0]["ts"], ev[-1]["ts"])
        self.assertEqual(st["EA1DGP-3"]["count"], 1)  # la copia [0H] no cuenta dos veces
        self.assertTrue(ev[1]["text"].startswith("Digipeado"))

    def test_prune_by_age(self):
        old = time.time() - 40 * 86400
        self.hub.ingest("capture", "viejo", ts=old, replay=True)
        self.hub.ingest("capture", "nuevo")
        self.hist.flush()
        self.hist.prune()
        self.assertEqual([r["text"] for r in self.hist.raw("capture")], ["nuevo"])


if __name__ == "__main__":
    unittest.main()
