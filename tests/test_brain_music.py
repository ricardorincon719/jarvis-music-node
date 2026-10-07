import unittest
from unittest.mock import MagicMock, patch

import music_server


class BrainMusicTests(unittest.TestCase):
    """La música enciende su región del cerebro sin afectar la reproducción."""

    def setUp(self):
        self.sent = []
        for target, value in (("BRAIN_URL", "http://brain"), ("MUSIC_TOKEN", "token"),
                              ("_brain_send", self.sent.append),
                              ("cleanup_socket", lambda: None)):
            patcher = patch.object(music_server, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        music_server._brain.update(id=None, detail="", renewed=0.0, playing=False)
        self.addCleanup(music_server._brain.update, id=None, playing=False)
        music_server.state.update(process=None, paused=False, title=None, query=None,
                                  index=1, last_error=None)
        self.auth = {"Authorization": "Bearer token"}

    def steps(self):
        return [(m["phase"], m.get("detail"), m.get("ok"), m.get("reason"))
                for m in self.sent]

    def play(self, title="Canción", fails=False):
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(music_server, "resolve_track",
                          side_effect=RuntimeError("403") if fails else None,
                          return_value={"url": "u", "title": title, "resolved_index": 1}), \
                patch.object(music_server, "wait_for_playback"), \
                patch.object(music_server.subprocess, "Popen", return_value=process), \
                patch.object(music_server, "YTDLP_FORMATS", ["bestaudio"]), \
                patch.object(music_server, "YTDLP_PLAYER_CLIENTS", ["web"]), \
                patch.object(music_server, "YTDLP_SEARCH_FALLBACKS", 1):
            try:
                music_server.start_playback("canción", 1)
            except RuntimeError:
                pass
        return process

    def test_searching_then_playing_with_the_title(self):
        self.play("Bohemian Rhapsody")
        self.assertEqual(self.steps(), [("start", "buscando música", None, None),
                                        ("start", "Bohemian Rhapsody", None, None)])
        self.assertEqual(self.sent[0]["id"], self.sent[1]["id"])
        self.assertEqual(self.sent[0]["region"], "musica")

    def test_no_playable_source_ends_in_error(self):
        self.play(fails=True)
        self.assertEqual(self.steps()[-1], ("end", None, False, "sin fuente"))

    def test_stop_pause_and_resume(self):
        self.play()
        client = music_server.app.test_client()
        with patch.object(music_server, "mpv_command"):
            client.post("/pause", headers=self.auth)
            client.post("/resume", headers=self.auth)
        music_server.stop_current()
        self.assertEqual(self.steps()[2:], [("end", None, True, "pausa"),
                                            ("start", "Canción", None, None),
                                            ("end", None, True, "")])
        self.assertNotEqual(self.sent[0]["id"], self.sent[3]["id"])

    def test_song_that_ends_by_itself_turns_the_region_off(self):
        process = self.play()
        process.poll.return_value = 0
        music_server.brain_watch()
        self.assertEqual(self.steps()[-1], ("end", None, True, "terminó"))

    def test_long_song_is_renewed_with_the_same_id(self):
        self.play()
        music_server._brain["renewed"] -= music_server.BRAIN_RENEW_SECONDS
        music_server.brain_watch()
        self.assertEqual(self.steps()[-1], ("start", "Canción", None, None))
        self.assertEqual(self.sent[-1]["id"], self.sent[0]["id"])

    def test_watch_does_not_end_while_still_searching(self):
        music_server.brain_music("buscando música", playing=False)
        music_server.brain_watch()
        self.assertEqual(len(self.sent), 1)

    def test_watch_failure_does_not_kill_the_sender(self):
        with patch.object(music_server, "brain_watch", side_effect=RuntimeError("x")), \
                patch.object(music_server, "_brain_post") as post, \
                patch.object(music_server, "BRAIN_WATCH_SECONDS", 0.01):
            music_server._brain_queue.put({"phase": "pulse"})
            worker = music_server.threading.Thread(target=music_server._brain_run,
                                                   daemon=True)
            worker.start()
            music_server.time.sleep(0.1)
            music_server._brain_queue.put({"phase": "end"})
            music_server.time.sleep(0.1)
        self.assertTrue(worker.is_alive())
        self.assertEqual([c.args[0]["phase"] for c in post.call_args_list], ["pulse", "end"])

    def test_disabled_without_url(self):
        with patch.object(music_server, "BRAIN_URL", ""):
            self.play()
            music_server.stop_current()
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
