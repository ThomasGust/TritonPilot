[CmdletBinding()]
param(
    [switch]$Force,
    [Alias("SkipGStreamerCheck")]
    [switch]$SkipSystemDeps,
    [switch]$SkipPythonInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:GStreamerRootEnvNames = @(
    "GSTREAMER_1_0_ROOT_MSVC_X86_64",
    "GSTREAMER_1_0_ROOT_X86_64",
    "GSTREAMER_ROOT_X86_64",
    "GSTREAMER_ROOT",
    "GST_ROOT"
)

$script:GStreamerRequiredElements = @(
    "rtph264depay",
    "h264parse",
    "avdec_h264",
    "videoconvert",
    "fdsink",
    "rtpjpegdepay",
    "jpegdec"
)

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

function Get-ScopedEnvironmentValue {
    param(
        [ValidateSet("Process", "User", "Machine")]
        [string]$Scope,
        [string]$Name
    )

    return [Environment]::GetEnvironmentVariable(
        $Name,
        [System.EnvironmentVariableTarget]::$Scope
    )
}

function Add-UniquePathEntry {
    param(
        [AllowNull()]
        [string]$PathValue,
        [string]$Entry
    )

    if (-not $Entry) {
        return $PathValue
    }

    $parts = @()
    if ($PathValue) {
        $parts = $PathValue -split ';' | Where-Object { $_ }
    }

    foreach ($part in $parts) {
        if ($part.TrimEnd('\').ToLowerInvariant() -eq $Entry.TrimEnd('\').ToLowerInvariant()) {
            return ($parts -join ';')
        }
    }

    return (($Entry + ';') + ($parts -join ';')).Trim(';')
}

function Set-UserEnvironmentValue {
    param(
        [string]$Name,
        [string]$Value
    )

    [Environment]::SetEnvironmentVariable(
        $Name,
        $Value,
        [System.EnvironmentVariableTarget]::User
    )
}

function Ensure-UserPathEntry {
    param([string]$Entry)

    $userPath = Get-ScopedEnvironmentValue -Scope User -Name "Path"
    $updated = Add-UniquePathEntry -PathValue $userPath -Entry $Entry
    if ($updated -ne $userPath) {
        Set-UserEnvironmentValue -Name "Path" -Value $updated
    }
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
        [string[]]$PackageIds
    )

    foreach ($packageId in $PackageIds) {
        try {
            & $WingetPath install --id $packageId --exact --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) {
                return $packageId
            }
            Write-Warning "winget install for '$packageId' exited with code $LASTEXITCODE."
        }
        catch {
            Write-Warning "winget install for '$packageId' failed: $_"
        }
    }

    throw "Failed to install package via winget. Tried: $($PackageIds -join ', ')"
}

