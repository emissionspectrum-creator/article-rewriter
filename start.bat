@echo off
chcp 65001 >nul
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.8+
    pause
    exit /b 1
)

if not exist .env (
    echo [INFO] .env not found. Copy .env.example to .env and set ANTHROPIC_API_KEY.
    echo        Command: copy .env.example .env
    echo.
)

echo Installing dependencies...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [ERROR] pip install failed
    pause
    exit /b 1
)

echo Freeing port 8766...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8766 " ^| findstr "LISTENING" 2^>nul') do (
    taskkill /F /PID %%a >nul 2>&1
)

echo.
echo Server starting at http://127.0.0.1:8766
echo Press Ctrl+C to stop.
echo.
start /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8766'"
python -m uvicorn main:app --host 127.0.0.1 --port 8766
