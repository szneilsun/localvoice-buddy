import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ASSET_ROOT = Path(os.environ.get("LOCALVOICE_ASSET_ROOT") or ROOT).resolve()
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TTS_SAMPLE = (
    "\u4f60\u597d\uff0c\u6211\u662f\u672c\u5730\u8bed\u97f3\u52a9\u624b\u3002"
    "\u73b0\u5728\u6b63\u5728\u6d4b\u8bd5\u4e2d\u6587\u8bed\u97f3\u5408\u6210\u3002"
)


def local_model_options(device):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    import torch

    if device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA is unavailable. Use --device cpu for a CPU test.")
    return {
        "dtype": torch.bfloat16 if device == "cuda" else torch.float32,
        "device_map": "cuda:0" if device == "cuda" else "cpu",
        "attn_implementation": "sdpa",
        "local_files_only": True,
    }


def run_record(args):
    import sounddevice as sd

    if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 60:
        raise ValueError("Recording duration must be between 1 and 60 seconds.")
    if args.input_device is not None and args.input_device < 0:
        raise ValueError("The input device index cannot be negative.")
    rate = 16000
    try:
        sd.check_input_settings(device=args.input_device, channels=1, dtype="int16", samplerate=rate)
    except sd.PortAudioError:
        try:
            rate = round(sd.query_devices(args.input_device, "input")["default_samplerate"])
            sd.check_input_settings(device=args.input_device, channels=1, dtype="int16", samplerate=rate)
        except sd.PortAudioError as error:
            raise ValueError(f"No usable microphone: {error}") from error

    for remaining in (3, 2, 1):
        print(f"Recording in {remaining}...", flush=True)
        time.sleep(1)
    if args.cue:
        import winsound

        try:
            winsound.Beep(880, 240)
        except RuntimeError as error:
            raise ValueError("Could not play the recording cue; no audio was recorded.") from error
    print(f"Speak now ({args.seconds:g} seconds).", flush=True)
    try:
        samples = sd.rec(
            round(rate * args.seconds), rate, channels=1, dtype="int16",
            device=args.input_device, blocking=True,
        )
    except sd.PortAudioError as error:
        raise ValueError(f"Microphone capture failed: {error}") from error
    peak = int(abs(samples.astype("int32")).max())
    with wave.open(str(args.output), "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(samples.astype("<i2", copy=False).tobytes())
    print(f"Saved {args.output.resolve()} ({rate} Hz mono, peak {peak}/32768).")
    if peak < 160:
        print("The recording is very quiet. Check the input device and microphone level.", file=sys.stderr)


def load_asr(device):
    model_path = ASSET_ROOT / "models" / "Qwen3-ASR-0.6B"
    if not (model_path / "model.safetensors").is_file():
        raise ValueError(f"Model weights not found: {model_path}")
    options = local_model_options(device)
    from qwen_asr import Qwen3ASRModel

    return Qwen3ASRModel.from_pretrained(
        str(model_path),
        max_inference_batch_size=1,
        max_new_tokens=256,
        **options,
    )


def transcribe(model, audio, language=None):
    results = model.transcribe(audio=str(audio.resolve(strict=True)), language=language)
    if not results or not results[0].text.strip():
        raise ValueError("No speech was transcribed.")
    return results[0].text.strip(), results[0].language


def run_asr(args):
    audio = args.input.resolve(strict=True)
    text, language = transcribe(load_asr(args.device), audio, args.language)
    args.output.write_text(text + "\n", encoding="utf-8")
    print(f"Language: {language}; device: {args.device}", file=sys.stderr)
    print(text)


def load_tts(device):
    model_path = ASSET_ROOT / "models" / "Qwen3-TTS-12Hz-0.6B-CustomVoice"
    for subpath in ("model.safetensors", "speech_tokenizer/model.safetensors"):
        if not (model_path / subpath).is_file():
            raise ValueError(f"Model weights not found: {model_path / subpath}")

    options = local_model_options(device)
    from qwen_tts import Qwen3TTSModel

    return Qwen3TTSModel.from_pretrained(str(model_path), **options)


def synthesize(model, text, speaker, language, output):
    import numpy as np
    import soundfile as sf

    wavs, rate = model.generate_custom_voice(text=text, speaker=speaker, language=language)
    if len(wavs) != 1 or rate <= 0:
        raise ValueError("The speech model did not return one audio clip.")
    samples = np.asarray(wavs[0])
    if samples.ndim != 1 or samples.size == 0 or not np.isfinite(samples).all():
        raise ValueError("The speech model returned invalid mono audio.")
    if np.max(np.abs(samples)) < 0.0001:
        raise ValueError("The speech model returned silent audio.")
    sf.write(output, samples, rate, format="WAV", subtype="PCM_16")
    print(f"Saved {output.resolve()} ({samples.size / rate:.2f} s, {rate} Hz mono, {speaker}).")


def run_tts(args):
    text = args.text if args.from_file is None else args.from_file.read_text(encoding="utf-8-sig")
    text = (TTS_SAMPLE if text is None else text).strip()
    if not text:
        raise ValueError("The text to speak is empty.")
    synthesize(load_tts(args.device), text, args.speaker, args.language, args.output)


def llama_post(route, payload, port):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with LOCAL_HTTP.open(request, timeout=120) as response:
        return json.load(response)


def chat_messages(text, history=()):
    system = {
        "role": "system",
        "content": ("Answer in concise Simplified Chinese suitable for speaking. Use plain text. "
                    "Start with a short, direct sentence, then add detail only as needed."),
    }
    if len(history) % 2 or any(
        item.get("role") != ("user" if i % 2 == 0 else "assistant")
        or not isinstance(item.get("content"), str)
        for i, item in enumerate(history)
    ):
        raise ValueError("Conversation history must contain complete user and assistant pairs.")
    return [system, *history, {"role": "user", "content": text}]


def chat_payload(text, port=8001, history=(), *, check=lambda: None):
    messages = chat_messages(text, history)
    if history:
        while True:
            check()
            prompt = llama_post("/apply-template", {"messages": messages}, port)["prompt"]
            check()
            tokens = llama_post("/tokenize", {"content": prompt, "add_special": False}, port)["tokens"]
            if len(tokens) <= 3500:
                break
            if len(messages) == 2:
                raise ValueError("The current turn is too long for the local context window.")
            del messages[1:3]
    return {
        "model": "qwen3-8b",
        "messages": messages,
        "max_tokens": 256,
        "temperature": 0.6,
    }


def chat(text, port=8001, history=()):
    payload = dict(chat_payload(text, port, history), stream=False)
    result = llama_post("/v1/chat/completions", payload, port)
    content = result["choices"][0]["message"].get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("The model returned no spoken answer.")
    return content.strip()


def run_chat(args):
    text = args.prompt if args.from_file is None else args.from_file.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError("The prompt is empty.")
    history = json.loads(args.history.read_text(encoding="utf-8")) if args.history else ()
    if args.history and (not isinstance(history, list) or not all(isinstance(item, dict) for item in history)):
        raise ValueError("Invalid conversation history file.")
    answer = chat(text.strip(), args.port, history)
    if args.output is not None:
        args.output.write_text(answer + "\n", encoding="utf-8")
    print(answer)


def main():
    parser = argparse.ArgumentParser(description="Local voice assistant smoke tests.")
    commands = parser.add_subparsers(dest="command", required=True)
    record_parser = commands.add_parser("record", help="Record a short microphone sample.")
    record_parser.add_argument("--seconds", type=float, default=6)
    record_parser.add_argument("--input-device", type=int)
    record_parser.add_argument("--output", type=Path, default=ROOT / "mic.wav")
    record_parser.add_argument("--cue", action="store_true", help="Play a tone immediately before recording.")
    record_parser.set_defaults(run=run_record)
    asr_parser = commands.add_parser("asr", help="Transcribe a local audio file.")
    asr_parser.add_argument("--input", type=Path, default=ROOT / "mic.wav")
    asr_parser.add_argument("--output", type=Path, default=ROOT / "transcript.txt")
    asr_parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    asr_parser.add_argument("--language", help="Force a language such as Chinese; otherwise detect it.")
    asr_parser.set_defaults(run=run_asr)
    tts_parser = commands.add_parser("tts", help="Speak Chinese text with a local built-in voice.")
    tts_source = tts_parser.add_mutually_exclusive_group()
    tts_source.add_argument("--text")
    tts_source.add_argument("--from-file", type=Path)
    tts_parser.add_argument("--output", type=Path, default=ROOT / "reply.wav")
    tts_parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    tts_parser.add_argument("--speaker", default="Vivian")
    tts_parser.add_argument("--language", default="Chinese")
    tts_parser.set_defaults(run=run_tts)
    chat_parser = commands.add_parser("chat", help="Ask the local text model.")
    source = chat_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt")
    source.add_argument("--from-file", type=Path)
    chat_parser.add_argument("--output", type=Path)
    chat_parser.add_argument("--port", type=int, default=8001)
    chat_parser.add_argument("--history", type=Path)
    chat_parser.set_defaults(run=run_chat)
    args = parser.parse_args()
    try:
        args.run(args)
    except urllib.error.HTTPError as error:
        parser.exit(1, f"Local model HTTP {error.code}: {error.read().decode('utf-8', 'replace')}\n")
    except (OSError, ValueError, IndexError, KeyError) as error:
        parser.exit(1, f"{args.command}: {error}\n")


if __name__ == "__main__":
    main()
