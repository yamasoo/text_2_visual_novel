@echo off
setlocal
cd /d "%~dp0"

rem ---- 1. pick the project virtualenv (venv or .venv); create "venv" if neither exists
set "VENV="
if exist venv\Scripts\python.exe set "VENV=venv"
if not defined VENV if exist .venv\Scripts\python.exe set "VENV=.venv"
if not defined VENV (
  echo [setup] no virtualenv found, creating venv ...
  python -m venv venv
  if errorlevel 1 (
    echo [error] could not create venv. Is Python 3.10+ installed and on PATH?
    pause
    exit /b 1
  )
  set "VENV=venv"
)
set "PY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\requirements.stamp"

rem ---- 2. install / update dependencies when requirements.txt changed or an import fails
set "NEED=0"
if not exist "%STAMP%" set "NEED=1"
if exist "%STAMP%" fc /b requirements.txt "%STAMP%" >nul 2>&1
if errorlevel 1 set "NEED=1"
"%PY%" -c "import fastapi, uvicorn, yaml, jsonschema, PIL, openpyxl, fontTools" >nul 2>&1
if errorlevel 1 set "NEED=1"
if "%NEED%"=="1" (
  echo [setup] installing dependencies from requirements.txt ...
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [error] pip install failed. Check the messages above.
    pause
    exit /b 1
  )
  copy /y requirements.txt "%STAMP%" >nul
)

rem ---- 3. first run: create config.yaml from config.example.yaml
if not exist config.yaml (
  echo [setup] first run: creating config.yaml from config.example.yaml ...
  "%PY%" -m t2vn.config --init
  if errorlevel 1 (
    echo [error] could not create config.yaml
    pause
    exit /b 1
  )
  echo [setup] edit config.yaml and set book_library.path to your novel folder.
  echo.
)

rem ---- 4. start the studio with the venv python
echo [run] %PY%
"%PY%" -m t2vn.server %*
if errorlevel 1 pause
