import unittest

from rfnetd import aprs, humanize


def mice_info(lon_deg, lon_min, lon_hund, speed, course, code=">", table="/"):
    # APRS101: sp = (b4-28)*10 + (b5-28)//10 ; course = ((b5-28)%10)*100 + (b6-28)
    b4 = speed // 10 + 28
    b5 = (speed % 10) * 10 + course // 100 + 28
    b6 = course % 100 + 28
    return "`" + "".join(chr(x) for x in (lon_deg + 28, lon_min + 28, lon_hund + 28, b4, b5, b6)) + code + table


class ParseTests(unittest.TestCase):
    def test_uncompressed_position(self):
        p = aprs.parse_line("[0.4] EA1ABC-9>APRS,EA1XYZ*,WIDE2-1:!4000.88N/00300.48W>088/045 Hola")
        self.assertEqual(p["src"], "EA1ABC-9")
        self.assertAlmostEqual(p["lat"], 40 + 0.88 / 60, places=4)
        self.assertAlmostEqual(p["lon"], -(3 + 0.48 / 60), places=4)
        self.assertEqual((p["course"], p["speed_kn"]), (88, 45))
        self.assertEqual(p["digis"], ["EA1XYZ"])

    def test_timestamped_position(self):
        p = aprs.parse_info("APRS", "/092345z4903.50N/07201.75W>")
        self.assertAlmostEqual(p["lat"], 49 + 3.5 / 60, places=4)
        self.assertAlmostEqual(p["lon"], -(72 + 1.75 / 60), places=4)

    def test_compressed_position(self):
        # ejemplo del estándar APRS101: 49°30'N 72°45'W
        p = aprs.parse_info("APRS", "=/5L!!<*e7>7P[")
        self.assertAlmostEqual(p["lat"], 49.5, places=1)
        self.assertAlmostEqual(p["lon"], -72.75, places=1)

    def test_mice(self):
        info = mice_info(12, 7, 74, 20, 251)
        p = aprs.parse_info("S32U6T", info)
        self.assertEqual(p["type"], "mic-e")
        self.assertAlmostEqual(p["lat"], 33 + 25.64 / 60, places=4)
        self.assertAlmostEqual(p["lon"], -(12 + 7.74 / 60), places=4)
        self.assertEqual((p["speed_kn"], p["course"]), (20, 251))

    def test_message_and_ack(self):
        p = aprs.parse_info("APRS", ":N0CALL-5 :hola que tal{12")
        self.assertEqual((p["to"], p["text"], p["msgno"]), ("N0CALL-5", "hola que tal", "12"))
        self.assertIn("confirma", humanize.aprs_packet(dict(
            aprs.parse_info("APRS", ":N0CALL-5 :ack12"), src="X", dir="rx", digis=[], path=[])))

    def test_object(self):
        p = aprs.parse_info("APRS", ";LEADER   *092345z4903.50N/07201.75W>")
        self.assertEqual((p["type"], p["name"], p["live"]), ("object", "LEADER", True))

    def test_third_party(self):
        p = aprs.parse_info("APRS", "}EA1AAA>APRS,TCPIP,EA1IGA*:!4000.88N/00300.48W>")
        self.assertEqual(p["third_party_from"], "EA1AAA")
        self.assertIn("lat", p)

    def test_path_only_real_digis(self):
        _, path, digis = aprs.path_info("APRS,EA1D1*,WIDE1*,EA1D2*,WIDE2-1")
        self.assertEqual(digis, ["EA1D1", "EA1D2"])
        _, _, none = aprs.path_info("APRS,WIDE1-1,WIDE2-1")
        self.assertEqual(none, [])

    def test_direction(self):
        self.assertEqual(aprs.parse_line("[0L] N0CALL-5>APDW16:>x")["dir"], "tx")
        self.assertEqual(aprs.parse_line("[ig>tx] A>B:>x")["dir"], "ig")

    def test_non_packet_line(self):
        self.assertIsNone(aprs.parse_line("Audio level = 45(21/12)"))


class HumanizeTests(unittest.TestCase):
    def test_position_sentence(self):
        p = aprs.parse_line("[0.4] EA1ABC-9>APRS,EA1XYZ*:!4000.88N/00300.48W>088/045 Hola")
        s = humanize.aprs_packet(p)
        self.assertIn("EA1ABC-9", s)
        self.assertIn("coche", s)
        self.assertIn("83 km/h", s)
        self.assertIn("retransmitido por EA1XYZ", s)

    def test_tcpdump(self):
        l = "2026-11-01 14:40:00.123456 IP 44.0.0.1.1234 > 44.0.0.2.22: Flags [S.], seq 1, ack 2, win 8, length 0"
        lvl, s = humanize.tcpdump_line(l)
        self.assertEqual(s, "TCP 44.0.0.1:1234 → 44.0.0.2:22 SYN-ACK 0 bytes")
        _, s = humanize.tcpdump_line("2026-11-01 14:40:00.1 IP 44.0.0.1 > 44.0.0.2: ICMP echo request, id 1, seq 1, length 64")
        self.assertIn("ping 44.0.0.1 → 44.0.0.2", s)

    def test_ansi_stripped(self):
        self.assertEqual(humanize.clean("\x1b[31mhola\x1b[0m\r"), "hola")


if __name__ == "__main__":
    unittest.main()
