#requires -Version 5.1
<#
.SYNOPSIS
    Builds the self-contained Windows installer  dist\netzsim-setup-<version>.exe

.DESCRIPTION
    The installer must work on a BLANK Windows PC: no Python, no Node, no
    internet, no admin rights. So everything is bundled:

      stage\
        python\        official CPython "embeddable package" (pinned, SHA256-
                       checked) + every runtime package in Lib\site-packages
        src\netzsim\   the simulator sources
        ui\dist\       the built React UI (the backend serves it itself,
                       see netzsim.api.webui / netzsim.desktop)
        data\          the committed dataset (grids, LPG library, scenarios)
        docs\          Benutzerhandbuch.pdf (Hilfe menu)
        licenses\      third-party notices (the UI libraries' licence texts)
        netzsim.bat    command-line launcher (the start menu calls python.exe)

    and Inno Setup wraps the stage folder into one setup.exe (per-user).

    Needed on the BUILD machine only: Node.js (UI build), a Python of the same
    minor version as the embedded one (runs pip), Inno Setup 6.3+ or 7.

.PARAMETER Version
    Installer version. Default: the version in pyproject.toml.
.PARAMETER Clean
    Start from scratch: wipe build\windows\stage and run "npm ci".
.PARAMETER SkipUi
    Reuse the existing ui\dist instead of rebuilding the UI.
.PARAMETER SkipInstaller
    Stop after staging (no Inno Setup needed) - the stage folder runs as is.
.PARAMETER KeepTests
    Do not prune the test suites of numpy / scipy / pandas / networkx.
.PARAMETER BuildRoot
    Where stage\ and cache\ live. Default: <repo>\build\windows. Point it at a
    short path if the repository itself sits deep (MAX_PATH).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
#>
[CmdletBinding()]
param(
    [string]$Version,
    [switch]$Clean,
    [switch]$SkipUi,
    [switch]$SkipInstaller,
    [switch]$KeepTests,
    [string]$BuildRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # Invoke-WebRequest is 10x faster without it

# ---- pinned inputs ----------------------------------------------------------
# The last CPython line with Windows binaries that the test suite runs on.
# SHA256 as published on https://www.python.org/downloads/release/python-31410/
$PythonVersion = '3.14.10'
$PythonSha256  = '90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159'
# Longest path inside the installed folder we accept. The install prefix
# (C:\Users\<name>\AppData\Local\Programs\netzsim\) needs the rest of MAX_PATH.
$MaxRelPath = 190

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $BuildRoot) { $BuildRoot = Join-Path $Root 'build\windows' }
$Stage = Join-Path $BuildRoot 'stage'
$Cache = Join-Path $BuildRoot 'cache'
$Out   = Join-Path $Root 'dist'
$Site  = Join-Path $Stage 'python\Lib\site-packages'
$PyMinor = ($PythonVersion -split '\.')[0..1] -join '.'

function Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Info([string]$Text) { Write-Host "    $Text" }
function Check([string]$What) {
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}
function Robo([string]$From, [string]$To, [string[]]$Extra) {
    # robocopy: exit codes below 8 mean success
    & robocopy $From $To /E /NFL /NDL /NJH /NJS /NP @Extra | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy $From -> $To failed ($LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
}

$sw = [Diagnostics.Stopwatch]::StartNew()
Write-Host '=== netzsim Windows installer build ===' -ForegroundColor White

# ---- version ----------------------------------------------------------------
# One version, three places: pyproject.toml, netzsim.__version__ (what /health
# and the console banner report) and - on a release build - the tag, passed as
# -Version. All of them must agree: a tag pushed without the version bump
# would otherwise ship a "netzsim-setup-0.2.0.exe" that installs a 0.1.0.
function Read-Version([string]$RelPath, [string]$Pattern) {
    $hit = Select-String -Path (Join-Path $Root $RelPath) -Pattern $Pattern | Select-Object -First 1
    if (-not $hit) { throw "no version found in $RelPath" }
    return $hit.Matches[0].Groups[1].Value
}
$ProjVersion = Read-Version 'pyproject.toml' '^version\s*=\s*"([^"]+)"'
$CodeVersion = Read-Version 'src\netzsim\__init__.py' '^__version__\s*=\s*"([^"]+)"'
if (-not $Version) { $Version = $ProjVersion }
if ($Version -notmatch '^(\d+(?:\.\d+){0,3})') { throw "version '$Version' must start with digits" }
$VersionNumeric = $Matches[1]
if ($ProjVersion -ne $Version -or $CodeVersion -ne $Version) {
    throw "version mismatch: installer $Version, pyproject.toml $ProjVersion, src\netzsim\__init__.py $CodeVersion - bump both files before tagging."
}
Info "version $Version   (repo: $Root)"

# ---- host Python (runs pip; must match the embedded minor version) ----------
Step "Host Python $PyMinor"
$HostPy = $null
$old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
if (Get-Command py -ErrorAction SilentlyContinue) {
    $p = & py "-$PyMinor" -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $p) { $HostPy = ([string]$p).Trim() }
}
if (-not $HostPy) {
    foreach ($name in 'python', 'python3') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $v = & $cmd.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and ([string]$v).Trim() -eq $PyMinor) { $HostPy = $cmd.Source; break }
    }
}
$ErrorActionPreference = $old; $global:LASTEXITCODE = 0
if (-not $HostPy) {
    throw "Python $PyMinor is needed on the build machine (it runs pip for the embedded $PythonVersion runtime)."
}
Info $HostPy

