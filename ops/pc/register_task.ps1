# 사내 PC 예약 작업 등록: 평일 08:15. PC가 꺼져 있어 놓치면 켜진 뒤 바로 실행한다.
# 현재 사용자로, 로그인해 있을 때만 실행한다(비밀번호 저장 없음).
param([string]$At = '08:15')
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script = Join-Path $PSScriptRoot 'nice_dispatch.py'
$python = (& python -c "import sys; print(sys.executable)").Trim()
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'   # 창 없이 실행
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}"' -f $script) -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'CreditMonitor-NICE' -Action $action -Trigger $trigger -Settings $settings `
    -Description '신용등급 모니터링: NICE 공시를 국내 IP로 읽어 GitHub Actions 실행 요청 (ops/pc/README.md)' -Force | Out-Null
Get-ScheduledTask -TaskName 'CreditMonitor-NICE' | Select-Object TaskName, State
