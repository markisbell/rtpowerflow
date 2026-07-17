@echo off
setlocal
rem ============================================================
rem  netzsim Starter: Backend (FastAPI) + UI (Vite)
rem  Doppelklick genuegt. Die Ports sind NICHT fest verdrahtet:
rem  Standard 8000/5173 (oder NETZSIM_PORT / NETZSIM_UI_PORT aus
rem  Umgebung/.env); ist ein Port von einer FREMDEN Anwendung
rem  belegt (z. B. rtheatflow), wird automatisch der naechste
rem  freie Port genommen. Ein bereits laufendes netzsim wird an
rem  /health ("app": "netzsim") erkannt und wiederverwendet.
rem ============================================================
cd /d "%~dp0"

echo === netzsim Starter ===

rem ---------- Backend-Port bestimmen ----------
for /f "tokens=1* delims==" %%a in ('powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pick_ports.ps1 -Mode api') do (
    if "%%a"=="PORT" set API_PORT=%%b
    if "%%a"=="REUSE" set API_REUSE=%%b
)
if not defined API_PORT (
    echo FEHLER: Kein freier Backend-Port gefunden.
    pause
    exit /b 1
)

if "%API_REUSE%"=="1" (
    echo Backend laeuft bereits auf Port %API_PORT% - wird wiederverwendet.
    goto backend_up
)
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo FEHLER: Die virtuelle Umgebung .venv fehlt. Einmalig anlegen mit:
    echo    python -m venv .venv
    echo    .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo Starte Backend auf http://localhost:%API_PORT% ...
start "netzsim Backend :%API_PORT%" cmd /k "set PYTHONPATH=src&& set NETZSIM_PORT=%API_PORT%&& .venv\Scripts\python.exe -m netzsim.main"

rem ---------- warten, bis das Backend antwortet (max. ~60 s) ----------
rem (Kaltstart laedt pandapower/numba - das kann 20-60 s dauern.)
rem Identitaet pruefen, nicht nur Erreichbarkeit: bindet in der
rem Kaltstart-Luecke eine FREMDE Anwendung den Port (Start-Rennen
rem zweier Launcher), antwortet dort jemand ohne "netzsim" -> Abbruch
rem statt die UI still an das falsche Backend zu verdrahten.
echo Warte auf das Backend ...
for /l %%i in (1,1,60) do (
    powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 ('http://127.0.0.1:%API_PORT%/health'); if ($r.Content -match 'netzsim') { exit 0 } else { exit 2 } } catch { exit 1 }" >nul 2>&1
    if errorlevel 2 goto port_stolen
    if not errorlevel 1 goto backend_up
    ping -n 2 127.0.0.1 >nul
)
echo WARNUNG: Backend antwortet noch nicht - Fenster "netzsim Backend" pruefen.
goto backend_up

:port_stolen
echo.
echo FEHLER: Port %API_PORT% wurde waehrend des Starts von einer fremden
echo Anwendung belegt (Antwort ist kein netzsim). Das Fenster "netzsim
echo Backend" zeigt vermutlich "address already in use" - bitte den
echo Starter einfach erneut ausfuehren (er waehlt dann den naechsten
echo freien Port).
pause
exit /b 1

:backend_up
rem ---------- UI-Port bestimmen (nach dem Backend, damit der ----------
rem ---------- Proxy-Check gegen ein lebendes Backend laeuft)  ----------
for /f "tokens=1* delims==" %%a in ('powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pick_ports.ps1 -Mode ui') do (
    if "%%a"=="PORT" set UI_PORT=%%b
    if "%%a"=="REUSE" set UI_REUSE=%%b
)
if not defined UI_PORT (
    echo FEHLER: Kein freier UI-Port gefunden.
    pause
    exit /b 1
)

if "%UI_REUSE%"=="1" (
    echo UI laeuft bereits auf Port %UI_PORT% - wird wiederverwendet.
    goto browser
)
if not exist "ui\node_modules" (
    echo Hinweis: ui\node_modules fehlt - installiere einmalig die Abhaengigkeiten...
    pushd ui
    call npm install
    popd
)
echo Starte UI auf http://localhost:%UI_PORT% (Backend-Proxy: Port %API_PORT%) ...
start "netzsim UI :%UI_PORT%" cmd /k "cd /d ui && set NETZSIM_PORT=%API_PORT%&& set NETZSIM_UI_PORT=%UI_PORT%&& npm run dev"

rem ---------- kurz warten, bis UNSERE UI antwortet (max. ~30 s) ----------
rem Identitaetscheck durch den Vite-Proxy (/api/health muss netzsim
rem melden); ein blosser Lausch-Check koennte eine fremde UI treffen,
rem die den Port in der Startluecke uebernommen hat. Beide Loopbacks
rem probieren - Vite bindet unter Windows ::1.
for /l %%i in (1,1,30) do (
    powershell -NoProfile -Command "foreach ($h in '127.0.0.1','[::1]') { try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 ('http://' + $h + ':%UI_PORT%/api/health')).Content -match 'netzsim') { exit 0 } } catch { } }; exit 1" >nul 2>&1
    if not errorlevel 1 goto browser
    ping -n 2 127.0.0.1 >nul
)
echo WARNUNG: UI antwortet noch nicht (oder ein fremder Dienst haelt
echo Port %UI_PORT%) - Fenster "netzsim UI" pruefen: dort steht, auf
echo welchem Port Vite tatsaechlich gestartet ist.

:browser
start "" http://localhost:%UI_PORT%/
echo.
echo Fertig: UI unter http://localhost:%UI_PORT% (Backend: http://localhost:%API_PORT%)
echo Zum Beenden: stop_netzsim.bat ausfuehren oder die beiden Serverfenster schliessen.
ping -n 6 127.0.0.1 >nul
endlocal
