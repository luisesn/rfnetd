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
        self.assertEqual(on, BASE.replace("\nDEDUP 30", "\nDEDUPE 30"))   # único cambio: DEDUP -> DEDUPE

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


class DigiFilterTests(unittest.TestCase):
    def test_filter_added_replaced_and_removed(self):
        out = dwconf.generate(BASE, {"digi_filter": "! b/EA1DGP-3"})
        self.assertEqual([l for l in out.splitlines() if l.startswith("FILTER")], ["FILTER 0 0 ! b/EA1DGP-3"])
        out = dwconf.generate(out, {"digi_filter": "! b/EA1AAA | b/EA1BBB"})
        self.assertEqual([l for l in out.splitlines() if l.startswith("FILTER")], ["FILTER 0 0 ! b/EA1AAA | b/EA1BBB"])
        self.assertEqual(dwconf.parse_base(out)[0]["digi_filter"], "! b/EA1AAA | b/EA1BBB")
        out = dwconf.generate(out, {"digi_filter": ""})
        self.assertNotIn("FILTER", out)

    def test_other_filters_untouched(self):
        base = BASE + "FILTER IG 0 t/m\nFILTER 0 1 t/wn\n"
        out = dwconf.generate(base, {"digi_filter": "! b/X"})
        self.assertIn("FILTER IG 0 t/m", out)
        self.assertIn("FILTER 0 1 t/wn", out)

    def test_validation(self):
        self.assertEqual(dwconf.validate({"digi_filter": " ! b/EA1DGP-3 "}), {"digi_filter": "! b/EA1DGP-3"})
        for bad in ("a\nIGLOGIN x y", 'a"b', "a;b", "x" * 130):
            with self.assertRaises(ValueError):
                dwconf.validate({"digi_filter": bad})

    def test_dedup_renamed_for_direwolf_1_6(self):
        out = dwconf.generate(BASE, {})
        self.assertIn("DEDUPE 30", out)
        self.assertNotIn("\nDEDUP 30", out)
        self.assertEqual(dwconf.parse_base(BASE)[0]["dedup"], "30")
        self.assertIn("DEDUPE 60", dwconf.generate(BASE, {"dedup": 60}))


class PaclenAndInfoTests(unittest.TestCase):
    def test_paclen(self):
        self.assertEqual(dwconf.parse_base(BASE + "PACLEN 128\n")[0]["paclen"], "128")
        self.assertIn("PACLEN 100", dwconf.generate(BASE, {"paclen": 100}).splitlines())
        out = dwconf.generate(BASE + "PACLEN 128\n", {"paclen": 100})
        self.assertEqual([l for l in out.splitlines() if l.startswith("PACLEN")], ["PACLEN 100"])
        for bad in (0, 3000):
            with self.assertRaises(ValueError):
                dwconf.validate({"paclen": bad})

    def test_schema_has_help_for_every_field(self):
        for f in dwconf.schema({}):
            self.assertTrue(f["help"], f["key"])
        self.assertEqual(next(f for f in dwconf.schema({}) if f["key"] == "paclen")["default"], "256")


class IgateAndOverlayTests(unittest.TestCase):
    def test_overlay_and_ig_filter_and_ig_beacon(self):
        out = dwconf.generate(BASE, dwconf.validate(
            {"beacon_via": "WIDE1-1", "beacon_overlay": "i", "ig_filter": "t/m", "ig_beacon": True}))
        pbs = [l for l in out.splitlines() if l.startswith("PBEACON")]
        self.assertEqual(len(pbs), 2)
        self.assertIn("via=WIDE1-1", pbs[0])
        self.assertIn("overlay=I", pbs[0])
        self.assertNotIn("via=", pbs[1])
        self.assertIn("sendto=IG", pbs[1])
        self.assertIn("overlay=I", pbs[1])
        self.assertIn("lat=43^22.88N", pbs[1])
        self.assertIn("FILTER IG 0 t/m", out.splitlines())
        # el original no cambia y la baliza de radio sigue siendo la principal al releer
        vals = dwconf.parse_base(out)[0]
        self.assertEqual((vals["beacon_via"], vals["beacon_overlay"], vals["ig_filter"], vals["ig_beacon"]), ("WIDE1-1", "I", "t/m", True))

    def test_ig_beacon_not_duplicated_and_can_be_removed(self):
        once = dwconf.generate(BASE, {"ig_beacon": True})
        twice = dwconf.generate(once, {"ig_beacon": True})
        self.assertEqual(len([l for l in twice.splitlines() if l.startswith("PBEACON")]), 2)
        off = dwconf.generate(once, {"ig_beacon": False})
        self.assertEqual(len([l for l in off.splitlines() if l.startswith("PBEACON")]), 1)

    def test_no_ig_beacon_without_radio_beacon(self):
        out = dwconf.generate(BASE, {"beacon": False, "ig_beacon": True})
        self.assertFalse([l for l in out.splitlines() if l.startswith("PBEACON")])

    def test_validation(self):
        for bad in ({"beacon_overlay": "II"}, {"beacon_overlay": "-"}, {"ig_filter": "t/m\nIGLOGIN a b"}):
            with self.assertRaises(ValueError):
                dwconf.validate(bad)
