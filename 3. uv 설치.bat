@echo off
rem AI Talk: install uv (needed for MCP servers marked 'uv'). Shows a warning and asks first.
cd /d "%~dp0"
python install_uv.py
echo.
pause
