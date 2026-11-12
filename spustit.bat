@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
set HF_HUB_DISABLE_XET=1
set HF_HUB_DOWNLOAD_TIMEOUT=120
set HF_HUB_ETAG_TIMEOUT=30

where uv >nul 2>&1
if errorlevel 1 (
  echo Instaluji uv...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

echo Rozlisuji pocitac...
set "HW=INTEL"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0rozpoznat.ps1"`) do set "HW=%%P"

if not exist .venv\Scripts\python.exe (
  echo Vytvarim prostredi .venv, Python 3.12.2
  uv venv --python 3.12.2
  if errorlevel 1 (
    echo Python 3.12.2 nesel, zkousim Python 3.12
    uv venv --python 3.12
  )
)

if not exist .venv\Scripts\python.exe goto fail

call .venv\Scripts\activate.bat
echo Prostredi: %VIRTUAL_ENV%
.venv\Scripts\python.exe -c "import sys; print('Python', sys.version.split()[0])"

echo Instaluji spolecne zavislosti...
uv pip install -r requirements.txt --python .venv\Scripts\python.exe
if errorlevel 1 goto fail

.venv\Scripts\python.exe -c "import torch" 1>nul 2>nul
if errorlevel 1 (
  echo Instaluji PyTorch pro procesor...
  uv pip install torch --index-url https://download.pytorch.org/whl/cpu --python .venv\Scripts\python.exe
  if errorlevel 1 goto fail
)

if /I "%HW%"=="NVIDIA" (
  echo Instaluji CUDA knihovny pro NVIDIA...
  uv pip install -r requirements-cuda.txt --python .venv\Scripts\python.exe
  if errorlevel 1 goto fail
)
if /I "%HW%"=="AMD" (
  echo Instaluji ROCm knihovny pro AMD grafiku...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalovat_amd.ps1"
  if errorlevel 1 goto fail
)
if /I "%HW%"=="INTEL" echo Bez grafickych knihoven. Prepis pobezi na procesoru.
if /I "%HW%"=="INTEL_AI" (
  echo Instaluji OpenVINO pro Intel Arc. Prepis pobezi na grafice.
  uv pip install -r requirements-intel.txt --python .venv\Scripts\python.exe
  if errorlevel 1 goto fail
)

set KMP_DUPLICATE_LIB_OK=TRUE

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0zastupce.ps1"

set PYTHONPATH=src
echo Spoustim program...
python -m muj_projekt.windous
if errorlevel 1 pause
exit /b 0

:fail
echo Instalace selhala.
pause
exit /b 1
