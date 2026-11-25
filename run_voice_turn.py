import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from voice_smoke import ASSET_ROOT


ROOT = Path(__file__).resolve().parent
LLAMA_ADDRESS = ("127.0.0.1", 8001)


def guard_network(command):
    blocked = set()

    def local_only(event, args):
        if command == "chat":
            if event == "socket.connect" and args[1] == LLAMA_ADDRESS:
                return
            if event == "socket.getaddrinfo" and args[:2] == LLAMA_ADDRESS:
                return
        if event in {
            "socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
            "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto",
        }:
            blocked.add(event)
            raise RuntimeError(f"Network operation not allowed during {command}: {event}")

    sys.addaudithook(local_only)
    return blocked


def run_worker(command, arguments):
    blocked = guard_network(command)
    import voice_smoke

    sys.argv = [str(ROOT / "voice_smoke.py"), command, *arguments]
    voice_smoke.main()
    if blocked:
        raise RuntimeError(f"Unexpected network operations: {', '.join(sorted(blocked))}")


def run_step(python, command, *arguments):
    started = time.monotonic()
    print(f"\n[{command}]", flush=True)
    try:
        subprocess.run(
            [str(python), str(Path(__file__).resolve()), "--worker", command, *map(str, arguments)],
            cwd=ROOT,
            env={**os.environ, "PYTHONUTF8": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=True,
            timeout=240,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"{command} failed with exit code {error.returncode}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{command} timed out") from error
    print(f"[{command}] completed in {time.monotonic() - started:.2f} s", flush=True)


def process_turn(audio, run, *, speaker="Vivian", asr_device="cuda", tts_device="cuda",
                 history=(), asr_runner=None, tts_runner=None, on_stage=lambda _: None):
    transcript = run / "transcript.txt"
    answer = run / "answer.txt"
    reply = run / "reply.wav"
    on_stage("asr")
    if asr_runner is None:
        run_step(ASSET_ROOT / ".venv-asr/Scripts/python.exe", "asr",
                 "--input", audio, "--output", transcript, "--device", asr_device)
    else:
        asr_runner(audio, transcript)
    on_stage("chat")
    options = []
    if history:
        history_file = run / "history.json"
        history_file.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
        options = ["--history", history_file]
    run_step(sys.executable, "chat", "--from-file", transcript, "--output", answer, *options)
    on_stage("tts")
    if tts_runner is None:
        run_step(ASSET_ROOT / ".venv-tts/Scripts/python.exe", "tts", "--from-file", answer,
                 "--output", reply, "--device", tts_device, "--speaker", speaker)
    else:
        tts_runner(answer, reply, speaker)
    return reply


def main():
    parser = argparse.ArgumentParser(description="Run one local voice turn from a WAV file.")
    parser.add_argument("input", type=Path)
    parser.add_argument("--speaker", default="Vivian")
    parser.add_argument("--asr-device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--tts-device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--play", action="store_true", help="Play the completed response on this computer.")
    args = parser.parse_args()
    if not args.input.is_file() or args.input.suffix.lower() != ".wav":
        parser.error("Choose an existing WAV file.")

    checks = ROOT / "checks"
    checks.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="voice-turn-", dir=checks))
    print(f"RUN_DIR={run}", flush=True)
    try:
        reply = process_turn(args.input.resolve(), run, speaker=args.speaker,
                             asr_device=args.asr_device, tts_device=args.tts_device)
        if args.play:
            import winsound

            winsound.PlaySound(str(reply), winsound.SND_FILENAME | winsound.SND_NODEFAULT)
    except (OSError, RuntimeError) as error:
        parser.exit(1, f"Voice turn failed: {error}\n")
    print(f"\nReply: {reply}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker" and sys.argv[2] in {"asr", "chat", "tts"}:
        run_worker(sys.argv[2], sys.argv[3:])
    else:
        main()
