@echo off
rem NetRollout for admins: netrollout start | stop | status | open | logs | help
rem (People use NetRollout Manager; NetRollout Setup installs.)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0netrollout.ps1" %*
exit /b %ERRORLEVEL%
