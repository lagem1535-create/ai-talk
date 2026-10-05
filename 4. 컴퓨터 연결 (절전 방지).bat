@echo off
rem AI Talk: keep this window open so the phone can start AIs on this computer.
rem --awake stops the computer from going to sleep automatically while this window is open.
rem To also allow commands typed on the phone, run:  "4. ... .bat" --commands
cd /d "%~dp0"
python pc_helper.py --awake %*
echo.
pause
