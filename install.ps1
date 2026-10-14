# simcoach one-shot installer for Windows.
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/rechandler/simcoach/main/install.ps1 | iex"
#
# or double-click install.cmd in a downloaded copy of the repo.
#
# What it does (no admin rights needed):
#   1. Finds Python 3.10-3.13, or installs Python 3.12 for the current user
#   2. Creates a private virtual environment in %LOCALAPPDATA%\simcoach
#   3. Installs simcoach with iRacing, voice and Claude support
#   4. Adds `simcoach` to your PATH and a "SimCoach" desktop/Start menu shortcut
#   5. Runs `simcoach setup` (wheel button, microphone, coach AI, API key)
#
# Options (environment variables):
#   SIMCOACH_BRANCH      git branch to install from GitHub (default: main)
#   SIMCOACH_HOME_DIR    install location (default: %LOCALAPPDATA%\simcoach)
#   SIMCOACH_SKIP_SETUP  set to 1 to skip the setup wizard
#
# Re-running the installer upgrades simcoach and keeps your settings.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # makes Invoke-WebRequest much faster on PS 5.1

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host "`nInstall failed: $msg" -ForegroundColor Red
    Write-Host "Report problems at https://github.com/rechandler/simcoach/issues"
    # throw (not exit) so a pasted `irm | iex` doesn't close the PowerShell window
    throw "simcoach install failed: $msg"
}

if ($env:OS -ne 'Windows_NT') { Fail 'This installer is for Windows (iRacing only runs on Windows).' }
if (-not [Environment]::Is64BitOperatingSystem) { Fail 'simcoach needs 64-bit Windows.' }

$Repo = 'rechandler/simcoach'
$Branch = if ($env:SIMCOACH_BRANCH) { $env:SIMCOACH_BRANCH } else { 'main' }
$InstallDir = if ($env:SIMCOACH_HOME_DIR) { $env:SIMCOACH_HOME_DIR } else { Join-Path $env:LOCALAPPDATA 'simcoach' }
$Venv = Join-Path $InstallDir 'venv'
$Bin = Join-Path $InstallDir 'bin'
$PythonVersion = '3.12.8'

function Test-Python($exe, $pyArgs) {
    # Returns $true when the interpreter runs and is 3.10-3.13 (3.14+ lacks
    # wheels for some voice dependencies).
    try {
        $out = & $exe @pyArgs -c "import sys; print(int((3,10) <= sys.version_info[:2] <= (3,13)))" 2>$null
        return ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq '1')
    } catch {
        return $false
    }
}

