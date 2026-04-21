# PowerShell аналог prepare-desktop-backend-bundle.sh для Windows.
# Копирует backend/templates/static в resources-fb-master-backend,
# устанавливает embedded Python + Playwright Chromium под Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts/prepare-desktop-backend-bundle-win.ps1
#
# Запускать на Windows (CI runner windows-latest или локальная Windows-машина/VM).

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Root 'desktop\fb-master-desktop\resources-fb-master-backend'

New-Item -ItemType Directory -Force -Path $Out | Out-Null

# Чистим только backend/templates/static — Python/Chromium переустановим ensure-win-python.ps1
foreach ($sub in @('backend', 'templates', 'static')) {
    $path = Join-Path $Out $sub
    if (Test-Path $path) { Remove-Item -Recurse -Force $path }
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}
foreach ($f in @('main.py', 'requirements.txt', 'launch.json')) {
    $p = Join-Path $Out $f
    if (Test-Path $p) { Remove-Item -Force $p }
}

Write-Host ">>> Копирую backend/templates/static в $Out"
# robocopy возвращает exit 0/1 (успех с копированием / без изменений) — оба OK
robocopy "$Root\backend" "$Out\backend" /MIR /NFL /NDL /NJH /NJS /XD '__pycache__' /XF '*.pyc' | Out-Null
if ($LASTEXITCODE -gt 7) { throw "robocopy backend failed: $LASTEXITCODE" }
robocopy "$Root\templates" "$Out\templates" /MIR /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -gt 7) { throw "robocopy templates failed: $LASTEXITCODE" }
robocopy "$Root\static" "$Out\static" /MIR /NFL /NDL /NJH /NJS /XD 'releases' | Out-Null
if ($LASTEXITCODE -gt 7) { throw "robocopy static failed: $LASTEXITCODE" }

Copy-Item "$Root\main.py" $Out -Force
Copy-Item "$Root\requirements.txt" $Out -Force

@'
{
  "executable": "python/python.exe",
  "appModule": "main:app",
  "port": 8799,
  "startTimeoutMs": 180000,
  "licenseApiBase": "https://socmaster.pro",
  "env": {
    "FB_MASTER_ACCESS_KEY_REQUIRED": "1",
    "FB_MASTER_WEB_CABINET_EMAIL_LOGIN_DISABLED": "1"
  }
}
'@ | Out-File (Join-Path $Out 'launch.json') -Encoding utf8

New-Item -ItemType File -Force -Path (Join-Path $Out '.gitkeep') | Out-Null

# Windows embedded Python + Playwright Chromium
& "$PSScriptRoot\ensure-windows-embedded-python.ps1" -OutDir $Out
if ($LASTEXITCODE -ne 0) { throw "ensure-windows-embedded-python failed" }

Write-Host ">>> Обновлено: $Out"
