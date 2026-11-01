"""Histórico persistente (SQLite): logs en bruto, eventos legibles y mensajes APRS."""
import json
import os
import queue
import re
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw(id INTEGER PRIMARY KEY, ts REAL, src TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS raw_src ON raw(src, id);
CREATE INDEX IF NOT EXISTS raw_ts ON raw(ts);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, src TEXT, level TEXT, text TEXT, call TEXT);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, src TEXT, dest TEXT, text TEXT,
  msgno TEXT, kind TEXT, direction TEXT, gateway TEXT, path TEXT, conv TEXT, heard INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS msg_conv ON messages(conv, id);
"""
BULLETIN_RE = re.compile(r"^(BLN|NWS|SKY|CWA|BOM|ANN)", re.I)
LOG_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d{3} (.*)$")


def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class History:
    def __init__(self, path, days=30, max_raw_rows=400000):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.days, self.max_raw_rows = days, max_raw_rows
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        # limpieza de versiones anteriores: las definiciones de telemetría no son mensajes
        self.db.execute("DELETE FROM messages WHERE text GLOB 'PARM.*' OR text GLOB 'UNIT.*' "
                        "OR text GLOB 'EQNS.*' OR text GLOB 'BITS.*'")
        self.db.commit()
        self.lock = threading.Lock()
        self.q = queue.Queue()
        threading.Thread(target=self._writer, daemon=True, name="history").start()

    # --- escritura por lotes (cuida la SD) ---
    def _writer(self):
        while True:
            batch = [self.q.get()]
            end = time.time() + 1.0
            while len(batch) < 500 and time.time() < end:
                try:
                    batch.append(self.q.get(timeout=max(0.0, end - time.time())))
                except queue.Empty:
                    break
            with self.lock:
                for table, row in batch:
                    if table == "raw":
                        self.db.execute("INSERT OR IGNORE INTO raw VALUES(?,?,?,?)", row)
                    else:
                        self.db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)", row)
                self.db.commit()
            for _ in batch:
                self.q.task_done()

    def flush(self):
        self.q.join()

    def add_raw(self, id_, ts, src, text):
        self.q.put(("raw", (id_, ts, src, text)))

    def add_event(self, id_, ts, src, level, text, call=None):
        self.q.put(("event", (id_, ts, src, level, text, call)))

    def max_ids(self):
        with self.lock:
            r = self.db.execute("SELECT COALESCE(MAX(id),0) FROM raw").fetchone()[0]
            e = self.db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        return r, e

    def empty(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM raw").fetchone()[0] == 0

    # --- mensajes APRS ---
    def add_message(self, ts, pkt):
        """Guarda un mensaje; las copias repetidas (digipeaters, reintentos) solo suben 'heard'."""
        txt = pkt["text"]
        kind = "ack" if txt.startswith("ack") else "rej" if txt.startswith("rej") else \
               "bulletin" if BULLETIN_RE.match(pkt["to"]) else "msg"
        conv = ("BLN:" + pkt["to"]) if kind == "bulletin" else "|".join(sorted([pkt["src"], pkt["to"]]))
        with self.lock:
            dup = self.db.execute(
                "SELECT id FROM messages WHERE src=? AND dest=? AND text=? AND IFNULL(msgno,'')=? AND ts>?",
                (pkt["src"], pkt["to"], txt, pkt.get("msgno") or "", ts - 600)).fetchone()
            if dup:
                self.db.execute("UPDATE messages SET heard=heard+1 WHERE id=?", (dup[0],))
                self.db.commit()
                return None
            cur = self.db.execute(
                "INSERT INTO messages(ts,src,dest,text,msgno,kind,direction,gateway,path,conv) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, pkt["src"], pkt["to"], txt, pkt.get("msgno"), kind, pkt["dir"], pkt.get("gateway"),
                 json.dumps(pkt["path"]), conv))
            self.db.commit()
            return cur.lastrowid

    # --- consultas paginadas (id descendente, se devuelven en orden cronológico) ---
    def _page(self, table, cols, where, args, before, until, q, limit, qcols):
        where, args = list(where), list(args)
        if before:
            where.append("id < ?"); args.append(before)
        if until:
            where.append("ts <= ?"); args.append(until)
        if q:
            where.append("(" + " OR ".join("%s LIKE ? ESCAPE '\\'" % c for c in qcols) + ")")
            args += [_like(q)] * len(qcols)
        sql = "SELECT %s FROM %s %s ORDER BY id DESC LIMIT ?" % (cols, table, ("WHERE " + " AND ".join(where)) if where else "")
        with self.lock:
            cur = self.db.execute(sql, args + [limit])
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        rows.reverse()
        return rows

    def raw(self, src, before=0, until=0, q="", limit=200):
        self.flush()
        rows = self._page("raw", "id AS seq, ts, text", ["src = ?"], [src], before, until, q, limit, ["text"])
        return rows

    def events(self, src="", before=0, until=0, q="", limit=200):
        self.flush()
        return self._page("events", "id AS seq, ts, src, level, text, call", ["src = ?"] if src else [],
                          [src] if src else [], before, until, q, limit, ["text"])

    def conversations(self, limit=200):
        with self.lock:
            cur = self.db.execute(
                "SELECT m.conv, c.n, c.last_id, m.ts, m.src, m.text, m.kind FROM "
                "(SELECT conv, COUNT(*) n, MAX(id) last_id FROM messages WHERE kind != 'ack' AND kind != 'rej' "
                "GROUP BY conv ORDER BY last_id DESC LIMIT ?) c JOIN messages m ON m.id = c.last_id", (limit,))
            out = []
            for conv, n, last_id, ts, src, text, kind in cur.fetchall():
                out.append({"conv": conv, "n": n, "last_id": last_id, "ts": ts, "last_src": src,
                            "last_text": text, "bulletin": conv.startswith("BLN:"),
                            "peers": conv[4:].split() if conv.startswith("BLN:") else conv.split("|")})
        return out

    def messages(self, conv="", before=0, after=0, q="", limit=100, to=""):
        where, args = [], []
        if conv:
            where.append("conv = ?"); args.append(conv)
        if to:
            where.append("dest = ?"); args.append(to)
        if after:
            where.append("id > ?"); args.append(after)
            sql = "SELECT * FROM messages WHERE %s ORDER BY id LIMIT ?" % " AND ".join(where)
            with self.lock:
                cur = self.db.execute(sql, args + [limit])
                names = [d[0] for d in cur.description]
                return [dict(zip(names, r)) for r in cur.fetchall()]
        return self._page("messages", "*", where, args, before, 0, q, limit, ["text", "src", "dest"])

    # --- mantenimiento ---
    def prune(self):
        cutoff = time.time() - self.days * 86400
        with self.lock:
            self.db.execute("DELETE FROM raw WHERE ts < ?", (cutoff,))
            self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            n = self.db.execute("SELECT COUNT(*) FROM raw").fetchone()[0]
            if n > self.max_raw_rows:
                self.db.execute("DELETE FROM raw WHERE id IN (SELECT id FROM raw ORDER BY id LIMIT ?)", (n - self.max_raw_rows,))
            self.db.commit()


def import_logs(hub, cfg, sources):
    """Primer arranque: reconstruye histórico y estaciones a partir de los ficheros de log."""
    d = cfg.get("logs", "dir")
    cutoff = time.time() - cfg.int("history", "days") * 86400
    rows = []
    for src in sources:
        base = os.path.join(d, src + ".log")
        files = ["%s.%d" % (base, i) for i in range(cfg.int("logs", "backups"), 0, -1)] + [base]
        for path in files:
            try:
                with open(path, errors="replace") as f:
                    for n, line in enumerate(f):
                        m = LOG_RE.match(line.rstrip("\n"))
                        if m:
                            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                            if ts >= cutoff:
                                rows.append((ts, len(rows), src, m.group(2)))
            except OSError:
                continue
    rows.sort()
    for ts, _, src, text in rows:
        hub.ingest(src, text, ts=ts, replay=True)
    return len(rows)