function Find-Python {
    $candidates = @(
        @{ exe = 'py'; args = @('-3.12') },
        @{ exe = 'py'; args = @('-3.11') },
        @{ exe = 'py'; args = @('-3.13') },
        @{ exe = 'py'; args = @('-3.10') },
        @{ exe = 'python'; args = @() },
        @{ exe = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'); args = @() }
    )
    foreach ($c in $candidates) {
        if ((Get-Command $c.exe -ErrorAction SilentlyContinue) -and (Test-Python $c.exe $c.args)) {
            return $c
        }
    }
    return $null
}

function Install-Python {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Installing Python $PythonVersion with winget..."
        & winget install --exact --id Python.Python.3.12 --scope user --silent `
            --accept-package-agreements --accept-source-agreements | Out-Host
    }
    $py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (-not (Test-Path $py)) {
        Write-Host "Downloading Python $PythonVersion from python.org..."
        $setup = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" -OutFile $setup
        $p = Start-Process $setup -Wait -PassThru -ArgumentList @(
            '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0')
        if ($p.ExitCode -ne 0) { Fail "Python installer exited with code $($p.ExitCode)" }
    }
    if (-not (Test-Path $py)) { Fail "Python was not found after installing it. Install Python 3.12 from python.org and re-run." }
    return @{ exe = $py; args = @() }
}

function Add-ToUserPath($dir) {
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($current) { $parts = $current.Split(';') | Where-Object { $_ } }
    if ($parts -notcontains $dir) {
        [Environment]::SetEnvironmentVariable('Path', (($parts + $dir) -join ';'), 'User')
    }
    if (($env:Path.Split(';')) -notcontains $dir) { $env:Path = "$env:Path;$dir" }
}

function New-Shortcut($path, $target, $workDir, $description) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.WorkingDirectory = $workDir
    $lnk.Description = $description
    $lnk.Save()
}


Write-Host "simcoach installer  ->  $InstallDir" -ForegroundColor Green

# 1. Python ---------------------------------------------------------------
Write-Step 'Checking for Python 3.10-3.13'
$py = Find-Python
if (-not $py) { $py = Install-Python }
$pyDesc = (& $py.exe @($py.args) -c "import sys; print(sys.version.split()[0], sys.executable)")
Write-Host "Using Python $pyDesc"

# 2. Virtual environment --------------------------------------------------
Write-Step 'Creating virtual environment'
New-Item -ItemType Directory -Force -Path $InstallDir, $Bin | Out-Null
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    & $py.exe @($py.args) -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail 'could not create the virtual environment' }
}
& $VenvPy -m pip install --upgrade --quiet pip
if ($LASTEXITCODE -ne 0) { Fail 'could not upgrade pip' }

# 3. simcoach -------------------------------------------------------------
Write-Step 'Installing simcoach and its dependencies (a few minutes the first time)'
$local = $null
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'pyproject.toml'))) { $local = $PSScriptRoot }
if ($local) {
    Write-Host "From local copy: $local"
    $spec = "${local}[all]"
} else {
    Write-Host "From GitHub: $Repo ($Branch)"
    $spec = "simcoach[all] @ https://github.com/$Repo/archive/refs/heads/$Branch.zip"
}
& $VenvPy -m pip install --upgrade --upgrade-strategy only-if-needed $spec
if ($LASTEXITCODE -ne 0) { Fail 'pip install failed (see the messages above)' }
# Always replace simcoach itself with the freshly downloaded copy: pip can
# otherwise keep an installed copy whose version number hasn't changed.
& $VenvPy -m pip install --force-reinstall --no-deps --no-cache-dir --quiet $spec
if ($LASTEXITCODE -ne 0) { Fail 'could not install the latest simcoach' }
$Exe = Join-Path $Venv 'Scripts\simcoach.exe'
if (-not (Test-Path $Exe)) { Fail 'simcoach.exe was not created' }

# 4. Launchers ------------------------------------------------------------
Write-Step 'Adding simcoach to PATH and creating shortcuts'
Set-Content -Encoding ASCII -Path (Join-Path $Bin 'simcoach.cmd') -Value "@`"$Exe`" %*"
Add-ToUserPath $Bin

$launcher = Join-Path $InstallDir 'SimCoach.cmd'
Set-Content -Encoding ASCII -Path $launcher -Value @(
    '@echo off',
    'title SimCoach',
    "`"$Exe`" run",
    'echo.',
    'pause'
)
$uninstall = Join-Path $InstallDir 'uninstall.cmd'
Set-Content -Encoding ASCII -Path $uninstall -Value @(
    '@echo off',
    'echo Removing simcoach (your laps and settings in %USERPROFILE%\.simcoach are kept)...',
    "del `"%USERPROFILE%\Desktop\SimCoach.lnk`" 2>nul",
    "del `"%APPDATA%\Microsoft\Windows\Start Menu\Programs\SimCoach.lnk`" 2>nul",
    "powershell -NoProfile -Command `"`$p=[Environment]::GetEnvironmentVariable('Path','User').Split(';') | Where-Object { `$_ -and `$_ -ne '$Bin' }; [Environment]::SetEnvironmentVariable('Path', (`$p -join ';'), 'User')`"",
    "cd /d `"%TEMP%`"",
    "rmdir /s /q `"$InstallDir`""
)

$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'SimCoach.lnk'
try {
    New-Shortcut (Join-Path $desktop 'SimCoach.lnk') $launcher $InstallDir 'Live AI driving coach for iRacing'
    New-Shortcut $startMenu $launcher $InstallDir 'Live AI driving coach for iRacing'
} catch {
    Write-Host "Could not create shortcuts ($_). Start it with: simcoach run" -ForegroundColor Yellow
}

# 5. Setup wizard ---------------------------------------------------------
if ($env:SIMCOACH_SKIP_SETUP -ne '1') {
    Write-Step 'Setup'
    & $Exe setup
}

Write-Host "`nsimcoach is installed." -ForegroundColor Green
Write-Host '  Start coaching:   double-click "SimCoach" on your desktop (or run: simcoach run)'
Write-Host '  Try a demo:       simcoach demo'
Write-Host '  Change settings:  simcoach setup'
Write-Host "  Uninstall:        $uninstall"
Write-Host '  Open a new terminal for the simcoach command to be on your PATH.'
