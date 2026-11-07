import os
import unittest
from unittest import mock

from rfnetd import supervisor
from rfnetd.supervisor import PTT_RE, ptt_check


class PttCheckTests(unittest.TestCase):
    def test_regex_ignores_comments_and_gpio(self):
        self.assertEqual(PTT_RE.search("#PTT COM1 RTS\nPTT /dev/ttyUSB1 DTR\n")[1], "/dev/ttyUSB1")
        self.assertIsNone(PTT_RE.search("PTT GPIO 25\n"))

    def test_group_pids_includes_self(self):
        self.assertIn(os.getpid(), supervisor.group_pids(os.getpgid(0)))

    def test_missing_device(self):
        ok, why = ptt_check("/dev/ttyUSB99", 1)
        self.assertFalse(ok)
        self.assertIn("no existe", why)

    def _fds(self, targets):
        names = [str(i) for i in range(len(targets))]
        links = dict(zip(names, targets))
        return mock.patch.multiple(supervisor.os, listdir=lambda p: names if p.startswith('/proc/') and p.endswith('/fd') else [],
                                   readlink=lambda p: links[p.rsplit("/", 1)[1]])

    def test_stale_descriptor_after_replug(self):
        with mock.patch.object(supervisor.os.path, "exists", return_value=True), \
                mock.patch.object(supervisor, "group_pids", return_value=[5]), \
                self._fds(["/dev/snd/timer", "/dev/ttyUSB1 (deleted)", "/dev/pts/3"]):
            ok, why = ptt_check("/dev/serial/by-id/x", 5)
        self.assertFalse(ok)
        self.assertIn("reiniciarlo", why)

    def test_healthy_and_not_open(self):
        with mock.patch.object(supervisor.os.path, "exists", return_value=True), \
                mock.patch.object(supervisor, "group_pids", return_value=[5]):
            with self._fds(["/dev/snd/timer", "/dev/ttyUSB2", "/dev/pts/3"]):
                self.assertEqual(ptt_check("/dev/ttyUSB2", 5), (True, None))
            with self._fds(["/dev/snd/timer", "/dev/pts/3"]):
                self.assertFalse(ptt_check("/dev/ttyUSB2", 5)[0])


if __name__ == "__main__":
    unittest.main()
