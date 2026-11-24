@echo off
cd /d "%~dp0"
echo Starting Jarvis from: %cd%
if not exist jarvis_hud.html echo WARNING: jarvis_hud.html is missing from this folder!
python jarvis.py
echo.
echo Jarvis has stopped. Any error message is shown above.
pause
