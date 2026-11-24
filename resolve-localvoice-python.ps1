function Resolve-LocalVoicePython {
    param([string]$DeploymentRoot, [string]$PythonPath)

    if ($PythonPath) {
        return (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
    }
    $bundled = Join-Path $DeploymentRoot 'runtime\python\python.exe'
    if (Test-Path -LiteralPath $bundled -PathType Leaf) {
        return (Resolve-Path -LiteralPath $bundled).Path
    }
    $python = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $python) {
        throw 'Put Python 3.12 on PATH or specify -PythonPath.'
    }
    return $python.Path
}
