# Wake Daemon Installer for Windows
# Installs sleep-listener.py as a Task Scheduler task that runs at logon.
#
# Usage (local):
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# Usage (remote, via setup.py):
#   python3 setup.py install-daemon Antoine@192.168.1.104

$ErrorActionPreference = 'Stop'
$TaskName   = 'WakeDaemon'
$InstallDir = "$env:USERPROFILE\wake-daemon"
$ScriptPath = "$InstallDir\sleep-listener.py"

# ── Find pythonw (runs silently, no console window) ──────────────────────────
function Find-Pythonw {
    # Prefer pythonw so no console window appears at startup
    foreach ($candidate in @('pythonw', 'python')) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    return $null
}

$python = Find-Pythonw
if (-not $python) {
    Write-Error @"
Python not found in PATH.
Install Python from https://python.org and check "Add Python to PATH",
then re-run this script.
"@
    exit 1
}
Write-Host "Python  : $python"

# ── Ensure install directory and script are in place ─────────────────────────
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

if (-not (Test-Path $ScriptPath)) {
    $src = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'sleep-listener.py'
    if (Test-Path $src) {
        Copy-Item $src $ScriptPath
    } else {
        Write-Error "sleep-listener.py not found next to install.ps1."
        exit 1
    }
}
Write-Host "Script  : $ScriptPath"

# ── Firewall rule ─────────────────────────────────────────────────────────────
netsh advfirewall firewall delete rule name="$TaskName" | Out-Null
netsh advfirewall firewall add rule name="$TaskName" dir=in action=allow protocol=TCP localport=8765 | Out-Null
Write-Host "Firewall: port 8765 allowed (inbound TCP)"

# ── Register scheduled task ───────────────────────────────────────────────────
$action    = New-ScheduledTaskAction -Execute $python -Argument $ScriptPath -WorkingDirectory $InstallDir
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet `
               -ExecutionTimeLimit 0 `
               -RestartCount 3 `
               -RestartInterval (New-TimeSpan -Minutes 1) `
               -StartWhenAvailable $true

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                       -Principal $principal -Settings $settings | Out-Null

# Start it right now
Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "  Wake daemon installed and started!" -ForegroundColor Green
Write-Host "  Runs at logon via Task Scheduler ('$TaskName')"
Write-Host "  Listening on port 8765"
Write-Host "  Log: $InstallDir\wake-daemon.log"
