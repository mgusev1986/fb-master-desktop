# PowerShell аналог ensure-macos-embedded-python.sh для Windows.
# Вкладывает в resources-fb-master-backend переносимый Python 3.12 (indygreg/python-build-standalone)
# и ставит зависимости через pip --prefix, чтобы на Windows клиента не требовалось устанавливать Python.
#
#   powershell -ExecutionPolicy Bypass -File scripts/ensure-windows-embedded-python.ps1 -OutDir "C:\path\to\resources-fb-master-backend"
#
# Запускается только на Windows (в GitHub Actions windows-latest или на локальной Windows-машине/VM).

param(
    [Parameter(Mandatory=$true)]
    [string]$OutDir
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
$Cache = Join-Path $Root '.cache\fbm-embedded-python-windows'
$Rel = '20241219'
$Ver = '3.12.8'

# Windows x64 (Electron Builder собирает x64 .exe по умолчанию)
$File = "cpython-${Ver}+${Rel}-x86_64-pc-windows-msvc-install_only.tar.gz"
$Url = "https://github.com/indygreg/python-build-standalone/releases/download/${Rel}/${File}"

New-Item -ItemType Directory -Force -Path $Cache | Out-Null
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$LocalTar = Join-Path $Cache $File
if (-not (Test-Path $LocalTar)) {
    Write-Host ">>> Скачиваю embedded Python для Windows: $File"
    Invoke-WebRequest -Uri $Url -OutFile "$LocalTar.tmp" -UseBasicParsing
    Move-Item "$LocalTar.tmp" $LocalTar -Force
}

Write-Host ">>> Распаковываю Python в $OutDir\python"
$PyDir = Join-Path $OutDir 'python'
if (Test-Path $PyDir) { Remove-Item -Recurse -Force $PyDir }

# tar входит в Windows 10+ по умолчанию
tar -xzf $LocalTar -C $OutDir
if ($LASTEXITCODE -ne 0) { throw "tar extract failed" }

$PY = Join-Path $OutDir 'python\python.exe'
if (-not (Test-Path $PY)) {
    throw "ОШИБКА: после распаковки нет $PY"
}

Write-Host ">>> pip install -r requirements.txt → $OutDir\python-lib"
$LibDir = Join-Path $OutDir 'python-lib'
if (Test-Path $LibDir) { Remove-Item -Recurse -Force $LibDir }

& $PY -m pip install --upgrade pip -q
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }

$Req = Join-Path $OutDir 'requirements.txt'
& $PY -m pip install -r $Req --prefix $LibDir
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# PYTHONPATH: на Windows site-packages лежит в Lib (с большой L), не lib
$LibSite = Join-Path $LibDir 'Lib\site-packages'
$RtSite = Join-Path $OutDir 'python\Lib\site-packages'
$env:PYTHONPATH = "$LibSite;$RtSite"

$PB = Join-Path $OutDir 'playwright-browsers'
New-Item -ItemType Directory -Force -Path $PB | Out-Null

Write-Host ">>> Playwright: chromium в $PB"
$env:PLAYWRIGHT_BROWSERS_PATH = $PB
& $PY -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "playwright install chromium failed" }

# Проверка: есть ли chrome.exe в распакованном Chromium
$ChromeExe = Get-ChildItem -Path $PB -Filter 'chrome.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $ChromeExe) {
    throw "ОШИБКА: после playwright install в $PB нет chrome.exe."
}

# Маркер что встроенный Python готов — читается local-backend-launcher.js (нужно добавить Windows ветку)
(Get-Date -Format 's') | Out-File (Join-Path $OutDir '.embedded_python') -Encoding utf8 -NoNewline
Write-Host ">>> Встроенный Python для Windows готов."
