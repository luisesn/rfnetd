"""Traduce líneas de log y paquetes a frases legibles (español)."""
import re

from .aprs import symbol_name

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean(line):
    return ANSI_RE.sub("", line).replace("\r", "").rstrip()


def _pos(p):
    return "%.4f°%s %.4f°%s" % (abs(p["lat"]), "N" if p["lat"] >= 0 else "S",
                                abs(p["lon"]), "E" if p["lon"] >= 0 else "W")


def _via(p):
    gw = p.get("gateway")
    if gw:
        rest = [d for d in p["digis"] if d != gw]
        origin = "recibido de Internet por el IGate " if p.get("via_internet") else "retransmitido por el IGate "
        return origin + " → ".join([gw] + rest)
    if p["digis"]:
        return "retransmitido por " + " → ".join(p["digis"])
    if p["path"]:
        return "directo (path %s)" % ",".join(p["path"])
    return "directo"


def aprs_packet(p):
    who = p["src"]
    prefix = {"tx": "Transmitido: ", "ig": "IGate: ", "digi": "Digipeado por esta estación: "}.get(p["dir"], "")
    t = p.get("type")
    if t in ("position", "mic-e", "object", "item"):
        kind = symbol_name(p.get("table"), p.get("symbol"))
        extra = []
        if p.get("speed_kn"):
            extra.append("%d km/h" % round(p["speed_kn"] * 1.852))
        if p.get("course") is not None and p.get("speed_kn"):
            extra.append("rumbo %d°" % p["course"])
        if t in ("object", "item"):
            who = "%s (%s %s)" % (p["name"], t == "object" and "objeto" or "item", p["src"])
            if not p.get("live", True):
                return "%s%s eliminado" % (prefix, who)
        s = "%s%s: %s en %s" % (prefix, who, kind, _pos(p))
        if extra:
            s += ", " + ", ".join(extra)
        if p.get("comment", "").strip():
            s += " «%s»" % p["comment"].strip()
        return s + " — " + _via(p)
    if t == "message":
        txt = p["text"]
        if txt.startswith("ack"):
            return "%s%s confirma (ack %s) a %s" % (prefix, who, txt[3:], p["to"])
        if txt.startswith("rej"):
            return "%s%s rechaza (rej %s) a %s" % (prefix, who, txt[3:], p["to"])
        return "%s%s → %s: «%s»" % (prefix, who, p["to"], txt)
    if t == "telemetry_def":
        return "%s%s: definición de telemetría (%s)" % (prefix, who, p["text"].split(".", 1)[0])
    if t == "status":
        return "%s%s estado: «%s»" % (prefix, who, p["text"])
    if t == "weather":
        return "%s%s informa meteo: %s" % (prefix, who, p["comment"])
    if t == "telemetry":
        return "%s%s telemetría" % (prefix, who)
    return "%s%s paquete %s — %s" % (prefix, who, p.get("type", "?"), _via(p))


DW_PATTERNS = [
    (re.compile(r"Ready to accept KISS TCP client application (\d+) on port (\d+)"),
     lambda m: ("info", "Direwolf listo: acepta clientes KISS en el puerto %s" % m[2])),
    (re.compile(r"Attached to KISS TCP client|KISS TCP.*client.*connected", re.I),
     lambda m: ("info", "Un cliente KISS (tncattach) se ha conectado a Direwolf")),
    (re.compile(r"(Could not|Cannot|Can't|Failed|ERROR)", re.I),
     lambda m: ("error", "Direwolf avisa de un problema")),
    (re.compile(r"Audio input device .* opened|Audio device for both receive and transmit"),
     lambda m: ("info", "Direwolf ha abierto el dispositivo de audio")),
]


def direwolf_line(line):
    for rx, fn in DW_PATTERNS:
        m = rx.search(line)
        if m:
            level, text = fn(m)
            if level == "error":
                text += ": " + line.strip()
            return level, text
    return None


TNC_PATTERNS = [
    (re.compile(r"(error|fail|cannot|could not|unable)", re.I), "error", "tncattach error: %s"),
    (re.compile(r"(created|opened|up|attached|connected|ready)", re.I), "info", "tncattach: %s"),
]


def tncattach_line(line):
    for rx, level, fmt in TNC_PATTERNS:
        if rx.search(line):
            return level, fmt % line.strip()
    return None


TCPDUMP_RE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ (?P<proto>IP6?|ARP)\s+(?P<rest>.*)$")
FLAGS = {"S": "SYN", "S.": "SYN-ACK", ".": "ACK", "P.": "PSH-ACK", "F.": "FIN-ACK",
         "R": "RST", "R.": "RST-ACK", "FP.": "FIN-PSH-ACK"}


def _hostport(s):
    s = s.strip()
    if s.count(".") >= 4:
        h, _, port = s.rpartition(".")
        return "%s:%s" % (h, port)
    return s


def tcpdump_line(line):
    m = TCPDUMP_RE.match(line)
    if not m:
        return None
    if m["proto"] == "ARP":
        return "info", "ARP: " + m["rest"]
    mm = re.match(r"^(?:\(.*?\)\s*)?(\S+) > ([^:]+): (.*)$", m["rest"])
    if not mm:
        return None
    src, dst, info = _hostport(mm[1]), _hostport(mm[2]), mm[3]
    ln = re.search(r"length (\d+)", info)
    size = "%s bytes" % ln[1] if ln else ""
    if info.startswith("Flags"):
        f = re.match(r"Flags \[([^\]]*)\]", info)
        return "info", "TCP %s → %s %s %s" % (src, dst, FLAGS.get(f[1], f[1]), size)
    if info.startswith("UDP"):
        return "info", "UDP %s → %s %s" % (src, dst, size)
    if info.startswith("ICMP echo request"):
        return "info", "ping %s → %s %s" % (src, dst, size)
    if info.startswith("ICMP echo reply"):
        return "info", "respuesta de ping %s → %s %s" % (src, dst, size)
    if info.startswith("ICMP"):
        return "info", "ICMP %s → %s: %s" % (src, dst, info)
    return "info", "IP %s → %s: %s" % (src, dst, info)
