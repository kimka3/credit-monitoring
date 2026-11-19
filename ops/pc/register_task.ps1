# Registers the office PC task: weekdays 08:15 KST. If the PC was off at that
# time, the task runs as soon as the PC is available. Runs as the current user,
# only while logged on (no stored password). ASCII only: Windows PowerShell 5.1
# reads BOM-less scripts in the ANSI code page. See ops/pc/README.md.
param([string]$At = '08:15')
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script = Join-Path $PSScriptRoot 'nice_dispatch.py'
$python = (& python -c "import sys; print(sys.executable)").Trim()
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'   # no console window
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}"' -f $script) -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'CreditMonitor-NICE' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'credit-monitoring: read NICE disclosures from a domestic IP and dispatch GitHub Actions (ops/pc/README.md)' -Force | Out-Null
Get-ScheduledTask -TaskName 'CreditMonitor-NICE' | Select-Object TaskName, State
