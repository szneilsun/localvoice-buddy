import argparse
import json
import statistics
import tempfile
import time
import wave
from pathlib import Path

from run_voice_turn import ROOT
from warm_models import WarmModel


TEXTS = (
    "上海是一座沿海城市。",
    "明天下午三点，我们在人民广场见面。",
    "从地铁二号线换乘十号线，大约需要十五分钟。",
)


def benchmark(engine, folder):
    worker = WarmModel({"fast": "fasttts", "quality": "tts"}[engine])
    results = []
    try:
        for i, text in enumerate((TEXTS[0], *TEXTS)):
            output = folder / f"{engine}-{i}.wav"
            started = time.monotonic()
            worker.call(text=text, speaker="Vivian", output=str(output))
            elapsed = time.monotonic() - started
            with wave.open(str(output), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
            result = {"elapsed_s": round(elapsed, 3), "audio_s": round(duration, 3),
                      "rtf": round(elapsed / duration, 3)}
            results.append(result)
            print(json.dumps({"engine": engine, "round": "cold" if not i else f"warm-{i}", **result}), flush=True)
    finally:
        worker.close()
    print(json.dumps({"engine": engine, "warm_median_rtf": statistics.median(
        result["rtf"] for result in results[1:])}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare offline speech synthesis using fixed Mandarin samples.")
    parser.add_argument("--engine", choices=["fast", "quality", "both"], default="both")
    args = parser.parse_args()
    samples = Path(tempfile.mkdtemp(prefix="tts-benchmark-", dir=ROOT / "checks"))
    print(samples, flush=True)
    for engine in ("fast", "quality") if args.engine == "both" else (args.engine,):
        benchmark(engine, samples)
