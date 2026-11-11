@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo PDF Master Pro is not set up yet. Run setup_windows.bat first.
  exit /b 1
)
if "%PDFMASTER_PORT%"=="" set PDFMASTER_PORT=8000
cd backend
"..\.venv\Scripts\python.exe" -m alembic upgrade head || exit /b 1
echo.
echo PDF Master Pro is starting at http://127.0.0.1:%PDFMASTER_PORT%  (press Ctrl+C to stop)
start "" "http://127.0.0.1:%PDFMASTER_PORT%"
"..\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PDFMASTER_PORT%
endlocal
