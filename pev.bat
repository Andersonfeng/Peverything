@echo off
rem Peverything CLI launcher - works from the repository folder without installing.
setlocal
cd /d "%~dp0"
python -m peverything %*
