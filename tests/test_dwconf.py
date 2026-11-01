import unittest

from rfnetd import dwconf

BASE = """# base
ADEVICE  plughw:1,0
MYCALL N0CALL-5
MODEM 1200
FIX_BITS 2
PBEACON delay=1  every=30 overlay=S symbol="digi" lat=43^22.88N long=3^52.48W comment="Test Digi" via=WIDE1-1,WIDE2-1
DEDUP 30
DIGIPEAT 0 0 ^WIDE[1-2]-[1-2]$ ^WIDE-$ TRACE
IGSERVER euro.aprs2.net
IGLOGIN N0CALL-5 12345
IGTXVIA 0 WIDE1-1
"""


class DwConfTests(unittest.TestCase):
    def test_parse_base(self):
        vals, avail = dwconf.parse_base(BASE)
        self.assertEqual((vals["mycall"], vals["modem"], vals["dedup"]), ("N0CALL-5", "1200", "30"))
        self.assertEqual((vals["beacon_every"], vals["beacon_via"], vals["beacon_comment"]), ("30", "WIDE1-1,WIDE2-1", "Test Digi"))
        self.assertTrue(vals["igate"] and vals["digipeat"] and avail["igate"])
        self.assertNotIn("12345", str(vals))

    def test_simple_and_append(self):
        out = dwconf.generate(BASE, {"modem": "9600", "txdelay": 25})
        self.assertIn("MODEM 9600", out)
        self.assertIn("TXDELAY 25", out.splitlines())
        self.assertIn("ADEVICE  plughw:1,0", out)

    def test_group_off_then_on_roundtrip(self):
        off = dwconf.generate(BASE, {"igate": False, "beacon": False})
        self.assertIn("# [rfnetd off] IGLOGIN N0CALL-5 12345", off)
        vals, _ = dwconf.parse_base(off)
        self.assertFalse(vals["igate"] or vals["beacon"])
        self.assertTrue(vals["digipeat"])
        on = dwconf.generate(off, {"igate": True, "beacon": True})
        self.assertEqual(on, BASE)

    def test_audio_in_keeps_output_device(self):
        out = dwconf.generate(BASE, {}, audio_in="rf_cap")
        self.assertIn("ADEVICE rf_cap plughw:1,0", out.splitlines())
        out2 = dwconf.generate("ADEVICE plughw:2,0 hw:3,0\n", {}, audio_in="rf_cap")
        self.assertEqual(out2, "ADEVICE rf_cap hw:3,0\n")

    def test_beacon_fields(self):
        out = dwconf.generate(BASE, {"beacon_every": 10, "beacon_comment": "Hola", "beacon_via": "WIDE2-1"})
        pb = [l for l in out.splitlines() if l.startswith("PBEACON")][0]
        self.assertIn("every=10", pb)
        self.assertIn('comment="Hola"', pb)
        self.assertIn("via=WIDE2-1", pb)
        self.assertIn("lat=43^22.88N", pb)

    def test_validation_blocks_injection(self):
        for bad in ({"beacon_comment": 'x"\nIGLOGIN a b'}, {"mycall": "A B"}, {"modem": "1200\nPTT x"},
                    {"txdelay": 999}, {"nope": 1}, {"beacon_via": "A\nB"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                dwconf.validate(bad)
        self.assertEqual(dwconf.validate({"mycall": "n0call-5", "txdelay": "30"}), {"mycall": "N0CALL-5", "txdelay": 30})


if __name__ == "__main__":
    unittest.main()