# ---- UI ---------------------------------------------------------------------
if ($SkipUi) {
    Step 'React UI - reusing ui\dist (-SkipUi)'
} else {
    Step 'React UI (npm run build)'
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw 'Node.js / npm is needed to build the UI.' }
    Push-Location (Join-Path $Root 'ui')
    try {
        if ($Clean -or -not (Test-Path 'node_modules')) {
            & npm ci --no-fund --no-audit; Check 'npm ci'
        }
        & npm run build; Check 'npm run build'
    } finally { Pop-Location }
}
if (-not (Test-Path (Join-Path $Root 'ui\dist\index.html'))) { throw 'ui\dist\index.html is missing - build the UI first.' }

# ---- stage: embedded Python -------------------------------------------------
Step "Embedded Python $PythonVersion"
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Force $Stage, (Join-Path $Cache 'python-embed') | Out-Null

$zipName = "python-$PythonVersion-embed-amd64.zip"
$zip = Join-Path $Cache "python-embed\$zipName"
if (-not (Test-Path $zip)) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $url = "https://www.python.org/ftp/python/$PythonVersion/$zipName"
    Info "downloading $url"
    Invoke-WebRequest -UseBasicParsing $url -OutFile $zip
}
$sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if ($sha -ne $PythonSha256) {
    Remove-Item $zip -Force
    throw "SHA256 of $zipName is $sha, expected $PythonSha256 - download discarded."
}
Info "$zipName verified (sha256 $($sha.Substring(0, 16))...)"
Expand-Archive -Path $zip -DestinationPath (Join-Path $Stage 'python')

