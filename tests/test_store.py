import os
import tempfile
import unittest

from rfnetd import aprs
from rfnetd.store import StationStore


def pkt(line):
    return aprs.parse_line(line)


class StoreOriginTests(unittest.TestCase):
    def test_rf_and_internet_counted_apart(self):
        with tempfile.TemporaryDirectory() as d:
            st = StationStore(os.path.join(d, "s.json"))
            ig = pkt("[ig>tx] ED1ABC>APRS,TCPIP*,qAC,T2X:!4000.00N/00300.00W-hola")
            rf = pkt("[0.3] EA1XYZ>APRS,WIDE1-1:!4000.00N/00300.00W>coche")
            self.assertEqual((ig["dir"], rf["dir"]), ("ig", "rx"))
            st.update(ig, 100)
            st.update(rf, 101)
            st.update(ig, 102)
            s = {x["call"]: x for x in st.snapshot()}
            self.assertEqual((s["ED1ABC"].get("rf_count", 0), s["ED1ABC"]["ig_count"]), (0, 2))
            self.assertEqual((s["EA1XYZ"]["rf_count"], s["EA1XYZ"].get("ig_count", 0)), (1, 0))
            self.assertEqual((s["EA1XYZ"]["table"], s["EA1XYZ"]["symbol"]), ("/", ">"))


if __name__ == "__main__":
    unittest.main()
