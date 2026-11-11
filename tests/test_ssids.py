import os
import tempfile
import time
import unittest

from rfnetd import aprs
from rfnetd.history import History
from rfnetd.ssids import build_groups, split_call


class SplitTests(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_call("EA1ABC-7"), ("EA1ABC", 7))
        self.assertEqual(split_call("EA1ABC"), ("EA1ABC", 0))
        self.assertEqual(split_call("ea1abc-15"), ("EA1ABC", 15))
        self.assertEqual(split_call("EA1ABC-16"), ("EA1ABC-16", 0))   # SSID fuera de rango: no es SSID
        self.assertEqual(split_call("LEADER   "), ("LEADER", 0))


class GroupTests(unittest.TestCase):
    NOW = 1_000_000.0

    def groups(self, **kw):
        live = [{"call": "EA1AAA", "last_heard": self.NOW - 10, "count": 5, "lat": 1.0, "lon": 2.0},
                {"call": "EA1AAA-9", "last_heard": self.NOW - 50, "count": 3},
                {"call": "EA1AAA-7", "last_heard": self.NOW - 5000, "count": 1},
                {"call": "EB1BBB-1", "last_heard": self.NOW - 100, "count": 2}]
        pos = {"EA1AAA-9": (4, self.NOW - 50)}
        sent = {"EA1AAA-7": (2, self.NOW - 4000)}
        rcvd = {"EA1AAA-7": (1, 0), "ZZ9ZZZ": (9, 0)}
        return build_groups(live, pos, sent, rcvd, **kw)

    def test_grouping_and_order(self):
        g = self.groups()
        self.assertEqual([x["base"] for x in g], ["EA1AAA", "EB1BBB"])
        self.assertEqual([m["ssid"] for m in g[0]["members"]], [0, 7, 9])    # sin SSID primero, luego por número
        self.assertEqual((g[0]["pos_n"], g[0]["msg_n"]), (4, 3))
        self.assertEqual(g[0]["last"], self.NOW - 10)

    def test_destinatarios_sin_oir_no_aparecen(self):
        self.assertNotIn("ZZ9ZZZ", [x["base"] for x in self.groups()])

    def test_filters(self):
        self.assertEqual([x["base"] for x in self.groups(q="bbb")], ["EB1BBB"])
        g = self.groups(ssid=9)
        self.assertEqual([[m["call"] for m in x["members"]] for x in g], [["EA1AAA-9"]])
        g = self.groups(ssid=0)
        self.assertEqual([[m["call"] for m in x["members"]] for x in g], [["EA1AAA"]])
        g = self.groups(since=self.NOW - 200)
        self.assertEqual([m["call"] for m in g[0]["members"]], ["EA1AAA", "EA1AAA-9"])


class HistoryPositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = History(os.path.join(self.tmp.name, "h.db"), 30, 1000)

    def tearDown(self):
        self.tmp.cleanup()

    def pkt(self, call, lat, lon, **kw):
        return dict({"src": call, "dir": "rx", "lat": lat, "lon": lon, "path": ["WIDE1-1"], "comment": "hola"}, **kw)

    def test_dedup_and_queries(self):
        t = time.time()
        self.h.add_position(t, "EA1AAA-9", self.pkt("EA1AAA-9", 40.0, -3.0))
        self.h.add_position(t + 30, "EA1AAA-9", self.pkt("EA1AAA-9", 40.0, -3.0))     # misma posición, <10 min: se ignora
        self.h.add_position(t + 60, "EA1AAA-9", self.pkt("EA1AAA-9", 40.1, -3.0))
        self.h.add_position(t + 61, "EA1AAA-7", self.pkt("EA1AAA-7", 41.0, -3.0))
        self.h.add_position(t + 700, "EA1AAA-9", self.pkt("EA1AAA-9", 40.1, -3.0))    # misma, pero pasados 10 min
        rows = self.h.positions(key="EA1AAA-9")
        self.assertEqual(len(rows), 3)
        self.assertGreater(rows[0]["ts"], rows[-1]["ts"])                              # de más nueva a más antigua
        self.assertEqual(len(self.h.positions(base="EA1AAA")), 4)                      # todos los SSID
        self.assertEqual(len(self.h.positions(base="EA1AAA", before=rows[0]["id"])), 3)
        self.assertEqual(self.h.positions(), [])
        pos, _, _ = self.h.station_summaries()
        self.assertEqual(pos["EA1AAA-9"][0], 3)

    def test_messages_by_peer_and_base(self):
        t = time.time()
        for i, (s, d) in enumerate([("EA1AAA-9", "EA1ZZZ-5"), ("EA1ZZZ-5", "EA1AAA-9"), ("EA1AAA", "EA1ZZZ-5"),
                                    ("EA1AAAB-1", "EA1ZZZ-5"), ("EA1ZZZ-5", "ack1")]):
            self.h.add_message(t + i, {"src": s, "to": d, "text": "m%d" % i, "dir": "rx", "path": [], "msgno": str(i)})
        self.assertEqual(len(self.h.messages(peer="EA1AAA-9")), 2)
        self.assertEqual(len(self.h.messages(base="EA1AAA")), 3)        # no mezcla EA1AAAB-1
        self.assertEqual(len(self.h.messages(base="EA1AAA", noack=True)), 3)

    def test_backfill_once(self):
        self.h.add_raw(1, time.time(), "direwolf", "[0.4] EA1ABC-9>APRS,EA1XYZ*:!4000.00N/00300.00W>hola")
        self.h.add_raw(2, time.time(), "direwolf", "linea sin paquete")
        self.h.flush()
        self.assertEqual(self.h.backfill_positions(aprs.parse_line), 1)
        self.assertEqual(len(self.h.positions(key="EA1ABC-9")), 1)
        self.assertEqual(self.h.backfill_positions(aprs.parse_line), 0)   # ya hecho


if __name__ == "__main__":
    unittest.main()
