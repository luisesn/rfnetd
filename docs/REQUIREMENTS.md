# rfnetd — Documento de requerimientos

Versión 0.1 · 2026-11-01

## 1. Propósito

Sustituir el script manual `activarRedRf` por un **servicio** que levante y
supervise una red IP sobre radio de paquetes (AX.25 / APRS) y ofrezca una
**interfaz web** para entender qué pasa en el enlace: logs en bruto, logs
traducidos a lenguaje humano y un mapa con las estaciones APRS recibidas y la
ruta que han seguido sus paquetes.

## 2. Contexto y entorno de destino

| Elemento        | Valor                                                             |
|-----------------|-------------------------------------------------------------------|
| Equipo          | Raspberry Pi, Raspbian 11 bullseye, armv7, 1 GB RAM |
| Python          | 3.9.2 (sin pip packages instalados: ni Flask ni aiohttp)          |
| Ya instalado    | `direwolf`, `tncattach`, `tcpdump`, `ip`, `systemd`, `git`        |
| No instalado    | `tshark` (se usa `tcpdump`, que cubre la necesidad)               |
| Estación        | N0CALL-5, módem 1200 baud, KISS TCP en 127.0.0.1:8001             |
| Red de radio    | `tnc0`, 44.0.0.2/24 (AMPRNet), MTU 250, solo IPv4               |

Comportamiento a replicar (script original):

1. Arrancar `direwolf` y esperar a que esté listo.
2. Arrancar `tncattach` contra el puerto KISS de Direwolf (crea `tnc0`).
3. Ajustar la interfaz para un canal lento: ruta con `initrwnd 1 initcwnd 1` y
   `txqueuelen 1`.

## 3. Requerimientos funcionales

### 3.1 Supervisión de procesos

- **RF-01** El servicio arranca Direwolf, tncattach y una captura de tráfico
  sobre `tnc0` en ese orden.
- **RF-02** Espera a que Direwolf acepte conexiones en el puerto KISS en lugar
  de dormir un tiempo fijo; espera a que exista la interfaz `tnc0` antes de
  configurarla. Cada espera tiene un tiempo máximo configurable.
- **RF-03** Si cualquiera de los procesos muere, se detiene el conjunto
  completo y se reinicia en orden (tncattach depende de la conexión KISS de
  Direwolf), con espera creciente entre intentos (máx. 60 s).
- **RF-04** Al parar el servicio se terminan todos los procesos hijos y
  desaparece `tnc0`.
- **RF-05** Cada proceso puede desactivarse por configuración
  (`enabled = no`), p. ej. para usar solo el visor con un Direwolf externo.

### 3.2 Configuración personalizable

- **RF-06** Un único fichero (`/etc/rfnetd/rfnetd.conf`, formato INI) define:
  indicativo y posición de la estación, línea de comando completa de
  Direwolf, tncattach y la captura, usuario con el que corre Direwolf,
  interfaz, lista de **comandos post-arranque** (rutas, `txqueuelen`, etc.),
  puerto/dirección del servidor web, directorios de logs y datos.
- **RF-07** Los comandos post-arranque se ejecutan en orden, uno por línea,
  sin shell; un fallo se registra pero no aborta el arranque.
- **RF-08** El fichero de ejemplo reproduce exactamente la configuración del
  script `activarRedRf` original.

### 3.3 Logs

- **RF-09** Se conserva el log en bruto de cada fuente (`direwolf`,
  `tncattach`, `capture`, y `rfnetd` para los mensajes del propio servicio) en
  fichero con rotación y en un buffer en memoria.
- **RF-10** Se genera un flujo paralelo de **eventos legibles** en español:
  - Direwolf: paquetes APRS recibidos/transmitidos/digipeados, posiciones,
    mensajes, estados, objetos, Mic-E, con la ruta y los digipeaters.
  - tncattach: creación de interfaz, conexión KISS y errores.
  - tcpdump: una línea por paquete IP (protocolo, origen → destino, flags TCP
    en palabras, tamaño).
- **RF-11** Los logs en bruto nunca se alteran; los eventos legibles son una
  vista derivada.

### 3.4 Interfaz web (HTTP)

- **RF-12** Servida por el propio servicio, sin dependencias de Python
  externas.
- **RF-13** Pestañas: **Mapa**, **Eventos** (legibles), **Logs** (selector de
  fuente: direwolf / tncattach / tcpdump / rfnetd).
- **RF-14** Barra de estado: estado de cada proceso (activo/parado, PID,
  reinicios), estado de `tnc0`, contadores de paquetes, hora del último
  paquete APRS.
- **RF-15** Actualización en vivo (sondeo incremental cada ~2 s) con
  autoescroll opcional, filtro de texto y botón de pausa.
- **RF-16** API JSON de solo lectura: `/api/status`, `/api/logs`,
  `/api/events`, `/api/stations`.

### 3.5 Mapa APRS

- **RF-17** Cartografía OpenStreetMap (Leaflet), centrada en la estación
  propia, que se muestra con marcador distinto.
- **RF-18** Una marca por estación APRS con posición conocida: indicativo,
  símbolo/tipo, última escucha, comentario, velocidad/rumbo y nº de
  paquetes.