function Install-WithChocolatey {
    param(
        [string]$ChocoPath,
        [string[]]$PackageNames
    )

    foreach ($packageName in $PackageNames) {
        try {
            & $ChocoPath install $packageName -y
            if ($LASTEXITCODE -eq 0) {
                return $packageName
            }
            Write-Warning "choco install for '$packageName' exited with code $LASTEXITCODE."
        }
        catch {
            Write-Warning "choco install for '$packageName' failed: $_"
        }
    }

    throw "Failed to install package via Chocolatey. Tried: $($PackageNames -join ', ')"
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

function Add-GStreamerRootCandidate {
    param(
        [System.Collections.Generic.List[string]]$Roots,
        [AllowNull()]
        [string]$Candidate
    )

    if (-not $Candidate) {
        return
    }

    try {
        $normalized = [System.IO.Path]::GetFullPath($Candidate)
    }
    catch {
        return
    }

    if (-not (Test-Path $normalized)) {
        return
    }

    foreach ($existing in $Roots) {
        if ($existing.TrimEnd('\').ToLowerInvariant() -eq $normalized.TrimEnd('\').ToLowerInvariant()) {
            return
        }
    }

    [void]$Roots.Add($normalized)
}

function Get-GStreamerRootCandidates {
    $roots = [System.Collections.Generic.List[string]]::new()

    if ($env:GST_LAUNCH -and (Test-Path $env:GST_LAUNCH)) {
        Add-GStreamerRootCandidate -Roots $roots -Candidate (Split-Path -Parent (Split-Path -Parent $env:GST_LAUNCH))
    }

    if ($env:GST_PLUGIN_SCANNER -and (Test-Path $env:GST_PLUGIN_SCANNER)) {
        $scannerDir = Split-Path -Parent $env:GST_PLUGIN_SCANNER
        $libexecDir = Split-Path -Parent $scannerDir
        $rootDir = Split-Path -Parent $libexecDir
        Add-GStreamerRootCandidate -Roots $roots -Candidate $rootDir
    }

    $cmdPath = Test-CommandPath @("gst-launch-1.0.exe", "gst-launch-1.0")
    if ($cmdPath) {
        Add-GStreamerRootCandidate -Roots $roots -Candidate (Split-Path -Parent (Split-Path -Parent $cmdPath))
    }

    foreach ($scope in @("Process", "User", "Machine")) {
        foreach ($name in $script:GStreamerRootEnvNames) {
            $value = Get-ScopedEnvironmentValue -Scope $scope -Name $name
            Add-GStreamerRootCandidate -Roots $roots -Candidate $value
        }
    }

    $commonRoots = @(
        "C:\gstreamer\1.0\msvc_x86_64",
        "C:\gstreamer\1.0\mingw_x86_64"
    )

    if ($env:ProgramFiles) {
        $commonRoots += @(
            (Join-Path $env:ProgramFiles "GStreamer\1.0\msvc_x86_64"),
            (Join-Path $env:ProgramFiles "gstreamer\1.0\msvc_x86_64"),
            (Join-Path $env:ProgramFiles "GStreamer\1.0\mingw_x86_64"),
            (Join-Path $env:ProgramFiles "gstreamer\1.0\mingw_x86_64")
        )
    }

    if ($env:LOCALAPPDATA) {
        $commonRoots += @(
            (Join-Path $env:LOCALAPPDATA "Programs\GStreamer\1.0\msvc_x86_64"),
            (Join-Path $env:LOCALAPPDATA "Programs\gstreamer\1.0\msvc_x86_64"),
            (Join-Path $env:LOCALAPPDATA "Programs\GStreamer\1.0\mingw_x86_64"),
            (Join-Path $env:LOCALAPPDATA "Programs\gstreamer\1.0\mingw_x86_64")
        )
    }

    foreach ($root in $commonRoots) {
        Add-GStreamerRootCandidate -Roots $roots -Candidate $root
    }

    return $roots
}

function Get-GStreamerRuntime {
    foreach ($root in Get-GStreamerRootCandidates) {
        $binDir = Join-Path $root "bin"
        $gstLaunch = Join-Path $binDir "gst-launch-1.0.exe"
        if (-not (Test-Path $gstLaunch)) {
            $gstLaunch = Join-Path $binDir "gst-launch-1.0"
        }
        if (-not (Test-Path $gstLaunch)) {
            continue
        }

        $gstInspect = Join-Path $binDir "gst-inspect-1.0.exe"
        if (-not (Test-Path $gstInspect)) {
            $gstInspect = Join-Path $binDir "gst-inspect-1.0"
        }
        if (-not (Test-Path $gstInspect)) {
            $gstInspect = $null
        }

        $pluginScanner = Join-Path $root "libexec\gstreamer-1.0\gst-plugin-scanner.exe"
        if (-not (Test-Path $pluginScanner)) {
            $pluginScanner = $null
        }

        $pluginDir = Join-Path $root "lib\gstreamer-1.0"
        if (-not (Test-Path $pluginDir)) {
            $pluginDir = $null
        }

        return @{
            Root = $root
            BinDir = $binDir
            GstLaunch = $gstLaunch
            GstInspect = $gstInspect
            PluginScanner = $pluginScanner
            PluginDir = $pluginDir
        }
    }

    return $null
}

function Find-GstLaunch {
    $runtime = Get-GStreamerRuntime
    if ($runtime) {
        return $runtime.GstLaunch
    }
    return $null
}

function Initialize-GStreamerSession {
    param([switch]$PersistUserEnv)

    $runtime = Get-GStreamerRuntime
    if (-not $runtime) {
        return $null
    }

    $env:PATH = Add-UniquePathEntry -PathValue $env:PATH -Entry $runtime.BinDir
    $env:GST_LAUNCH = $runtime.GstLaunch
    $env:GSTREAMER_1_0_ROOT_MSVC_X86_64 = $runtime.Root

    if ($runtime.PluginScanner) {
        $env:GST_PLUGIN_SCANNER = $runtime.PluginScanner
    }

    if ($runtime.PluginDir -and ((-not $env:GST_PLUGIN_SYSTEM_PATH_1_0) -or -not (Test-Path $env:GST_PLUGIN_SYSTEM_PATH_1_0))) {
        $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $runtime.PluginDir
    }

    if ($PersistUserEnv) {
        Set-UserEnvironmentValue -Name "GST_LAUNCH" -Value $runtime.GstLaunch
        Set-UserEnvironmentValue -Name "GSTREAMER_1_0_ROOT_MSVC_X86_64" -Value $runtime.Root
        if ($runtime.PluginScanner) {
            Set-UserEnvironmentValue -Name "GST_PLUGIN_SCANNER" -Value $runtime.PluginScanner
        }
        Ensure-UserPathEntry -Entry $runtime.BinDir
    }

    return $runtime
}

function Test-GStreamerElement {
    param(
        [hashtable]$Runtime,
        [string]$Element
    )

    if (-not $Runtime.GstInspect) {
        return $false
    }

    $args = @("--gst-disable-registry-fork", "--exists", $Element)
    $null = & $Runtime.GstInspect @args 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Test-GStreamerRuntime {
    param([hashtable]$Runtime)

    if (-not $Runtime) {
        throw "GStreamer 1.0 was not detected after setup."
    }

    $smokeArgs = @(
        "--gst-disable-registry-fork",
        "-q",
        "videotestsrc",
        "num-buffers=1",
        "!",
        "videoconvert",
        "!",
        "fakesink"
    )
    $null = & $Runtime.GstLaunch @smokeArgs 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "GStreamer was found at '$($Runtime.GstLaunch)' but a basic smoke test failed. Reinstall the complete x86_64 package and rerun setup_windows.ps1."
    }

    if (-not $Runtime.GstInspect) {
        throw "GStreamer was found, but gst-inspect-1.0.exe was missing. Reinstall the complete x86_64 package and rerun setup_windows.ps1."
    }

    $missing = [System.Collections.Generic.List[string]]::new()
    foreach ($element in $script:GStreamerRequiredElements) {
        if (-not (Test-GStreamerElement -Runtime $Runtime -Element $element)) {
            [void]$missing.Add($element)
        }
    }

    if ($missing.Count -gt 0) {
        throw "GStreamer is installed but missing required plugins/elements: $($missing -join ', '). Install the complete x86_64 package and rerun setup_windows.ps1."
    }
}

function Ensure-SystemPackage {
    param(
        [string]$DisplayName,
        [ScriptBlock]$Probe,
        [hashtable]$PackageManager,
        [string[]]$WingetIds,
        [string[]]$ChocoNames
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
        $installedId = Install-WithWinget -WingetPath $PackageManager.Command -PackageIds $WingetIds
        Write-Host "Installed via winget package id: $installedId" -ForegroundColor Green
    }
    elseif ($PackageManager.Name -eq "choco") {
        $installedName = Install-WithChocolatey -ChocoPath $PackageManager.Command -PackageNames $ChocoNames
        Write-Host "Installed via Chocolatey package: $installedName" -ForegroundColor Green
    }
    else {
        throw "Unsupported package manager: $($PackageManager.Name)"
    }

    $installed = & $Probe
    if (-not $installed) {
        throw "$DisplayName was installed but was not detected. The installer may have completed without updating this shell; rerun setup_windows.ps1 if needed."
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
        $null = Install-WithWinget -WingetPath $PackageManager.Command -PackageIds @("Python.Python.3.11")
    }
    elseif ($PackageManager.Name -eq "choco") {
        $null = Install-WithChocolatey -ChocoPath $PackageManager.Command -PackageNames @("python")
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
$packageManager = Find-PackageManager

Write-Step "Using repository root $repoRoot"

if (-not (Test-Path $requirementsFile)) {
    throw "Missing requirements file: $requirementsFile"
}

if (-not $SkipSystemDeps) {
    Write-Step "Checking system dependencies"
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
        -WingetIds @("gstreamerproject.gstreamer", "GStreamer.GStreamer") `
        -ChocoNames @("gstreamer") | Out-Null

    Write-Step "Configuring GStreamer environment"
    $gstRuntime = Initialize-GStreamerSession -PersistUserEnv
    if (-not $gstRuntime) {
        throw "GStreamer 1.0 was not detected after installation."
    }
    Write-Host "Using GStreamer root: $($gstRuntime.Root)" -ForegroundColor Green
}

$pythonCmd = Ensure-PythonCommand -PackageManager $packageManager -SkipInstall:$SkipPythonInstall
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
    $gstRuntime = Initialize-GStreamerSession -PersistUserEnv
    Test-GStreamerRuntime -Runtime $gstRuntime
    Write-Host "Found gst-launch-1.0 at: $($gstRuntime.GstLaunch)" -ForegroundColor Green
    Write-Host "Persisted GStreamer environment for future PowerShell sessions." -ForegroundColor Green
}

Write-Step "Setup complete"
Write-Host "Activate the virtual environment with:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host ""
Write-Host "Then launch the app with:" -ForegroundColor Green
Write-Host "  python .\main_topside.py"
