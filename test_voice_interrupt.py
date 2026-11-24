import json
import shutil
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

import voice_web
from voice_smoke import LOCAL_HTTP


class InterruptTest(unittest.TestCase):
    def test_cpu_turn_releases_qwen_and_keeps_asr_loaded(self):
        whisper = Mock()
        whisper.call.return_value = {"text": "test"}
        qwen = Mock(running=True)
        with tempfile.TemporaryDirectory() as temp, (
            voice_web.VoiceServer(("127.0.0.1", 0), asr=whisper, tts=qwen, fast_tts=Mock())
        ) as server:
            transcript = Path(temp) / "transcript.txt"
            audio = Path(temp) / "input.wav"
            turn = voice_web.VoiceTurn({}, {"tts_mode": "fast"}, audio)
            server.recognize(audio, transcript, turn)
            qwen.close.assert_called_once()
            whisper.call.assert_called_once_with(audio=str(audio), reserve_mib=0)
            self.assertEqual(transcript.read_text(encoding="utf-8").strip(), "test")

    def test_cancel_stream_and_replace_without_overlapping_inference(self):
        more_text = threading.Event()
        started = threading.Event()
        release = threading.Event()
        histories = []
        room, other = "a" * 32, "b" * 32

        class Synthesizer:
            running = True
            calls = 0

            def call(self, *, text, output, speaker=None):
                self.calls += 1
                if speaker is None and self.calls == 2:
                    started.set()
                    if not release.wait(5):
                        raise TimeoutError("Test did not release synthesis")
                shutil.copyfile(sample, output)

        def fake_asr(_server, audio, transcript, turn):
            transcript.write_text("test question", encoding="utf-8")

        def fake_generate(_stream, transcript, history, check):
            histories.append(history)
            if len(histories) == 1:
                yield "The first "
                yield "sentence!"
                if not more_text.wait(5):
                    raise TimeoutError("Test did not release the text stream")
                check()
                yield " The second sentence."
            else:
                yield "New reply! "
                yield "More detail is necessary to explain how the second part plays! "
                yield "The final sentence needs to be long enough to remain separate."

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(voice_web, "CHECKS", Path(temp)),
            patch.object(voice_web.VoiceServer, "recognize", fake_asr),
            patch.object(voice_web.LlamaStream, "generate", fake_generate),
            patch.object(voice_web, "llama_ready", return_value=True),
            patch.object(voice_web, "gpu_free_mib", return_value=None),
            voice_web.VoiceServer(("127.0.0.1", 0), tts=Synthesizer(), fast_tts=Synthesizer()) as server,
        ):
            sample = Path(temp) / "shanghai-question.wav"
            with wave.open(str(sample), "wb") as target:
                target.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                target.writeframes(b"\0\0" * 2400)
            host = f"http://127.0.0.1:{server.server_port}"
            thread = threading.Thread(target=server.serve_forever)
            thread.start()

            def request(path, method="GET", conversation=room, expect=200, mode=None):
                headers = {"Origin": host, "X-Conversation-Id": conversation}
                if mode:
                    headers["X-Voice-TTS"] = mode
                req = Request(host + path, method=method, headers=headers)
                try:
                    response = LOCAL_HTTP.open(req, timeout=3)
                except HTTPError as error:
                    response = error
                with response:
                    self.assertEqual(response.code, expect, path)
                    return response.read()

            def submit(mode=None):
                token = json.loads(request("/api/sample", "POST", expect=202, mode=mode))["id"]
                return f"/api/turn/{token}"

            def wait_for(path, phase, parts=None):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    job = json.loads(request(path))
                    if job["phase"] == phase and (parts is None or job["parts"] == parts):
                        return job
                    time.sleep(0.01)
                self.fail(f"{path} never reached {phase}")

            try:
                self.assertEqual(json.loads(request("/api/status"))["tts_modes"], ["fast", "quality"])
                request("/api/sample", "POST", expect=400, mode="unknown")
                first = submit()
                partial = wait_for(first, "chat", parts=1)
                self.assertEqual(partial["tts_mode"], "fast")
                self.assertEqual(partial["answer"], "The first sentence!")
                self.assertIn("first_text", partial)
                self.assertIn("first_tts", partial)
                self.assertNotIn("chat", partial["timings"])
                request(f"{first}/part-0.wav?conversation={room}")
                more_text.set()
                self.assertTrue(started.wait(3))
                request(f"{first}/cancel", "POST", conversation=other, expect=404)
                request(f"{first}/cancel", "POST")
                request(f"{first}/cancel", "POST")
                wait_for(first, "cancelled")
                request(f"{first}/part-0.wav?conversation={room}", expect=404)

                skipped = submit()
                wait_for(skipped, "queued")
                request(f"{skipped}/cancel", "POST")
                wait_for(skipped, "cancelled")
                replacement = submit("quality")
                wait_for(replacement, "queued")
                self.assertEqual(histories, [[]])
                release.set()
                done = wait_for(replacement, "done", parts=3)
                self.assertEqual(histories, [[], []])
                self.assertEqual(done["tts_mode"], "quality")
                self.assertEqual(server.fast_tts.calls, 2)
                self.assertEqual(server.tts.calls, 3)
                self.assertEqual(set(done["timings"]), {"asr", "chat", "tts"})
                request(f"{replacement}/part-2.wav?conversation={room}")
                request(f"{replacement}/part-02.wav?conversation={room}", expect=404)
                request(f"{replacement}/reply.wav?conversation={room}")
                request(f"{replacement}/cancel", "POST")
                request(f"{replacement}/reply.wav?conversation={room}", expect=404)
                self.assertEqual(server.get_room(room)["turns"], [])
            finally:
                more_text.set()
                release.set()
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
