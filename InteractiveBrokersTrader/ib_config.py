# IB Gateway connection configuration
# To switch between paper and live trading run (from repo root):
#   python switch_trading_mode.py live    -- switches to live  (port 7496)
#   python switch_trading_mode.py paper   -- switches to paper (port 7497)
#   python switch_trading_mode.py status  -- shows current mode
#
# The script updates this file plus:
#   C:\OptionsHistory\bin\IB_Watchdog.ps1  ($IB_GW_PORT)
#   C:\OptionsHistory\bin\Health.ps1       ($IB_PORT)
#   C:\IBC\config.ini                      (TradingMode, ApiPort, OverrideTwsApiPort)
# Then restarts IBGateway so IBC auto-logs in to the new account.
IB_HOST: str = "127.0.0.1"
IB_PORT: int = 7497  # Paper trading (change to 7496 for live)
