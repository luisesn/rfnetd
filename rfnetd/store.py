"""Estado de estaciones APRS (posición, ruta, rastro) con persistencia JSON."""
import json
import os
import threading
import time

from .aprs import symbol_name


class StationStore:
    def __init__(self, path, max_age_hours=48, track_points=50):
        self.path = path
        self.max_age = max_age_hours * 3600
        self.track_points = track_points
        self.lock = threading.Lock()
        self.stations = {}
        self.dirty = False
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                self.stations = json.load(f)
        except (OSError, ValueError):
            self.stations = {}
        self._expire()

    def _expire(self):
        limit = time.time() - self.max_age
        for k in [k for k, v in self.stations.items() if v.get("last_heard", 0) < limit]:
            del self.stations[k]

    def update(self, pkt, now=None):
        """Registra un paquete. Devuelve True si cambió el mapa."""
        key = pkt.get("name") or pkt["src"]
        now = now or time.time()
        with self.lock:
            st = self.stations.setdefault(key, {"call": key, "count": 0, "track": []})
            st["count"] += 1
            st["last_heard"] = now
            st["via"] = pkt.get("gateway") or pkt["src"]
            st["gateway"] = pkt.get("gateway")
            st["via_internet"] = bool(pkt.get("via_internet"))
            st["path"] = pkt["path"]
            st["route"] = [pkt["src"]] + list(pkt["digis"])
            st["dir"] = pkt["dir"]
            k = "ig_count" if pkt["dir"] == "ig" else "rf_count"
            st[k] = st.get(k, 0) + 1
            if pkt.get("type") == "status":
                st["status"] = pkt["text"]
            if "lat" not in pkt:
                self.dirty = True
                return False
            for k_src, k_dst in (("lat", "lat"), ("lon", "lon"), ("symbol", "symbol"), ("table", "table"),
                                 ("comment", "comment"), ("speed_kn", "speed_kn"), ("course", "course")):
                if k_src in pkt:
                    st[k_dst] = pkt[k_src]
            st["type"] = pkt["type"]
            st["symbol_name"] = symbol_name(pkt.get("table"), pkt.get("symbol"))
            tr = st["track"]
            pt = [round(pkt["lat"], 5), round(pkt["lon"], 5), int(now)]
            if not tr or tr[-1][:2] != pt[:2]:
                tr.append(pt)
                del tr[: -self.track_points]
            self.dirty = True
            return True

    def reset(self):
        with self.lock:
            self.stations = {}
            self.dirty = True

    def snapshot(self):
        with self.lock:
            return [dict(v) for v in self.stations.values()]

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            self._expire()
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(self.stations, f)
            os.replace(tmp, self.path)
            self.dirty = False
