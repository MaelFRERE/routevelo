@echo off
setlocal
cd /d "%~dp0"

set "PY_CMD="
where py >nul 2>nul
if %errorlevel%==0 set "PY_CMD=py -3"

if not defined PY_CMD (
  where python >nul 2>nul
  if %errorlevel%==0 set "PY_CMD=python"
)

if not defined PY_CMD (
  echo Python 3 est necessaire pour lancer RouteVelo.
  echo Installez Python puis relancez ce fichier.
  pause
  exit /b 1
)

rem Lance le serveur dans une fenetre minimisee. Le serveur ouvre ensuite le navigateur.
start "RouteVelo Server" /min cmd /c "%PY_CMD% server.py"

rem Laisse quelques instants au serveur pour demarrer, puis ferme ce lanceur.
timeout /t 1 /nobreak >nul
exit /b 0
