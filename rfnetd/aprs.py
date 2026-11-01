"""Parser de paquetes APRS (formato TNC2) suficiente para posición y ruta."""
import re

PKT_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s+(?P<src>[A-Za-z0-9-]+)>(?P<rest>[^:]*):(?P<info>.*)$")
ALIAS_RE = re.compile(r"^(WIDE\d*|RELAY|TRACE\d*|TEMP\d*|GATE|ECHO|HOP\d*|RFONLY|NOGATE|TCPIP|TCPXX|qA.)(-\d+)?$", re.I)

UNCOMP_RE = re.compile(
    r"^(?P<lat>\d{2}[\d ]{2}\.[\d ]{2})(?P<ns>[NSns])(?P<tab>.)"
    r"(?P<lon>\d{3}[\d ]{2}\.[\d ]{2})(?P<ew>[EWew])(?P<code>.)(?P<rest>.*)$", re.S)
CSE_SPD_RE = re.compile(r"^(\d{3})/(\d{3})")
ALT_RE = re.compile(r"/A=(-?\d{6})")

SYMBOLS = {
    ">": "coche", "<": "moto", "[": "persona", "b": "bicicleta", "k": "camión",
    "u": "camión", "v": "furgoneta", "j": "jeep", "Y": "velero", "s": "barco",
    "O": "globo", "^": "avión", "'": "avioneta", "-": "casa", "#": "digipeater",
    "&": "pasarela", "r": "repetidor", "_": "meteo", "y": "yagi", "a": "ambulancia",
    "f": "bomberos", "!": "policía", "R": "autocaravana", "$": "teléfono",
    "I": "TCP/IP", "n": "nodo", "-": "casa", "W": "meteo", "`": "antena",
}


def symbol_name(table, code):
    return SYMBOLS.get(code, "estación")


def _deg(txt, deg_len, hemi):
    txt = txt.replace(" ", "0")
    deg = int(txt[:deg_len])
    minutes = float(txt[deg_len:])
    val = deg + minutes / 60.0
    return -val if hemi.upper() in "SW" else val


def _base91(s):
    v = 0
    for c in s:
        o = ord(c) - 33
        if not 0 <= o < 91:
            raise ValueError("base91")
        v = v * 91 + o
    return v


def _parse_uncompressed(body):
    m = UNCOMP_RE.match(body)
    if not m:
        return None
    out = {
        "lat": _deg(m["lat"], 2, m["ns"]),
        "lon": _deg(m["lon"], 3, m["ew"]),
        "table": m["tab"], "symbol": m["code"], "comment": m["rest"],
    }
    c = CSE_SPD_RE.match(m["rest"])
    if c:
        out["course"], out["speed_kn"] = int(c[1]), int(c[2])
        out["comment"] = m["rest"][7:]
    return out


def _parse_compressed(body):
    if len(body) < 12:
        return None
    try:
        lat = 90 - _base91(body[1:5]) / 380926.0
        lon = -180 + _base91(body[5:9]) / 190463.0
    except ValueError:
        return None
    out = {"lat": lat, "lon": lon, "table": body[0], "symbol": body[9], "comment": body[12:] if len(body) > 12 else ""}
    c, s = ord(body[10]) - 33, ord(body[11]) - 33
    if 0 <= c <= 89 and body[10] != " ":
        out["course"] = c * 4
        out["speed_kn"] = round(1.08 ** s - 1, 1)
    return out


def _parse_position(body):
    if not body:
        return None
    if body[0].isdigit() or body[0] == " ":
        return _parse_uncompressed(body)
    return _parse_compressed(body)


