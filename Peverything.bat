@echo off
rem Launch the Peverything window without a console window.
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    start "" python -m peverything gui
) else (
    start "" pythonw -m peverything gui
)
