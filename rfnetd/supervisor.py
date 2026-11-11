"""Arranca y vigila direwolf -> tncattach -> post_up -> captura."""
import json
import os
import pty
import re
import signal
import socket
import subprocess
import threading
import time
import traceback

from . import dwconf


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
                    try:
                        self.on_line(self.name, ln.decode("utf-8", "replace"))
                    except Exception:  # noqa: BLE001 - una línea rara no debe matar el lector
                        # Si este hilo muere, nadie vacía el pty, el proceso se bloquea al escribir
                        # y parece «colgado» sin dar error.
                        traceback.print_exc()
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


PTT_RE = re.compile(r"^\s*PTT\s+(/dev/\S+)", re.M)
TTY_FD_RE = re.compile(r"^/dev/(tty(USB|ACM|S|AMA)|serial/)")


def group_pids(pgid):
    """pid del proceso y de sus descendientes (mismo grupo: con «runuser» el hijo directo no es Direwolf)."""
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open("/proc/%s/stat" % d) as f:
                    if int(f.read().rsplit(")", 1)[1].split()[2]) == pgid:
                        out.append(int(d))
            except (OSError, ValueError, IndexError):
                pass
    return out


def ptt_check(device, pid):
    """(ok, problema) del puerto serie del PTT de Direwolf.

    Si el adaptador USB se desconecta y vuelve, Direwolf sigue con el descriptor del nodo
    borrado («(deleted)») y la emisora no recibe PTT aunque el puerto exista otra vez.
    """
    if not os.path.exists(device):
        return False, "el puerto %s no existe (¿adaptador USB desconectado?)" % device
    if not pid:
        return True, None
    seen = denied = False
    try:
        for p in group_pids(pid) or [pid]:
            for fd in os.listdir("/proc/%d/fd" % p):
                try:
                    target = os.readlink("/proc/%d/fd/%s" % (p, fd))
                except OSError:
                    denied = True
                    continue
                if TTY_FD_RE.match(target.replace(" (deleted)", "")):
                    if target.endswith(" (deleted)"):
                        return False, "Direwolf tiene el PTT en un puerto que ya no existe (%s); hay que reiniciarlo" % target
                    seen = True
    except OSError:
        return True, None   # sin permiso para mirar: no se juzga
    if not seen and denied:
        return True, None   # no se pudieron leer todos los descriptores: no se juzga
    if not seen:
        return False, "Direwolf no tiene abierto el puerto del PTT %s" % device
    return True, None


class Restart(Exception):
    """Reinicio pedido por el usuario (sin espera ni cuenta de fallos)."""


