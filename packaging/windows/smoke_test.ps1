#requires -Version 5.1
<#
.SYNOPSIS
    End-to-end check of an installed (or staged) netzsim - run the way a blank
    Windows PC would run it.

.DESCRIPTION
    Starts  <AppDir>\python\python.exe -m netzsim.desktop  with a SCRUBBED
    environment: PATH holds only the Windows system folders (no Python, no
    Node), the working directory is not the app folder, and PYTHONHOME /
    PYTHONPATH point at nonsense - the bundled runtime has to ignore all of
    that. Then it exercises what a user touches first:

      UI delivery, the /api prefix, the /ws stream, the grid catalog,
      the manual, a real power flow on a library grid and on an IEEE
      reference feeder, state estimation, and writing a recording into data\.

    Exit code 0 = every check passed. Used locally after a build and by the
    windows-installer workflow on the freshly installed product.

.EXAMPLE
    packaging\windows\smoke_test.ps1 -AppDir "$env:LOCALAPPDATA\Programs\netzsim"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AppDir,
    [int]$Port = 8321,
    [int]$StartupTimeout = 240
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AppDir = (Resolve-Path $AppDir).Path
$Py = Join-Path $AppDir 'python\python.exe'
if (-not (Test-Path $Py)) { throw "no bundled interpreter at $Py" }
$Log = Join-Path ([IO.Path]::GetTempPath()) ("netzsim-smoke-{0}.log" -f $PID)

$script:Failed = 0
function Pass([string]$What, [string]$Detail = '') { Write-Host ("  PASS  {0,-34} {1}" -f $What, $Detail) -ForegroundColor Green }
function Fail([string]$What, [string]$Detail = '') { Write-Host ("  FAIL  {0,-34} {1}" -f $What, $Detail) -ForegroundColor Red; $script:Failed++ }
function Expect([bool]$Ok, [string]$What, [string]$Detail = '') { if ($Ok) { Pass $What $Detail } else { Fail $What $Detail } }

# ---- a blank PC's environment ----------------------------------------------
function New-CleanStartInfo([string]$Exe, [string]$Arguments) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = $Arguments
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = [IO.Path]::GetTempPath()          # deliberately NOT the app folder
    $keep = 'SystemRoot', 'SystemDrive', 'windir', 'ComSpec', 'TEMP', 'TMP', 'USERPROFILE', 'USERNAME',
            'LOCALAPPDATA', 'APPDATA', 'HOMEDRIVE', 'HOMEPATH', 'ProgramData', 'PUBLIC', 'PATHEXT', 'OS',
            'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE'
    $saved = @{}
    foreach ($k in $keep) { $v = [Environment]::GetEnvironmentVariable($k); if ($v) { $saved[$k] = $v } }
    $psi.EnvironmentVariables.Clear()
    foreach ($k in $saved.Keys) { $psi.EnvironmentVariables[$k] = $saved[$k] }
    $psi.EnvironmentVariables['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot"
    # leftovers a real PC may carry - the bundled runtime must ignore them
    $psi.EnvironmentVariables['PYTHONHOME'] = 'C:\does-not-exist'
    $psi.EnvironmentVariables['PYTHONPATH'] = 'C:\does-not-exist'
    return $psi
}

