"""Audio de la radio por HTTP (MP3) con GStreamer; el pipeline solo corre si hay oyentes."""
import os
import queue
import subprocess
import threading
import time


class Streamer:
    def __init__(self, cfg, hub):
        self.hub = hub
        self.enabled = cfg.bool("audio", "enabled")
        self.device = cfg.get("audio", "device")
        self.rate = cfg.int("audio", "rate")
        self.bitrate = cfg.int("audio", "bitrate")
        self.idle = cfg.int("audio", "idle_stop_seconds")
        self.lock = threading.Lock()
        self.clients = set()
        self.proc = None
        self.error = None
        self.bytes = 0

    def _argv(self):
        return ["gst-launch-1.0", "-q", "alsasrc", "device=" + self.device, "!", "audioconvert", "!",
                "audioresample", "!", "audio/x-raw,rate=%d,channels=1" % self.rate, "!",
                "lamemp3enc", "target=bitrate", "bitrate=%d" % self.bitrate, "cbr=true", "!", "fdsink", "fd=1"]

    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.clients.add(q)
            if self.proc is None or self.proc.poll() is not None:
                self._start()
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def _start(self):
        self.error = None
        self.hub.log_src("audio", "Arrancando captura de audio (%s)" % self.device)
        self.proc = subprocess.Popen(self._argv(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._stderr, args=(self.proc,), daemon=True).start()

    def _stderr(self, proc):
        for line in proc.stderr:
            text = line.decode("utf-8", "replace").strip()
            if text:
                self.error = text
                self.hub.log_src("audio", text, "error")

    def _pump(self, proc):
        idle_since = None
        fd = proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            self.bytes += len(chunk)
            with self.lock:
                for q in list(self.clients):
                    try:
                        q.put_nowait(chunk)
                    except queue.Full:
                        try:
                            q.get_nowait()
                            q.put_nowait(chunk)
                        except (queue.Empty, queue.Full):
                            pass
                if not self.clients:
                    idle_since = idle_since or time.time()
                    if time.time() - idle_since > self.idle:
                        break
                else:
                    idle_since = None
        self._kill(proc)
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(None)  # fin de stream para los oyentes
                except queue.Full:
                    pass
        self.hub.log_src("audio", "Captura de audio detenida")

    @staticmethod
    def _kill(proc):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, 15)
                proc.wait(3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass

    def shutdown(self):
        if self.proc:
            self._kill(self.proc)

    def status(self):
        with self.lock:
            return {"enabled": self.enabled, "device": self.device, "listeners": len(self.clients),
                    "running": self.proc is not None and self.proc.poll() is None,
                    "error": self.error, "bytes": self.bytes}
