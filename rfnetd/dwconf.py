"""Opciones editables de Direwolf: se aplican sobre una COPIA del direwolf.conf base.

El fichero base nunca se modifica ni se expone (contiene el passcode de APRS-IS).
"""
import json
import os
import re
import threading

CALL_RE = re.compile(r"^[A-Za-z0-9]{1,6}(-\d{1,2})?$")
VIA_RE = re.compile(r"^[A-Za-z0-9,-]{0,40}$")

# directiva -> (clave, etiqueta, tipo, límites/opciones)
SIMPLE = {
    "MYCALL": ("mycall", "Indicativo (MYCALL)", "call", None),
    "MODEM": ("modem", "Velocidad del módem", "choice", ["300", "1200", "2400", "9600"]),
    "TXDELAY": ("txdelay", "TXDELAY (×10 ms)", "int", (0, 255)),
    "TXTAIL": ("txtail", "TXTAIL (×10 ms)", "int", (0, 255)),
    "PERSIST": ("persist", "PERSIST (0-255)", "int", (0, 255)),
    "SLOTTIME": ("slottime", "SLOTTIME (×10 ms)", "int", (0, 255)),
    "DWAIT": ("dwait", "DWAIT (×10 ms)", "int", (0, 255)),
    "FIX_BITS": ("fix_bits", "Corrección de bits (FIX_BITS)", "choice", ["0", "1", "2", "3", "4", "5", "6"]),
    "DEDUP": ("dedup", "Duplicados: segundos (DEDUP)", "int", (0, 600)),
}
GROUPS = {  # clave -> directivas que se comentan cuando está desactivado
    "beacon": ("Baliza APRS (PBEACON)", {"PBEACON"}),
    "digipeat": ("Digipeater (DIGIPEAT)", {"DIGIPEAT", "CDIGIPEAT"}),
    "igate": ("IGate a APRS-IS", {"IGSERVER", "IGLOGIN", "IGTXVIA", "IGTXLIMIT"}),
}
BEACON_FIELDS = {
    "beacon_every": ("Cada (minutos)", "every", "int", (1, 1440)),
    "beacon_via": ("Vía (path)", "via", "via", None),
    "beacon_comment": ("Comentario", "comment", "text", None),
}
MARK = "# [rfnetd off] "


def _directive(line):
    t = line.split(None, 1)
    return t[0].upper() if t and not t[0].startswith("#") else None


def _first_line(lines, key):
    for i, ln in enumerate(lines):
        if _directive(ln) == key:
            return i
    return None


def _field(line, name):
    m = re.search(r'(?:^|\s)%s=("([^"]*)"|\S+)' % name, line)
    return (m.group(2) if m.group(2) is not None else m.group(1)) if m else None


def _commented_lines(lines):
    return [ln[len(MARK):] for ln in lines if ln.startswith(MARK)]


def parse_base(text):
    """Valores efectivos del fichero base (los grupos desactivados por rfnetd cuentan como off)."""
    lines = text.splitlines()
    vals = {}
    for key, (opt, _, _, _) in SIMPLE.items():
        i = _first_line(lines, key)
        if i is not None:
            vals[opt] = lines[i].split()[1] if len(lines[i].split()) > 1 else ""
    avail = {}
    for g, (_, keys) in GROUPS.items():
        present = [ln for ln in lines if _directive(ln) in keys]
        off = [ln for ln in _commented_lines(lines) if _directive(ln) in keys]
        avail[g] = bool(present or off)
        vals[g] = bool(present)
    b = _first_line(lines, "PBEACON")
    src = lines[b] if b is not None else next((ln for ln in _commented_lines(lines) if _directive(ln) == "PBEACON"), "")
    for opt, (_, field, kind, _) in BEACON_FIELDS.items():
        v = _field(src, field)
        if v is not None:
            vals[opt] = v
    return vals, avail


