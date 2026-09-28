# Build a release: dist\QuickSub\ (PyInstaller folder), dist\QuickSub-Setup-<ver>.exe and dist\QuickSub-<ver>-portable.zip.
# Usage:  powershell -ExecutionPolicy Bypass -File build_release.ps1 -Version 1.0.0 [-Test]
#   -Test builds the installer as TESTBUILD (different AppId, no Start menu / Send to shortcuts).
# Needs: .venv with requirements.txt + pyinstaller (see README), and Inno Setup 6.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [switch]$Test
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

# The app shows its version and checks GitHub for newer ones, so they must match.
$line = Select-String -Path "$Root\qs\common.py" -Pattern '^APP_VERSION = "(.+)"' | Select-Object -First 1
if (-not $line -or $line.Matches[0].Groups[1].Value -ne $Version) { throw "APP_VERSION in qs\common.py is not $Version" }

Write-Host "== QuickSub (PyInstaller)"
$data = @("icon.ico", "icon-48.png", "icon-96.png") | ForEach-Object { "--add-data=$Root\$_;." }
$collect = @("faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers", "opencc", "tkinterdnd2", "soundcard") | ForEach-Object { "--collect-all=$_" }
& $Py -m PyInstaller --noconfirm --onedir --windowed --name QuickSub --icon "$Root\icon.ico" @data @collect `
    --exclude-module nvidia --distpath "$Root\dist" --workpath "$Root\build_pyi" --specpath "$Root\build_pyi" "$Root\quicksub.pyw"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "== portable zip"
$zip = "$Root\dist\QuickSub-$Version-portable.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$Root\dist\QuickSub" -DestinationPath $zip -CompressionLevel Optimal

Write-Host "== installer"
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found" }
$defs = @("/DAppVersion=$Version")
if ($Test) { $defs += "/DTESTBUILD" }
& $iscc /Q @defs "$Root\installer\quicksub.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
Get-ChildItem "$Root\dist" -File | ForEach-Object { "{0,-36} {1,10:N0} KB" -f $_.Name, ($_.Length / 1KB) }
