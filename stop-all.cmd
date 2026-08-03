@echo off
cd /d "%~dp0"
REM 实际逻辑统一放在 PowerShell 脚本中，CMD 只负责兼容双击和透传全部参数。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-all.ps1" %*
exit /b %ERRORLEVEL%