def parse_mice(dest, info):
    """Mic-E: latitud en el destino, longitud/velocidad en el cuerpo."""
    if len(dest) < 6 or len(info) < 9:
        return None
    dest = dest[:6].upper()
    digits = []
    for ch in dest:
        if "0" <= ch <= "9":
            digits.append(ord(ch) - 48)
        elif "A" <= ch <= "J":
            digits.append(ord(ch) - 65)
        elif "P" <= ch <= "Y":
            digits.append(ord(ch) - 80)
        elif ch in "KLZ":
            digits.append(0)
        else:
            return None
    north = dest[3] >= "P"
    lon_off = dest[4] >= "P"
    west = dest[5] >= "P"
    lat = digits[0] * 10 + digits[1] + (digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100.0) / 60.0
    if not north:
        lat = -lat
    b = [ord(c) - 28 for c in info[1:9]]
    deg = b[0] + (100 if lon_off else 0)
    if 180 <= deg <= 189:
        deg -= 80
    elif 190 <= deg <= 199:
        deg -= 190
    mins = b[1] - 60 if b[1] >= 60 else b[1]
    lon = deg + (mins + b[2] / 100.0) / 60.0
    if west:
        lon = -lon
    speed = b[3] * 10 + b[4] // 10
    course = (b[4] % 10) * 100 + b[5]
    if speed >= 800:
        speed -= 800
    if course >= 400:
        course -= 400
    return {"lat": lat, "lon": lon, "speed_kn": speed, "course": course,
            "symbol": info[7], "table": info[8], "comment": info[9:]}


def parse_info(dest, info):
    """Devuelve dict con 'type' y, según el caso, posición/mensaje."""
    if not info:
        return {"type": "empty"}
    t = info[0]
    if t in "!=":
        pos = _parse_position(info[1:])
        return dict(pos, type="position") if pos else {"type": "other", "comment": info}
    if t in "/@":
        pos = _parse_position(info[8:])
        return dict(pos, type="position") if pos else {"type": "other", "comment": info}
    if t in "`'":
        pos = parse_mice(dest, info)
        return dict(pos, type="mic-e") if pos else {"type": "other", "comment": info}
    if t == ";" and len(info) > 18:
        pos = _parse_position(info[18:])
        base = {"type": "object", "name": info[1:10].strip(), "live": info[10] == "*"}
        return dict(pos, **base) if pos else dict(base, comment=info[18:])
    if t == ")":
        i = min([x for x in (info.find("!", 1), info.find("_", 1)) if x > 0] or [-1])
        if i > 0:
            pos = _parse_position(info[i + 1:])
            base = {"type": "item", "name": info[1:i], "live": info[i] == "!"}
            return dict(pos, **base) if pos else dict(base, comment=info[i + 1:])
    if t == ":" and len(info) >= 11 and info[10] == ":":
        text = info[11:]
        msgno = None
        if "{" in text:
            text, _, msgno = text.rpartition("{")
        return {"type": "message", "to": info[1:10].strip(), "text": text, "msgno": msgno}
    if t == ">":
        return {"type": "status", "text": info[1:]}
    if t == "}":
        inner = re.match(r"^([A-Za-z0-9-]+)>([^:]*):(.*)$", info[1:], re.S)
        if inner:
            sub = parse_info(inner[2].split(",")[0], inner[3])
            sub["third_party_from"] = inner[1]
            return sub
    if t == "_":
        return {"type": "weather", "comment": info[1:]}
    if info.startswith("T#"):
        return {"type": "telemetry", "comment": info[2:]}
    return {"type": "other", "comment": info}


def path_info(rest):
    """rest = 'DEST,PATH1*,PATH2' -> (destino, elementos, digis_reales_usados)."""
    parts = [p for p in rest.split(",") if p]
    dest, path = (parts[0], parts[1:]) if parts else ("", [])
    last = -1
    for i, p in enumerate(path):
        if p.endswith("*"):
            last = i
    digis = []
    for p in path[: last + 1]:
        name = p.rstrip("*")
        if not ALIAS_RE.match(name):
            digis.append(name)
    return dest, path, digis


def parse_line(line):
    """Línea de salida de Direwolf con un paquete -> dict o None."""
    m = PKT_RE.match(line.rstrip())
    if not m:
        return None
    dest, path, digis = path_info(m["rest"])
    tag = m["tag"]
    if "ig" in tag.lower():
        direction = "ig"
    elif tag.endswith(("L", "H")):
        direction = "tx"
    else:
        direction = "rx"
    pkt = {"tag": tag, "dir": direction, "src": m["src"].upper(), "dest": dest,
           "path": path, "digis": digis, "info": m["info"]}
    pkt.update(parse_info(dest, m["info"]))
    return pkt
