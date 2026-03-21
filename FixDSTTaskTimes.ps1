# Fix BT: Reset all IB scheduled task triggers to local time (no UTC sync).
# Run once after DST spring-forward (EST->EDT, March 2026).
# Uses New-ScheduledTaskTrigger which stores local time without UTC sync,
# so future DST changes are handled automatically.

$ErrorActionPreference = "Stop"

# Daily tasks: task name -> correct local ET time
$dailyTasks = @(
    @{ Name = "IB_PreMarket_StartListener";              Time = "06:00" },
    @{ Name = "IB_Health_0715";                          Time = "07:15" },
    @{ Name = "IB_DailyHealth_0830";                     Time = "08:30" },
    @{ Name = "IB_Open_PlaceMissing_0935";               Time = "09:45" },
    @{ Name = "IB_RiskExits_Retry_1030";                 Time = "10:30" },
    @{ Name = "IB_Midday_Health_1200";                   Time = "12:00" },
    @{ Name = "IB_ForceClose_MarketOrders_1500";         Time = "15:00" },
    @{ Name = "IB_PreClose_RestartListener_1530";        Time = "15:30" },
    @{ Name = "IB_AfterHours_PlaceFromWebhook_1700";     Time = "17:00" }
)

foreach ($t in $dailyTasks) {
    try {
        $null = Get-ScheduledTask -TaskName $t.Name -ErrorAction Stop
        $trigger = New-ScheduledTaskTrigger -Daily -At $t.Time
        Set-ScheduledTask -TaskName $t.Name -Trigger $trigger | Out-Null
        Write-Host "OK  $($t.Name) -> $($t.Time)" -ForegroundColor Green
    } catch {
        Write-Host "SKIP $($t.Name) - not found or error: $_" -ForegroundColor Yellow
    }
}

# Watchdog: every 15 min, 06:07 to 20:07 local time (14 hours), DAILY.
# Fix AQ: start at :07 so it misses the 15:30 listener restart window.
# NOTE: New-ScheduledTaskTrigger -Once with RepetitionDuration only fires on the
# day the script is run (duration expires same day). Must use XML with
# CalendarTrigger + ScheduleByDay + Repetition to get daily + 15-min repeat.
try {
    $wdXml = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Fix AB8: Check IB Gateway + Listener health every 15 min, auto-restart if down</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-03-11T06:07:00</StartBoundary>
      <Repetition>
        <Interval>PT15M</Interval>
        <Duration>PT14H</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -File "C:\OptionsHistory\bin\IB_Watchdog.ps1"</Arguments>
      <WorkingDirectory>C:\OptionsHistory\bin</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'@
    $wdXmlPath = "$env:TEMP\ib_watchdog_trigger.xml"
    $wdXml | Out-File -Encoding Unicode $wdXmlPath -Force
    schtasks /Delete /TN "IB_Watchdog_Every15Min" /F 2>&1 | Out-Null
    $result = schtasks /Create /TN "IB_Watchdog_Every15Min" /XML $wdXmlPath /RU "SYSTEM" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "OK  IB_Watchdog_Every15Min -> daily 06:07, every 15 min for 14h (local)" -ForegroundColor Green
    } else {
        Write-Host "WARN IB_Watchdog_Every15Min - schtasks returned: $result" -ForegroundColor Yellow
    }
} catch {
    Write-Host "SKIP IB_Watchdog_Every15Min - error: $_" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Verifying NextRunTime for all IB_ tasks:"
Get-ScheduledTask | Where-Object TaskName -like "IB_*" |
    Get-ScheduledTaskInfo |
    Select-Object TaskName, LastRunTime, NextRunTime |
    Format-Table -AutoSize
