[CmdletBinding()]
param(
    [switch]$SkipDependencyInstall,
    [switch]$Clean,
    [switch]$CreateDesktopShortcut,
    [switch]$OneFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
    }
}

function Invoke-WindowedChecked {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "Command failed with exit code $($process.ExitCode)`: $FilePath $($Arguments -join ' ')"
    }
}

function Remove-BuildOutput {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    $resolvedRepo = [System.IO.Path]::GetFullPath($repoRoot).TrimEnd('\')
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolvedPath.StartsWith($resolvedRepo, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove build output outside repository: $resolvedPath"
    }

    Remove-Item -LiteralPath $resolvedPath -Recurse -Force
}

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$requirementsFile = Join-Path $repoRoot "requirements-windows.txt"
$buildRequirementsFile = Join-Path $repoRoot "requirements-build.txt"
$iconScript = Join-Path $repoRoot "tools\make_app_icon.py"
$oneDirSpecFile = Join-Path $repoRoot "deploy\tritonpilot.spec"
$oneFileSpecFile = Join-Path $repoRoot "deploy\tritonpilot_onefile.spec"
$specFile = if ($OneFile) { $oneFileSpecFile } else { $oneDirSpecFile }
$distDir = Join-Path $repoRoot "dist"
$distAppDir = if ($OneFile) { $distDir } else { Join-Path $distDir "TritonPilot" }
$exePath = if ($OneFile) { Join-Path $distDir "TritonPilot.exe" } else { Join-Path $distAppDir "TritonPilot.exe" }

Write-Step "Using repository root $repoRoot"
if ($OneFile) {
    Write-Host "Build mode: single-file app" -ForegroundColor Green
}
else {
    Write-Host "Build mode: one-folder app" -ForegroundColor Green
}

if (-not (Test-Path $venvPython)) {
    Write-Step "Creating development virtual environment"
    $setupScript = Join-Path $repoRoot "setup_windows.ps1"
    if (-not (Test-Path $setupScript)) {
        throw "Missing setup script: $setupScript"
    }
    Invoke-Checked -FilePath "powershell" -Arguments @("-ExecutionPolicy", "Bypass", "-File", $setupScript, "-SkipSystemDeps")
}

if (-not (Test-Path $venvPython)) {
    throw "Virtual environment python not found: $venvPython"
}

if (-not $SkipDependencyInstall) {
    Write-Step "Installing runtime and build dependencies"
    Invoke-Checked -FilePath $venvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
    Invoke-Checked -FilePath $venvPython -Arguments @("-m", "pip", "install", "-r", $requirementsFile)
    Invoke-Checked -FilePath $venvPython -Arguments @("-m", "pip", "install", "-r", $buildRequirementsFile)
}

Write-Step "Generating TritonPilot icon"
Invoke-Checked -FilePath $venvPython -Arguments @($iconScript, "--out-dir", (Join-Path $repoRoot "assets"))

if ($Clean -and (Test-Path $exePath)) {
    Write-Step "Removing previous $exePath"
    Remove-BuildOutput -Path $exePath
}

if ($Clean -and (-not $OneFile) -and (Test-Path $distAppDir)) {
    Write-Step "Removing previous dist\TritonPilot bundle"
    Remove-BuildOutput -Path $distAppDir
}

Write-Step "Building TritonPilot desktop app"
Invoke-Checked -FilePath $venvPython -Arguments @("-m", "PyInstaller", "--noconfirm", "--clean", $specFile)

if (-not (Test-Path $exePath)) {
    throw "Expected build output was not created: $exePath"
}

Write-Step "Verifying packaged resources"
Invoke-WindowedChecked -FilePath $exePath -Arguments @("--smoke-test")

if ($CreateDesktopShortcut) {
    Write-Step "Creating desktop shortcut"
    $desktopDir = [Environment]::GetFolderPath("DesktopDirectory")
    $shortcutPath = Join-Path $desktopDir "TritonPilot.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $exePath
    $shortcut.WorkingDirectory = $distAppDir
    $shortcut.IconLocation = "$exePath,0"
    $shortcut.Description = "Launch TritonPilot"
    $shortcut.Save()
    Write-Host "Shortcut: $shortcutPath" -ForegroundColor Green
}

Write-Step "Build complete"
Write-Host "App: $exePath" -ForegroundColor Green
if ($OneFile) {
    Write-Host "This single-file app can be copied to Desktop or another folder by itself." -ForegroundColor Green
}
else {
    Write-Host "Keep this executable together with its _internal folder, or use -OneFile for a copy-anywhere app."
}
Write-Host "Recordings default to: $([Environment]::GetFolderPath('MyDocuments'))\TritonPilot\Recordings"
Write-Host "GStreamer still needs to be installed on the pilot laptop; run setup_windows.ps1 if video pipelines do not start."
