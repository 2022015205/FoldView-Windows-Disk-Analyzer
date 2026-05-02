Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location -LiteralPath $ProjectDir

function Test-PyInstaller {
    param([string]$PythonCommand)
    try {
        & $PythonCommand -m PyInstaller --version *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

$Candidates = @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "py",
    "python"
) | Where-Object { $_ -and (Get-Command $_ -ErrorAction SilentlyContinue) }

$PythonCommand = $Candidates | Where-Object { Test-PyInstaller $_ } | Select-Object -First 1

if (-not $PythonCommand) {
    $PythonCommand = $Candidates | Select-Object -First 1
}

if (-not $PythonCommand) {
    throw "Python was not found. Please install Python 3.10+ first."
}

if (-not (Test-PyInstaller $PythonCommand)) {
    & $PythonCommand -m pip install pyinstaller
}

& $PythonCommand -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --name FoldView `
    "$ProjectDir\foldview.py"

Write-Host "Build complete: dist\FoldView.exe"
