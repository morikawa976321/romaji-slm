@echo off
rem romaji-slm trial launcher. ASCII only: cmd.exe cannot read UTF-8 batch text reliably,
rem so all messages and the real steps live in trial\run.ps1 (see docs/TRIAL.md).
setlocal
set "PS1=%~dp0trial\run.ps1"
if exist "%PS1%" goto run
set "PS1=%~dp0run.ps1"
if exist "%PS1%" goto run
echo trial\run.ps1 was not found next to run.cmd.
pause
exit /b 1
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
if errorlevel 1 pause
endlocal
