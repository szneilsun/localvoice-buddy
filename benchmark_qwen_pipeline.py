import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from benchmark_qwen_realtime import GPUSampler, LONG_TEXT, TEXTS


def emit(record):
    print(json.dumps(record, ensure_ascii=False), flush=True)


class CpuRecognizer:
    def __init__(self, root, threads):
        self.worker = subprocess.Popen(
            [str(root / ".venv-asr/Scripts/python.exe"), str(Path(__file__).resolve()),
             "--root", str(root), "--cpu-worker", "--cpu-threads", str(threads)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"}, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def call(self, *, audio, reserve_mib):
        self.worker.stdin.write(json.dumps(audio) + "\n")
        self.worker.stdin.flush()
        line = self.worker.stdout.readline()
        if not line:
            raise RuntimeError("CPU ASR exited before returning text.")
        return json.loads(line)

    def close(self):
        self.worker.stdin.close()
        try:
            self.worker.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.worker.terminate()
            self.worker.wait(timeout=5)
        self.worker.stdout.close()


def cpu_worker(root, threads):
    sys.path.insert(0, str(root))
    from run_voice_turn import guard_network
    from voice_smoke import load_asr, transcribe

    blocked = guard_network("asr")
    model = None
    for line in sys.stdin:
        with contextlib.redirect_stdout(sys.stderr):
            import torch

            if model is None:
                torch.set_num_threads(threads)
                torch.set_num_interop_threads(1)
                model = load_asr("cpu")
            text, _ = transcribe(model, Path(json.loads(line)))
            if blocked:
                raise RuntimeError(f"Unexpected CPU ASR network operations: {blocked}")
        emit({"text": text})


def edit_distance(text, reference):
    def normalize(value):
        digits = str.maketrans("零一二三四五六七八九", "0123456789")
        return "".join(char for char in unicodedata.normalize("NFKC", value).translate(digits)
                       if char.isalnum()).lower()

    text, reference = normalize(text), normalize(reference)
    row = list(range(len(text) + 1))
    for i, char in enumerate(reference, 1):
        previous, row[0] = row[0], i
        for j, other in enumerate(text, 1):
            value = row[j]
            row[j] = min(row[j - 1] + 1, value + 1, previous + (char != other))
            previous = value
    return row[-1], len(reference)


def run(args):
    sys.path.append(str(args.root / ".venv-tts/Lib/site-packages"))
    sys.path.insert(0, str(args.root))
    os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TRANSFORMERS_OFFLINE="1")
    from run_voice_turn import guard_network
    from voice_stream import LlamaStream, Sentences
    from warm_models import WarmModel

    blocked = guard_network("chat")
    import torch
    from faster_qwen3_tts import FasterQwen3TTS

    asr = CpuRecognizer(args.root, args.cpu_threads) if args.asr_device == "cpu" else WarmModel("asr")
    wav = args.root / "checks/shanghai-question.wav"
    model_path = args.root / "models/Qwen3-TTS-12Hz-0.6B-CustomVoice"
    try:
        with GPUSampler() as gpu:
            model = FasterQwen3TTS.from_pretrained(
                str(model_path), device="cuda:0", dtype=torch.bfloat16,
                attn_implementation="sdpa", max_seq_len=1024, local_files_only=True,
            )

            def speak(text):
                return model.generate_custom_voice_streaming(
                    text, speaker="Vivian", language="Chinese", chunk_size=8,
                )

            started = time.perf_counter()
            for _ in speak(TEXTS[0]):
                pass
            emit({"type": "capture_warmup", "seconds": round(time.perf_counter() - started, 3)})
            history = []
            for i in range(args.turns):
                start = time.perf_counter()
                clip = (wav, args.results / "stream8-warm-1-2.wav", args.results / "stream8-warm-1-3.wav")[i % 3]
                transcript = asr.call(audio=str(clip), reserve_mib=0)["text"]
                recognized = time.perf_counter()
                stream, sentences = LlamaStream(), Sentences()
                answer = ""
                first = None
                spoken = 0.0
                arrivals = []

                def render(sentence):
                    nonlocal first, spoken
                    for samples, rate, _ in speak(sentence):
                        if len(samples):
                            now = time.perf_counter()
                            first = now if first is None else first
                            duration = len(samples) / rate
                            spoken += duration
                            arrivals.append((now, duration))

                try:
                    for delta in stream.generate(transcript, history, lambda: None):
                        answer += delta
                        for sentence in sentences.feed(delta):
                            render(sentence)
                    for sentence in sentences.finish():
                        render(sentence)
                finally:
                    stream.stop()
                if first is None:
                    raise ValueError("The pipeline returned no audio.")
                cursor, gaps = first + 0.12, 0.0
                for arrival, duration in arrivals:
                    gaps += max(0, arrival - cursor)
                    cursor = max(cursor, arrival) + duration
                emit({"type": "turn", "round": i+1, "asr_s": round(recognized-start, 3),
                      "first_s": round(first-start, 3), "from_asr_s": round(first-recognized, 3),
                      "audio_s": round(spoken, 3), "gaps_s_at_120ms_buffer": round(gaps, 3)})
                history = [*history, {"role": "user", "content": transcript},
                           {"role": "assistant", "content": answer}][-8:]
            emit({"type": "pipeline_device_memory", "peak_used_mib": max((used for used, _ in gpu.samples), default=None),
                  "min_free_mib": min((free for _, free in gpu.samples), default=None)})
        if blocked:
            raise RuntimeError(f"Unexpected network operations: {blocked}")
        for mode in (() if args.skip_readback else ("stock", "stream8")):
            cases = [(f"warm-1-{i+1}", text) for i, text in enumerate(TEXTS)] + [("long", LONG_TEXT)]
            for label, sentence in cases:
                clip = args.results / f"{mode}-{label}.wav"
                if not clip.exists():
                    continue
                text = asr.call(audio=str(clip), reserve_mib=0)["text"]
                edits, chars = edit_distance(text, sentence)
                emit({"type": "readback", "mode": mode, "case": label, "edits": edits,
                      "chars": chars, "transcript": text})
    finally:
        asr.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test GPU co-residency with Qwen graph TTS, ASR and the running local LLM.")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument("--asr-device", choices=["cuda", "cpu"], default="cuda")
    parser.add_argument("--cpu-threads", type=int, default=8)
    parser.add_argument("--skip-readback", action="store_true")
    parser.add_argument("--cpu-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.cpu_worker:
        cpu_worker(args.root, args.cpu_threads)
    else:
        if args.turns < 1 or args.results is None:
            parser.error("At least one turn and a results directory are required.")
        run(args)
