@echo off
rem DICOM Cloak launcher for Windows. Double-click this file, or run it from a command prompt:
rem   cloak.bat                          asks for the input and output folders
rem   cloak.bat INPUT_FOLDER OUTPUT_FOLDER
rem   cloak.bat check FOLDER  /  cloak.bat selftest  /  cloak.bat --help
rem The work is done by cloak.ps1 (in this folder) inside Docker Desktop.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deid.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
