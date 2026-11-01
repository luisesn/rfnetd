import argparse
import os
import signal
import threading

from .config import Config
from .pipeline import Hub
from .store import StationStore
from .supervisor import Supervisor
from .web import make_server


def main():
    ap = argparse.ArgumentParser(prog="rfnetd")
    ap.add_argument("-c", "--config", default="/etc/rfnetd/rfnetd.conf")
    ap.add_argument("--no-supervisor", action="store_true", help="solo web (con datos existentes)")
    args = ap.parse_args()

    cfg = Config(args.config if os.path.exists(args.config) else None)
    data_dir = cfg.get("data", "dir")
    os.makedirs(data_dir, exist_ok=True)
    store = StationStore(os.path.join(data_dir, "stations.json"),
                         cfg.int("data", "max_age_hours"), cfg.int("data", "track_points"))
    hub = Hub(cfg, store)
    sup = Supervisor(cfg, hub)
    httpd = make_server(cfg, hub, store, sup)
    stop = threading.Event()

    def saver():
        while not stop.wait(60):
            store.save()

    threading.Thread(target=saver, daemon=True).start()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())

    hub.log("rfnetd arrancado; web en http://%s:%s" % (cfg.get("web", "bind"), cfg.get("web", "port")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if not args.no_supervisor:
        sup.start()
    stop.wait()
    hub.log("Parando rfnetd")
    sup.shutdown()
    httpd.shutdown()
    store.save()


if __name__ == "__main__":
    main()
