import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
import wave
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import voice_web
from voice_chat_settings import ChatSettings, completion_url
from voice_smoke import LOCAL_HTTP


MODEL = {"provider": "remote", "url": "https://chat.example.test/api/v1",
         "model": "sample-chat", "api_key": "test-key-not-for-logs"}


@contextmanager
def serving(server):
    with server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


class ChatSettingsTest(unittest.TestCase):
    def test_api_urls_and_key_reuse(self):
        self.assertEqual(completion_url("https://api.openai.com"),
                         "https://api.openai.com/v1/chat/completions")
        endpoint = "https://proxy.example.test:8443/openai/v1/chat/completions"
        self.assertEqual(completion_url(endpoint + "/"), endpoint)
        for value in ("http://chat.example.test", "https://user:key@chat.example.test",
                      "https://chat.example.test/?key=secret", "https://chat.example.test/#path",
                      "https://chat.example.test:99999", "https://chat.example.test/\r\nbad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                completion_url(value)
        settings = ChatSettings().update(MODEL)
        self.assertEqual(settings.public()["url"], MODEL["url"])
        self.assertEqual(settings.remote.endpoint, MODEL["url"] + "/chat/completions")
        self.assertEqual(settings.update(dict(MODEL, api_key="", model="other")).remote.api_key,
                         MODEL["api_key"])
        self.assertEqual(settings.update(dict(MODEL, api_key="", url=settings.remote.endpoint)), settings)
        root_endpoint = "https://proxy.example.test/chat/completions"
        root_settings = ChatSettings().update(dict(MODEL, url=root_endpoint))
        self.assertEqual(root_settings.remote.endpoint, root_endpoint)
        with self.assertRaises(ValueError):
            settings.update(dict(MODEL, api_key="", url=endpoint))

    def test_persistence_switch_and_clear(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runtime" / "chat-settings.json"
            self.assertEqual(ChatSettings.load(path).provider, "local")
            remote = ChatSettings().update(MODEL)
            remote.save(path)
            self.assertEqual(ChatSettings.load(path), remote)
            path.write_text(json.dumps(dict(MODEL, url=remote.remote.endpoint)), encoding="utf-8")
            self.assertEqual(ChatSettings.load(path), remote)
            local = remote.update({"provider": "local"})
            local.save(path)
            self.assertEqual(ChatSettings.load(path), local)
            self.assertTrue(local.public()["has_api_key"])
            local.update({"provider": "local", "clear_remote": True}).save(path)
            self.assertEqual(ChatSettings.load(path), ChatSettings())
            self.assertNotIn(MODEL["api_key"], path.read_text(encoding="utf-8"))

    def test_remote_turn_streams_to_local_tts_and_reports_provider_errors(self):
        requests = []
        finish_stream = threading.Event()

        class FakeProvider(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, self.headers.get("Authorization"),
                                 self.headers.get("User-Agent"), body))
                if len(requests) == 3:
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(MODEL["api_key"].encode())
                    return
                if len(requests) == 4:
                    self.send_response(403)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><title>Forbidden</title></html>")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"Test reply!"}}]}\n\n')
                self.wfile.flush()
                if finish_stream.wait(3):
                    if len(requests) == 1:
                        self.wfile.write(b"data: [DONE]\n\n")
                    else:
                        self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')

            def log_message(self, *args):
                pass

        def fake_recognize(_server, audio, transcript, turn):
            transcript.write_text("Test transcript", encoding="utf-8")

        def fake_speak(_server, text, part, turn):
            self.assertEqual(text, "Test reply!")
            shutil.copyfile(turn.audio, part)

        with (
            tempfile.TemporaryDirectory() as temp,
            serving(ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)) as remote,
            patch.object(voice_web, "CHECKS", Path(temp)),
            patch.object(voice_web.VoiceServer, "recognize", fake_recognize),
            patch.object(voice_web.VoiceServer, "speak", fake_speak),
            patch.object(voice_web, "llama_ready", return_value=False) as llama,
            patch("voice_stream.http.client.HTTPSConnection", side_effect=lambda *args, **kwargs:
                  http.client.HTTPConnection("127.0.0.1", remote.server_port, timeout=4)) as tls,
            serving(voice_web.VoiceServer(("127.0.0.1", 0),
                    chat_settings_path=Path(temp) / "chat-settings.json")) as server,
        ):
            origin = f"http://127.0.0.1:{server.server_port}"

            def request(path, method="GET", body=None, status=200, provider="local", same_origin=True):
                request = Request(origin + path, method=method,
                                  data=json.dumps(body).encode() if body is not None else None, headers={
                    "Origin": origin if same_origin else "https://other.example.test",
                    "Content-Type": "application/json",
                    "X-Conversation-Id": "a" * 32,
                    "X-Voice-Chat-Provider": provider,
                })
                try:
                    response = LOCAL_HTTP.open(request, timeout=3)
                except HTTPError as error:
                    response = error
                with response:
                    self.assertEqual(response.status, status, path)
                    return json.load(response)

            def submit():
                return "/api/turn/" + request("/api/sample", "POST", provider="remote", status=202)["id"]

            def wait_for(path, condition):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    job = request(path)
                    if condition(job):
                        return job
                    time.sleep(0.01)
                self.fail(f"Turn did not reach expected state: {job}")

            try:
                request("/api/chat-settings", "POST", MODEL, status=403, same_origin=False)
                self.assertEqual(request("/api/chat-settings")["provider"], "local")
                request("/api/sample", "POST", status=503)
                llama.reset_mock()
                saved = request("/api/chat-settings", "POST", MODEL)
                self.assertTrue(saved["has_api_key"])
                self.assertNotIn(MODEL["api_key"], json.dumps(saved))
                self.assertEqual(request("/api/chat-settings"), saved)
                self.assertEqual(request("/api/status")["chat_provider"], "remote")
                self.assertFalse(requests)
                request("/api/sample", "POST", status=409)
                missing = request("/api/sample", "POST", provider="remote", status=503)
                self.assertIn("未找到本地样音", missing["error"])
                self.assertFalse(list(Path(temp).glob("web-*")))
                with wave.open(str(server.sample_audio), "wb") as target:
                    target.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    target.writeframes(b"\0\0" * 8000)

                turn = submit()
                job = wait_for(turn, lambda job: job["parts"] == 1)
                self.assertEqual(job["phase"], "chat")
                finish_stream.set()
                job = wait_for(turn, lambda job: job["phase"] == "done")
                self.assertEqual(job["answer"], "Test reply!")
                wait_for(submit(), lambda job: job["phase"] == "done")

                path, authorization, user_agent, payload = requests[1]
                self.assertEqual(path, "/api/v1/chat/completions")
                self.assertEqual(authorization, "Bearer " + MODEL["api_key"])
                self.assertEqual(user_agent, "LocalVoice/1.0")
                self.assertEqual(payload["model"], MODEL["model"])
                self.assertIs(payload["stream"], True)
                self.assertEqual(payload["messages"][1:], [
                    {"role": "user", "content": "Test transcript"},
                    {"role": "assistant", "content": "Test reply!"},
                    {"role": "user", "content": "Test transcript"},
                ])
                tls.assert_called_with("chat.example.test", None, timeout=8)
                failed = wait_for(submit(), lambda job: job["phase"] == "error")
                self.assertIn("鉴权失败", failed["error"])
                self.assertNotIn(MODEL["api_key"], json.dumps(failed))
                denied = wait_for(submit(), lambda job: job["phase"] == "error")
                self.assertIn("网关拒绝访问（HTTP 403）", denied["error"])
                self.assertNotIn("API Key", denied["error"])
                llama.assert_not_called()
                request("/api/chat-settings", "POST", {"provider": "local"})
                request("/api/sample", "POST", status=503)
                self.assertEqual(len(requests), 4)
            finally:
                finish_stream.set()


if __name__ == "__main__":
    unittest.main()
