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


class RxWatchTests(unittest.TestCase):
    def _sup(self, mins, last_rx):
        from rfnetd.config import Config
        cfg = Config()
        cfg.cp.set("direwolf", "rx_watchdog_minutes", str(mins))
        hub = mock.Mock()
        hub.counters = {"last_rf_rx": last_rx}
        sup = supervisor.Supervisor(cfg, hub)
        sup.procs = {"direwolf": mock.Mock()}
        return sup

    def test_restarts_after_silence(self):
        import time
        sup = self._sup(30, time.time() - 31 * 60)
        sup._watch_rx(time.time() - 40 * 60)
        self.assertTrue(sup.kick.is_set())

    def test_no_restart_if_recent_rx_or_recent_start(self):
        import time
        sup = self._sup(30, time.time() - 5 * 60)
        sup._watch_rx(time.time() - 90 * 60)
        self.assertFalse(sup.kick.is_set())
        sup = self._sup(30, None)           # sin tramas, pero recién arrancado
        sup._watch_rx(time.time() - 60)
        self.assertFalse(sup.kick.is_set())

    def test_disabled(self):
        sup = self._sup(0, None)
        sup._watch_rx(0)
        self.assertFalse(sup.kick.is_set())


if __name__ == "__main__":
    unittest.main()
