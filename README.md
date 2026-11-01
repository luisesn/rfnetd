# rfnetd

Servicio para una red IP sobre radio de paquetes (AX.25/APRS) con
[Direwolf](https://github.com/wb2osz/direwolf) y
[tncattach](https://github.com/markqvist/tncattach). Sustituye a un script
manual y añade una interfaz web con logs, eventos legibles y un mapa APRS.

- Arranca **direwolf → tncattach → comandos post-arranque → tcpdump** en orden,
  espera a que cada uno esté listo y reinicia el conjunto si algo muere.
- Interfaz web (solo lectura): **mapa OpenStreetMap** con las estaciones APRS y su
  ruta (origen → digipeaters → esta estación), **eventos en español** y **logs en bruto**.
- Solo biblioteca estándar de Python 3.9+; sin `pip install`.

Requisitos y diseño: [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md).

## Instalación (Raspberry Pi / Debian)

```bash
git clone <este repo> && cd rfnetd
sudo ./install.sh            # copia a /opt/rfnetd, crea /etc/rfnetd/rfnetd.conf y la unidad systemd
sudoedit /etc/rfnetd/rfnetd.conf
sudo systemctl enable --now rfnetd
```

Si ya tienes `direwolf`/`tncattach` lanzados a mano, párales antes de arrancar
el servicio (comparten audio y puerto KISS).

Web: `http://<ip>:8088/` · Logs: `journalctl -u rfnetd -f` y `/var/log/rfnetd/`.

## Configuración

Todo está en `/etc/rfnetd/rfnetd.conf` ([ejemplo](etc/rfnetd.conf.example)):
comando de cada programa, usuario de Direwolf, interfaz, lista de
`post_up` (rutas, `txqueuelen`…), puerto web y directorios. Para usar `tshark`
en lugar de `tcpdump`, cambia `[capture] cmd`.

## API (JSON, solo lectura)

| Ruta                                  | Contenido                                  |
|---------------------------------------|--------------------------------------------|
| `/api/status`                         | procesos, interfaz, contadores, estación   |
| `/api/events?after=N`                 | eventos legibles                           |
| `/api/logs?src=direwolf&after=N`      | log en bruto (`direwolf`, `tncattach`, `capture`, `rfnetd`) |
| `/api/stations`                       | estaciones APRS con posición, ruta, rastro |

## Seguridad

- No hay autenticación: limita con `bind = 127.0.0.1` o un firewall/VPN si hace falta.
- El texto recibido por radio se muestra siempre como texto, nunca como HTML.
- El servicio corre como root (tncattach, `ip` y captura lo necesitan); Direwolf se
  lanza con el usuario configurado.
- No subas tu `direwolf.conf`: contiene el passcode de APRS-IS.
- El navegador necesita Internet para las teselas de OSM y Leaflet (CDN), no la Pi.

## Pruebas

```bash
python3 -m unittest discover tests
```

## Limitaciones

Telemetría y meteorología se muestran como comentario; no se pueden enviar
mensajes APRS desde la web; el mapa no funciona sin conexión.
