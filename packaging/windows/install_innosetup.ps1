#requires -Version 5.1
<#
.SYNOPSIS
    Installs the pinned Inno Setup compiler for the current user (no admin).

.DESCRIPTION
    build.ps1 needs ISCC.exe. This fetches the official installer from the
    Inno Setup GitHub releases, verifies it twice before running it - the
    SHA256 pinned below AND a valid Authenticode signature of the publisher -
    and installs it silently into %LOCALAPPDATA%\Programs. Does nothing when a
    matching Inno Setup is already present.

    Inno Setup is free to use (see https://jrsoftware.org/files/is/license.txt);
    its authors ask commercial users to buy a licence.
#>
[CmdletBinding()]
param([switch]$Force)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Version   = '7.1.1'
$Url       = "https://github.com/jrsoftware/issrc/releases/download/is-7_1_1/innosetup-$Version-x64.exe"
$Sha256    = '8a5a6711c3766a6e5fb74f280ba0e3551e719ce94538d901fdbd1f8f9920b3f0'
$Publisher = 'Pyrsys B.V.'            # as stated on https://jrsoftware.org/isdl.php
$Target    = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'

if ((Test-Path $Target) -and -not $Force) {
    Write-Host "Inno Setup already installed: $Target"
    exit 0
}

$dir = Join-Path ([IO.Path]::GetTempPath()) ("innosetup-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $dir | Out-Null
$exe = Join-Path $dir "innosetup-$Version-x64.exe"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Host "downloading $Url"
    Invoke-WebRequest -UseBasicParsing $Url -OutFile $exe

    $sha = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
    if ($sha -ne $Sha256) { throw "SHA256 mismatch: got $sha, expected $Sha256" }
    $sig = Get-AuthenticodeSignature $exe
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike "*$Publisher*") {
        throw "unexpected signature: $($sig.Status) / $($sig.SignerCertificate.Subject)"
    }
    Write-Host "verified: sha256 + signature ($Publisher)"

    $p = Start-Process -FilePath $exe -Wait -PassThru `
        -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER'
    if ($p.ExitCode -ne 0) { throw "Inno Setup installer exit code $($p.ExitCode)" }
    if (-not (Test-Path $Target)) { throw "installed, but $Target is missing" }
    Write-Host "Inno Setup $Version ready: $Target"
}
finally {
    Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
}
