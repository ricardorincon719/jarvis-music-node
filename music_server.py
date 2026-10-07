import hmac
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

MUSIC_TOKEN = os.getenv("JARVIS_MUSIC_TOKEN", "").strip()

MPV_SOCKET = "/tmp/jarvis-mpv.sock"
DEFAULT_NODE_PATH = (
    Path.home() / ".nvm" / "versions" / "node" / "v24.15.0" / "bin" / "node"
)
DEFAULT_YTDLP_PATH = Path.home() / ".local" / "bin" / "yt-dlp"
YTDLP_FORMATS = [
    value.strip()
    for value in os.getenv(
        "JARVIS_YTDLP_FORMATS",
        "bestaudio/best[height<=480]/best,best",
    ).split(",")
    if value.strip()
]
YTDLP_PLAYER_CLIENTS = [
    value.strip()
    for value in os.getenv(
        "JARVIS_YTDLP_PLAYER_CLIENTS",
        "web_embedded,default",
    ).split(",")
    if value.strip()
]
YTDLP_SEARCH_FALLBACKS = max(1, int(os.getenv("JARVIS_YTDLP_SEARCH_FALLBACKS", "5")))
MPV_STARTUP_TIMEOUT = max(1.0, float(os.getenv("JARVIS_MPV_STARTUP_TIMEOUT", "8")))

state = {
    "query": None,
    "index": 1,
    "process": None,
    "paused": False,
    "title": None,
    "webpage_url": None,
    "duration": None,
    "thumbnail": None,
    "player_client": None,
    "last_error": None,
}


def node_runtime_path():
    configured = os.getenv("JARVIS_YTDLP_NODE_PATH") or os.getenv("YTDLP_NODE_PATH")
    candidates = [configured, str(DEFAULT_NODE_PATH), shutil.which("node")]

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate

    return None


def yt_dlp_path():
    configured = os.getenv("JARVIS_YTDLP_PATH")
    candidates = [configured, str(DEFAULT_YTDLP_PATH), shutil.which("yt-dlp")]

    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    raise RuntimeError("No se encontró un ejecutable de yt-dlp")


def yt_dlp_base_args(player_client=None):
    args = [yt_dlp_path()]
    node_path = node_runtime_path()
    if node_path:
        args.extend(["--no-js-runtimes", "--js-runtimes", f"node:{node_path}"])
    if player_client and player_client != "default":
        args.extend(["--extractor-args", f"youtube:player_client={player_client}"])
    return args


def cleanup_socket():
    if os.path.exists(MPV_SOCKET):
        try:
            os.remove(MPV_SOCKET)
        except Exception:
            pass


def concise_error(message: str) -> str:
    message = (message or "").strip()
    if "403" in message:
        return "YouTube rechazó el flujo de audio con HTTP 403"
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    return (lines[-1] if lines else "error desconocido")[:300]


def resolve_track(
    query: str,
    index: int,
    selected_format: str,
    player_client: str,
) -> dict:
    result = subprocess.run(
        [
            *yt_dlp_base_args(player_client),
            "-f",
            selected_format,
            "--dump-json",
            f"ytsearch{index}:{query}",
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )

    if result.returncode != 0:
        raise RuntimeError(concise_error(result.stderr or "yt-dlp falló"))

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"No se encontró metadata para el resultado {index}")

    try:
        info = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp devolvió metadata inválida: {exc}") from exc

    url = info.get("url")
    if not url:
        raise RuntimeError(f"No se encontró URL para el resultado {index}")

    return {
        "url": url,
        "title": info.get("title") or query,
        "webpage_url": info.get("webpage_url") or info.get("original_url"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "resolved_index": index,
        "player_client": player_client,
    }


def terminate_process(proc):
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def stop_current():
    proc = state.get("process")
    terminate_process(proc)

    state["process"] = None
    state["paused"] = False
    state["title"] = None
    state["webpage_url"] = None
    state["duration"] = None
    state["thumbnail"] = None
    state["player_client"] = None
    state["last_error"] = None
    cleanup_socket()


def wait_for_playback(proc):
    deadline = time.monotonic() + MPV_STARTUP_TIMEOUT
    last_error = "mpv no creó su socket de control"

    while time.monotonic() < deadline:
        returncode = proc.poll()
        if returncode is not None:
            raise RuntimeError(
                f"mpv terminó antes de reproducir (código {returncode})"
            )

        if os.path.exists(MPV_SOCKET):
            try:
                response = json.loads(mpv_command(["get_property", "audio-out-params"]))
                if response.get("error") == "success" and response.get("data"):
                    return
                last_error = response.get("error") or "mpv aún no abrió el audio"
            except Exception as exc:
                last_error = str(exc)

        time.sleep(0.1)

    raise RuntimeError(f"mpv no confirmó la salida de audio: {last_error}")


def start_playback(query: str, index: int):
    stop_current()
    errors = []

    for candidate_index in range(index, index + YTDLP_SEARCH_FALLBACKS):
        for player_client in YTDLP_PLAYER_CLIENTS:
            for selected_format in YTDLP_FORMATS:
                cleanup_socket()
                try:
                    track = resolve_track(
                        query,
                        candidate_index,
                        selected_format,
                        player_client,
                    )
                except Exception as exc:
                    errors.append(
                        f"{player_client}/{candidate_index}: {concise_error(str(exc))}"
                    )
                    continue

                proc = subprocess.Popen([
                    "mpv",
                    f"--input-ipc-server={MPV_SOCKET}",
                    "--no-video",
                    "--no-ytdl",
                    "--msg-level=statusline=no",
                    track["url"],
                ])

                try:
                    wait_for_playback(proc)
                except Exception as exc:
                    error = concise_error(str(exc))
                    errors.append(f"{player_client}/{candidate_index}: {error}")
                    print(
                        "⚠️ Falló reproducción "
                        f"{player_client}/{candidate_index}: {error}",
                        flush=True,
                    )
                    terminate_process(proc)
                    cleanup_socket()
                    continue

                state["query"] = query
                state["index"] = int(track.get("resolved_index") or index)
                state["process"] = proc
                state["paused"] = False
                state["title"] = track.get("title")
                state["webpage_url"] = track.get("webpage_url")
                state["duration"] = track.get("duration")
                state["thumbnail"] = track.get("thumbnail")
                state["player_client"] = track.get("player_client")
                state["last_error"] = None
                return track

    state["last_error"] = (
        errors[-1] if errors else "No se encontró una fuente reproducible"
    )
    raise RuntimeError(
        "No pude iniciar una fuente de audio reproducible. "
        f"Último error: {state['last_error']}"
    )


def mpv_command(command_list):
    if not os.path.exists(MPV_SOCKET):
        raise RuntimeError("Socket de mpv no disponible")

    payload = json.dumps({"command": command_list}).encode("utf-8") + b"\n"

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.0)
    try:
        client.connect(MPV_SOCKET)
        client.sendall(payload)
        response = client.recv(4096).decode("utf-8", errors="ignore")
        return response
    finally:
        client.close()


