#Requires -Version 7.6
param(
    [Parameter(Mandatory)][string]$LanIp,
    [Parameter(Mandatory)][string]$ClientNetwork,
    [int]$Port = 8443,
    [string]$DeploymentRoot = $PSScriptRoot,
    [string]$PythonPath,
    [switch]$ReissueCertificate
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'resolve-localvoice-python.ps1')
$directory = Join-Path $PSScriptRoot '.voice-lan'
$configPath = Join-Path $directory 'access.json'
$python = Resolve-LocalVoicePython -DeploymentRoot $DeploymentRoot -PythonPath $PythonPath
$ip = [System.Net.IPAddress]::Parse($LanIp)
if ($ip.AddressFamily -ne 'InterNetwork' -or $Port -lt 1024 -or $Port -gt 65535) {
    throw 'Provide a private IPv4 address and an unprivileged HTTPS port.'
}

[void][IO.Directory]::CreateDirectory($directory)
$acl = Get-Acl -LiteralPath $directory
if (-not $acl.AreAccessRulesProtected) {
    $workspaceOwner = (Get-Acl -LiteralPath $PSScriptRoot).GetOwner([Security.Principal.SecurityIdentifier])
    $runner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($workspaceOwner, $runner,
            [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
            [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
        $acl.SetAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow'
        ))
    }
    Set-Acl -LiteralPath $directory -AclObject $acl
}

if ((Test-Path -LiteralPath $configPath) -and -not $ReissueCertificate) {
    $current = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json -AsHashtable
    if ($current.lanIp -ne $LanIp -or $current.clientNetwork -ne $ClientNetwork -or $current.port -ne $Port) {
        throw 'Existing certificate is for a different address. Stop the gateway, reissue it, and trust the new certificate on the Mac.'
    }
    if ($current.Contains('username') -or $current.Contains('password')) {
        [void]$current.Remove('username')
        [void]$current.Remove('password')
        [IO.File]::WriteAllText($configPath, ($current | ConvertTo-Json))
    }
} else {
    $rootKey = [Security.Cryptography.RSA]::Create(2048)
    $leafKey = [Security.Cryptography.RSA]::Create(2048)
    try {
        $sha = [Security.Cryptography.HashAlgorithmName]::SHA256
        $pad = [Security.Cryptography.RSASignaturePadding]::Pkcs1
        $now = [DateTimeOffset]::UtcNow
        $caRequest = [Security.Cryptography.X509Certificates.CertificateRequest]::new(
            'CN=LocalVoice LAN CA', $rootKey, $sha, $pad
        )
        $caRequest.CertificateExtensions.Add(
            [Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($true, $true, 0, $true)
        )
        $caRequest.CertificateExtensions.Add(
            [Security.Cryptography.X509Certificates.X509SubjectKeyIdentifierExtension]::new($caRequest.PublicKey, $false)
        )
        $usage = [Security.Cryptography.X509Certificates.X509KeyUsageFlags]
        $caRequest.CertificateExtensions.Add(
            [Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new(
                ($usage::KeyCertSign -bor $usage::CrlSign), $true
            )
        )
        $ca = $caRequest.CreateSelfSigned($now.AddDays(-1), $now.AddYears(2))
        try {
            $leafRequest = [Security.Cryptography.X509Certificates.CertificateRequest]::new(
                'CN=LocalVoice', $leafKey, $sha, $pad
            )
            $san = [Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
            $san.AddIpAddress($ip)
            $leafRequest.CertificateExtensions.Add($san.Build())
            $leafRequest.CertificateExtensions.Add(
                [Security.Cryptography.X509Certificates.X509AuthorityKeyIdentifierExtension]::CreateFromCertificate(
                    $ca, $true, $false
                )
            )
            $leafRequest.CertificateExtensions.Add(
                [Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true)
            )
            $leafRequest.CertificateExtensions.Add(
                [Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new(
                    ($usage::DigitalSignature -bor $usage::KeyEncipherment), $true
                )
            )
            $oid = [Security.Cryptography.OidCollection]::new()
            [void]$oid.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.1'))
            $leafRequest.CertificateExtensions.Add(
                [Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($oid, $false)
            )
            $serial = [Security.Cryptography.RandomNumberGenerator]::GetBytes(16)
            $leaf = $leafRequest.Create($ca, $now.AddDays(-1), $now.AddDays(365), $serial)
            try {
                [IO.File]::WriteAllText((Join-Path $directory 'server.pem'), $leaf.ExportCertificatePem())
                [IO.File]::WriteAllText((Join-Path $directory 'server-key.pem'), $leafKey.ExportPkcs8PrivateKeyPem())
                [IO.File]::WriteAllText((Join-Path $directory 'localvoice-ca.pem'), $ca.ExportCertificatePem())
                [IO.File]::WriteAllBytes((Join-Path $directory 'localvoice-ca.cer'), $ca.RawData)
            } finally { $leaf.Dispose() }
        } finally { $ca.Dispose() }
    } finally {
        $leafKey.Dispose()
        $rootKey.Dispose()
    }

    $config = @{
        lanIp = $LanIp
        port = $Port
        clientNetwork = $ClientNetwork
        backendPort = 8010
    }
    [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json))
}

& $python -X utf8 (Join-Path $PSScriptRoot 'voice_lan.py') $configPath --detach
if ($LASTEXITCODE) { throw 'The LAN gateway did not start.' }
$certificate = Join-Path $directory 'localvoice-ca.cer'
[pscustomobject]@{
    Certificate = $certificate
    SHA256 = (Get-FileHash -LiteralPath $certificate -Algorithm SHA256).Hash
    ConfigFile = $configPath
}
