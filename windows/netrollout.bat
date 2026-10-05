@echo off
rem NetRollout management: netrollout start | stop | status | open | logs | help
rem With no command (the "NetRollout Manager" shortcut): a numbered menu.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0netrollout.ps1" %*
exit /b %ERRORLEVEL%
