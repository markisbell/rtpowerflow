# Port picker for the dev launcher (start_netzsim.bat).
#
# Problem it solves: several apps share the same default ports (netzsim and
# rtheatflow both like :8000/:5173). "Port is busy" therefore does NOT mean
# "netzsim is already running". This script decides per role:
#   - a listener that answers /health with app=netzsim  -> REUSE=1 (don't start)
#   - a foreign/unresponsive listener                   -> try the next port
#   - the first free port                               -> REUSE=0 (start there)
# The UI role checks /api/health through the Vite proxy, so a stale dev server
# whose backend is gone (proxy-500) is skipped instead of reused.
#
# Output (parsed by the .bat): two lines "PORT=<n>" and "REUSE=<0|1>".
# Defaults honor NETZSIM_PORT / NETZSIM_UI_PORT from the environment or .env.
param(
    [Parameter(Mandatory = $true)][ValidateSet("api", "ui")][string]$Mode,
    [int]$Range = 20
)

$ErrorActionPreference = "SilentlyContinue"

function Read-DotEnv([string]$Name) {
    # minimal .env reader: first "NAME=value" line, no quoting/expansion
    $envFile = Join-Path $PSScriptRoot "..\.env"
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        if ($line -match "^\s*$Name\s*=\s*(\S+)") { return $Matches[1] }
    }
    return $null
}

function Get-BasePort([string]$Name, [int]$Fallback) {
    $v = [Environment]::GetEnvironmentVariable($Name)
    if (-not $v) { $v = Read-DotEnv $Name }
    if ($v -match "^\d+$") { return [int]$v }
    return $Fallback
}

function Test-Listening([int]$Port) {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Test-IsNetzsim([int]$Port, [string]$Path) {
    # try both loopbacks: uvicorn binds 127.0.0.1 (IPv4), the Vite dev
    # server binds ::1 (IPv6) on Windows
    foreach ($ip in "127.0.0.1", "[::1]") {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://${ip}:$Port$Path"
            return (($r.Content | ConvertFrom-Json).app -eq "netzsim")
        } catch { }
    }
    return $false
}

if ($Mode -eq "api") {
    $base = Get-BasePort "NETZSIM_PORT" 8000
    $probe = { param($p) Test-IsNetzsim $p "/health" }
} else {
    $base = Get-BasePort "NETZSIM_UI_PORT" 5173
    $probe = { param($p) Test-IsNetzsim $p "/api/health" }
}

$reuse = 0
$port = $base
while ($port -lt $base + $Range) {
    if (-not (Test-Listening $port)) { break }
    if (& $probe $port) { $reuse = 1; break }
    $port++
}
if ($port -ge $base + $Range) {
    # not Write-Error: the script-global SilentlyContinue would swallow it
    [Console]::Error.WriteLine("kein freier Port in $base..$($base + $Range - 1) gefunden")
    exit 1
}

Write-Output "PORT=$port"
Write-Output "REUSE=$reuse"
