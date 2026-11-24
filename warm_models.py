import base64
import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from run_voice_turn import ROOT, guard_network


PREFIX = "MODEL_RESULT "
MIB = 1024**2
DEFAULT_SPEAKER = "Vivian"


class ModelMemoryError(RuntimeError):
    pass


class WarmModel:
    def __init__(self, kind, idle_seconds=None, *, python=None, asr_device="cuda"):
        self.kind = kind
        self.python = python or ROOT / f".venv-{kind}/Scripts/python.exe"
        self.asr_device = asr_device
        self.idle_seconds = idle_seconds
        self.worker = None
        self.timer = None
        self.lock = threading.RLock()

    @property
    def running(self):
        with self.lock:
            return bool(self.worker and self.worker.poll() is None and not self.worker.stdin.closed)

    def start(self):
        self.worker = subprocess.Popen(
            [str(self.python), str(Path(__file__).resolve()), self.kind, self.asr_device],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            encoding="utf-8", text=True, bufsize=1,
            env={**os.environ, "PYTHONUTF8": "1"},
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.results = queue.Queue()

        def read_output(stream, results):
            try:
                for line in stream:
                    if line.startswith(PREFIX):
                        results.put(line[len(PREFIX):])
                    else:
                        print(line.rstrip(), file=sys.stderr)
            finally:
                stream.close()
                results.put(None)

        threading.Thread(target=read_output, args=(self.worker.stdout, self.results), daemon=True).start()

    def cancel_timer(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None

    def arm_timer(self):
        if self.idle_seconds is None:
            return

        def expire():
            with self.lock:
                if self.timer is timer:
                    self.close()

        timer = threading.Timer(self.idle_seconds, expire)
        timer.daemon = True
        self.timer = timer
        timer.start()

    def send(self, payload):
        self.worker.stdin.write(json.dumps(payload) + "\n")
        self.worker.stdin.flush()

    def receive(self):
        try:
            message = self.results.get(timeout=180)
        except queue.Empty as error:
            raise RuntimeError(f"The {self.kind} worker timed out.") from error
        if message is None:
            raise RuntimeError(f"The {self.kind} worker exited without a result.")
        result = json.loads(message)
        if "error" in result:
            problem = ModelMemoryError if result.get("oom") else RuntimeError
            raise problem(result["error"])
        return result

    @contextlib.contextmanager
    def request(self, payload):
        with self.lock:
            self.cancel_timer()
            if self.worker and self.worker.poll() is not None:
                self.close()
            if self.worker is None:
                self.start()
            if not self.running:
                raise RuntimeError(f"The previous {self.kind} worker is still shutting down.")
            try:
                self.send(payload)
                yield
            except (OSError, RuntimeError, ValueError):
                self.close()
                raise
            finally:
                if self.worker:
                    self.arm_timer()

    def call(self, **payload):
        with self.request(payload):
            return self.receive()

    def pcm(self, text, speaker=DEFAULT_SPEAKER):
        with self.request({"text": text, "speaker": speaker}):
            while "pcm" in (result := self.receive()):
                # The worker waits here so closing the generator stops at a frame boundary.
                try:
                    yield base64.b64decode(result["pcm"], validate=True)
                except GeneratorExit:
                    self.send("stop")
                    self.receive()
                    return
                self.send("next")

    def close(self):
        with self.lock:
            self.cancel_timer()
            if not self.worker:
                return
            try:
                self.worker.stdin.close()
            except OSError:
                pass
            try:
                self.worker.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait()
            self.worker = None


def gpu_free_mib():
    try:
        return int(subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            text=True, timeout=2, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        ).splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def emit(result):
    print(PREFIX + json.dumps(result, ensure_ascii=False), file=sys.__stdout__, flush=True)


def graph_chunks(model, text, speaker=DEFAULT_SPEAKER):
    import numpy as np

    with contextlib.closing(model.generate_custom_voice_streaming(
        text, speaker=speaker, language="Chinese", chunk_size=8,
    )) as chunks:
        for samples, rate, _ in chunks:
            if rate != 24000 or samples.ndim != 1 or not np.isfinite(samples).all():
                raise ValueError("Qwen returned invalid 24 kHz mono samples.")
            if samples.size:
                yield np.clip(samples * 32768, -32768, 32767).astype("<i2").tobytes()


def worker_loop(kind, asr_device="cuda"):
    blocked = guard_network(kind)
    if kind == "fasttts":
        from fast_tts import PiperTTS
    elif kind == "qwen-graph":
        sys.path.append(str(ROOT / ".venv-tts/Lib/site-packages"))
        os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TRANSFORMERS_OFFLINE="1")
    else:
        from voice_smoke import load_asr, load_tts, synthesize, transcribe

    model = None
    for request in sys.stdin:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                payload = json.loads(request)
                if kind == "fasttts":
                    if model is None:
                        model = PiperTTS()
                    if not payload.get("warmup"):
                        model.synthesize(payload["text"], Path(payload["output"]))
                    result = {"ok": True}
                elif kind == "qwen-graph":
                    if model is None:
                        import torch
                        from faster_qwen3_tts import FasterQwen3TTS

                        model = FasterQwen3TTS.from_pretrained(
                            str(ROOT / "models/Qwen3-TTS-12Hz-0.6B-CustomVoice"),
                            device="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa",
                            max_seq_len=1024, local_files_only=True,
                        )
                        from voice_smoke import TTS_SAMPLE

                        for _ in graph_chunks(model, TTS_SAMPLE):
                            pass
                    if not payload.get("warmup"):
                        with contextlib.closing(graph_chunks(model, payload["text"], payload["speaker"])) as chunks:
                            for chunk in chunks:
                                emit({"pcm": base64.b64encode(chunk).decode("ascii")})
                                if sys.stdin.readline().strip() != '"next"':
                                    break
                    result = {"ok": True}
                else:
                    import torch

                    device = asr_device if kind == "asr" else "cuda"
                    if model is None:
                        if device == "cpu":
                            torch.set_num_threads(8)
                            torch.set_num_interop_threads(1)
                        model = (load_asr if kind == "asr" else load_tts)(device)
                    if kind == "asr":
                        text, language = transcribe(model, Path(payload["audio"]))
                        result = {"text": text, "language": language}
                    else:
                        synthesize(model, payload["text"], payload["speaker"], "Chinese", Path(payload["output"]))
                        result = {"ok": True}
                    if device == "cuda":
                        torch.cuda.empty_cache()
                        if kind == "asr" and torch.cuda.mem_get_info()[0] < payload["reserve_mib"] * MIB:
                            model = None
                            torch.cuda.empty_cache()
                            print("ASR released its model to leave room for TTS.", file=sys.stderr)
            if blocked:
                raise RuntimeError(f"Unexpected network operation in {kind}.")
        except Exception as error:
            print(f"{kind} worker: {error}", file=sys.stderr)
            emit({
                "error": str(error), "oom": "out of memory" in str(error).lower()
                or type(error).__name__ == "OutOfMemoryError",
            })
            return
        emit(result)


if __name__ == "__main__":
    worker_loop(*sys.argv[1:])