# ---- HTTP without any proxy (campus proxies answer for 127.0.0.1 otherwise) --
function Invoke-Http([string]$Method, [string]$Path, $Body = $null, [int]$Timeout = 60) {
    $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port$Path")
    $req.Method = $Method
    $req.Proxy = $null
    $req.Timeout = $Timeout * 1000
    $req.ReadWriteTimeout = $Timeout * 1000
    if ($null -ne $Body) {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 8 -Compress))
        $req.ContentType = 'application/json'
        $req.ContentLength = $bytes.Length
        $s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Close()
    } elseif ($Method -ne 'GET') {
        $req.ContentLength = 0
    }
    $resp = $null
    try { $resp = $req.GetResponse() }
    catch {
        $e = $_.Exception
        while ($e -and -not ($e -is [System.Net.WebException])) { $e = $e.InnerException }
        if ($e -and $e.Response) { $resp = $e.Response } else { throw }
    }
    $ms = New-Object IO.MemoryStream
    $resp.GetResponseStream().CopyTo($ms)
    $out = [pscustomobject]@{ Status = [int]$resp.StatusCode; ContentType = [string]$resp.ContentType; Bytes = $ms.ToArray() }
    $resp.Close()
    return $out
}
function Get-Text($r) { [Text.Encoding]::UTF8.GetString($r.Bytes) }
function Get-Json($r) { (Get-Text $r) | ConvertFrom-Json }
function Has-Prop($o, [string]$Name) { $null -ne $o -and $null -ne $o.PSObject.Properties[$Name] }

function Wait-State([scriptblock]$Until, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-Http GET '/api/state' $null 20
            if ($r.Status -eq 200) { $st = Get-Json $r; if (& $Until $st) { return $st } }
        } catch { }
        Start-Sleep -Milliseconds 400
    }
    return $null
}

Write-Host "=== netzsim smoke test ===" -ForegroundColor White
Write-Host "  app:  $AppDir"
Write-Host "  port: $Port   log: $Log"

