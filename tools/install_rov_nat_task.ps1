#requires -RunAsAdministrator
<#
  install_rov_nat_task.ps1
  Registers a Scheduled Task that keeps the ROV internet-sharing NAT alive, then
  runs ensure_rov_nat.ps1 once. Run this elevated (it self-elevates via the
  launcher). Idempotent: re-running just refreshes the task.

  Task "TritonROV-NAT" runs ensure_rov_nat.ps1 as SYSTEM:
    - at startup            (survives laptop reboots)
    - at logon
    - every 5 minutes       (self-heals WinNAT after sleep/wake / Wi-Fi switch)
#>

$ErrorActionPreference = 'Stop'
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ensure = Join-Path $here 'ensure_rov_nat.ps1'
$log    = Join-Path $here 'rov_nat_install.log'

if (Test-Path $log) { Remove-Item $log -Force -ErrorAction SilentlyContinue }
Start-Transcript -Path $log -Force | Out-Null
try {
    $taskName = 'TritonROV-NAT'

    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $ensure)

    $triggers = @(
        (New-ScheduledTaskTrigger -AtStartup),
        (New-ScheduledTaskTrigger -AtLogOn),
        (New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
            -RepetitionInterval (New-TimeSpan -Minutes 5))
    )

    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' `
        -LogonType ServiceAccount -RunLevel Highest

    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
        -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host "Registered scheduled task '$taskName'."

    Write-Host "`n--- Running ensure_rov_nat.ps1 now ---"
    & $ensure

    Write-Host "`n--- Verification ---"
    Get-NetNat -Name 'TritonROV' -ErrorAction SilentlyContinue |
        Format-List Name, InternalIPInterfaceAddressPrefix, Active
    Get-NetIPInterface -AddressFamily IPv4 |
        Where-Object { $_.InterfaceAlias -like 'Ethernet*' } |
        Format-Table InterfaceAlias, Forwarding, ConnectionState -AutoSize
    Get-ScheduledTask -TaskName 'TritonROV-NAT' |
        Select-Object TaskName, State | Format-Table -AutoSize
    Write-Host "DONE-OK"
}
catch {
    Write-Host ("ERROR: {0}" -f $_.Exception.Message)
    throw
}
finally {
    Stop-Transcript | Out-Null
}