# ---- stage: Python packages -------------------------------------------------
Step 'Python packages (pip install --target)'
New-Item -ItemType Directory -Force $Site | Out-Null
# --no-compile: bytecode would embed THIS machine's paths; the target compiles
# on first start (its install folder is writable).
& $HostPy -m pip install --disable-pip-version-check --no-compile --no-warn-script-location `
    --target $Site -r (Join-Path $Root 'requirements.txt'); Check 'pip install'

Remove-Item (Join-Path $Site 'bin') -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $Site -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
if (-not $KeepTests) {
    $pruned = 0
    foreach ($pkg in 'numpy', 'scipy', 'pandas', 'networkx') {
        $dir = Join-Path $Site $pkg
        if (-not (Test-Path $dir)) { continue }
        foreach ($t in Get-ChildItem $dir -Recurse -Directory -Filter 'tests') {
            if (Test-Path $t.FullName) {
                $pruned += (Get-ChildItem $t.FullName -Recurse -File | Measure-Object Length -Sum).Sum
                Remove-Item $t.FullName -Recurse -Force
            }
        }
    }
    Info ("pruned {0:N0} MB of bundled test suites" -f ($pruned / 1MB))
}

# The embeddable distribution takes its import path from python<XY>._pth and
# ignores PYTHONPATH/PYTHONHOME and the user site - exactly the isolation a
# shipped runtime wants. "import site" is only switched on when a package
# needs its .pth file processed.
$pth = Get-ChildItem (Join-Path $Stage 'python') -Filter 'python*._pth' | Select-Object -First 1
if (-not $pth) { throw 'no python*._pth in the embeddable package' }
$zipLib = (Get-ChildItem (Join-Path $Stage 'python') -Filter 'python*.zip' | Select-Object -First 1).Name
$lines = @($zipLib, '.', 'Lib\site-packages', '..\src')
if (Get-ChildItem $Site -Filter '*.pth' -File) {
    $lines += 'import site'
    Info 'a package ships a .pth file - "import site" enabled'
}
[IO.File]::WriteAllLines($pth.FullName, $lines, (New-Object Text.ASCIIEncoding))

# ---- stage: application -----------------------------------------------------
Step 'Application files'
Robo (Join-Path $Root 'src\netzsim') (Join-Path $Stage 'src\netzsim') @('/XD', '__pycache__')
Robo (Join-Path $Root 'ui\dist') (Join-Path $Stage 'ui\dist') @()

# data\: exactly the committed dataset - never local recordings / own grids
$tracked = $null
if (Get-Command git -ErrorAction SilentlyContinue) {
    # ask git itself (a worktree or submodule has a .git FILE, a source
    # archive has nothing at all)
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $inside = & git -C $Root rev-parse --is-inside-work-tree 2>$null
    $isRepo = ($LASTEXITCODE -eq 0 -and ([string]$inside).Trim() -eq 'true')
    $ErrorActionPreference = $old; $global:LASTEXITCODE = 0
    if ($isRepo) { $tracked = & git -C $Root ls-files -- data; Check 'git ls-files' }
}
# Pictures under data\ are reference material, not dataset - nothing reads
# them at runtime, and a photo is not ours to redistribute. They stay out.
$Pictures = '\.(jpe?g|png|gif|webp|bmp|tiff?)$'
if ($tracked) {
    $all = @($tracked).Count
    $tracked = @($tracked | Where-Object { $_ -notmatch $Pictures })
    foreach ($rel in $tracked) {
        $dst = Join-Path $Stage $rel
        New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
        Copy-Item -LiteralPath (Join-Path $Root $rel) -Destination $dst
    }
    Info "data: $($tracked.Count) tracked files ($($all - $tracked.Count) picture(s) left out)"
} else {
    Robo (Join-Path $Root 'data') (Join-Path $Stage 'data') `
        @('/XD', 'user_grids', 'recordings', '/XF', '*.jpg', '*.jpeg', '*.png', '*.gif', '*.webp', '*.bmp', '*.tif', '*.tiff')
    Info 'data: copied (no git metadata - excluded user_grids, recordings and pictures)'
}

New-Item -ItemType Directory -Force (Join-Path $Stage 'docs') | Out-Null
Copy-Item (Join-Path $Root 'docs\Benutzerhandbuch.pdf') (Join-Path $Stage 'docs')
Copy-Item (Join-Path $Root 'LICENSE') $Stage

# Third-party notices. Python and its packages bring their licence texts along
# (python\LICENSE.txt, every *.dist-info). The UI bundle does not: the minifier
# strips the headers of React, Leaflet, i18next ..., and MIT / BSD ask for the
# notice to accompany every copy - so collect them from node_modules.
if (-not (Test-Path (Join-Path $Root 'ui\node_modules'))) {
    throw 'ui\node_modules is missing - run "npm ci" in ui\ (the installer ships the licence texts of the bundled UI libraries).'
}
$licOut = & $HostPy -B (Join-Path $PSScriptRoot 'collect_ui_licenses.py') (Join-Path $Root 'ui') (Join-Path $Stage 'licenses\ui')
Check 'collect_ui_licenses.py'
Info ([string]$licOut)
[IO.File]::WriteAllLines((Join-Path $Stage 'licenses\README.txt'), @(
    'Third-party software in this installation',
    '=========================================',
    '',
    'netzsim itself          LICENSE (in the installation folder)',
    'Python runtime          python\LICENSE.txt',
    'Python packages         python\Lib\site-packages\<package>-<version>.dist-info\',
    '                        (LICENSE / licenses\ of each package; the full list is',
    '                        python\bundled-packages.txt)',
    'JavaScript libraries    licenses\ui\   (compiled into ui\dist: React, Leaflet,',
    '                        i18next and what they depend on)',
    'Grid and load data      data\DATASET.md'
), (New-Object Text.ASCIIEncoding))
Copy-Item (Join-Path $Root '.env.example') $Stage
Copy-Item (Join-Path $PSScriptRoot 'netzsim.ico') $Stage
Copy-Item (Join-Path $PSScriptRoot 'netzsim.bat') $Stage

