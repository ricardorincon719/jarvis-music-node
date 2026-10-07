import json
import os
import unittest
from unittest.mock import MagicMock, patch

import music_server


class MusicServerTests(unittest.TestCase):
    def setUp(self):
        token_patch = patch("music_server.MUSIC_TOKEN", "token-de-prueba")
        token_patch.start()
        self.addCleanup(token_patch.stop)
        self.auth = {"Authorization": "Bearer token-de-prueba"}
        music_server.state.update({
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
        })

    def test_ytdlp_prefers_updated_user_binary(self):
        updated_binary = str(music_server.DEFAULT_YTDLP_PATH)
        with patch.dict(os.environ, {}, clear=True), patch(
            "music_server.os.path.isfile",
            side_effect=lambda path: path == updated_binary,
        ), patch("music_server.os.access", return_value=True), patch(
            "music_server.shutil.which",
            return_value="/usr/bin/yt-dlp",
        ):
            self.assertEqual(music_server.yt_dlp_path(), updated_binary)

    @patch("music_server.subprocess.run")
    def test_resolve_track_selects_web_embedded_client(self, run):
        run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "url": "https://media.example/audio",
                "title": "Canción",
                "webpage_url": "https://youtube.example/watch?v=1",
            }),
            stderr="",
        )

        track = music_server.resolve_track(
            "canción",
            1,
            "bestaudio",
            "web_embedded",
        )

        command = run.call_args.args[0]
        self.assertIn("youtube:player_client=web_embedded", command)
        self.assertEqual(track["player_client"], "web_embedded")

    @patch("music_server.cleanup_socket")
    @patch("music_server.wait_for_playback")
    @patch("music_server.subprocess.Popen")
    @patch("music_server.resolve_track")
    def test_start_playback_retries_after_mpv_failure(
        self,
        resolve_track,
        popen,
        wait_for_playback,
        _cleanup_socket,
    ):
        resolve_track.return_value = {
            "url": "https://media.example/audio",
            "title": "Canción",
            "resolved_index": 1,
            "player_client": "web_embedded",
        }
        failed_process = MagicMock()
        failed_process.poll.return_value = 2
        working_process = MagicMock()
        working_process.poll.return_value = None
        popen.side_effect = [failed_process, working_process]
        wait_for_playback.side_effect = [RuntimeError("HTTP 403"), None]

        with patch.object(music_server, "YTDLP_FORMATS", ["bestaudio"]), patch.object(
            music_server,
            "YTDLP_PLAYER_CLIENTS",
            ["web_embedded", "default"],
        ):
            track = music_server.start_playback("canción", 1)

        self.assertEqual(popen.call_count, 2)
        self.assertEqual(track["title"], "Canción")
        self.assertIs(music_server.state["process"], working_process)
        self.assertIsNone(music_server.state["last_error"])

    @patch("music_server.start_playback", side_effect=RuntimeError("HTTP 403"))
    def test_play_returns_error_instead_of_false_success(self, _start_playback):
        client = music_server.app.test_client()

        response = client.post(
            "/play", json={"query": "canción"}, headers=self.auth
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["status"], "error")
        self.assertNotIn("Reproduciendo", response.get_json()["message"])

    def test_requests_without_valid_token_are_rejected(self):
        client = music_server.app.test_client()

        for headers in (
            {},
            {"Authorization": "Bearer otro-token"},
            {"Authorization": "token-de-prueba"},
        ):
            response = client.get("/status", headers=headers)
            self.assertEqual(response.status_code, 401)

        response = client.get("/status", headers=self.auth)
        self.assertEqual(response.status_code, 200)

    def test_requests_are_rejected_when_no_token_is_configured(self):
        client = music_server.app.test_client()

        with patch("music_server.MUSIC_TOKEN", ""):
            response = client.get("/status", headers={"Authorization": "Bearer "})

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
