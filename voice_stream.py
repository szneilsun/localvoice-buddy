import http.client
import json
import re
import socket
import threading
from urllib.parse import urlsplit

from voice_smoke import chat_messages, chat_payload


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


def deltas(lines, *, allow_finished_eof=False):
    data = []
    finished = False
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
                raise RuntimeError("The chat service reported a streaming error.")
            choices = chunk.get("choices", [])
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason") in ("stop", "length", "content_filter", "tool_calls", "function_call"):
                finished = True
            content = choice.get("delta", {}).get("content")
            if isinstance(content, str) and content:
                yield content
    if allow_finished_eof and finished and not data:
        return
    raise EOFError("The chat service closed the stream before [DONE].")


class CompletionHTTPError(RuntimeError):
    def __init__(self, status, content_type):
        super().__init__(f"Chat service returned HTTP {status}.")
        self.status = status
        self.content_type = content_type


class CompletionTimeout(TimeoutError):
    def __init__(self, phase):
        super().__init__(f"Chat service timed out during {phase}.")
        self.phase = phase


class CompletionStream:
    def __init__(self):
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

    def request(self, connection, path, payload, check, *, api_key=None):
        check()
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["User-Agent"] = "LocalVoice/1.0"
        phase = "connect"
        try:
            with self.lock:
                if self.stopped:
                    return
            connection.connect()
            with self.lock:
                if self.stopped:
                    return
                # Keep the socket even if HTTPConnection detaches it for a closing response.
                self.socket = connection.sock
                if api_key is not None:
                    self.socket.settimeout(60)
            phase = "response"
            connection.request("POST", path, body=json.dumps(dict(payload, stream=True)).encode("utf-8"),
                               headers=headers)
            with connection.getresponse() as response:
                content_type = response.headers.get_content_type()
                if response.status != 200:
                    raise CompletionHTTPError(response.status, content_type)
                if api_key is not None and content_type != "text/event-stream":
                    raise ValueError("The remote service did not return an SSE stream.")
                phase = "stream"
                yield from deltas(response, allow_finished_eof=api_key is not None)
        except TimeoutError as error:
            if api_key is None:
                raise
            raise CompletionTimeout(phase) from error
        finally:
            with self.lock:
                self.socket = None
            connection.close()


class LlamaStream(CompletionStream):
    def __init__(self, port=8001):
        super().__init__()
        self.port = port

    def generate(self, text, history, check):
        payload = chat_payload(text, self.port, history, check=check)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        yield from self.request(connection, "/v1/chat/completions", payload, check)


class OpenAIStream(CompletionStream):
    def __init__(self, config):
        super().__init__()
        self.config = config

    def generate(self, text, history, check):
        url = urlsplit(self.config.endpoint)
        payload = {"model": self.config.model, "messages": chat_messages(text, history)}
        connection = http.client.HTTPSConnection(url.hostname, url.port, timeout=8)
        yield from self.request(connection, url.path, payload, check, api_key=self.config.api_key)
