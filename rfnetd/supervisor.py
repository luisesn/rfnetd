"""Arranca y vigila direwolf -> tncattach -> post_up -> captura."""
import os
import pty
import signal
import socket
import subprocess
import threading
import time


class Proc:
    """Proceso hijo bajo pty (así escribe sus logs línea a línea)."""

    def __init__(self, name, argv, on_line, user=None):
        self.name = name
        self.argv = argv
        self.on_line = on_line
        self.user = user
        self.p = None
        self.started = None
        self.restarts = 0

    def start(self):
        argv = self.argv
        if self.user:
            argv = ["runuser", "-u", self.user, "--"] + argv
        master, slave = pty.openpty()
        self.p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                                  close_fds=True, start_new_session=True)
        os.close(slave)
        self.started = time.time()
        threading.Thread(target=self._read, args=(master,), daemon=True).start()

    def _read(self, fd):
        buf = b""
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    self.on_line(self.name, ln.decode("utf-8", "replace"))
        except OSError:
            pass
        finally:
            if buf:
                self.on_line(self.name, buf.decode("utf-8", "replace"))
            os.close(fd)

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def stop(self):
        if self.p is None or self.p.poll() is not None:
            return
        for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
            try:
                os.killpg(self.p.pid, sig)
            except ProcessLookupError:
                return
            try:
                self.p.wait(wait)
                return
            except subprocess.TimeoutExpired:
                pass


class Supervisor(threading.Thread):
    def __init__(self, cfg, hub):
        super().__init__(daemon=True, name="supervisor")
        self.cfg = cfg
        self.hub = hub
        self.stop_evt = threading.Event()
        self.procs = {}
        self.restarts = {}
        self.state = "parado"
        self.cycles = 0

    # --- utilidades ---
    def _wait(self, what, check, timeout):
        end = time.time() + timeout
        while time.time() < end and not self.stop_evt.is_set():
            if check():
                return True
            for p in self.procs.values():
                if not p.alive():
                    raise RuntimeError("%s terminó antes de que %s estuviera listo" % (p.name, what))
            time.sleep(0.5)
        return False

    def _port_open(self):
        try:
            with socket.create_connection((self.cfg.get("direwolf", "kiss_host"),
                                           self.cfg.int("direwolf", "kiss_port")), timeout=1):
                return True
        except OSError:
            return False

    def _iface_up(self):
        return os.path.exists("/sys/class/net/%s" % self.cfg.get("network", "interface"))

    def _start(self, name, section, user=None):
        p = Proc(name, self.cfg.argv(section), self.hub.line, user)
        p.restarts = self.restarts.get(name, 0)
        self.procs[name] = p
        self.hub.log("Arrancando %s: %s" % (name, " ".join(p.argv)))
        p.start()

    def _post_up(self):
        for argv in self.cfg.commands("network", "post_up"):
            try:
                r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
                msg = "post_up %s -> código %d %s" % (" ".join(argv), r.returncode, (r.stderr or r.stdout).strip())
                self.hub.log(msg, "info" if r.returncode == 0 else "error")
            except (OSError, subprocess.TimeoutExpired) as e:
                self.hub.log("post_up %s falló: %s" % (" ".join(argv), e), "error")

    # --- ciclo ---
    def _cycle(self):
        cfg = self.cfg
        self.state = "arrancando"
        self.procs = {}
        if cfg.bool("direwolf", "enabled"):
            self._start("direwolf", "direwolf", cfg.get("direwolf", "user") or None)
            if not self._wait("el puerto KISS", self._port_open, cfg.int("direwolf", "ready_timeout")):
                raise RuntimeError("Direwolf no abrió el puerto KISS a tiempo")
            self.hub.log("Direwolf listo (KISS %s:%s)" % (cfg.get("direwolf", "kiss_host"),
                                                          cfg.get("direwolf", "kiss_port")))
        if cfg.bool("tncattach", "enabled"):
            self._start("tncattach", "tncattach")
            if not self._wait("la interfaz", self._iface_up, cfg.int("tncattach", "ready_timeout")):
                raise RuntimeError("no apareció la interfaz %s" % cfg.get("network", "interface"))
            self.hub.log("Interfaz %s creada" % cfg.get("network", "interface"))
            self._post_up()
        if cfg.bool("capture", "enabled"):
            self._start("capture", "capture")
        self.state = "activo"
        up_since = time.time()
        while not self.stop_evt.wait(1):
            for p in self.procs.values():
                if not p.alive():
                    raise RuntimeError("%s terminó (código %s)" % (p.name, p.p.returncode))
            if time.time() - up_since > 120:
                self.cycles = 0  # estable: reinicia el backoff

    def _teardown(self):
        for name in ("capture", "tncattach", "direwolf"):
            p = self.procs.get(name)
            if p:
                p.stop()

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self._cycle()
            except Exception as e:  # noqa: BLE001 - el servicio debe sobrevivir
                self.hub.log("Fallo: %s" % e, "error")
            self.state = "parado" if self.stop_evt.is_set() else "reiniciando"
            self._teardown()
            if self.stop_evt.is_set():
                break
            for name in self.procs:
                self.restarts[name] = self.restarts.get(name, 0) + 1
            self.cycles += 1
            delay = min(60, 2 ** min(self.cycles, 6))
            self.hub.log("Reintentando en %d s" % delay, "warn")
            self.stop_evt.wait(delay)
        self.state = "parado"

    def shutdown(self):
        self.stop_evt.set()
        self._teardown()

    def status(self):
        iface = self.cfg.get("network", "interface")
        return {
            "state": self.state,
            "interface": iface,
            "interface_up": self._iface_up(),
            "procs": {n: {"alive": p.alive(), "pid": p.p.pid if p.p else None,
                          "since": p.started, "restarts": p.restarts} for n, p in self.procs.items()},
        }
