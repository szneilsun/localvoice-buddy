param(
    [string]$DeploymentRoot = $PSScriptRoot,
    [int]$Port = 8010,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'resolve-localvoice-python.ps1')
$root = (Resolve-Path -LiteralPath $DeploymentRoot).Path
$python = Resolve-LocalVoicePython -DeploymentRoot $root -PythonPath $PythonPath
$graph = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '.venv-qwen-graph\Scripts\python.exe')).Path
$launcher = @'
import datetime
import socket
import subprocess
import sys
from pathlib import Path

root, port, graph = sys.argv[1:]
with socket.socket() as check:
    check.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    check.bind(("127.0.0.1", int(port)))

stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
log = Path(root) / f"voice-realtime-{port}-{stamp}"
with log.with_suffix(".stdout.log").open("xb") as out, log.with_suffix(".stderr.log").open("xb") as err:
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-u", "voice_web.py", "--port", port,
         "--asr-device", "cpu", "--qwen-graph-python", graph],
        cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err, close_fds=True,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
print(f"PID {process.pid} | http://127.0.0.1:{port}/")
'@
$launcher | & $python -X utf8 - $root $Port $graph
if ($LASTEXITCODE) { throw 'Could not start LocalVoice. Check whether the port is in use.' }
