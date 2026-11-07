"""Carga de rfnetd.conf (INI)."""
import configparser
import shlex

DEFAULTS = """
[station]
callsign = N0CALL
lat = 0.0
lon = 0.0

[direwolf]
enabled = yes
cmd = direwolf -t 0 -c /etc/direwolf.conf
user =
kiss_host = 127.0.0.1
kiss_port = 8001
ready_timeout = 30
# Si se indica, rfnetd genera la configuración efectiva a partir de este fichero + las
# opciones editables desde la web; usar {conf} en cmd. El fichero base no se modifica.
base_conf =
# Dispositivo ALSA de captura para Direwolf (p. ej. rf_cap, un dsnoop, para poder escuchar a la vez).
# Vacío = el que ponga el fichero base. La salida (TX) no se toca.
audio_in =
# Vigila el puerto serie del PTT (línea «PTT /dev/...» del fichero de Direwolf): avisa y bloquea la
# transmisión desde la web si el adaptador USB se ha desconectado, y con ptt_autorestart reinicia
# Direwolf cuando el puerto vuelve (como mucho una vez cada 2 minutos).
ptt_watch = yes
ptt_autorestart = yes

[tncattach]
enabled = yes
cmd = tncattach -T -H 127.0.0.1 -P 8001 --mtu 250 --noipv6 --ipv4 44.0.0.2/24 -v
ready_timeout = 20

[network]
interface = tnc0
disable_ipv6 = yes
post_up =

[capture]
enabled = yes
cmd = tcpdump -l -nn -tttt -i {interface}

[radio]
enabled = no
port = /dev/ttyUSB0
command_timeout_ms = 500
channel_delay_ms = 250
poll_ms = 500
reconnect_delay_ms = 3000
# Un canal por línea, en el orden de la radio (el primero es el canal 0): NOMBRE | MHz
channels =

[tx]
# Transmisión APRS (mensajes, estado, posición) por el puerto KISS de Direwolf. Exige [web] password.
enabled = no
tocall = APZRFN
path = WIDE1-1
# Segundos mínimos entre transmisiones y tope por hora (protege de bucles y abusos)
min_interval = 5
max_per_hour = 30
# Espera antes de cada reintento de un mensaje sin ack (3 valores = 3 reintentos)
retry_seconds = 30, 60, 120
# Solo transmite si la radio controlada está en esta frecuencia (MHz); 0 = no comprobar
require_freq = 0
# Responder con ack a los mensajes recibidos para esta estación
auto_ack = no
# Símbolo de la posición manual: tabla + código (/- = casa)
symbol = /-

[audio]
enabled = no
device = default
rate = 22050
bitrate = 32
idle_stop_seconds = 5

[web]
bind = 0.0.0.0
port = 8088
user = admin
# Sin contraseña la web es solo de lectura y no hay audio ni control de la radio/Direwolf
password =

[logs]
dir = /var/log/rfnetd
buffer_lines = 2000
max_bytes = 5000000
backups = 3

[history]
# Días que se conservan logs y eventos (los mensajes APRS se guardan siempre)
days = 30
# Tope de líneas en bruto (tcpdump puede generar muchas)
max_raw_rows = 400000

[data]
dir = /var/lib/rfnetd
max_age_hours = 48
track_points = 50
"""


class Config:
    def __init__(self, path=None):
        self.cp = configparser.ConfigParser(interpolation=None)
        self.cp.read_string(DEFAULTS)
        if path:
            if not self.cp.read(path):
                raise FileNotFoundError(path)

    def get(self, section, key):
        return self.cp.get(section, key).strip()

    def int(self, section, key):
        return self.cp.getint(section, key)

    def float(self, section, key):
        return self.cp.getfloat(section, key)

    def bool(self, section, key):
        return self.cp.getboolean(section, key)

    def argv(self, section, key="cmd"):
        iface = self.get("network", "interface")
        return shlex.split(self.get(section, key).replace("{interface}", iface))

    def commands(self, section, key):
        """Valor multilínea -> lista de argv, ignorando vacías y #comentarios."""
        out = []
        for line in self.get(section, key).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(shlex.split(line.replace("{interface}", self.get("network", "interface"))))
        return out
