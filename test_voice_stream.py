import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from voice_stream import LlamaStream, Sentences, deltas


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


if __name__ == "__main__":
    unittest.main()