class Supervisor(threading.Thread):
    def __init__(self, cfg, hub, controls=None):
        super().__init__(daemon=True, name="supervisor")
        self.cfg = cfg
        self.hub = hub
        self.controls = controls
        self.stop_evt = threading.Event()
        self.kick = threading.Event()
        self.procs = {}
        self.restarts = {}
        self.state = "parado"
        self.cycles = 0
        self.ptt_dev = None
        self.ptt = None
        self.ptt_fix_at = 0.0
        self.rx_silence = None
        self.chain_on = controls.get("chain_on") if controls else True

    # --- control desde la web ---
    def set_chain(self, on):
        self.chain_on = bool(on)
        if self.controls:
            self.controls.set("chain_on", self.chain_on)
        self.hub.log("Direwolf + red %s por el usuario" % ("activados" if on else "desactivados"))
        self.kick.set()

    def restart(self):
        self.hub.log("Reinicio solicitado por el usuario")
        self.kick.set()

    # --- utilidades ---
    def _wait(self, what, check, timeout):
        end = time.time() + timeout
        while time.time() < end and not self.stop_evt.is_set():
            if self.kick.is_set():
                raise Restart()
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

    def _dw_argv(self):
        argv = self.cfg.argv("direwolf")
        if any("{conf}" in a for a in argv):
            argv = [a.replace("{conf}", self._build_dw_conf()) for a in argv]
        return argv

    def _build_dw_conf(self):
        base = self.cfg.get("direwolf", "base_conf")
        if not base:
            raise RuntimeError("cmd usa {conf} pero [direwolf] base_conf está vacío")
        with open(base) as f:
            text = f.read()
        opts = self.controls.get("dw_opts") if self.controls else {}
        out = dwconf.generate(text, dwconf.validate(opts), self.cfg.get("direwolf", "audio_in") or None)
        m = PTT_RE.search(re.sub(r"(?m)^\s*#.*$", "", out))
        self.ptt_dev = m[1] if m else None
        path = os.path.join(self.cfg.get("data", "dir"), "direwolf.conf")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w") as f:
            f.write(out)
        user = self.cfg.get("direwolf", "user")
        if user:
            import pwd
            pw = pwd.getpwnam(user)
            os.chown(path, 0, pw.pw_gid)
        return path

    def _start(self, name, section, user=None, argv=None):
        p = Proc(name, argv or self.cfg.argv(section), self.hub.line, user)
        p.restarts = self.restarts.get(name, 0)
        self.procs[name] = p
        self.hub.log("Arrancando %s: %s" % (name, " ".join(p.argv)))
        p.start()

    def _ipv6_path(self):
        return "/proc/sys/net/ipv6/conf/%s/disable_ipv6" % self.cfg.get("network", "interface")

    def _disable_ipv6(self):
        iface = self.cfg.get("network", "interface")
        path = self._ipv6_path()
        if not os.path.exists(path):
            self.hub.log("IPv6 no activo en %s (el kernel lo omite con MTU < 1280)" % iface)
            return
        try:
            with open(path, "w") as f:
                f.write("1")
            self.hub.log("IPv6 desactivado en %s" % iface)
        except OSError as e:
            self.hub.log("No se pudo desactivar IPv6 en %s: %s" % (iface, e), "error")

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
        self.ptt, self.hub.ptt_problem = None, None
        self.rx_silence = None
        if cfg.bool("direwolf", "enabled"):
            self._start("direwolf", "direwolf", cfg.get("direwolf", "user") or None, self._dw_argv())
            if not self._wait("el puerto KISS", self._port_open, cfg.int("direwolf", "ready_timeout")):
                raise RuntimeError("Direwolf no abrió el puerto KISS a tiempo")
            self.hub.log("Direwolf listo (KISS %s:%s)" % (cfg.get("direwolf", "kiss_host"),
                                                          cfg.get("direwolf", "kiss_port")))
        if cfg.bool("tncattach", "enabled"):
            self._start("tncattach", "tncattach")
            if not self._wait("la interfaz", self._iface_up, cfg.int("tncattach", "ready_timeout")):
                raise RuntimeError("no apareció la interfaz %s" % cfg.get("network", "interface"))
            self.hub.log("Interfaz %s creada" % cfg.get("network", "interface"))
            if cfg.bool("network", "disable_ipv6"):
                self._disable_ipv6()
            self._post_up()
        if cfg.bool("capture", "enabled"):
            self._start("capture", "capture")
        self.state = "activo"
        up_since = time.time()
        while not self.stop_evt.wait(1):
            if self.kick.is_set() or not self.chain_on:
                raise Restart()
            for p in self.procs.values():
                if not p.alive():
                    raise RuntimeError("%s terminó (código %s)" % (p.name, p.p.returncode))
            if time.time() - up_since > 120:
                self.cycles = 0  # estable: reinicia el backoff
            if self.cfg.bool("direwolf", "ptt_watch") and int(time.time()) % 5 == 0:
                self._watch_ptt(up_since)
            self._watch_rx(up_since)

    def _watch_ptt(self, up_since):
        dw = self.procs.get("direwolf")
        if not self.ptt_dev or not dw or time.time() - up_since < 15:
            self.ptt = None
            self.hub.ptt_problem = None
            return
        ok, why = ptt_check(self.ptt_dev, dw.p.pid)
        before = self.ptt
        self.ptt = {"device": self.ptt_dev, "ok": ok, "problem": why}
        self.hub.ptt_problem = None if ok else why
        if (before or {}).get("ok", True) != ok:
            self.hub.log("PTT %s: %s" % (self.ptt_dev, "recuperado" if ok else why), "info" if ok else "error")
        if not ok and "reiniciarlo" in why and self.cfg.bool("direwolf", "ptt_autorestart") \
                and time.time() - self.ptt_fix_at > 120:
            self.ptt_fix_at = time.time()
            self.hub.log("PTT: reiniciando Direwolf para que vuelva a abrir %s" % self.ptt_dev, "warn")
            self.kick.set()

    def _watch_rx(self, up_since):
        """Reinicia Direwolf si lleva demasiado tiempo sin decodificar tramas de radio."""
        mins = self.cfg.int("direwolf", "rx_watchdog_minutes")
        dw = self.procs.get("direwolf")
        if mins <= 0 or not dw:
            self.rx_silence = None
            return
        ref = max(up_since, (self.hub.counters.get("last_rf_rx") or 0))
        silent = time.time() - ref
        self.rx_silence = silent
        if silent > mins * 60:
            self.hub.log("Direwolf lleva %d min sin recibir ninguna trama por radio (límite %d): reiniciándolo"
                         % (silent // 60, mins), "warn")
            self.kick.set()

    def _teardown(self):
        for name in ("capture", "tncattach", "direwolf"):
            p = self.procs.get(name)
            if p:
                p.stop()

    def run(self):
        while not self.stop_evt.is_set():
            if not self.chain_on:
                self.state = "desactivado"
                self.kick.clear()
                self.stop_evt.wait(0.5)
                continue
            self.kick.clear()
            restart = False
            try:
                self._cycle()
            except Restart:
                restart = True
            except Exception as e:  # noqa: BLE001 - el servicio debe sobrevivir
                self.hub.log("Fallo: %s" % e, "error")
            self.state = "parado" if self.stop_evt.is_set() else "reiniciando"
            self._teardown()
            if self.stop_evt.is_set():
                break
            if restart:
                self.cycles = 0
                continue
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

    # --- estado ---
    def iface_info(self):
        iface = self.cfg.get("network", "interface")
        base = "/sys/class/net/" + iface

        def rd(name, default=None):
            try:
                with open(os.path.join(base, name)) as f:
                    return f.read().strip()
            except OSError:
                return default

        info = {"name": iface, "exists": os.path.exists(base), "operstate": rd("operstate"), "mtu": rd("mtu"),
                "txqueuelen": rd("tx_queue_len")}
        for k in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "tx_dropped"):
            v = rd("statistics/" + k)
            info[k] = int(v) if v and v.isdigit() else None
        try:
            r = subprocess.run(["ip", "-j", "addr", "show", "dev", iface], capture_output=True, text=True, timeout=3)
            data = json.loads(r.stdout)[0]
            info["addrs"] = ["%s/%s" % (a["local"], a["prefixlen"]) for a in data.get("addr_info", [])]
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            info["addrs"] = []
        v6 = None
        try:
            with open(self._ipv6_path()) as f:
                v6 = f.read().strip()
        except OSError:
            pass
        info["ipv6"] = "no activo (MTU < 1280)" if v6 is None else ("desactivado" if v6 == "1" else "ACTIVO")
        info["ipv6_addrs"] = [a for a in info["addrs"] if ":" in a]
        return info

    def status(self):
        iface = self.cfg.get("network", "interface")
        return {
            "state": self.state,
            "chain_on": self.chain_on,
            "interface": iface,
            "interface_up": self._iface_up(),
            "ptt": self.ptt,
            "rx_silence": self.rx_silence,
            "procs": {n: {"alive": p.alive(), "pid": p.p.pid if p.p else None,
                          "since": p.started, "restarts": p.restarts} for n, p in self.procs.items()},
        }
