from DailyCycleManagement import _AttemptLogger
# fresh path for this run
_AttemptLogger._active_path = None
_AttemptLogger.write(
    symbol="TSM", action="close", status="placed", reason="unit_test",
    exp="20251121", right="C", atm="300", oth="305", limit="1.23",
    source="unit-test"
)
print("PRIMARY:", _AttemptLogger.path())