& $HostPy -m pip list --disable-pip-version-check --path $Site --format freeze |
    Set-Content -Encoding ascii (Join-Path $Stage 'python\bundled-packages.txt'); Check 'pip list'

# ---- stage: verify ----------------------------------------------------------
Step 'Verify the staged runtime'
# (single quotes inside the Python code: Windows PowerShell 5.1 mangles
#  embedded double quotes in native-command arguments)
$probe = 'import sys, netzsim, pandapower, scipy, numpy, pandas, fastapi, uvicorn; ' +
         'print(sys.version.split()[0], ''| pandapower'', pandapower.__version__, ''| numpy'', numpy.__version__, ''| netzsim'', netzsim.__version__)'
# -B: the probe must not leave bytecode behind - .pyc files record the path of
# THIS build folder and would end up in the installer.
$probeOut = & (Join-Path $Stage 'python\python.exe') -B -c $probe; Check 'import check with the embedded interpreter'
Info ([string]$probeOut)
$caches = @(Get-ChildItem $Stage -Recurse -Directory -Filter '__pycache__')
if ($caches.Count) {
    $caches | Remove-Item -Recurse -Force
    Info "removed $($caches.Count) __pycache__ folders from the stage"
}

$files = Get-ChildItem $Stage -Recurse -File
$longest = $files | Sort-Object { $_.FullName.Length } -Descending | Select-Object -First 1
$rel = $longest.FullName.Substring($Stage.Length + 1)
Info ("{0:N0} files, {1:N0} MB; longest relative path {2} chars" -f $files.Count, (($files | Measure-Object Length -Sum).Sum / 1MB), $rel.Length)
if ($rel.Length -gt $MaxRelPath) {
    throw "path too long for a per-user install ($($rel.Length) > $MaxRelPath): $rel"
}

if ($SkipInstaller) {
    Write-Host "`nStage ready (-SkipInstaller): $Stage" -ForegroundColor Green
    Write-Host "Try it:  $Stage\netzsim.bat"
    exit 0
}

# ---- Inno Setup -------------------------------------------------------------
Step 'Inno Setup'
$bases = @((Join-Path $env:LOCALAPPDATA 'Programs'), $env:ProgramFiles, ${env:ProgramFiles(x86)})
$candidates = foreach ($v in '7', '6') {
    foreach ($base in $bases) {
        if ($base) { Join-Path $base "Inno Setup $v\ISCC.exe" }
    }
}
$Iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) { $Iscc = $cmd.Source }
}
if (-not $Iscc) {
    throw 'Inno Setup (ISCC.exe) not found - run packaging\windows\install_innosetup.ps1 or install it from https://jrsoftware.org/isdl.php'
}
Info $Iscc
New-Item -ItemType Directory -Force $Out | Out-Null
& $Iscc /Qp "/DAppVersion=$Version" "/DAppVersionNumeric=$VersionNumeric" `
    "/DStageDir=$Stage" "/DOutputDir=$Out" (Join-Path $PSScriptRoot 'netzsim.iss'); Check 'ISCC'

$setup = Join-Path $Out "netzsim-setup-$Version.exe"
if (-not (Test-Path $setup)) { throw "ISCC reported success but $setup is missing" }
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
Write-Host ("`nInstaller ready in {0:N0} s:" -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green
Write-Host "  $setup"
Write-Host ("  {0:N1} MB   sha256 {1}" -f ((Get-Item $setup).Length / 1MB), $hash)
