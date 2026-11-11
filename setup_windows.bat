@echo off
setlocal
cd /d "%~dp0"
echo === PDF Master Pro: Windows setup ===

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.12 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer, then run this script again.
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 (
  echo Python 3.12 or newer is required. Found:
  python --version
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment in .venv ...
  python -m venv .venv || exit /b 1
)
echo Installing Python dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r backend\requirements.txt || exit /b 1

if not exist ".env" (
  echo Creating .env with a random secret key ...
  ".venv\Scripts\python.exe" -c "import secrets; open('.env','w').write('PDFMASTER_ENVIRONMENT=development\nPDFMASTER_SECRET_KEY=' + secrets.token_urlsafe(48) + '\n')" || exit /b 1
)

echo Creating / upgrading the database ...
pushd backend
"..\.venv\Scripts\python.exe" -m alembic upgrade head || (popd & exit /b 1)
popd

echo.
echo Setup complete. Run start_pdf_master.bat to launch the application.
endlocal
