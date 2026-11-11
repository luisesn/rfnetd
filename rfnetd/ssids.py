"""Agrupación de estaciones por indicativo base (EA1ABC-7 -> EA1ABC + SSID 7)."""
import re

SSID_RE = re.compile(r"^(.+)-(\d{1,2})$")


def split_call(call):
    """'EA1ABC-7' -> ('EA1ABC', 7); sin SSID válido (0-15) -> (llamada, 0)."""
    call = (call or "").strip()
    m = SSID_RE.match(call)
    if m and int(m[2]) <= 15:
        return m[1].upper(), int(m[2])
    return call.upper(), 0


def build_groups(live, pos, sent, rcvd, q="", ssid=None, since=0, limit=500):
    """Estaciones agrupadas por indicativo base.

    live: lista de dicts del StationStore; pos / sent / rcvd: {clave: (n, ultimo_ts)} del histórico
    (posiciones, mensajes enviados y recibidos). ssid: None = todos, 0 = sin SSID, 1..15 = ese SSID.
    since: solo estaciones oídas desde ese instante. Devuelve grupos ordenados por última actividad.
    """
    members = {}

    def get(key):
        m = members.get(key)
        if m is None:
            base, n = split_call(key)
            m = members[key] = {"call": key, "base": base, "ssid": n, "last": 0, "count": 0, "pos_n": 0,
                                "msg_out": 0, "msg_in": 0}
        return m

    for s in live:
        m = get(s["call"])
        m["last"] = max(m["last"], s.get("last_heard", 0))
        m["count"] = s.get("count", 0)
        for k in ("lat", "lon", "symbol", "table", "status", "comment", "dir", "rf_count", "ig_count"):
            if k in s:
                m[k] = s[k]
    for key, (n, last) in pos.items():
        m = get(key)
        m["pos_n"], m["last"] = n, max(m["last"], last or 0)
    for key, (n, last) in sent.items():
        m = get(key)
        m["msg_out"], m["last"] = n, max(m["last"], last or 0)
    for key, (n, _) in rcvd.items():
        if key in members:      # solo cuentan los mensajes recibidos de estaciones que hemos oído
            members[key]["msg_in"] = n

    q = (q or "").strip().upper()
    groups = {}
    for m in members.values():
        if not m["last"] or (since and m["last"] < since) or (q and q not in m["call"].upper()):
            continue
        if ssid is not None and m["ssid"] != ssid:
            continue
        g = groups.setdefault(m["base"], {"base": m["base"], "members": [], "last": 0, "pos_n": 0, "msg_n": 0})
        g["members"].append(m)
        g["last"] = max(g["last"], m["last"])
        g["pos_n"] += m["pos_n"]
        g["msg_n"] += m["msg_out"] + m["msg_in"]
    out = sorted(groups.values(), key=lambda g: -g["last"])[:limit]
    for g in out:
        g["members"].sort(key=lambda m: (m["ssid"], m["call"]))
    return out
