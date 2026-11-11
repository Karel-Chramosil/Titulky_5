$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "Chybi .venv. Nejdriv spustit.bat."
}
Set-Location $root

function Test-AmdGpu {
    $output = & $python -c "import os,sys; sys.path.insert(0,'src'); os.environ['KMP_DUPLICATE_LIB_OK']='TRUE'; from muj_projekt.windous import prepare_gpu; backend=prepare_gpu(); print('GPU', backend or 'nic'); raise SystemExit(0 if backend=='hip' else 1)"
    if ($output) { Write-Host $output }
    return $LASTEXITCODE -eq 0
}

if (Test-AmdGpu) {
    Write-Host "AMD grafika uz je pripravena."
    exit 0
}

Write-Host "Instaluji ROCm runtime..."
uv pip install -r (Join-Path $root "requirements-amd.txt") --python $python
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$version = "4.9.0"
$cache = Join-Path $env:LOCALAPPDATA "Titulky5"
New-Item -ItemType Directory -Force -Path $cache | Out-Null
$zip = Join-Path $cache "rocm-python-wheels-Windows-$version.zip"
$url = "https://github.com/OpenNMT/CTranslate2/releases/download/v$version/rocm-python-wheels-Windows.zip"
if (-not (Test-Path $zip)) {
    Write-Host "Stahuji ctranslate2 $version pro AMD..."
    & curl.exe -L --fail --retry 3 -o $zip $url
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$dest = Join-Path $cache "ctranslate2-rocm-$version"
if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
Expand-Archive -Path $zip -DestinationPath $dest
$wheel = Get-ChildItem -Path $dest -Recurse -Filter "ctranslate2-*-cp312-cp312-win_amd64.whl" | Select-Object -First 1
if (-not $wheel) {
    Write-Error "Ve vydani $version neni wheel pro Python 3.12."
}

Write-Host "Nahrazuji ctranslate2 ROCm vydanim..."
uv pip install --reinstall --no-deps $wheel.FullName --python $python
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-AmdGpu)) {
    Write-Host "AMD grafika se neozvala. Prepis by spadl zpet na procesor."
    exit 1
}
Write-Host "AMD grafika je pripravena."
