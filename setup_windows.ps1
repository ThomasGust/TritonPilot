[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$SkipGStreamerCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Find-PythonCommand {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return @($py.Source, "-3")
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @($python.Source)
    }

    throw "Python 3 was not found. Install Python 3.10+ first, then re-run this script."
}

function Find-GstLaunch {
    if ($env:GST_LAUNCH -and (Test-Path $env:GST_LAUNCH)) {
        return $env:GST_LAUNCH
    }

    $cmd = Get-Command gst-launch-1.0.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $cmd = Get-Command gst-launch-1.0 -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidates = @(
        "C:\gstreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe",
        "C:\gstreamer\1.0\mingw_x86_64\bin\gst-launch-1.0.exe",
        "C:\Program Files\GStreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe",
        "C:\Program Files\GStreamer\1.0\bin\gst-launch-1.0.exe"
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    return $null
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $repoRoot ".venv"
$requirementsFile = Join-Path $repoRoot "requirements-windows.txt"
$pythonCmd = Find-PythonCommand
$pythonExe = $pythonCmd[0]
$pythonArgs = @()
if ($pythonCmd.Length -gt 1) {
    $pythonArgs = $pythonCmd[1..($pythonCmd.Length - 1)]
}

Write-Step "Using repository root $repoRoot"

if (-not (Test-Path $requirementsFile)) {
    throw "Missing requirements file: $requirementsFile"
}

if ((Test-Path $venvDir) -and $Force) {
    Write-Step "Removing existing virtual environment"
    Remove-Item -Recurse -Force $venvDir
}

if (-not (Test-Path $venvDir)) {
    Write-Step "Creating virtual environment"
    & $pythonExe @pythonArgs -m venv $venvDir
}
else {
    Write-Step "Reusing existing virtual environment"
}

$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Virtual environment python not found: $venvPython"
}

Write-Step "Upgrading pip tooling"
& $venvPython -m pip install --upgrade pip setuptools wheel

Write-Step "Installing Python dependencies"
& $venvPython -m pip install -r $requirementsFile

Write-Step "Verifying Python imports"
& $venvPython -c "import PyQt6, zmq, pygame, numpy, cv2; print('Python packages verified')"

if (-not $SkipGStreamerCheck) {
    Write-Step "Checking for GStreamer"
    $gstLaunch = Find-GstLaunch
    if ($gstLaunch) {
        Write-Host "Found gst-launch-1.0 at: $gstLaunch" -ForegroundColor Green
    }
    else {
        Write-Warning "GStreamer was not found. The GUI can start, but live video will not work until GStreamer 1.0 is installed."
        Write-Host "Install GStreamer 1.0 on Windows, then either add its bin directory to PATH or set GST_LAUNCH to gst-launch-1.0.exe." -ForegroundColor Yellow
    }
}

Write-Step "Setup complete"
Write-Host "Activate the virtual environment with:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host ""
Write-Host "Then launch the app with:" -ForegroundColor Green
Write-Host "  python .\main_topside.py"
