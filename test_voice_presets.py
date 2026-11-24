import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

import voice_web
from voice_smoke import LOCAL_HTTP


class VoicePresetTest(unittest.TestCase):
    def test_preview_streams_without_llama_or_changes_to_the_conversation(self):
        graph = Mock()
        graph.pcm.side_effect = lambda text, speaker: iter_pcm()

        def iter_pcm():
            yield b"\x01\x00" * 640

        room = "a" * 32
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(voice_web, "CHECKS", Path(temp)),
            patch.object(voice_web, "llama_ready", return_value=False) as llama,
            voice_web.VoiceServer(("127.0.0.1", 0), graph=graph, asr_device="cpu") as server,
        ):
            host = f"http://127.0.0.1:{server.server_port}"
            thread = threading.Thread(target=server.serve_forever)
            thread.start()

            def request(path, method="GET", key=room, speaker="Serena", status=200):
                req = Request(host + path, method=method, headers={
                    "Origin": host, "X-Conversation-Id": key, "X-Voice-Speaker": speaker,
                })
                try:
                    response = LOCAL_HTTP.open(req, timeout=3)
                except HTTPError as error:
                    response = error
                with response:
                    self.assertEqual(response.status, status)
                    return response.read()

            try:
                request("/api/voice-preview", "POST", speaker="not-a-voice", status=400)
                preview = json.loads(request("/api/voice-preview", "POST", status=202))
                self.assertNotEqual(preview["room"], room)
                self.assertTrue(preview["pcm"])
                route = f"/api/turn/{preview['id']}"
                request(route + "/stream.pcm", status=404)
                pcm = request(route + "/stream.pcm", key=preview["room"])
                self.assertEqual(pcm, b"\x01\x00" * 640)
                result = json.loads(request(route, key=preview["room"]))
                self.assertEqual(result["speaker"], "Serena")
                self.assertEqual(result["phase"], "done")
                self.assertEqual(set(result["timings"]), {"tts"})
                graph.pcm.assert_called_once_with(voice_web.PREVIEW_TEXT, "Serena")
                llama.assert_not_called()
                self.assertEqual(json.loads(request("/api/conversation")), {"turns": [], "latest": None})
                request(route + "/cancel", "POST", key=preview["room"])
                request(f"{route}/reply.wav?conversation={preview['room']}", status=404)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