def schema(avail):
    out = []
    for key, (opt, label, kind, lim) in SIMPLE.items():
        out.append({"key": opt, "label": label, "type": kind, "limits": lim})
    for g, (label, _) in GROUPS.items():
        out.append({"key": g, "label": label, "type": "bool", "available": avail.get(g, False)})
        if g == "beacon":
            for opt, (lbl, _, kind, lim) in BEACON_FIELDS.items():
                out.append({"key": opt, "label": lbl, "type": kind, "limits": lim, "depends": "beacon"})
    return out


def validate(opts):
    """Devuelve opciones saneadas o lanza ValueError. Sin saltos de línea ni comillas."""
    clean = {}
    for key, v in opts.items():
        spec = next((s for s in schema({g: True for g in GROUPS}) if s["key"] == key), None)
        if spec is None:
            raise ValueError("opción desconocida: %s" % key)
        t = spec["type"]
        if t == "bool":
            clean[key] = bool(v)
        elif t == "int":
            n = int(v)
            lo, hi = spec["limits"]
            if not lo <= n <= hi:
                raise ValueError("%s fuera de rango (%d-%d)" % (key, lo, hi))
            clean[key] = n
        elif t == "choice":
            if str(v) not in spec["limits"]:
                raise ValueError("%s: valor no permitido" % key)
            clean[key] = str(v)
        elif t == "call":
            if not CALL_RE.match(str(v)):
                raise ValueError("indicativo no válido")
            clean[key] = str(v).upper()
        elif t == "via":
            if not VIA_RE.match(str(v)):
                raise ValueError("path no válido")
            clean[key] = str(v)
        elif t == "text":
            v = str(v)
            if len(v) > 60 or any(c in v for c in '"\r\n\\') or not v.isprintable():
                raise ValueError("comentario no válido (máx. 60, sin comillas ni barras)")
            clean[key] = v
    return clean


def generate(base_text, opts, audio_in=None):
    lines = base_text.splitlines()
    if audio_in:  # solo cambia el dispositivo de ENTRADA (p. ej. dsnoop); la salida se mantiene
        i = _first_line(lines, "ADEVICE")
        if i is not None:
            parts = lines[i].split()
            lines[i] = "ADEVICE %s %s" % (audio_in, parts[2] if len(parts) > 2 else parts[1])
    # 1) directivas simples: sustituir el primer argumento, o añadir al final
    for key, (opt, _, _, _) in SIMPLE.items():
        if opt not in opts:
            continue
        i = _first_line(lines, key)
        if i is None:
            lines.append("%s %s" % (key, opts[opt]))
        else:
            parts = lines[i].split()
            parts[1:2] = [str(opts[opt])]
            lines[i] = " ".join(parts)
    # 2) grupos on/off (los reactiva si rfnetd los había comentado antes)
    for g, (_, keys) in GROUPS.items():
        if g not in opts:
            continue
        for i, ln in enumerate(lines):
            if opts[g] and ln.startswith(MARK) and _directive(ln[len(MARK):]) in keys:
                lines[i] = ln[len(MARK):]
            elif not opts[g] and _directive(ln) in keys:
                lines[i] = MARK + ln
    # 3) campos de la baliza (solo la primera línea PBEACON activa)
    b = _first_line(lines, "PBEACON")
    if b is not None:
        for opt, (_, field, kind, _) in BEACON_FIELDS.items():
            if opt not in opts:
                continue
            val = '"%s"' % opts[opt] if kind == "text" else str(opts[opt])
            if re.search(r'(?:^|\s)%s=("[^"]*"|\S+)' % field, lines[b]):
                lines[b] = re.sub(r'(^|\s)%s=("[^"]*"|\S+)' % field, lambda m: m.group(1) + "%s=%s" % (field, val), lines[b], count=1)
            else:
                lines[b] += " %s=%s" % (field, val)
    return "\n".join(lines) + "\n"


class Controls:
    """Estado persistente de rfnetd: encendido del conjunto Direwolf y opciones editadas."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"chain_on": True, "dw_opts": {}}
        try:
            with open(path) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def get(self, key):
        with self.lock:
            return self.data[key]

    def set(self, key, value):
        with self.lock:
            self.data[key] = value
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
