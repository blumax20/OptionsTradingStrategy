from ib_insync import IB, util

ib = IB()
try:
    ib.connect('127.0.0.1', 7497, clientId=1)  # Paper trading port
    print("Successfully connected to IB API!")
    # Example: Fetch account values
    account_values = ib.accountValues()
    print(account_values)
except Exception as e:
    print(f"Connection failed: {e}")
finally:
    ib.disconnect()