- **RF-19** Posiciones soportadas: formato sin comprimir, comprimido, Mic-E,
  objetos, items y paquetes de terceros (`}`).
- **RF-20** **Ruta**: para cada paquete se obtiene la cadena
  origen → digipeaters que lo retransmitieron (campo de path con `*`) →
  estación propia. Se dibuja sobre el mapa la ruta de la estación
  seleccionada; los digipeaters sin posición conocida se indican como
  «sin posición». Opcionalmente se dibujan las rutas de todas.
- **RF-21** Rastro (últimas N posiciones) de la estación seleccionada.
- **RF-22** Las estaciones se persisten periódicamente en disco y se
  recargan al arrancar; se descartan las más antiguas que un límite
  configurable (por defecto 48 h).

## 4. Requerimientos no funcionales

- **RNF-01** Solo biblioteca estándar de Python 3.9; sin compilación.
- **RNF-02** Consumo bajo en Pi 1 GB: buffers acotados (líneas y estaciones),
  ficheros con rotación.
- **RNF-03** **Seguridad:**
  - Todo texto procedente de la radio (comentarios, mensajes, indicativos) es
    no fiable y se inserta en la página solo como texto, nunca como HTML.
  - La interfaz web es de solo lectura; por defecto escucha en todas las
    interfaces y se puede limitar con `bind`. No incluye autenticación (ver
    sección 6).
  - El servicio corre como root (necesario para tncattach, `ip` y captura);
    Direwolf se lanza con el usuario configurado (`runuser`).
  - El repositorio no contiene `direwolf.conf` ni contraseñas (el passcode de
    APRS-IS es un secreto).
- **RNF-04** Instalación con `install.sh` en `/opt/rfnetd`, unidad systemd
  `rfnetd.service`, `Restart=on-failure`, `KillMode=control-group`.
- **RNF-05** Pruebas unitarias del parser APRS y de los humanizadores
  ejecutables sin hardware (`python3 -m unittest`).
- **RNF-06** Los mapas requieren acceso a Internet **del navegador** del
  usuario (teselas OSM y Leaflet desde CDN), no de la Pi.

## 5. Arquitectura

```
                    ┌──────────────────────── rfnetd (root) ───────────────────────┐
 audio/PTT ◄──────► │ Supervisor ─► direwolf ──KISS 8001──► tncattach ─► tnc0      │
                    │     │            │ stdout               │ stdout     │       │
                    │     │            ▼                      ▼            ▼       │
                    │     │         ┌──────── pty line readers ───────────────┐    │
                    │     │         │ tcpdump -i tnc0  ◄────────────────────┐ │    │
                    │     │         └──────┬───────────────────────────────── │ ┘    │
                    │     ▼                ▼                                       │
                    │  post_up cmds   Pipeline: log bruto + parser APRS/IP         │
                    │                   │            │                             │
                    │                   ▼            ▼                             │
                    │              LogHub (buffers,  StationStore (JSON)           │
                    │              ficheros rotados)      │                        │
                    │                   └─────┬──────────┘                         │
                    │                         ▼                                    │
                    │               HTTP server (/ , /api/*)                       │
                    └──────────────────────────┬───────────────────────────────────┘
                                               ▼
                                 Navegador: Leaflet + OSM + tablas
```

Módulos: `config.py`, `supervisor.py`, `pipeline.py`, `aprs.py`, `humanize.py`,
`store.py`, `web.py`, `static/index.html`.

Los procesos hijos se ejecutan bajo un pseudo-terminal para que escriban sus
logs línea a línea (Direwolf y tncattach usan buffer completo si stdout es una
tubería).

## 6. Decisiones y alcance

- Diferencia con el script original: `tncattach` se lanza **sin `-d`**
  (demonio) para que el servicio pueda supervisarlo. El resto de opciones se
  conservan.
- `tcpdump` en lugar de `tshark`: ya está instalado, y para una línea por
  paquete es equivalente. Se puede cambiar el comando en la configuración.
- Los mensajes APRS se leen de la salida de Direwolf (que también registra lo
  recibido por IGate), no de una segunda conexión KISS/AGW.
- **Fuera de alcance (v0.1):** autenticación en la web, envío de mensajes APRS
  desde la interfaz, mapas sin conexión, telemetría y meteorología detalladas
  (se muestran como comentario), IPv6.

## 7. Criterios de aceptación

1. `systemctl start rfnetd` deja `tnc0` arriba con `44.0.0.2/24`, la ruta y
   `txqueuelen 1` aplicados, sin intervención manual.
2. Matar Direwolf (`kill`) provoca el reinicio ordenado de los tres procesos
   y la web lo refleja en la barra de estado.
3. Un paquete APRS con posición recibido por Direwolf aparece en menos de 5 s
   en Eventos, en Logs y como marca en el mapa.
4. Un paquete con digipeaters dibuja la ruta origen → digis → estación
   propia al seleccionar la estación.
5. `ping` por `tnc0` aparece como líneas legibles en Eventos.
6. `python3 -m unittest discover tests` pasa sin hardware.
