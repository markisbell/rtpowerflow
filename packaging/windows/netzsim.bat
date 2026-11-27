@echo off
rem netzsim starten (installierte Version): Simulator und Oberflaeche in EINEM
rem Prozess, mit dem mitgelieferten Python - es wird nichts weiter benoetigt.
rem Der Startmenue-Eintrag ruft dasselbe direkt auf. Optionen: --port N, --no-browser
cd /d "%~dp0"
"%~dp0python\python.exe" -m netzsim.desktop %*
