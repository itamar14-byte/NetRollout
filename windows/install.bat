@echo off
rem NetRollout: install it in the folder this zip was extracted to.
rem Double-click this file. (Bypass: Windows blocks unsigned PowerShell scripts by default.)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0netrollout.ps1" install -PauseAtEnd %*
exit /b %ERRORLEVEL%
