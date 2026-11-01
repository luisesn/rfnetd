import argparse
import os
import signal
import threading

from .audio import Streamer
from .config import Config
from .dwconf import Controls
from .history import History, import_logs
from .pipeline import Hub
from .radio import Radio
from .store import StationStore
from .supervisor import Supervisor
from .tx import Transmitter
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
    history = History(os.path.join(data_dir, "history.db"), cfg.int("history", "days"),
                      cfg.int("history", "max_raw_rows"))
    first_run = history.empty()
    hub = Hub(cfg, store, history)
    controls = Controls(os.path.join(data_dir, "controls.json"))
    radio = Radio(cfg, hub)
    audio = Streamer(cfg, hub)
    tx = hub.tx = Transmitter(cfg, hub, history, radio)
    sup = Supervisor(cfg, hub, controls)
    httpd = make_server(cfg, hub, store, sup, radio, audio, controls, history, tx)
    stop = threading.Event()

    if first_run:
        store.reset()  # las estaciones se reconstruyen con el parser actual
        n = import_logs(hub, cfg, ("rfnetd", "direwolf", "tncattach", "capture", "radio", "audio"))
        hub.log("Histórico inicial: importadas %d líneas de los logs existentes" % n)

    def saver():
        k = 0
        while not stop.wait(60):
            store.save()
            k += 1
            if k % 60 == 0:
                history.prune()

    threading.Thread(target=saver, daemon=True).start()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())

    hub.log("rfnetd arrancado; web en http://%s:%s" % (cfg.get("web", "bind"), cfg.get("web", "port")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if not args.no_supervisor:
        sup.start()
        radio.start()
        tx.start()
    stop.wait()
    hub.log("Parando rfnetd")
    sup.shutdown()
    radio.shutdown()
    tx.shutdown()
    audio.shutdown()
    httpd.shutdown()
    store.save()


if __name__ == "__main__":
    main()
