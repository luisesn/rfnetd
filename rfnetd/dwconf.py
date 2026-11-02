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
    "PACLEN": ("paclen", "PACLEN (bytes)", "int", (1, 2048)),
    "FIX_BITS": ("fix_bits", "Corrección de bits (FIX_BITS)", "choice", ["0", "1", "2", "3", "4", "5", "6"]),
    "DEDUPE": ("dedup", "Duplicados: segundos (DEDUPE)", "int", (0, 600)),
}
# clave -> (valor por defecto de Direwolf 1.6, ayuda). El valor por defecto es el que se usa si el fichero no define la directiva.
INFO = {
    "mycall": ("N0CALL", "Indicativo con el que Direwolf transmite y se identifica (con SSID, p. ej. EA1XXX-5)."),
    "modem": ("1200", "Velocidad en baudios. 1200 es la de APRS en VHF (AFSK); 9600 solo para canales de datos."),
    "txdelay": ("25", "Tiempo entre pulsar PTT y empezar los datos, para que el transmisor se estabilice. Si el otro extremo pierde el principio de las tramas, súbelo (30-50)."),
    "txtail": ("10", "Tiempo que se mantiene la portadora tras el último dato, para no cortar el final de la trama."),
    "persist": ("63", "Probabilidad CSMA (0-255): con el canal libre, transmite en cada ranura con probabilidad (valor+1)/256. Menor = más cortés, más espera."),
    "slottime": ("10", "Duración de cada ranura CSMA: cuánto se espera entre intentos si el canal estaba ocupado."),
    "dwait": ("0", "Espera extra tras quedar libre el canal antes de transmitir. Útil para dejar pasar confirmaciones de otras estaciones."),
    "paclen": ("256", "Tamaño máximo (bytes) de la parte de datos de las tramas que genera Direwolf en modo conectado. No recorta las tramas que llegan por KISS (p. ej. tncattach, cuyo MTU manda)."),
    "fix_bits": ("1", "Intentos de reparar tramas con errores de CRC: 0 = ninguno, más = más tramas recuperadas pero algo más de riesgo de falsos positivos."),
    "dedup": ("30", "Segundos durante los que se descarta un paquete idéntico ya repetido, para que el digipeater no lo reenvíe dos veces."),
    "beacon": ("", "Transmite automáticamente la posición de la estación."),
    "beacon_every": ("", "Minutos entre balizas. Para un digipeater fijo, 30 o más."),
    "beacon_via": ("", "Ruta de la baliza. Un digi fijo suele usar WIDE1-1 o vacío; WIDE2-1 la propaga más lejos."),
    "beacon_comment": ("", "Texto que acompaña a la posición."),
    "beacon_overlay": ("", "Carácter sobre el icono. Para un digipeater con IGate lo convencional es I; S es de SSn-N."),
    "ig_filter": ("", "Qué paquetes de Internet se pueden transmitir por radio. «t/m» = solo mensajes. Vacío = sin filtro (Direwolf ya solo pasa mensajes a estaciones oídas)."),
    "ig_beacon": ("", "Envía tu posición directamente a APRS-IS, sin radio, para aparecer en aprs.fi aunque nadie repita tu baliza."),
    "digipeat": ("", "Repite los paquetes que oye según el path (WIDE1-1, WIDE2-n…)."),
    "digi_filter": ("", "Excluye o admite paquetes en el digipeater. Ej.: «! b/EA1XXX-3» no repite a esa estación. Vacío = sin filtro."),
    "igate": ("", "Pasa lo recibido por radio a APRS-IS (Internet) y viceversa."),
}
LEGACY = {"DEDUP": "DEDUPE"}   # Direwolf 1.6 no reconoce DEDUP; se corrige en la copia generada
FILTER_RE = re.compile(r"^[A-Za-z0-9/!&|()*., _-]{0,120}$")
IG_FILTER_RE = re.compile(r"^[A-Za-z0-9/!&|()*., _-]{0,120}$")
IG_FILTER_FIELD = ("ig_filter", "Filtro hacia la radio (FILTER IG 0), p. ej. t/m = solo mensajes", "filter", None)
IG_BEACON_FIELD = ("ig_beacon", "Baliza también a Internet (sendto=IG, sin radio)", "bool", None)
FILTER_FIELD = ("digi_filter", "Filtro del digipeater (FILTER 0 0), p. ej. ! b/EA1DGP-3", "filter", None)
GROUPS = {  # clave -> directivas que se comentan cuando está desactivado
    "beacon": ("Baliza APRS (PBEACON)", {"PBEACON"}),
    "digipeat": ("Digipeater (DIGIPEAT)", {"DIGIPEAT", "CDIGIPEAT"}),
    "igate": ("IGate a APRS-IS", {"IGSERVER", "IGLOGIN", "IGTXVIA", "IGTXLIMIT"}),
}
BEACON_FIELDS = {
    "beacon_every": ("Cada (minutos)", "every", "int", (1, 1440)),
    "beacon_via": ("Vía (path)", "via", "via", None),
    "beacon_comment": ("Comentario", "comment", "text", None),
    "beacon_overlay": ("Símbolo: overlay (letra o cifra)", "overlay", "word", None),
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


def _is_ig_beacon(line):
    return _directive(line) == "PBEACON" and (_field(line, "sendto") or "").upper() == "IG"


def _main_beacon(lines):
    """Primera PBEACON activa que sale por radio (no la de sendto=IG)."""
    for i, ln in enumerate(lines):
        if _directive(ln) == "PBEACON" and not _is_ig_beacon(ln):
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
        if i is None and key in LEGACY.values():
            i = _first_line(lines, next(k for k, v in LEGACY.items() if v == key))
        if i is not None:
            vals[opt] = lines[i].split()[1] if len(lines[i].split()) > 1 else ""
    avail = {}
    for g, (_, keys) in GROUPS.items():
        present = [ln for ln in lines if _directive(ln) in keys]
        off = [ln for ln in _commented_lines(lines) if _directive(ln) in keys]
        avail[g] = bool(present or off)
        vals[g] = bool(present)
    vals["digi_filter"] = next((ln.split(None, 3)[3] for ln in lines if _directive(ln) == "FILTER"
                                and ln.split()[1:3] == ["0", "0"] and len(ln.split()) > 3), "")
    vals["ig_filter"] = next((ln.split(None, 3)[3] for ln in lines if _directive(ln) == "FILTER"
                              and [x.upper() for x in ln.split()[1:3]] == ["IG", "0"] and len(ln.split()) > 3), "")
    vals["ig_beacon"] = any(_is_ig_beacon(ln) for ln in lines)
    b = _main_beacon(lines)
    src = lines[b] if b is not None else next((ln for ln in _commented_lines(lines) if _directive(ln) == "PBEACON"), "")
    for opt, (_, field, kind, _) in BEACON_FIELDS.items():
        v = _field(src, field)
        if v is not None:
            vals[opt] = v
    return vals, avail


def _info(field):
    default, help_ = INFO.get(field["key"], ("", ""))
    field.update(default=default, help=help_)
    return field


def schema(avail):
    out = []
    for key, (opt, label, kind, lim) in SIMPLE.items():
        out.append(_info({"key": opt, "label": label, "type": kind, "limits": lim}))
    for g, (label, _) in GROUPS.items():
        out.append(_info({"key": g, "label": label, "type": "bool", "available": avail.get(g, False)}))
        if g == "beacon":
            for opt, (lbl, _, kind, lim) in BEACON_FIELDS.items():
                out.append(_info({"key": opt, "label": lbl, "type": kind, "limits": lim, "depends": "beacon"}))
        if g == "igate":
            for fld in (IG_FILTER_FIELD, IG_BEACON_FIELD):
                out.append(_info({"key": fld[0], "label": fld[1], "type": fld[2], "limits": None, "depends": "igate"}))
        if g == "digipeat":
            out.append(_info({"key": FILTER_FIELD[0], "label": FILTER_FIELD[1], "type": "filter", "limits": None, "depends": "digipeat"}))
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
        elif t == "filter":
            v = str(v).strip()
            if not (IG_FILTER_RE if key == "ig_filter" else FILTER_RE).match(v):
                raise ValueError("filtro no válido (solo letras, cifras y / ! & | ( ) * , . _ -)")
            clean[key] = v
        elif t == "word":
            v = str(v).strip().upper()
            if not re.match(r"^[A-Z0-9]$", v):
                raise ValueError("overlay: una letra o cifra")
            clean[key] = v
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
    for i, ln in enumerate(lines):   # DEDUP -> DEDUPE (la antigua da «Unrecognized command»)
        if _directive(ln) in LEGACY:
            lines[i] = LEGACY[_directive(ln)] + ln[len(_directive(ln)):]
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
    if "digi_filter" in opts:   # un solo FILTER 0 0; vacío = sin filtro
        lines = [ln for ln in lines if not (_directive(ln) == "FILTER" and ln.split()[1:3] == ["0", "0"])]
        if opts["digi_filter"]:
            lines.append("FILTER 0 0 %s" % opts["digi_filter"])
    if "ig_filter" in opts:     # un solo FILTER IG 0; vacío = sin filtro
        lines = [ln for ln in lines if not (_directive(ln) == "FILTER" and [x.upper() for x in ln.split()[1:3]] == ["IG", "0"])]
        if opts["ig_filter"]:
            lines.append("FILTER IG 0 %s" % opts["ig_filter"])
    # 3) campos de la baliza (solo la primera línea PBEACON activa de radio)
    b = _main_beacon(lines)
    if b is not None:
        for opt, (_, field, kind, _) in BEACON_FIELDS.items():
            if opt not in opts:
                continue
            val = '"%s"' % opts[opt] if kind == "text" else str(opts[opt])
            if re.search(r'(?:^|\s)%s=("[^"]*"|\S+)' % field, lines[b]):
                lines[b] = re.sub(r'(^|\s)%s=("[^"]*"|\S+)' % field, lambda m: m.group(1) + "%s=%s" % (field, val), lines[b], count=1)
            else:
                lines[b] += " %s=%s" % (field, val)
    if "ig_beacon" in opts:     # copia de la baliza de radio que solo va a APRS-IS (no transmite por radio)
        have = [i for i, ln in enumerate(lines) if _is_ig_beacon(ln)]
        if not opts["ig_beacon"]:
            for i in have:
                lines[i] = MARK + lines[i]
        elif not have and b is not None:
            ig = re.sub(r"\s+via=\S+", "", lines[b].rstrip())
            ig = re.sub(r"\s+sendto=\S+", "", ig)
            lines.append(re.sub(r"^(\s*PBEACON)", r"\1 sendto=IG", ig, count=1, flags=re.I))
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
