@echo off
setlocal
rem ============================================================
rem  netzsim Stopper: beendet Backend, UI und verwaiste
rem  Hintergrundprozesse (node/esbuild/python) DIESES Projekts.
rem  Bewusst port-unabhaengig: der Starter waehlt Ports dynamisch
rem  (scripts\pick_ports.ps1), erkannt wird deshalb ueber
rem  Fenstertitel und Prozess-Kommandozeile. Ein parallel
rem  laufendes rtheatflow (8001/5174) bleibt unberuehrt.
rem ============================================================
cd /d "%~dp0"

echo === netzsim Stopper ===

rem ---------- Serverfenster schliessen (samt Kindprozessen) ----------
taskkill /FI "WINDOWTITLE eq netzsim Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq netzsim UI*" /T /F >nul 2>&1

rem ---------- verwaiste Prozesse dieses Projekts beenden ----------
rem Erkennung ueber die KOMMANDOZEILE, nicht den Exe-Pfad: Vite laeuft
rem als "...\nodejs\node.exe <repo>\ui\...\vite.js", das Backend als
rem relatives ".venv\Scripts\python.exe -m netzsim.main" - beide
rem entgehen einem Exe-Pfad-Filter. Findet auch Server auf dynamisch
rem gewaehlten Ports und Reste abgestuerzter Vite-Instanzen (esbuild).
powershell -NoProfile -Command "$root = (Get-Location).Path; Get-CimInstance Win32_Process | Where-Object { ($_.Name -in 'node.exe','esbuild.exe','python.exe') -and ( $_.CommandLine -like ('*' + $root + '*') -or $_.CommandLine -like '*netzsim.main*' ) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

rem ---------- Ergebnis pruefen ----------
powershell -NoProfile -Command "$root = (Get-Location).Path; $left = @(Get-CimInstance Win32_Process | Where-Object { ($_.Name -in 'node.exe','esbuild.exe','python.exe') -and ( $_.CommandLine -like ('*' + $root + '*') -or $_.CommandLine -like '*netzsim.main*' ) }); if ($left.Count) { Write-Host ('WARNUNG: ' + $left.Count + ' netzsim-Prozess(e) laufen noch.') } else { Write-Host 'Alle netzsim-Dienste beendet.' }"
ping -n 4 127.0.0.1 >nul
endlocal
