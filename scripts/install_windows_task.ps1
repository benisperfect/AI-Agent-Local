# Register Sirius Daemon as a User Logon Scheduled Task on Windows 10/11
$ErrorActionPreference = "Stop"
$RepoBackend = Join-Path $PSScriptRoot "..\backend"
$Action = New-ScheduledTaskAction -Execute "uv.exe" -Argument "run sirius --download-models" -WorkingDirectory $RepoBackend
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "SiriusAutonomousVoiceDaemon" -Action $Action -Trigger $Trigger -Settings $Settings -Force
Write-Host "Registered SiriusAutonomousVoiceDaemon scheduled task."
