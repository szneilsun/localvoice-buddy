import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

from voice_stream import CompletionStream, CompletionTimeout, LlamaStream, Sentences, deltas


class StreamTest(unittest.TestCase):
    def test_sentences_are_emitted_incrementally_without_splitting_a_decimal(self):
        buffer = Sentences()
        self.assertEqual(list(buffer.feed("数值是3.")), [])
        self.assertEqual(list(buffer.feed("14。下一句")), ["数值是3.14。"])
        self.assertEqual(list(buffer.feed("完成了！尾句没有句号")), ["下一句完成了！"])
        self.assertEqual(list(buffer.finish()), ["尾句没有句号"])

    def test_sse_parses_events_and_rejects_truncated_completion(self):
        lines = [
            b": comment\r\n", b"\r\n",
            b'data: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}\r\n', b"\r\n",
            b'data: {"choices":\r\n', b'data: [{"delta":{"content":"hi"}}]}\r\n', b"\r\n",
        ]
        self.assertEqual(list(deltas(lines + [b"data: [DONE]\n", b"\n"])), ["hi"])
        with self.assertRaises(EOFError):
            list(deltas(lines))

    def test_remote_eof_requires_an_explicit_finish_reason(self):
        lines = [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n', b"\n"]
        finished = lines + [b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n', b"\n"]
        self.assertEqual(list(deltas(finished, allow_finished_eof=True)), ["hi"])
        with self.assertRaises(EOFError):
            list(deltas(lines, allow_finished_eof=True))
        with self.assertRaises(EOFError):
            list(deltas(finished))

    def test_cancellation_closes_a_stalled_http_response(self):
        heard_text = threading.Event()
        peer_closed = threading.Event()
        request = []
        failures = []

        class FakeLlama(BaseHTTPRequestHandler):
            def do_POST(self):
                request.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"First sentence!"}}]}\n\n')
                self.wfile.flush()
                self.connection.settimeout(4)
                try:
                    if self.connection.recv(1) == b"":
                        peer_closed.set()
                except ConnectionResetError:
                    peer_closed.set()

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), FakeLlama) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            stream = LlamaStream(server.server_port)

            def read():
                try:
                    for text in stream.generate("Hello", (), lambda: None):
                        if text == "First sentence!":
                            heard_text.set()
                except Exception as error:
                    failures.append(error)

            reader = threading.Thread(target=read)
            reader.start()
            try:
                self.assertTrue(heard_text.wait(2))
                self.assertIs(request[0]["stream"], True)
                stream.stop()
                self.assertTrue(peer_closed.wait(2))
                reader.join(2)
                self.assertFalse(reader.is_alive())
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(failures[0], (EOFError, OSError, ValueError))
            finally:
                stream.stop()
                reader.join(5)
                server.shutdown()
                worker.join()

    def test_remote_timeout_identifies_the_phase(self):
        for phase in ("connect", "response", "stream"):
            with self.subTest(phase=phase):
                conn = MagicMock()
                response = conn.getresponse.return_value.__enter__.return_value
                response.status = 200
                response.headers.get_content_type.return_value = "text/event-stream"
                failing = {
                    "connect": conn.connect,
                    "response": conn.getresponse,
                    "stream": response.__iter__,
                }[phase]
                failing.side_effect = TimeoutError
                stream = CompletionStream()
                with self.assertRaises(CompletionTimeout) as raised:
                    list(stream.request(conn, "/v1/chat/completions", {}, lambda: None, api_key="test-key"))
                self.assertEqual(raised.exception.phase, phase)
                conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
