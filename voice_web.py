import argparse
import contextlib
import copy
import io
import json
import queue
import re
import shutil
import socket
import threading
import time
import traceback
import uuid
import wave
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fast_tts import available as fast_tts_available
from run_voice_turn import ROOT, run_step
from voice_smoke import LOCAL_HTTP
from voice_stream import LlamaStream, Sentences
from warm_models import DEFAULT_SPEAKER, ModelMemoryError, WarmModel, gpu_free_mib


WEB = ROOT / "web"
CHECKS = ROOT / "checks"
ROOM_LIMIT = 16
TURN_LIMIT = 12
ID_PATTERN = re.compile(r"[0-9a-f]{32}")
MAX_UPLOAD = 2 * 1024 * 1024
PCM_RATE = 24000
WAV_HEADER_BYTES = 44  # The wave writer emits a standard PCM RIFF header.
VOICE_PRESETS = {
    "Vivian": "Vivian · 普通话女声",
    "Serena": "Serena · 普通话女声",
    "Uncle_Fu": "Uncle Fu · 普通话男声",
    "Dylan": "Dylan · 北京口音",
    "Eric": "Eric · 四川口音",
}
PREVIEW_TEXT = "你好，很高兴认识你。今天想聊些什么？"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/voice-input.mjs": ("voice-input.mjs", "text/javascript; charset=utf-8"),
    "/pcm-output.js": ("pcm-output.js", "text/javascript; charset=utf-8"),
}
for filename, mime in {
    "ort.wasm.min.mjs": "text/javascript",
    "ort-wasm-simd-threaded.mjs": "text/javascript",
    "ort-wasm-simd-threaded.wasm": "application/wasm",
    "silero_vad_v5.onnx": "application/octet-stream",
    "vad.worklet.bundle.min.js": "text/javascript",
}.items():
    STATIC[f"/vad/{filename}"] = (f"vad/{filename}", mime)
STAGE_ERRORS = {
    "asr": "语音识别失败。请检查音频内容，或用样音测试。",
    "chat": "本地问答失败。请确认 llama.cpp 仍在运行。",
    "tts": "语音合成失败。请检查本地 TTS 环境或显存。",
}


def llama_ready():
    try:
        with LOCAL_HTTP.open("http://127.0.0.1:8001/health", timeout=2) as response:
            return json.load(response)["status"] == "ok"
    except (OSError, ValueError, KeyError):
        return False


def validate_clip(data):
    with wave.open(io.BytesIO(data), "rb") as audio:
        channels, width, rate, count, *_ = audio.getparams()
        if channels != 1 or width != 2 or not 8000 <= rate <= 48000:
            raise ValueError("需要 8–48 kHz 的单声道 PCM 音频。")
        if not 0.3 <= count / rate <= 15.1 or len(audio.readframes(count)) != count * width:
            raise ValueError("音频须完整，时长为 0.3 至 15 秒。")


def join_audio(parts, output):
    if len(parts) == 1:
        shutil.copyfile(parts[0], output)
        return
    with wave.open(str(output), "wb") as merged:
        audio_format = None
        for part in parts:
            with wave.open(str(part), "rb") as source:
                current = source.getparams()[:3]
                if audio_format is None:
                    audio_format = current
                    merged.setnchannels(current[0])
                    merged.setsampwidth(current[1])
                    merged.setframerate(current[2])
                if current != audio_format:
                    raise ValueError("Speech segments have different audio formats.")
                merged.writeframesraw(source.readframes(source.getnframes()))


class TurnCancelled(Exception):
    pass


class ChatFailed(RuntimeError):
    pass


@dataclass(slots=True)
class VoiceTurn:
    room: dict
    job: dict
    audio: Path
    started: float = field(default_factory=time.monotonic)
    cancelled: threading.Event = field(default_factory=threading.Event)
    stream: LlamaStream = field(default_factory=LlamaStream)

    def check(self):
        if self.cancelled.is_set():
            raise TurnCancelled

    def cancel(self):
        self.cancelled.set()
        self.stream.stop()


class VoiceServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, asr=None, tts=None, fast_tts=None, graph=None, asr_device="cuda"):
        super().__init__(address, Handler)
        self.lock = threading.Lock()
        self.audio_changed = threading.Condition(self.lock)
        self.rooms = OrderedDict()
        self.previews = OrderedDict()
        self.running = None
        self.waiting = None
        self.asr = asr
        self.tts = tts
        self.fast_tts = fast_tts
        self.graph = graph
        self.asr_device = asr_device
        self.graph_warming = False
        self.tts_modes = ("fast", "quality") if fast_tts else ("quality",)

    def warm_fast_tts(self):
        try:
            self.fast_tts.call(warmup=True)
        except Exception as error:
            print(f"Piper prewarm failed: {error}", flush=True)

    def warm_asr(self):
        try:
            self.asr.call(audio=str(CHECKS / "shanghai-question.wav"), reserve_mib=0)
            print("CPU ASR ready.", flush=True)
        except Exception as error:
            print(f"CPU ASR prewarm failed: {error}", flush=True)

    def warm_quality(self):
        with self.lock:
            if not self.graph or self.graph_warming or self.running:
                return
            self.graph_warming = True

        def warm():
            try:
                if self.tts:
                    self.tts.close()
                self.graph.call(warmup=True)
                print("Qwen 8-frame PCM ready.", flush=True)
            except Exception as error:
                print(f"Qwen graph prewarm failed: {error}", flush=True)
            finally:
                with self.lock:
                    self.graph_warming = False

        threading.Thread(target=warm, daemon=True).start()

    def recognize(self, audio, transcript, turn):
        turn.check()
        if self.asr_device == "cpu" and self.asr:
            result = self.asr.call(audio=str(audio), reserve_mib=0)
            transcript.write_text(result["text"] + "\n", encoding="utf-8")
            return
        if turn.job["tts_mode"] == "fast":
            # CPU speech synthesis does not need a GPU reservation or a resident Qwen worker.
            if self.tts and self.tts.running:
                self.tts.close()
            if self.asr:
                turn.check()
                result = self.asr.call(audio=str(audio), reserve_mib=0)
                transcript.write_text(result["text"] + "\n", encoding="utf-8")
                return
        if self.asr is None:
            turn.check()
            run_step(ROOT / ".venv-asr/Scripts/python.exe", "asr",
                     "--input", audio, "--output", transcript, "--device", self.asr_device)
            return
        if self.tts and self.tts.running:
            free = gpu_free_mib()
            if free is not None and free < 900:
                self.tts.close()
        reserve = 768 if self.tts and self.tts.running else 3072
        try:
            turn.check()
            result = self.asr.call(audio=str(audio), reserve_mib=reserve)
        except ModelMemoryError:
            if not self.tts or not self.tts.running:
                raise
            self.tts.close()
            turn.check()
            result = self.asr.call(audio=str(audio), reserve_mib=3072)
        transcript.write_text(result["text"] + "\n", encoding="utf-8")

    def speak(self, text, part, turn):
        turn.check()
        if turn.job["tts_mode"] == "fast":
            self.fast_tts.call(text=text, output=str(part))
            return
        speaker = turn.job["speaker"]
        if self.tts is None:
            run_step(ROOT / ".venv-tts/Scripts/python.exe", "tts",
                     f"--text={text}", "--output", part, "--device", "cuda", "--speaker", speaker)
            return
        options = {"text": text, "speaker": speaker, "output": str(part)}
        try:
            self.tts.call(**options)
        except ModelMemoryError:
            turn.check()
            if self.asr_device != "cuda" or not self.asr or not self.asr.running:
                raise
            self.asr.close()
            turn.check()
            self.tts.call(**options)

    def speak_pcm(self, text, part, turn, publish):
        if turn.job["tts_backend"] == "qwen-graph":
            try:
                if not turn.job["pcm_bytes"] and self.tts:
                    self.tts.close()
                with contextlib.closing(self.graph.pcm(text, turn.job["speaker"])) as chunks:
                    for chunk in chunks:
                        turn.check()
                        publish(chunk)
                return
            except (OSError, RuntimeError, ValueError) as error:
                # Never repeat a sentence that may already have reached the speaker.
                turn.check()
                if turn.job["pcm_bytes"]:
                    raise
                self.graph.close()
                print(f"Qwen graph failed; using original Qwen for this turn: {error}", flush=True)
                with self.lock:
                    turn.job["tts_backend"] = "qwen-stock"
        self.speak(text, part, turn)
        with wave.open(str(part), "rb") as audio:
            if audio.getparams()[:3] != (1, 2, PCM_RATE):
                raise ValueError("Unexpected fallback PCM format.")
            publish(audio.readframes(audio.getnframes()))

    def stream_reply(self, turn, transcript, history, chat_started):
        with contextlib.ExitStack() as stack:
            publish = None
            if turn.job.get("pcm"):
                file = stack.enter_context((turn.audio.parent / "reply.wav").open("wb"))
                wav = stack.enter_context(wave.open(file, "wb"))
                wav.setparams((1, 2, PCM_RATE, 0, "NONE", "not compressed"))

                def publish(chunk):
                    turn.check()
                    wav.writeframesraw(chunk)
                    file.flush()
                    with self.audio_changed:
                        turn.check()
                        if not turn.job["pcm_bytes"]:
                            turn.job["first_audio"] = round(time.monotonic() - turn.started, 3)
                        turn.job["pcm_bytes"] += len(chunk)
                        self.audio_changed.notify_all()

            if turn.job.get("preview"):
                started = time.monotonic()
                self.speak_pcm(PREVIEW_TEXT, turn.audio.parent / "part-0.wav", turn, publish)
                if not turn.job["pcm_bytes"]:
                    raise ValueError("The voice preview returned no audio.")
                return time.monotonic() - started
            return self.render_reply(turn, transcript, history, chat_started, publish)

    def render_reply(self, turn, transcript, history, chat_started, publish):
        job = turn.job
        run = turn.audio.parent
        sentences = queue.SimpleQueue()
        generation_error = None

        def generate():
            nonlocal generation_error
            answer = ""
            buffer = Sentences()
            try:
                for delta in turn.stream.generate(transcript, history, turn.check):
                    with self.lock:
                        turn.check()
                        answer += delta
                        job["answer"] = answer
                        if answer.strip() and "first_text" not in job:
                            job["first_text"] = round(time.monotonic() - turn.started, 2)
                    for sentence in buffer.feed(delta):
                        sentences.put(sentence)
                turn.check()
                if not answer.strip():
                    raise ValueError("The model returned no spoken answer.")
                (run / "answer.txt").write_text(answer.strip() + "\n", encoding="utf-8")
                with self.lock:
                    turn.check()
                    job["answer"] = answer.strip()
                    job["timings"]["chat"] = round(time.monotonic() - chat_started, 2)
                    job["phase"] = "tts"
                for sentence in buffer.finish():
                    sentences.put(sentence)
            except Exception as error:
                generation_error = error
            finally:
                sentences.put(None)

        producer = threading.Thread(target=generate, daemon=True)
        producer.start()
        parts = []
        tts_seconds = 0
        empty = object()
        pending = empty
        try:
            while True:
                sentence = sentences.get() if pending is empty else pending
                pending = empty
                turn.check()
                if generation_error is not None:
                    raise ChatFailed("Live answer generation failed.") from generation_error
                if sentence is None:
                    break
                # Never delay the first sentence; combine later sentences only if already ready.
                while parts and len(sentence) < 72:
                    try:
                        pending = sentences.get_nowait()
                    except queue.Empty:
                        break
                    if pending is None or len(sentence) + len(pending) + 1 > 72:
                        break
                    sentence = f"{sentence} {pending}"
                    pending = empty
                started = time.monotonic()
                if (not parts and job["tts_mode"] == "quality" and self.asr_device == "cuda"
                        and self.asr and self.asr.running
                        and not (self.tts and self.tts.running)):
                    free = gpu_free_mib()
                    if free is not None and free < 2850:
                        self.asr.close()
                if not parts:
                    with self.lock:
                        turn.check()
                        job["first_tts"] = round(time.monotonic() - turn.started, 2)
                part = run / f"part-{len(parts)}.wav"
                if publish:
                    self.speak_pcm(sentence, part, turn, publish)
                else:
                    self.speak(sentence, part, turn)
                tts_seconds += time.monotonic() - started
                parts.append(part)
                with self.lock:
                    turn.check()
                    if generation_error is not None:
                        raise ChatFailed("Live answer generation failed.") from generation_error
                    if not publish:
                        job["parts"] = len(parts)
                    if len(parts) == 1 and not publish:
                        job["first_audio"] = round(time.monotonic() - turn.started, 2)
            if not parts or (publish and not job["pcm_bytes"]):
                raise ChatFailed("The answer contained no speakable text.")
            if publish:
                return tts_seconds
            started = time.monotonic()
            join_audio(parts, run / "reply.wav")
            return tts_seconds + time.monotonic() - started
        finally:
            turn.stream.stop()
            producer.join()

    def create_turn(self, conversation, data=None, tts_mode=None, *, speaker=DEFAULT_SPEAKER, preview=False):
        with self.lock:
            queued = self.running is not None
            if queued:
                running_phase = self.running.job["phase"]
                if running_phase not in {"cancelled", "done", "error"} or self.waiting is not None:
                    return None
            token = uuid.uuid4().hex
            run = CHECKS / f"web-{token}"
            run.mkdir(parents=True)
            audio = run / "input.wav"
            if not preview:
                if data is None:
                    shutil.copyfile(CHECKS / "shanghai-question.wav", audio)
                else:
                    audio.write_bytes(data)
            rooms = self.previews if preview else self.rooms
            room = rooms.setdefault(conversation, {"turns": [], "latest": None})
            rooms.move_to_end(conversation)
            if len(rooms) > ROOM_LIMIT:
                rooms.popitem(last=False)
            phase = "queued" if queued else "tts" if preview else "asr"
            job = {"id": token, "phase": phase, "transcript": "", "answer": "",
                   "timings": {}, "parts": 0, "tts_mode": tts_mode or self.tts_modes[0]}
            if job["tts_mode"] == "quality":
                job["speaker"] = speaker
            if preview:
                job["preview"] = True
            if self.graph and job["tts_mode"] == "quality":
                job.update(pcm=True, pcm_bytes=0, tts_backend="qwen-graph")
            room["latest"] = job
            turn = VoiceTurn(room, job, audio)
            if queued:
                self.waiting = turn
            else:
                self.running = turn
        if not queued:
            self.launch(turn)
        return token

    def launch(self, turn):
        threading.Thread(target=self.process, args=(turn,), daemon=True).start()

    def process(self, turn):
        job = turn.job
        run = turn.audio.parent
        preview = job.get("preview")
        stage = "tts" if preview else "asr"
        try:
            transcript, history = "", []
            if not preview:
                with self.lock:
                    turn.check()
                    history = [
                        message for item in turn.room["turns"]
                        for message in (
                            {"role": "user", "content": item["transcript"]},
                            {"role": "assistant", "content": item["answer"]},
                        )
                    ]
                transcript_file = run / "transcript.txt"
                self.recognize(turn.audio, transcript_file, turn)
                transcript = transcript_file.read_text(encoding="utf-8").strip()
                chat_started = time.monotonic()
                with self.lock:
                    turn.check()
                    job.update(phase="chat", transcript=transcript)
                    job["timings"]["asr"] = round(chat_started - turn.started, 2)
                stage = "tts"
            else:
                turn.check()
                chat_started = time.monotonic()
            tts_seconds = self.stream_reply(turn, transcript, history, chat_started)
            with self.lock:
                turn.check()
                job["timings"]["tts"] = round(tts_seconds, 2)
                job.update(phase="done", seconds=round(time.monotonic() - turn.started, 1))
                if not preview:
                    completed = turn.room["turns"]
                    completed.append(job)
                    del completed[:-TURN_LIMIT]
        except TurnCancelled:
            pass
        except Exception as error:
            with self.lock:
                if not turn.cancelled.is_set():
                    failed_stage = "chat" if isinstance(error, ChatFailed) else stage
                    job.update(error=STAGE_ERRORS[failed_stage], phase="error", parts=0)
                    traceback.print_exc()
        finally:
            with self.audio_changed:
                next_turn = self.running = self.waiting
                self.waiting = None
                if next_turn:
                    next_turn.job["phase"] = "tts" if next_turn.job.get("preview") else "asr"
                    next_turn.started = time.monotonic()
                self.audio_changed.notify_all()
            if next_turn:
                self.launch(next_turn)

    def cancel_turn(self, conversation, token):
        with self.audio_changed:
            room = self.rooms.get(conversation) or self.previews.get(conversation)
            job = room["latest"] if room else None
            if not job or job["id"] != token or job["phase"] == "error":
                return False
            if self.running and self.running.job is job:
                self.running.cancel()
            if self.waiting and self.waiting.job is job:
                self.waiting.cancel()
                self.waiting = None
            room["turns"][:] = [item for item in room["turns"] if item is not job]
            job.update(phase="cancelled", parts=0, transcript="", answer="")
            job.pop("first_audio", None)
            job.pop("first_text", None)
            job.pop("first_tts", None)
            self.audio_changed.notify_all()
            return True

    def get_room(self, conversation):
        with self.lock:
            return copy.deepcopy(self.rooms.get(conversation, {"turns": [], "latest": None}))

    def get_turn(self, conversation, token):
        with self.lock:
            room = copy.deepcopy(self.rooms.get(conversation) or self.previews.get(conversation)
                                 or {"turns": [], "latest": None})
        if room["latest"] and room["latest"]["id"] == token:
            return room["latest"]
        return next((job for job in reversed(room["turns"]) if job["id"] == token), None)

    def pcm_job(self, conversation, token):
        with self.lock:
            room = self.rooms.get(conversation) or self.previews.get(conversation)
            if room:
                job = room["latest"]
                if job and job["id"] == token and job.get("pcm"):
                    return job
                return next((job for job in reversed(room["turns"])
                             if job["id"] == token and job.get("pcm")), None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def send_data(self, status, data, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Permissions-Policy", "microphone=(self)")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; media-src 'self' blob:")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def send_json(self, status, value):
        self.send_data(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def send_pcm(self, job):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Connection", "close")
        self.close_connection = True
        changed = self.server.audio_changed
        offset = 0
        try:
            self.end_headers()
            with contextlib.ExitStack() as stack:
                wav = None
                while True:
                    with changed:
                        if not changed.wait_for(lambda: job["pcm_bytes"] > offset
                                                or job["phase"] in {"done", "cancelled", "error"}, timeout=180):
                            return
                        pending = job["pcm_bytes"] - offset
                        if job["phase"] in {"cancelled", "error"} or not pending:
                            break
                    if wav is None:
                        wav = stack.enter_context((CHECKS / f"web-{job['id']}" / "reply.wav").open("rb"))
                        wav.seek(WAV_HEADER_BYTES)
                    data = wav.read(min(pending, 32768))
                    if not data:
                        return
                    self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")
                    offset += len(data)
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass

    def local_request(self, post=False):
        host = self.headers.get("Host", "")
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if host not in allowed or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        return not post or self.headers.get("Origin") == f"http://{host}"

    def do_GET(self):
        if not self.local_request():
            return self.send_json(403, {"error": "仅允许同源的本机访问。"})
        url = urlsplit(self.path)
        path = url.path
        if path in STATIC:
            filename, mime = STATIC[path]
            return self.send_data(200, (WEB / filename).read_bytes(), mime)
        if path == "/api/status":
            return self.send_json(200, {"llama": llama_ready(), "tts_modes": self.server.tts_modes,
                                        "quality_pcm": bool(self.server.graph),
                                        "quality_warming": self.server.graph_warming,
                                        "quality_voices": VOICE_PRESETS})
        if path == "/api/conversation":
            conversation = self.headers.get("X-Conversation-Id", "")
            if not ID_PATTERN.fullmatch(conversation):
                return self.send_json(400, {"error": "会话编号无效，请刷新页面。"})
            return self.send_json(200, self.server.get_room(conversation))
        parts = path.strip("/").split("/")
        if parts[:2] == ["api", "turn"] and len(parts) in (3, 4):
            conversation = self.headers.get("X-Conversation-Id", "")
            if parts[3:] == ["stream.pcm"] and ID_PATTERN.fullmatch(conversation):
                job = self.server.pcm_job(conversation, parts[2])
                if job:
                    return self.send_pcm(job)
                return self.send_json(404, {"error": "未找到语音流。"})
            if len(parts) == 4:
                conversation = parse_qs(url.query).get("conversation", [""])[0]
            job = self.server.get_turn(conversation, parts[2]) if ID_PATTERN.fullmatch(conversation) else None
            if job and len(parts) == 3:
                return self.send_json(200, job)
            if job and job["phase"] == "done" and parts[3:] == ["reply.wav"]:
                audio = (CHECKS / f"web-{job['id']}" / "reply.wav").read_bytes()
                return self.send_data(200, audio, "audio/wav")
            if job and len(parts) == 4:
                match = re.fullmatch(r"part-(0|[1-9][0-9]{0,2})\.wav", parts[3])
                if match and int(match[1]) < job["parts"]:
                    audio = (CHECKS / f"web-{job['id']}" / parts[3]).read_bytes()
                    return self.send_data(200, audio, "audio/wav")
        self.send_json(404, {"error": "未找到。"})

    def do_POST(self):
        if not self.local_request(post=True):
            return self.send_json(403, {"error": "仅允许同源的本机访问。"})
        path = urlsplit(self.path).path
        cancel = re.fullmatch(r"/api/turn/([0-9a-f]{32})/cancel", path)
        if not cancel and path not in {"/api/turn", "/api/sample", "/api/warm-quality", "/api/voice-preview"}:
            return self.send_json(404, {"error": "未找到。"})
        conversation = self.headers.get("X-Conversation-Id", "")
        if not ID_PATTERN.fullmatch(conversation):
            return self.send_json(400, {"error": "会话编号无效，请刷新页面。"})
        if path == "/api/warm-quality":
            self.server.warm_quality()
            return self.send_json(202, {"warming": self.server.graph_warming})
        if cancel:
            if self.server.cancel_turn(conversation, cancel[1]):
                return self.send_json(200, {"phase": "cancelled"})
            return self.send_json(404, {"error": "未找到可以打断的回复。"})
        preview = path == "/api/voice-preview"
        if preview and not self.server.graph:
            return self.send_json(503, {"error": "当前服务不支持流式音色试听。"})
        tts_mode = "quality" if preview else self.headers.get("X-Voice-TTS", self.server.tts_modes[0])
        if tts_mode not in self.server.tts_modes:
            return self.send_json(400, {"error": "不支持所选的语音合成模式。"})
        speaker = self.headers.get("X-Voice-Speaker", DEFAULT_SPEAKER)
        if tts_mode == "quality" and speaker not in VOICE_PRESETS:
            return self.send_json(400, {"error": "不支持所选的音色。"})
        data = None
        if path == "/api/turn":
            if self.headers.get_content_type() != "audio/wav":
                return self.send_json(415, {"error": "音频格式不受支持。"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 44 <= length <= MAX_UPLOAD:
                return self.send_json(400, {"error": "音频为空或超过大小限制。"})
            try:
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError("音频上传不完整。")
                validate_clip(data)
            except (OSError, ValueError, wave.Error, EOFError) as error:
                return self.send_json(400, {"error": str(error) if isinstance(error, ValueError) else "无法读取 WAV 音频。"})
        if not preview and not llama_ready():
            return self.send_json(503, {"error": "llama.cpp 未就绪，请先启动本地模型。"})
        try:
            turn_room = uuid.uuid4().hex if preview else conversation
            token = self.server.create_turn(turn_room, data, tts_mode, speaker=speaker, preview=preview)
        except OSError:
            return self.send_json(500, {"error": "无法写入本地音频文件。"})
        if not token:
            return self.send_json(409, {"error": "已有一轮正在处理。"})
        response = {"id": token, "pcm": tts_mode == "quality" and bool(self.server.graph)}
        if preview:
            response["room"] = turn_room
        self.send_json(202, response)

    def log_message(self, format, *args):
        if args and str(args[0]).startswith("GET /api/"):
            return
        super().log_message(format, *args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local voice conversation page.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--cold-asr", action="store_true", help="Unload the recognizer between turns.")
    parser.add_argument("--cold-tts", action="store_true", help="Use one-shot Qwen speech synthesis.")
    parser.add_argument("--asr-device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--qwen-graph-python", type=Path, help="Python with faster-qwen3-tts installed.")
    options = parser.parse_args()
    if options.qwen_graph_python and (not options.qwen_graph_python.is_file()
                                      or options.asr_device != "cpu" or options.cold_tts):
        parser.error("Qwen streaming requires an existing graph Python, CPU ASR, and warm TTS.")
    recognizer = None if options.cold_asr else WarmModel(
        "asr", idle_seconds=300 if options.asr_device == "cuda" else None, asr_device=options.asr_device,
    )
    synthesizer = None if options.cold_tts else WarmModel("tts", idle_seconds=120)
    graph = WarmModel("qwen-graph", idle_seconds=600, python=options.qwen_graph_python) if options.qwen_graph_python else None
    fast_synthesizer = WarmModel("fasttts", idle_seconds=300) if fast_tts_available() else None
    try:
        with VoiceServer(("127.0.0.1", options.port), recognizer, synthesizer,
                         fast_synthesizer, graph, options.asr_device) as server:
            if fast_synthesizer:
                threading.Thread(target=server.warm_fast_tts, daemon=True).start()
            if recognizer and options.asr_device == "cpu":
                threading.Thread(target=server.warm_asr, daemon=True).start()
            server.warm_quality()
            print(f"Voice console: http://127.0.0.1:{server.server_port}/", flush=True)
            server.serve_forever()
    finally:
        if recognizer:
            recognizer.close()
        if synthesizer:
            synthesizer.close()
        if fast_synthesizer:
            fast_synthesizer.close()
        if graph:
            graph.close()
