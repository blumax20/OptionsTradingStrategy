# OptionsTradingStrategy
Repository for code to trade options or indicate a confident buy or sell for a call debit spread or put debit spread.
To set up on a new machine:

Clone repo
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
Copy bin\* → C:\OptionsHistory\bin\
Copy bin\ibc_config.ini.example → C:\IBC\config.ini, add credentials
Install IBC + IB Gateway, set up NSSM services, set up Task Scheduler tasks (per CLAUDE.md)