@app.before_request
def require_token():
    """Exige Authorization: Bearer <JARVIS_MUSIC_TOKEN> en todas las rutas."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if not (
        MUSIC_TOKEN
        and scheme.lower() == "bearer"
        and hmac.compare_digest(token.strip().encode(), MUSIC_TOKEN.encode())
    ):
        return jsonify({"status": "error", "message": "unauthorized"}), 401
    return None


@app.route("/play", methods=["POST"])
def play():
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()

    if not query:
        return jsonify({"status": "error", "message": "No query provided"}), 400

    try:
        track = start_playback(query, 1)
        print(f"🎵 Query recibida: {query}", flush=True)
        print(f"🎼 Título resuelto: {track.get('title')}", flush=True)

        return jsonify({
            "status": "ok",
            "message": f"Reproduciendo: {track.get('title') or query}",
            "query": query,
            "index": state["index"],
            "title": track.get("title"),
            "webpage_url": track.get("webpage_url"),
            "duration": track.get("duration"),
            "thumbnail": track.get("thumbnail"),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/pause", methods=["POST"])
def pause():
    try:
        mpv_command(["set_property", "pause", True])
        state["paused"] = True
        return jsonify({"status": "ok", "message": "Música en pausa"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/resume", methods=["POST"])
def resume():
    try:
        mpv_command(["set_property", "pause", False])
        state["paused"] = False
        return jsonify({"status": "ok", "message": "Música reanudada"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/stop", methods=["POST"])
def stop():
    try:
        stop_current()
        return jsonify({"status": "ok", "message": "Reproducción detenida"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/next", methods=["POST"])
def next_track():
    query = state.get("query")
    if not query:
        return jsonify({
            "status": "error",
            "message": "No hay reproducción activa",
        }), 400

    try:
        new_index = state["index"] + 1
        track = start_playback(query, new_index)
        print(f"⏭️ Siguiente resultado: {query} [{new_index}]", flush=True)
        print(f"🎼 Título resuelto: {track.get('title')}", flush=True)

        return jsonify({
            "status": "ok",
            "message": f"Siguiente: {track.get('title') or query}",
            "query": query,
            "index": state["index"],
            "title": track.get("title"),
            "webpage_url": track.get("webpage_url"),
            "duration": track.get("duration"),
            "thumbnail": track.get("thumbnail"),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/previous", methods=["POST"])
def previous_track():
    query = state.get("query")
    if not query:
        return jsonify({
            "status": "error",
            "message": "No hay reproducción activa",
        }), 400

    if state["index"] <= 1:
        return jsonify({
            "status": "error",
            "message": "Ya estás en el primer resultado",
        }), 400

    try:
        new_index = state["index"] - 1
        track = start_playback(query, new_index)
        print(f"⏮️ Resultado anterior: {query} [{new_index}]", flush=True)
        print(f"🎼 Título resuelto: {track.get('title')}", flush=True)

        return jsonify({
            "status": "ok",
            "message": f"Anterior: {track.get('title') or query}",
            "query": query,
            "index": state["index"],
            "title": track.get("title"),
            "webpage_url": track.get("webpage_url"),
            "duration": track.get("duration"),
            "thumbnail": track.get("thumbnail"),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/status", methods=["GET"])
def status():
    running = state["process"] is not None and state["process"].poll() is None
    return jsonify({
        "status": "ok",
        "running": running,
        "playing": running and not state["paused"],
        "query": state["query"],
        "index": state["index"],
        "paused": state["paused"],
        "title": state["title"],
        "webpage_url": state["webpage_url"],
        "duration": state["duration"],
        "thumbnail": state["thumbnail"],
        "player_client": state["player_client"],
        "last_error": state["last_error"],
        "target": "laptop",
    })


if __name__ == "__main__":
    if not MUSIC_TOKEN:
        raise SystemExit("Falta JARVIS_MUSIC_TOKEN: el nodo de música no arranca sin token.")
    from waitress import serve

    print("🔥 Music Node corriendo...", flush=True)
    serve(app, host="0.0.0.0", port=5005, threads=4)
