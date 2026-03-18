[CmdletBinding()]
param(
    [switch]$Force,
    [Alias("SkipGStreamerCheck")]
    [switch]$SkipSystemDeps,
    [switch]$SkipPythonInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-CommandPath {
    param([string[]]$Names)
    foreach ($name in $Names) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            return $cmd.Source
        }
    }
    return $null
}

function Find-PackageManager {
    $wingetCmd = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($wingetCmd) {
        try {
            $null = & $wingetCmd.Source --version
            return @{
                Name = "winget"
                Command = $wingetCmd.Source
            }
        }
        catch {
        }
    }

    $chocoCmd = Get-Command choco.exe -ErrorAction SilentlyContinue
    if ($chocoCmd) {
        try {
            $null = & $chocoCmd.Source --version
            return @{
                Name = "choco"
                Command = $chocoCmd.Source
            }
        }
        catch {
        }
    }

    return $null
}

function Install-WithWinget {
    param(
        [string]$WingetPath,
        [string]$PackageId
    )

    & $WingetPath install --id $PackageId --exact --accept-package-agreements --accept-source-agreements
}

function Install-WithChocolatey {
    param(
        [string]$ChocoPath,
        [string]$PackageName
    )

    & $ChocoPath install $PackageName -y
}

function Find-PythonCommand {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return @{
            Exe = $py.Source
            Args = @("-3")
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @{
            Exe = $python.Source
            Args = @()
        }
    }

    $searchPatterns = @(
        "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe",
        "C:\Program Files\Python*\python.exe",
        "C:\Python*\python.exe"
    )
    $candidates = @()
    foreach ($pattern in $searchPatterns) {
        $candidates += Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue
    }
    $candidate = $candidates | Sort-Object FullName -Descending | Select-Object -First 1
    if ($candidate) {
        return @{
            Exe = $candidate.FullName
            Args = @()
        }
    }

    return $null
}

function Find-GstLaunch {
    if ($env:GST_LAUNCH -and (Test-Path $env:GST_LAUNCH)) {
        return $env:GST_LAUNCH
    }

    $cmdPath = Test-CommandPath @("gst-launch-1.0.exe", "gst-launch-1.0")
    if ($cmdPath) {
        return $cmdPath
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

function Ensure-SystemPackage {
    param(
        [string]$DisplayName,
        [ScriptBlock]$Probe,
        [hashtable]$PackageManager,
        [string]$WingetId,
        [string]$ChocoName
    )

    $existing = & $Probe
    if ($existing) {
        Write-Host "$DisplayName already available at: $existing" -ForegroundColor Green
        return $existing
    }

    if (-not $PackageManager) {
        throw "$DisplayName is not installed and no supported package manager (winget/choco) is available."
    }

    Write-Step "Installing $DisplayName with $($PackageManager.Name)"
    if ($PackageManager.Name -eq "winget") {
        Install-WithWinget -WingetPath $PackageManager.Command -PackageId $WingetId
    }
    elseif ($PackageManager.Name -eq "choco") {
        Install-WithChocolatey -ChocoPath $PackageManager.Command -PackageName $ChocoName
    }
    else {
        throw "Unsupported package manager: $($PackageManager.Name)"
    }

    $installed = & $Probe
    if (-not $installed) {
        throw "$DisplayName was not detected after installation. Open a new PowerShell session and run setup_windows.ps1 again."
    }

    Write-Host "$DisplayName installed successfully: $installed" -ForegroundColor Green
    return $installed
}

function Ensure-PythonCommand {
    param(
        [hashtable]$PackageManager,
        [switch]$SkipInstall
    )

    $pythonCmd = Find-PythonCommand
    if ($pythonCmd) {
        return $pythonCmd
    }

    if ($SkipInstall) {
        throw "Python 3.10+ was not found. Install it first, or rerun without -SkipPythonInstall."
    }

    if (-not $PackageManager) {
        throw "Python 3.10+ was not found and no supported package manager (winget/choco) is available."
    }

    Write-Step "Installing Python 3"
    if ($PackageManager.Name -eq "winget") {
        Install-WithWinget -WingetPath $PackageManager.Command -PackageId "Python.Python.3.11"
    }
    elseif ($PackageManager.Name -eq "choco") {
        Install-WithChocolatey -ChocoPath $PackageManager.Command -PackageName "python"
    }
    else {
        throw "Unsupported package manager: $($PackageManager.Name)"
    }

    $pythonCmd = Find-PythonCommand
    if (-not $pythonCmd) {
        throw "Python was installed but was not detected in this shell. Open a new PowerShell session and rerun setup_windows.ps1."
    }

    Write-Host "Python installed successfully." -ForegroundColor Green
    return $pythonCmd
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $repoRoot ".venv"
$requirementsFile = Join-Path $repoRoot "requirements-windows.txt"

Write-Step "Using repository root $repoRoot"

if (-not (Test-Path $requirementsFile)) {
    throw "Missing requirements file: $requirementsFile"
}

if (-not $SkipSystemDeps) {
    Write-Step "Checking system dependencies"
    $packageManager = Find-PackageManager
    if ($packageManager) {
        Write-Host "Using package manager: $($packageManager.Name)" -ForegroundColor Green
    }
    else {
        Write-Warning "No supported package manager was detected. Existing system dependencies will be used if already installed."
    }

    Ensure-SystemPackage `
        -DisplayName "GStreamer 1.0" `
        -Probe ${function:Find-GstLaunch} `
        -PackageManager $packageManager `
        -WingetId "GStreamer.GStreamer" `
        -ChocoName "gstreamer" | Out-Null
}

$pythonCmd = Ensure-PythonCommand -PackageManager (Find-PackageManager) -SkipInstall:$SkipPythonInstall
$pythonExe = $pythonCmd.Exe
$pythonArgs = @($pythonCmd.Args)

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

if (-not $SkipSystemDeps) {
    Write-Step "Verifying GStreamer"
    $gstLaunch = Find-GstLaunch
    if (-not $gstLaunch) {
        throw "GStreamer 1.0 was not detected after setup."
    }
    Write-Host "Found gst-launch-1.0 at: $gstLaunch" -ForegroundColor Green
}

Write-Step "Setup complete"
Write-Host "Activate the virtual environment with:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host ""
Write-Host "Then launch the app with:" -ForegroundColor Green
Write-Host "  python .\main_topside.py"
