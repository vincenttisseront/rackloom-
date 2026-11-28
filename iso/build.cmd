@echo off
REM Lance le build ISO Windows (Docker Desktop, Linux containers).
REM Usage : iso\build.cmd
REM         iso\build.cmd -SkipImages
REM         iso\build.cmd -TestSeed
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
