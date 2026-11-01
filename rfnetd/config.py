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

[tncattach]
enabled = yes
cmd = tncattach -T -H 127.0.0.1 -P 8001 --mtu 250 --noipv6 --ipv4 44.0.0.2/24 -v
ready_timeout = 20

[network]
interface = tnc0
post_up =

[capture]
enabled = yes
cmd = tcpdump -l -nn -tttt -i {interface}

[web]
bind = 0.0.0.0
port = 8088

[logs]
dir = /var/log/rfnetd
buffer_lines = 2000
max_bytes = 5000000
backups = 3

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
