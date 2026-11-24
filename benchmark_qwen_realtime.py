import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path


TEXTS = (
    "上海是一座沿海城市。",
    "明天下午三点，我们在人民广场见面。",
    "从地铁二号线换乘十号线，大约需要十五分钟。",
)
LONG_TEXT = (
    "上海是一座沿海城市。你可以先去外滩看看黄浦江两岸的风景，"
    "然后坐地铁去人民广场。下午可以逛逛上海博物馆，"
    "傍晚到城隍庙尝一尝小笼包。最后记得留出回酒店的时间。"
)


def allocated_mib(torch):
    return round(torch.cuda.memory_allocated() / 2**20)


class GPUSampler:
    def __init__(self):
        self.stop = threading.Event()
        self.samples = []

    def sample(self):
        while not self.stop.is_set():
            try:
                output = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=memory.used,memory.free", "--format=csv,noheader,nounits"],
                    text=True, timeout=2, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                used, free = map(int, output.splitlines()[0].split(","))
                self.samples.append((used, free))
            except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                pass
            self.stop.wait(0.2)

    def __enter__(self):
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()


def run(args):
    root = args.model.resolve().parents[1]
    if args.shared_site:
        sys.path.append(str(args.shared_site.resolve()))
    sys.path.insert(0, str(root))
    os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TRANSFORMERS_OFFLINE="1")
    from run_voice_turn import guard_network

    blocked = guard_network("tts")
    import numpy as np
    import soundfile as sf
    import torch
    from importlib.metadata import version

    emit = lambda record: print(json.dumps(record, ensure_ascii=False), flush=True)
    emit({"type": "environment", "engine": args.engine, "torch": torch.__version__,
          "gpu": torch.cuda.get_device_name(), "transformers": version("transformers"),
          "qwen": version("qwen-tts"),
          "graph": version("faster-qwen3-tts") if args.engine == "graph" else None})
    if args.engine == "graph":
        from faster_qwen3_tts import FasterQwen3TTS

        load = lambda: FasterQwen3TTS.from_pretrained(
            str(args.model.resolve()), device="cuda:0", dtype=torch.bfloat16,
            attn_implementation="sdpa", max_seq_len=1024, local_files_only=True,
        )
    else:
        from voice_smoke import load_tts

        load = lambda: load_tts("cuda")

    args.output.mkdir(parents=True, exist_ok=True)
    opts = {"speaker": "Vivian", "language": "Chinese"}
    rows = []
    with GPUSampler() as gpu:
        before = time.perf_counter()
        model = load()
        torch.cuda.synchronize()
        emit({"type": "load", "seconds": round(time.perf_counter() - before, 3),
              "allocated_mib": allocated_mib(torch)})
        modes = ("full", "stream8", "stream12") if args.engine == "graph" else ("stock",)
        if args.mode:
            modes = (args.mode,)
        first_call = True
        for mode in modes:
            cases = ([("cold", TEXTS[0])] if first_call else []) + [
                (f"warm-{rep + 1}-{i + 1}", text)
                for rep in range(args.repeats) for i, text in enumerate(TEXTS)
            ] + [("long", LONG_TEXT)]
            for label, text in cases:
                torch.manual_seed(31 + (TEXTS.index(text) if text in TEXTS else 8))
                torch.cuda.reset_peak_memory_stats()
                started = time.perf_counter()
                if mode.startswith("stream"):
                    stream = model.generate_custom_voice_streaming(text, **opts, chunk_size=int(mode[6:]))
                else:
                    def full():
                        wavs, rate = model.generate_custom_voice(text, **opts)
                        yield wavs[0], rate, {}
                    stream = full()
                chunks = []
                arrivals = []
                for samples, rate, _ in stream:
                    if len(samples):
                        arrivals.append(time.perf_counter() - started)
                        chunks.append(samples)
                elapsed = time.perf_counter() - started
                audio = np.concatenate(chunks)
                if not np.isfinite(audio).all() or not np.any(np.abs(audio) > 0.001):
                    raise ValueError("Invalid synthesis audio.")
                durations = [len(chunk) / rate for chunk in chunks]
                play_until = arrivals[0] + 0.12
                gap = 0.0
                for arrival, duration in zip(arrivals, durations):
                    gap += max(0, arrival - play_until)
                    play_until = max(play_until, arrival) + duration
                row = {"type": "sample", "mode": mode, "case": label, "seconds": round(elapsed, 3),
                       "first_s": round(arrivals[0], 3), "audio_s": round(len(audio) / rate, 3),
                       "rtf": round(elapsed * rate / len(audio), 3), "chunks": len(chunks),
                       "gaps_s_at_120ms_buffer": round(gap, 3),
                       "peak_allocated_mib": round(torch.cuda.max_memory_allocated() / 2**20),
                       "peak_reserved_mib": round(torch.cuda.max_memory_reserved() / 2**20)}
                sf.write(args.output / f"{mode}-{label}.wav", audio, rate, subtype="PCM_16")
                emit(row)
                rows.append(row)
            first_call = False
    if blocked:
        raise RuntimeError(f"Unexpected network operations: {blocked}")
    for mode in modes:
        subset = [row for row in rows if row["mode"] == mode and row["case"].startswith("warm")]
        summary = {key: round(statistics.median(row[key] for row in subset), 3)
                   for key in ("rtf", "first_s", "seconds")}
        emit({"type": "warm_median", "mode": mode, **summary, "n": len(subset)})
    emit({"type": "device_memory", "peak_used_mib": max((used for used, _ in gpu.samples), default=None),
          "min_free_mib": min((free for _, free in gpu.samples), default=None)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare Qwen Vivian CPU-output latency and CUDA memory.")
    parser.add_argument("engine", choices=["stock", "graph"])
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--shared-site", type=Path)
    parser.add_argument("--mode", choices=["stock", "full", "stream8", "stream12"])
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("At least one warm repeat is required.")
    if args.mode and (args.mode == "stock") != (args.engine == "stock"):
        parser.error("Mode is not supported by the selected engine.")
    run(args)
