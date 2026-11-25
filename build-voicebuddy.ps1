param([string]$AssetRoot)

$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw 'The Windows .NET Framework C# compiler is missing.'
}

$output = Join-Path $PSScriptRoot 'VoiceBuddy.exe'
$source = Join-Path $PSScriptRoot 'launcher\VoiceBuddy.cs'
& $compiler /nologo /target:winexe /codepage:65001 "/out:$output" $source
if ($LASTEXITCODE) { throw 'Could not build VoiceBuddy.exe.' }

if ($AssetRoot) {
    $resolved = (Resolve-Path -LiteralPath $AssetRoot).Path.TrimEnd('\')
    $configure = Start-Process -FilePath $output -ArgumentList "--configure `"$resolved`"" `
        -WindowStyle Hidden -Wait -PassThru
    if ($configure.ExitCode) { throw 'Could not configure the local model directory.' }
}
Write-Output $output
