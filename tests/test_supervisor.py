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


class ReaderTests(unittest.TestCase):
    def test_reader_survives_handler_errors(self):
        got = []

        def on_line(name, line):
            if line.strip() == "boom":
                raise KeyError("lat")
            got.append(line.strip())

        p = supervisor.Proc("t", ["sh", "-c", "echo uno; echo boom; echo dos"], on_line)
        p.start()
        p.p.wait(5)
        for _ in range(50):
            if len(got) == 2:
                break
            import time
            time.sleep(0.1)
        self.assertEqual(got, ["uno", "dos"])


class Il2pArgvTests(unittest.TestCase):
    def _sup(self, mode, supported=True):
        from rfnetd.config import Config
        hub = mock.Mock()
        controls = mock.Mock()
        controls.get.return_value = {"il2p": mode} if mode is not None else {}
        sup = supervisor.Supervisor(Config(), hub, controls)
        sup._dw_supports_il2p = lambda exe: supported
        return sup, hub

    def test_adds_flag_after_executable(self):
        sup, _ = self._sup("1")
        self.assertEqual(sup._with_il2p(["direwolf", "-t", "0", "-c", "x"]), ["direwolf", "-I", "1", "-t", "0", "-c", "x"])
        sup, _ = self._sup("i0")
        self.assertEqual(sup._with_il2p(["direwolf", "-c", "x"])[:3], ["direwolf", "-i", "0"])

    def test_off_or_unsupported_or_already_present(self):
        for mode, sup_ok, argv in ((None, True, ["direwolf"]), ("", True, ["direwolf"]),
                                   ("1", False, ["direwolf", "-c", "x"]), ("1", True, ["direwolf", "-I", "0"])):
            sup, hub = self._sup(mode, sup_ok)
            self.assertEqual(sup._with_il2p(list(argv)), argv)
        sup, hub = self._sup("1", False)
        sup._with_il2p(["direwolf"])
        self.assertEqual(hub.log.call_args[0][1] if len(hub.log.call_args[0]) > 1 else hub.log.call_args[1].get("level"), "error")


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
