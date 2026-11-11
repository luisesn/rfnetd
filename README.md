# rfnetd

Servicio para una red IP sobre radio de paquetes (AX.25/APRS) con
[Direwolf](https://github.com/wb2osz/direwolf) y
[tncattach](https://github.com/markqvist/tncattach), con control de una radio **Kenwood TK-780**,
audio en directo, histórico completo y una web con estilo *WinBox legacy*.

- Arranca **direwolf → tncattach → comandos post-arranque → tcpdump** en orden, espera a que cada
  uno esté listo y reinicia el conjunto si algo muere. Direwolf + red se pueden **activar/desactivar**
  desde la web.
- **Mapa** OpenStreetMap con estaciones APRS, su ruta (origen → digipeaters/IGate → esta estación) y rastro.
- **SSID**: estaciones agrupadas por indicativo base (EA1ABC → -1, -7, -9…), con filtro por texto, SSID y
  tiempo, y el histórico de posiciones y mensajes de cada una (o de todos sus SSID), con rastro en el mapa.
- **Mensajes** APRS con conversaciones y boletines (histórico completo, carga dinámica).
- **Radio**: canal, frecuencia, señal, squelch y scan del TK-780.
- **Direwolf**: opciones habituales (módem, tiempos, baliza, digipeater, IGate…) sin tocar tu `direwolf.conf`.
- **Audio** de la radio en el navegador, a la vez que Direwolf (ALSA `dsnoop`).
- **Eventos** en español y **logs** en bruto, con histórico persistente, búsqueda e «ir a fecha».
- IPv6 desactivado en `tnc0`. Solo biblioteca estándar de Python 3.9+; sin `pip install`.

Requisitos y diseño: [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md).

## Instalación (Raspberry Pi / Debian)

```bash
git clone <este repo> && cd rfnetd
sudo ./install.sh --audio-share   # /opt/rfnetd, /etc/rfnetd/rfnetd.conf, unidad systemd, dsnoop
sudoedit /etc/rfnetd/rfnetd.conf  # indicativo, base_conf, [radio], [audio], [web] password…
sudo systemctl enable --now rfnetd
```

Si ya tienes `direwolf`/`tncattach` lanzados a mano, páralos antes (comparten audio y puerto KISS).
Web: `http://<ip>:8088/` · Logs del servicio: `journalctl -u rfnetd -f`.

### Audio a la vez que Direwolf

Una tarjeta USB solo puede abrirse una vez para capturar. `etc/asound.conf.example` define `rf_cap`
(un `dsnoop` sobre `hw:1,0`; ajusta el número de tarjeta) y rfnetd lo usa con
`[direwolf] audio_in = rf_cap` y `[audio] device = rf_cap`. La reproducción (TX) no cambia.

### Opciones de Direwolf desde la web

Con `[direwolf] base_conf` rfnetd genera `/var/lib/rfnetd/direwolf.conf` a partir de tu fichero y de las
opciones guardadas, y lo pasa a Direwolf (`-c {conf}`). Tu fichero nunca se modifica ni se muestra
(contiene el passcode de APRS-IS). IL2P (Direwolf 1.7+) se activa en la transmisión con un desplegable (añade `-I`/`-i` al lanzarlo; la recepción es automática).
Los cambios se validan (sin comillas ni saltos de línea) y piden reinicio.

## Seguridad

- **Sin contraseña** (`[web] password` vacío): web de **solo lectura**, sin audio ni control.
- **Con contraseña**: toda la web pide login (Basic auth). Las órdenes (POST) exigen además la cabecera
  `X-Requested-With: rfnetd` y JSON, para evitar CSRF desde otras webs. No hay TLS: úsalo en red de confianza o VPN.
- El servicio corre como root (tncattach, `ip` y captura lo necesitan); Direwolf se lanza con `[direwolf] user`.
- Todo texto recibido por radio se muestra como texto, nunca como HTML.
- No subas `direwolf.conf` ni `/etc/rfnetd/rfnetd.conf` (contraseñas, passcode APRS-IS).
- El navegador necesita Internet para las teselas de OSM y Leaflet (CDN), no la Pi.

## API (JSON)

| Ruta | Contenido |
|---|---|
| `GET /api/status` | procesos, interfaz, contadores, radio, audio |
| `GET /api/events?after=N`, `/api/logs?src=…&after=N` | tiempo real (ids crecientes) |
| `GET /api/history/events`, `/api/history/raw` | histórico: `before`, `until`, `q`, `limit`, `src` |
| `GET /api/conversations`, `/api/messages` | mensajes APRS: `conv`, `before`, `after`, `q`, `to` |
| `GET/POST /api/tx` | transmisión APRS: `{"action":"message\|status\|position",…}` (ver sección Transmitir) |
| `GET /api/stations` | estaciones con posición, ruta y rastro |
| `GET/POST /api/radio` | TK-780: `{"action":"channel\|monitor\|scan",…}` |
| `GET/POST /api/direwolf` | `{"action":"start\|stop\|restart\|apply","options":{…}}` |
| `GET /api/network`, `/api/audio`, `/stream` | interfaz, audio, MP3 en directo |

## Notas sobre APRS

- Los paquetes **de tercero** (`}`) que reenvía un IGate se atribuyen a la estación **interior**, no al IGate.
  El IGate aparece como pasarela en la ruta («recibido de Internet por el IGate X»).
- `[0H]` de Direwolf (copia digipeada por nuestra estación) y `[0L]` (transmitido por nosotros) no
  actualizan el mapa; las definiciones de telemetría (`PARM.`, `UNIT.`, `EQNS.`, `BITS.`) no son mensajes.

## Pruebas

```bash
python3 -m unittest discover tests     # sin hardware: radio simulada con un pty
```

## Transmitir APRS

Con `[tx] enabled = yes` y `[web] password`, la pestaña **Mensajes** permite enviar:

- **Mensajes** a una estación (hasta 67 caracteres, sin acentos ni `{ | ~`): llevan número de mensaje, se
  reintentan según `retry_seconds` y se marcan como entregado (ack), rechazado o sin confirmación.
  Los destinos `BLN…`/`NWS…` se envían una vez, sin ack.
- **Estado** y **posición fija** (la de `[station]`), de un solo disparo.

Cada envío pide confirmación en la web y queda en el histórico. No se transmite si no hay indicativo propio,
si la radio no está en `require_freq` ni en un canal de `allow_channels` (por nombre, p. ej. `DATA`), está en scan o desconectada, si se supera `max_per_hour` o si no pasa
`min_interval`. Con `auto_ack = yes` también se confirman los mensajes recibidos para esta estación.
Los envíos pendientes se pierden si se reinicia el servicio (quedan marcados como fallidos).

## Limitaciones

Telemetría y meteorología se muestran como comentario; no hay mensajes multitrama ni cifrado;
el mapa necesita Internet en el navegador; Squelch y Scan de la radio solo se conocen tras conmutarlos
(la radio no los informa).
