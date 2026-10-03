@echo off
rem AI Talk: start the page server if it is not running, then open http://localhost:8765
rem (Google sign-in only works on the localhost address, so the page is not opened as a file.)
cd /d "%~dp0"
set "URL=http://localhost:8765"
set "PING=http://127.0.0.1:8765/"
curl -s -f -o nul --max-time 2 %PING%
if not errorlevel 1 goto open
echo Starting AI Talk page server...
start "AI Talk" cmd /k python server.py --no-browser
set /a TRIES=0
:wait
ping -n 2 127.0.0.1 >nul
curl -s -f -o nul --max-time 2 %PING%
if not errorlevel 1 goto open
set /a TRIES+=1
if %TRIES% lss 15 goto wait
:open
start "" "%URL%"
