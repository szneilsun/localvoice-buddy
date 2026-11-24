import http.client
import json
import re
import socket
import threading

from voice_smoke import chat_payload


SENTENCE_END = re.compile(r"[。！？!?；;\n]+[”’」』）\"]?|\.(?=\s)")
CLAUSE_END = re.compile(r"[，、：,:]\s*|\s+")
NUMBERED_LINE = re.compile(r"\d{1,2}")


class Sentences:
    def __init__(self):
        self.pending = ""

    def feed(self, text):
        self.pending += text
        while self.pending:
            end = 0
            for match in SENTENCE_END.finditer(self.pending):
                prefix = self.pending[:match.start()].strip()
                if any(char.isalnum() for char in prefix) and not (
                    match.group() == "." and NUMBERED_LINE.fullmatch(prefix)
                ):
                    end = match.end()
                    break
            if not end and len(self.pending) > 72:
                end = max((match.end() for match in CLAUSE_END.finditer(self.pending[:72])
                           if match.start() >= 26), default=72)
            if not end:
                return
            sentence, self.pending = self.pending[:end].strip(), self.pending[end:].lstrip()
            if any(char.isalnum() for char in sentence):
                yield sentence

    def finish(self):
        tail, self.pending = self.pending.strip(), ""
        if any(char.isalnum() for char in tail):
            yield tail


def deltas(lines):
    data = []
    for line in lines:
        line = line.rstrip(b"\r\n")
        if line.startswith(b"data:"):
            data.append(line[5:].lstrip(b" "))
        elif not line and data:
            event = b"\n".join(data)
            data.clear()
            if event == b"[DONE]":
                return
            chunk = json.loads(event)
            if chunk.get("error"):
                raise RuntimeError(f"llama.cpp stream failed: {chunk['error']}")
            choices = chunk.get("choices", [])
            content = choices[0].get("delta", {}).get("content") if choices else None
            if isinstance(content, str) and content:
                yield content
    raise EOFError("llama.cpp closed the stream before [DONE].")


class LlamaStream:
    def __init__(self, port=8001):
        self.port = port
        self.lock = threading.Lock()
        self.socket = None
        self.stopped = False

    def stop(self):
        with self.lock:
            self.stopped = True
            if self.socket is not None:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def generate(self, text, history, check):
        payload = chat_payload(text, self.port, history, check=check)
        check()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            connection.connect()
            with self.lock:
                if self.stopped:
                    return
                # Keep the socket even if HTTPConnection detaches it for a closing response.
                self.socket = connection.sock
            connection.request("POST", "/v1/chat/completions",
                               body=json.dumps(dict(payload, stream=True)).encode("utf-8"),
                               headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise RuntimeError(f"llama.cpp returned HTTP {response.status}.")
                yield from deltas(response)
        finally:
            with self.lock:
                self.socket = None
            connection.close()
