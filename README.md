# JARVIS Music Node

Nodo de música de PEARL HOME. Recibe órdenes HTTP del Core (`jarvis_core`),
busca la canción en YouTube con `yt-dlp` y la reproduce solo como audio con
`mpv` en los parlantes de la laptop.

```text
PEARL Core :5004  ──Bearer──▶  Music Node :5005  ──▶  yt-dlp (búsqueda)
                                       │               mpv (audio)
                                       └──▶ JARVIS Brain :5008 (región "musica")
```

## Cómo funciona

1. `POST /play` con una búsqueda en texto libre (`"query": "..."`).
2. `yt-dlp` resuelve el resultado `ytsearchN:<query>` y devuelve la URL directa
   del audio.
3. `mpv` la reproduce sin video y expone un socket IPC
   (`/tmp/jarvis-mpv.sock`) para pausar, reanudar y consultar el estado.
4. El nodo espera hasta `JARVIS_MPV_STARTUP_TIMEOUT` segundos a que mpv abra la
   salida de audio. Si falla, prueba la siguiente combinación de resultado,
   cliente de YouTube y formato antes de rendirse.

`/next` y `/previous` no usan una lista de reproducción: avanzan o retroceden
el índice dentro de los resultados de la misma búsqueda.

### Reintentos

Por cada resultado (desde el índice pedido hasta `JARVIS_YTDLP_SEARCH_FALLBACKS`
más) se prueban todos los clientes de `JARVIS_YTDLP_PLAYER_CLIENTS` y todos los
formatos de `JARVIS_YTDLP_FORMATS`. Los errores 403 de YouTube se resumen en un
mensaje corto; el último error queda en `last_error` de `/status`.

## API

Todas las rutas exigen `Authorization: Bearer <JARVIS_MUSIC_TOKEN>`; sin token
válido responden `401`. Las respuestas son JSON con `status` (`ok` o `error`) y
`message`.

| Método | Ruta | Cuerpo | Qué hace |
| --- | --- | --- | --- |
| `POST` | `/play` | `{"query": "artista canción"}` | Detiene lo que suena y reproduce el primer resultado |
| `POST` | `/pause` | — | Pausa mpv |
| `POST` | `/resume` | — | Reanuda mpv |
| `POST` | `/stop` | — | Termina mpv y limpia el estado |
| `POST` | `/next` | — | Reproduce el siguiente resultado de la misma búsqueda |
| `POST` | `/previous` | — | Reproduce el resultado anterior (error si ya está en el primero) |
| `GET` | `/status` | — | Estado actual: `running`, `playing`, `paused`, `title`, `webpage_url`, `duration`, `thumbnail`, `player_client`, `last_error` |

`/play`, `/next` y `/previous` devuelven además `title`, `webpage_url`,
`duration`, `thumbnail` e `index` del tema que empezó a sonar.

Ejemplo:

```bash
curl -s -X POST http://127.0.0.1:5005/play \
  -H "Authorization: Bearer $JARVIS_MUSIC_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "soda stereo de música ligera"}'
```

## JARVIS Brain

Si `JINNEX_BRAIN_URL` está definido, el nodo enciende la región `musica` de
JARVIS Brain mientras suena algo: `buscando música` al resolver, el título al
empezar y un evento de fin al pausar, detener o cuando mpv termina solo. Un
hilo propio renueva la región cada 4 minutos (TTL de 10) y vigila el proceso de
mpv cada 5 segundos.

Los envíos al cerebro van por una cola con timeout de 0,5 s y nunca bloquean ni
hacen fallar la música. `JINNEX_BRAIN_TOKEN`, si existe, se manda como
`X-Brain-Token`.

## Requisitos

- Python 3 con `Flask` y `waitress` (`requirements.txt`).
- `mpv` en el `PATH`.
- `yt-dlp` (por defecto `~/.local/bin/yt-dlp`).
- Node.js para los desafíos JavaScript de YouTube (por defecto el de `nvm`
  en `~/.nvm/versions/node/v24.15.0`; si no existe, el `node` del `PATH`).

## Instalación

```bash
cd ~/jarvis_node
python3 -m venv venv
venv/bin/pip install -r requirements.txt

mkdir -p ~/.config/jarvis-music
printf 'JARVIS_MUSIC_TOKEN=%s\n' "$(openssl rand -hex 32)" > ~/.config/jarvis-music/music.env
chmod 600 ~/.config/jarvis-music/music.env
```

El mismo token tiene que estar en `jarvis_core/.env` para que el Core pueda
llamar al nodo. Sin `JARVIS_MUSIC_TOKEN` el servidor se niega a arrancar.

### Servicio

`jarvis-music.service` es una unidad de **usuario** de systemd:

```bash
cp jarvis-music.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jarvis-music
journalctl --user -u jarvis-music -f
```

En el despliegue actual hay además un override de aislamiento en
`~/.config/systemd/user/jarvis-music.service.d/` (auditoría 2026-10-07):
`ProtectSystem=full` (no `strict`, porque mpv necesita PipeWire en `/run/user`),
`ProtectHome=read-only` con escritura solo en `~/.cache` y el estado de mpv,
`PrivateTmp=true` (el socket de mpv queda en un `/tmp` privado) y
`MemoryMax=1G`.

El servidor escucha en `0.0.0.0:5005` con waitress y 4 hilos.

## Configuración

| Variable | Por defecto | Uso |
| --- | --- | --- |
| `JARVIS_MUSIC_TOKEN` | — (obligatoria) | Token Bearer de todas las rutas |
| `JARVIS_YTDLP_PATH` | `~/.local/bin/yt-dlp` | Ejecutable de yt-dlp |
| `JARVIS_YTDLP_NODE_PATH` / `YTDLP_NODE_PATH` | node de nvm | Runtime JS para yt-dlp |
| `JARVIS_YTDLP_FORMATS` | `bestaudio/best[height<=480]/best,best` | Formatos a probar, separados por comas |
| `JARVIS_YTDLP_PLAYER_CLIENTS` | `web_embedded,default` | Clientes de YouTube a probar |
| `JARVIS_YTDLP_SEARCH_FALLBACKS` | `5` | Cuántos resultados probar desde el pedido |
| `JARVIS_MPV_STARTUP_TIMEOUT` | `8` | Segundos para que mpv confirme el audio |
| `JINNEX_BRAIN_URL` | vacío (la unidad usa `http://127.0.0.1:5008/event`) | Evento de JARVIS Brain |
| `JINNEX_BRAIN_TOKEN` | vacío | Token del cerebro |

## Problemas frecuentes

- **`YouTube rechazó el flujo de audio con HTTP 403`**: casi siempre es un
  `yt-dlp` desactualizado. Es el binario autónomo, así que se actualiza con
  `~/.local/bin/yt-dlp -U`.
- **`mpv no confirmó la salida de audio`**: revisar que PipeWire esté activo en
  la sesión del usuario y que la unidad corra como servicio de usuario.
- **`401 unauthorized`**: el token de `jarvis_core/.env` no coincide con el de
  `~/.config/jarvis-music/music.env`.

## Licencia

Propietaria; todos los derechos reservados. Ver `LICENSE`.
