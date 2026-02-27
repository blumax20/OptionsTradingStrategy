# IB Gateway connection configuration
# To switch from paper to live trading, change IB_PORT only:
#   Paper trading : IB_PORT = 7497
#   Live trading  : IB_PORT = 7496
#
# Also update IB_GW_PORT in:
#   C:\OptionsHistory\bin\IB_Watchdog.ps1  ($IB_GW_PORT)
#   C:\OptionsHistory\bin\Health.ps1       ($IB_PORT)
IB_HOST: str = "127.0.0.1"
IB_PORT: int = 7497  # Paper trading (change to 7496 for live)
