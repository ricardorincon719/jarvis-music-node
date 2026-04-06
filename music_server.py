from flask import Flask, request, jsonify
import subprocess
import socket
import json
import os
import time

app = Flask(__name__)

MPV_SOCKET = "/tmp/jarvis-mpv.sock"

state = {
    "query": None,
    "index": 1,
    "process": None,
    "paused": False,
}

def cleanup_socket():
    if os.path.exists(MPV_SOCKET):
        try:
            os.remove(MPV_SOCKET)
        except Exception:
            pass

def resolve_url(query: str, index: int) -> str:
    result = subprocess.run(
        ["yt-dlp", f"ytsearch{index}:{query}", "-g"],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "yt-dlp falló")

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("No se encontró URL para reproducir")

    return lines[-1]

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
    cleanup_socket()

def start_playback(query: str, index: int):
    stop_current()
    cleanup_socket()

    url = resolve_url(query, index)

    proc = subprocess.Popen([
        "mpv",
        f"--input-ipc-server={MPV_SOCKET}",
        "--force-window=yes",
        url
    ])

    # Darle tiempo a mpv para abrir el socket
    for _ in range(20):
        if os.path.exists(MPV_SOCKET):
            break
        time.sleep(0.1)

    state["query"] = query
    state["index"] = index
    state["process"] = proc
    state["paused"] = False

    return url

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
        url = start_playback(query, 1)
        print(f"🎵 Query recibida: {query}", flush=True)
        print(f"🔗 URL resuelta: {url[:120]}...", flush=True)

        return jsonify({
            "status": "ok",
            "message": f"Reproduciendo: {query}",
            "query": query,
            "index": 1
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
        url = start_playback(query, new_index)
        print(f"⏭️ Siguiente resultado: {query} [{new_index}]", flush=True)
        print(f"🔗 URL resuelta: {url[:120]}...", flush=True)

        return jsonify({
            "status": "ok",
            "message": f"Siguiente: {query} (resultado {new_index})",
            "query": query,
            "index": new_index
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
        url = start_playback(query, new_index)
        print(f"⏮️ Resultado anterior: {query} [{new_index}]", flush=True)
        print(f"🔗 URL resuelta: {url[:120]}...", flush=True)

        return jsonify({
            "status": "ok",
            "message": f"Anterior: {query} (resultado {new_index})",
            "query": query,
            "index": new_index
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/status", methods=["GET"])
def status():
    running = state["process"] is not None and state["process"].poll() is None
    return jsonify({
        "status": "ok",
        "running": running,
        "query": state["query"],
        "index": state["index"],
        "paused": state["paused"]
    })

if __name__ == "__main__":
    print("🔥 Music Node corriendo...", flush=True)
    app.run(host="0.0.0.0", port=5005)
