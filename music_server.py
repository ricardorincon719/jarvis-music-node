from flask import Flask, request, jsonify
import subprocess
import socket
import json
import os
import shutil
import time
from pathlib import Path

app = Flask(__name__)

MPV_SOCKET = "/tmp/jarvis-mpv.sock"
DEFAULT_NODE_PATH = Path.home() / ".nvm" / "versions" / "node" / "v24.15.0" / "bin" / "node"
YTDLP_FORMATS = [
    value.strip()
    for value in os.getenv("JARVIS_YTDLP_FORMATS", "bestaudio/best[height<=480]/best,best").split(",")
    if value.strip()
]
YTDLP_SEARCH_FALLBACKS = max(1, int(os.getenv("JARVIS_YTDLP_SEARCH_FALLBACKS", "5")))

state = {
    "query": None,
    "index": 1,
    "process": None,
    "paused": False,
    "title": None,
    "webpage_url": None,
    "duration": None,
    "thumbnail": None,
}

def node_runtime_path():
    configured = os.getenv("JARVIS_YTDLP_NODE_PATH") or os.getenv("YTDLP_NODE_PATH")
    candidates = [configured, str(DEFAULT_NODE_PATH), shutil.which("node")]

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate

    return None

def yt_dlp_base_args():
    args = ["yt-dlp"]
    node_path = node_runtime_path()
    if node_path:
        args.extend(["--no-js-runtimes", "--js-runtimes", f"node:{node_path}"])
    return args

def cleanup_socket():
    if os.path.exists(MPV_SOCKET):
        try:
            os.remove(MPV_SOCKET)
        except Exception:
            pass

def resolve_track(query: str, index: int) -> dict:
    last_error = None

    for candidate_index in range(index, index + YTDLP_SEARCH_FALLBACKS):
        for selected_format in YTDLP_FORMATS:
            result = subprocess.run(
                [
                    *yt_dlp_base_args(),
                    "-f",
                    selected_format,
                    "--dump-json",
                    f"ytsearch{candidate_index}:{query}",
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )

            if result.returncode != 0:
                last_error = result.stderr.strip() or "yt-dlp falló"
                continue

            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if not lines:
                last_error = f"No se encontró metadata para reproducir en resultado {candidate_index}"
                continue

            try:
                info = json.loads(lines[-1])
            except json.JSONDecodeError as e:
                last_error = f"yt-dlp devolvió metadata inválida en resultado {candidate_index}: {e}"
                continue

            url = info.get("url")
            if not url:
                last_error = f"No se encontró URL para reproducir en resultado {candidate_index}"
                continue

            return {
                "url": url,
                "title": info.get("title") or query,
                "webpage_url": info.get("webpage_url") or info.get("original_url"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "resolved_index": candidate_index,
            }

    raise RuntimeError(last_error or "yt-dlp falló")

def stop_current():
    proc = state.get("process")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    state["process"] = None
    state["paused"] = False
    state["title"] = None
    state["webpage_url"] = None
    state["duration"] = None
    state["thumbnail"] = None
    cleanup_socket()

def start_playback(query: str, index: int):
    stop_current()
    cleanup_socket()

    track = resolve_track(query, index)

    proc = subprocess.Popen([
        "mpv",
        f"--input-ipc-server={MPV_SOCKET}",
        "--no-video",
        track["url"]
    ])

    # Darle tiempo a mpv para abrir el socket
    for _ in range(20):
        if os.path.exists(MPV_SOCKET):
            break
        time.sleep(0.1)

    state["query"] = query
    state["index"] = int(track.get("resolved_index") or index)
    state["process"] = proc
    state["paused"] = False
    state["title"] = track.get("title")
    state["webpage_url"] = track.get("webpage_url")
    state["duration"] = track.get("duration")
    state["thumbnail"] = track.get("thumbnail")

    return track

def mpv_command(command_list):
    if not os.path.exists(MPV_SOCKET):
        raise RuntimeError("Socket de mpv no disponible")

    payload = json.dumps({"command": command_list}).encode("utf-8") + b"\n"

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(MPV_SOCKET)
        client.sendall(payload)
        response = client.recv(4096).decode("utf-8", errors="ignore")
        return response
    finally:
        client.close()

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
        return jsonify({"status": "error", "message": "No hay reproducción activa"}), 400

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
        return jsonify({"status": "error", "message": "No hay reproducción activa"}), 400

    if state["index"] <= 1:
        return jsonify({"status": "error", "message": "Ya estás en el primer resultado"}), 400

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
        "target": "laptop",
    })

if __name__ == "__main__":
    print("🔥 Music Node corriendo...", flush=True)
    app.run(host="0.0.0.0", port=5005)
