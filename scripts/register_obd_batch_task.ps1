<#
 OBD 일일 배치 작업 스케줄러 등록 (Windows 작업 스케줄러)

 매일 03:30(본 처리), 04:00·06:00(늦게 도착한 파일·미도착 재확인)에
 scripts\run_obd_batch.cmd 를 실행하는 작업 3개를 등록한다.
 업체가 전날 데이터를 새벽 02~03시에 보내는 것을 전제로 한 시각이다.

 사용법 (PowerShell)
   .\scripts\register_obd_batch_task.ps1                       # 등록(기본 shadow = 운영 반영 안 함)
   .\scripts\register_obd_batch_task.ps1 -Mode live -LiveEvs "EV-TORRES-01"   # 1대만 운영 반영
   .\scripts\register_obd_batch_task.ps1 -Mode live            # 전체 운영 반영
   .\scripts\register_obd_batch_task.ps1 -WhatIf               # 등록하지 않고 명령만 확인
   .\scripts\register_obd_batch_task.ps1 -Status               # 등록 상태·마지막 실행 결과
   .\scripts\register_obd_batch_task.ps1 -Remove               # 등록한 작업 삭제

 모드 전환(shadow -> 1대 -> 전체)은 -Mode/-LiveEvs 값만 바꿔 다시 실행하면 같은 이름 작업을 덮어쓴다.
 로그: data\batch\logs\batch_yyyymmdd.log  /  종료코드 0 정상, 1 격리·오류·미도착 있음
#>
param(
    [ValidateSet("shadow", "live")][string]$Mode = "shadow",
    [string]$LiveEvs = "",
    [string[]]$Times = @("03:30", "04:00", "06:00"),
    [switch]$Remove,
    [switch]$Status,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Wrapper = Join-Path $ProjectRoot "scripts\run_obd_batch.cmd"
$TaskPrefix = "AI-V2G OBD Batch"

if ($Status) {
    $tasks = Get-ScheduledTask -TaskName "$TaskPrefix*" -ErrorAction SilentlyContinue
    if (-not $tasks) { Write-Host "등록된 작업이 없습니다."; return }
    $tasks | ForEach-Object {
        $info = $_ | Get-ScheduledTaskInfo
        [pscustomobject]@{ Task = $_.TaskName; State = $_.State; LastRun = $info.LastRunTime; LastResult = $info.LastTaskResult; NextRun = $info.NextRunTime }
    } | Format-Table -AutoSize
    Write-Host "LastResult 0 = 정상, 1 = 격리/오류/미도착 있음(리포트 확인), 그 외 = 실행 실패"
    return
}

if ($Remove) {
    foreach ($t in @(Get-ScheduledTask -TaskName "$TaskPrefix*" -ErrorAction SilentlyContinue)) {
        if ($WhatIf) { Write-Host "[WhatIf] 삭제: $($t.TaskName)"; continue }
        Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
        Write-Host "삭제: $($t.TaskName)"
    }
    return
}

if (-not (Test-Path $Wrapper)) { throw "run_obd_batch.cmd를 찾을 수 없습니다: $Wrapper" }
if ($Mode -eq "live" -and $LiveEvs -eq "") {
    Write-Warning "live 모드이며 -LiveEvs가 비어 있어 모든 차량이 운영 데이터에 반영됩니다. 1대 반영 단계라면 -LiveEvs를 지정하세요."
}

$argText = if ($LiveEvs -ne "") { "/c `"`"$Wrapper`" $Mode `"$LiveEvs`"`"" } else { "/c `"`"$Wrapper`" $Mode`"" }

foreach ($time in $Times) {
    $name = "$TaskPrefix $time"
    if ($WhatIf) { Write-Host "[WhatIf] '$name' 매일 $time : cmd.exe $argText"; continue }
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $argText
    $trigger = New-ScheduledTaskTrigger -Daily -At $time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
        -Description "OBD 일일 배치($Mode) - 전날 데이터 수신·정제·반영" -Force | Out-Null
    Write-Host "등록: $name (매일 $time, 모드 $Mode)"
}
Write-Host "완료. 상태 확인: .\scripts\register_obd_batch_task.ps1 -Status"
