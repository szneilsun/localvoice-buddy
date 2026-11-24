#Requires -RunAsAdministrator
param(
    [string]$MacAddress,
    [string]$DeploymentRoot = $PSScriptRoot,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'resolve-localvoice-python.ps1')
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot '.voice-lan\access.json') -Raw | ConvertFrom-Json
$python = Resolve-LocalVoicePython -DeploymentRoot $DeploymentRoot -PythonPath $PythonPath
$remote = $config.clientNetwork
if ($MacAddress) {
    $address = [Net.IPAddress]::Parse($MacAddress)
    if ($address.AddressFamily -ne 'InterNetwork') { throw 'Provide the Mac IPv4 address.' }
    $remote = $address.ToString()
}
$name = "LocalVoice.Mac.HTTPS.$($config.port)"
if (Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue) {
    throw "A rule named $name already exists. Review its remote address filter before changing it."
}
New-NetFirewallRule -Name $name -DisplayName 'LocalVoice Mac HTTPS' -Action Allow -Direction Inbound `
    -Profile Private -Protocol TCP -LocalAddress $config.lanIp -LocalPort $config.port `
    -RemoteAddress $remote -Program $python -EdgeTraversalPolicy Block |
    Select-Object Name, Enabled, Profile, Action