# ---- 1. the runtime is self-contained ---------------------------------------
# (json.dumps escapes non-ASCII: the install path may hold umlauts, and a
#  redirected Python stdout does not use the code page .NET reads it with)
$psi = New-CleanStartInfo $Py '-c "import sys, json; print(json.dumps([sys.executable, sys.prefix] + sys.path))"'
$psi.RedirectStandardOutput = $true
$p = [System.Diagnostics.Process]::Start($psi)
$raw = $p.StandardOutput.ReadToEnd()
$p.WaitForExit()
$lines = @()
if ($p.ExitCode -eq 0 -and $raw.Trim()) {
    # assign first: piped, Windows PowerShell 5.1 passes the JSON array on as ONE object
    $parsed = ConvertFrom-Json $raw
    $lines = @(foreach ($entry in $parsed) { if ($entry) { [string]$entry } })
}
$outside = @($lines | Where-Object { -not $_.StartsWith($AppDir, [StringComparison]::OrdinalIgnoreCase) })
Expect ($p.ExitCode -eq 0 -and $lines.Count -ge 3 -and $outside.Count -eq 0) 'runtime is self-contained' `
    $(if ($outside.Count) { "outside the app folder: $($outside -join '; ')" } else { "$($lines.Count - 2) import paths, all inside the app folder" })

# ---- 2. start it like the start-menu entry does ------------------------------
$cmd = "$env:SystemRoot\System32\cmd.exe"
$psi = New-CleanStartInfo $cmd ("/d /c `"`"{0}`" -m netzsim.desktop --no-browser --port {1} > `"{2}`" 2>&1`"" -f $Py, $Port, $Log)
$server = [System.Diagnostics.Process]::Start($psi)

try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $health = $null
    while ($sw.Elapsed.TotalSeconds -lt $StartupTimeout -and -not $server.HasExited) {
        try {
            $r = Invoke-Http GET '/api/health' $null 3
            if ($r.Status -eq 200) { $health = Get-Json $r; if ($health.app -eq 'netzsim') { break } }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    $up = $null -ne $health -and $health.app -eq 'netzsim'
    Expect $up 'server answers /api/health' $(if ($up) { "netzsim $($health.version) after $([int]$sw.Elapsed.TotalSeconds) s" } else { 'no answer' })
    if (-not $up) { throw 'server did not come up' }

    # the installer / uninstaller recognise a running simulator by this mutex
    $mx = $null
    $held = [System.Threading.Mutex]::TryOpenExisting('netzsim-desktop-running', [ref]$mx)
    if ($mx) { $mx.Dispose() }
    Expect $held 'running instance is detectable' 'mutex netzsim-desktop-running (Inno Setup AppMutex)'

    # ---- 3. the UI is delivered by the backend itself -------------------------
    $r = Invoke-Http GET '/'
    $html = Get-Text $r
    Expect ($r.Status -eq 200 -and $html.Contains('id="root"')) 'GET / serves the React app'
    $m = [regex]::Match($html, 'src="(/assets/[^"]+\.js)"')
    if ($m.Success) {
        $js = Invoke-Http GET $m.Groups[1].Value
        Expect ($js.Status -eq 200 -and $js.Bytes.Length -gt 100KB) 'UI bundle is served' ("{0} ({1:N0} KiB)" -f $m.Groups[1].Value, ($js.Bytes.Length / 1KB))
        # a browser refuses a module script that is not typed as JavaScript -
        # and Windows takes MIME types from the registry unless we pin them
        $types = @($js.ContentType)
        $ok = $js.ContentType -like '*javascript*'
        $c = [regex]::Match($html, 'href="(/assets/[^"]+\.css)"')
        if ($c.Success) {
            $css = Invoke-Http GET $c.Groups[1].Value
            $types += $css.ContentType
            $ok = $ok -and $css.Status -eq 200 -and $css.ContentType -like 'text/css*'
        }
        Expect $ok 'UI files carry web MIME types' ($types -join ' | ')
    } else { Fail 'UI bundle is served' 'no /assets/*.js reference in index.html' }

    # ---- a second start joins the running instance instead of starting a twin --
    $psi2 = New-CleanStartInfo $Py "-m netzsim.desktop --no-browser --port $Port"
    $psi2.RedirectStandardOutput = $true
    $second = [System.Diagnostics.Process]::Start($psi2)
    $said = $second.StandardOutput.ReadToEndAsync()
    if ($second.WaitForExit(60000)) {
        Expect ($second.ExitCode -eq 0 -and $said.Result.Contains("127.0.0.1:$Port")) 'second start reuses the instance' "exit code $($second.ExitCode), no second server"
    } else {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $second.Id /T /F 2>&1 | Out-Null
        Fail 'second start reuses the instance' 'the second launcher kept running'
    }
    $r = Invoke-Http GET '/monitor'
    Expect ($r.Status -eq 200 -and (Get-Text $r).Contains('realtime power flow')) 'GET /monitor (built-in monitor)'

    # ---- 4. dataset + manual ---------------------------------------------------
    $grids = (Get-Json (Invoke-Http GET '/api/grids')).grids
    $lib = @($grids | Where-Object { $_.source -eq 'library' -and $_.voltage -eq 'LV' } | Sort-Object nodes)
    $ref = @($grids | Where-Object { $_.source -eq 'reference' })
    Expect ($lib.Count -ge 1 -and $ref.Count -ge 1) 'grid catalog' "$(@($grids).Count) grids ($($lib.Count) library LV, $($ref.Count) reference feeders)"
    $r = Invoke-Http GET '/api/manual'
    Expect ($r.Status -eq 200 -and $r.Bytes.Length -gt 1MB -and $r.ContentType -like '*pdf*') 'user manual (PDF)' ("{0:N1} MB" -f ($r.Bytes.Length / 1MB))
    $arch = Get-Json (Invoke-Http GET '/api/loadgen/archetypes')
    Expect ([bool]$arch.available) 'LPG load library' "$(@($arch.archetypes).Count) archetypes"

    # ---- 5. real physics: a library grid, live ---------------------------------
    $gid = $lib[0].id
    $r = Invoke-Http POST '/api/config/apply' @{ grid_id = $gid } 180
    Expect ($r.Status -eq 200) "apply grid $gid"
    [void](Invoke-Http POST '/api/control/interval?seconds=0.2')
    [void](Invoke-Http POST '/api/control/start')
    $st = Wait-State { param($s) $s.converged -and $s.step -ge 2 } 90
    Expect ($null -ne $st) 'power flow converges, engine ticks' $(if ($st) { "step $($st.step), U {0:N3}..{1:N3} pu, {2:N1} ms/solve" -f $st.summary.vm_pu_min, $st.summary.vm_pu_max, $st.solve_ms })

    # ---- 6. the WebSocket the UI lives on: /ws, exactly as the UI opens it ------
    try {
        $ws = New-Object System.Net.WebSockets.ClientWebSocket
        $ws.Options.Proxy = $null
        $cts = New-Object Threading.CancellationTokenSource 20000
        [void]$ws.ConnectAsync([Uri]"ws://127.0.0.1:$Port/ws", $cts.Token).GetAwaiter().GetResult()
        $buf = New-Object byte[] 262144
        $got = 0
        do {
            $seg = New-Object 'System.ArraySegment[byte]' -ArgumentList $buf, 0, $buf.Length
            $res = $ws.ReceiveAsync($seg, $cts.Token).GetAwaiter().GetResult()
            $got += $res.Count
        } until ($res.EndOfMessage)
        $ws.Abort()
        Expect ($got -gt 100) 'WebSocket /ws streams steps' ("first frame {0:N0} bytes" -f $got)
    } catch { Fail 'WebSocket /ws streams steps' $_.Exception.Message }

    # ---- 7. state estimation (pandapower WLS + scipy) --------------------------
    [void](Invoke-Http POST '/api/measurements/preset?name=substation_trafos')
    $st = Wait-State { param($s) (Has-Prop $s 'estimated') -and $s.estimated -and @($s.estimated.buses).Count -gt 0 } 120
    Expect ($null -ne $st) 'state estimation runs' $(if ($st) { "$(@($st.estimated.buses).Count) estimated buses" })

    # ---- 8. data\ is writable: record a few steps ------------------------------
    $rec = Get-Json (Invoke-Http POST '/api/recording/start' @{ name = 'smoke-test' })
    Start-Sleep -Seconds 2
    [void](Invoke-Http POST '/api/recording/stop')
    $rid = $null
    foreach ($k in 'id', 'active_id', 'recording') { if (Has-Prop $rec $k) { $rid = [string]$rec.$k; break } }
    $stored = @((Get-Json (Invoke-Http GET '/api/recordings')).recordings)
    if (-not $rid -and $stored.Count) { $rid = [string]($stored | Select-Object -Last 1).id }
    $recDir = if ($rid) { Join-Path $AppDir "data\recordings\$rid" } else { $null }
    Expect ([bool]$recDir -and (Test-Path $recDir)) 'recording written into data\' $(if ($recDir) { $recDir.Substring($AppDir.Length + 1) })
    if ($rid) { [void](Invoke-Http DELETE "/api/recordings/$rid") }

    # ---- 9. an IEEE reference feeder (pandapower.networks, exact conversion) ---
    $r = Invoke-Http POST '/api/config/apply' @{ grid_id = 'ieee_33bw' } 180
    [void](Invoke-Http POST '/api/control/start')
    $st = Wait-State { param($s) $s.converged -and $s.summary.n_bus -eq 33 } 60
    Expect ($r.Status -eq 200 -and $null -ne $st) 'IEEE 33-bus reference feeder' $(if ($st) { "U min {0:N4} pu" -f $st.summary.vm_pu_min })
}
catch {
    Fail 'smoke test aborted' $_.Exception.Message
}
finally {
    if ($server -and -not $server.HasExited) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $server.Id /T /F 2>&1 | Out-Null
    }
    Start-Sleep -Milliseconds 500
}

if ($script:Failed) {
    Write-Host "`n$($script:Failed) check(s) FAILED. Last lines of the server log:" -ForegroundColor Red
    if (Test-Path $Log) { Get-Content $Log -Tail 30 | ForEach-Object { Write-Host "    $_" } }
    exit 1
}
Remove-Item $Log -Force -ErrorAction SilentlyContinue
Write-Host "`nAll checks passed." -ForegroundColor Green
exit 0
