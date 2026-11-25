import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from voice_lan import gateway_address, gateway_ready, load_lan_config

ROOT = Path(__file__).resolve().parents[1]
LAN = ROOT / ".voice-lan"
BACKGROUND = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0


def server_command(port, root=ROOT, asset_root=None):
    command = [sys.executable, "-X", "utf8", "-u", str(root / "voice_web.py"),
               "--port", str(port), "--asr-device", "cpu"]
    for location in dict.fromkeys((root, asset_root or root)):
        environment = location / ".venv-qwen-graph"
        python = environment / "Scripts" / "python.exe"
        package = environment / "Lib" / "site-packages" / "faster_qwen3_tts" / "__init__.py"
        if python.is_file() and package.is_file():
            return command + ["--qwen-graph-python", str(python)]
    return command


def service_ready(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/api/status")
        with connection.getresponse() as response:
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                return False
            status = json.loads(response.read(16384))
            return (isinstance(status, dict) and status.get("chat_provider") in ("remote", "local")
                    and "tts_modes" in status)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def launch(command, log):
    return subprocess.Popen(
        command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        close_fds=True, creationflags=BACKGROUND,
    )


def wait_ready(process, ready, seconds):
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Child process exited with status {process.returncode}.")
            if ready():
                return
            time.sleep(0.25)
        raise TimeoutError("The local listener did not become ready.")
    except Exception:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        raise


def ensure_backend(port, log):
    if service_ready(port):
        return
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", port))
    assets = Path(os.environ.get("LOCALVOICE_ASSET_ROOT") or ROOT)
    command = server_command(port, asset_root=assets)
    mode = "Qwen CUDA Graph streaming" if "--qwen-graph-python" in command else "warm Qwen worker"
    print(f"TTS startup mode: {mode}", file=log, flush=True)
    server = launch(command, log)
    print(f"Starting voice service on port {port} (PID {server.pid})", file=log, flush=True)
    wait_ready(server, lambda: service_ready(port), 26)


def ensure_gateway(port, log):
    path = LAN / "access.json"
    config = load_lan_config(path, port)
    if config is None or gateway_ready(config, LAN):
        return
    address, https_port, _ = gateway_address(config)
    command = [sys.executable, "-X", "utf8", "-u", str(ROOT / "voice_lan.py"), str(path)]
    try:
        gateway = launch(command, log)
        print(f"Starting LAN HTTPS on {address}:{https_port} (PID {gateway.pid})", file=log, flush=True)
        wait_ready(gateway, lambda: gateway_ready(config, LAN), 6)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        # A change in the network or cert must not block local use.
        print(f"LAN HTTPS unavailable: {error}", file=log, flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1])
    log_path = ROOT / "runtime" / f"voice-buddy-{port}.log"
    log_path.parent.mkdir(exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        try:
            ensure_backend(port, log)
            ensure_gateway(port, log)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"Startup failed: {error}", file=log, flush=True)
            sys.exit(1)
