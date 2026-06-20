#requires -RunAsAdministrator
# Triggers the TritonROV-NAT task once and reports whether it ran cleanly.
$ErrorActionPreference = 'Stop'
$log = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'rov_nat_verify.log'
if (Test-Path $log) { Remove-Item $log -Force -ErrorAction SilentlyContinue }
Start-Transcript -Path $log -Force | Out-Null
try {
    $name = 'TritonROV-NAT'
    Write-Host "--- Triggers ---"
    (Get-ScheduledTask -TaskName $name).Triggers |
        ForEach-Object {
            "{0}  start={1}  repeatEvery={2}" -f `
                $_.CimClass.CimClassName, $_.StartBoundary, $_.Repetition.Interval
        }
    Write-Host "`n--- Running task on demand ---"
    Start-ScheduledTask -TaskName $name
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        $info = Get-ScheduledTaskInfo -TaskName $name
        $state = (Get-ScheduledTask -TaskName $name).State
        if ($state -eq 'Ready' -and $info.LastRunTime) { break }
    }
    $info | Format-List LastRunTime, LastTaskResult, NextRunTime
    Write-Host ("LastTaskResult={0} (0 = success)" -f $info.LastTaskResult)
    Write-Host "`n--- Live state ---"
    Get-NetNat -Name 'TritonROV' | Format-List Name, Active
    Write-Host "DONE-OK"
}
catch { Write-Host ("ERROR: {0}" -f $_.Exception.Message); throw }
finally { Stop-Transcript | Out-Null }
