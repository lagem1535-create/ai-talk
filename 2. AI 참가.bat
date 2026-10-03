@echo off
rem AI Talk: let a terminal AI (Claude Code / Antigravity / Copilot) join a chat room
cd /d "%~dp0"
python ai_agent.py %*
echo.
pause
