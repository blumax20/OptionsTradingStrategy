from ib_insync import IB, Stock, Option, util
from datetime import datetime, timedelta
import logging
import time

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def pull_option_data(symbol, width=5):
    # Connect to IB paper trading
    ib = IB()
    try:
        ib.connect('127.0.0.1', 7497, clientId=1)  # Paper trading port
    except Exception as e:
        logger.error(f"Failed to connect to IB: {e}")
        return {}

    # Request market data in frozen mode (for paper trading)
    ib.reqMarketDataType(3)  # 3 for frozen market data

    # Define the stock contract
    stock = Stock(symbol, 'SMART', 'USD')

    # Request stock market data
    ticks = ib.reqTickers(stock)
    if not ticks:
        logger.error("Failed to request ticker data.")
        ib.disconnect()
        return {}

    ticker = ticks[0]
    for _ in range(20):  # Try for 10 seconds to get the price
        ib.sleep(0.5)
        if ticker.last and not str(ticker.last) == 'nan':
            break
    else:
        logger.error("Failed to get stock price. Exiting.")
        ib.disconnect()
        return {}

    stock_price = ticker.last
    atm_strike = int(round(stock_price))
    logger.info(f"Current {symbol} price: {stock_price}, ATM strike: {atm_strike}")

    # Fetch contract details to get the contract ID
    contracts = ib.reqContractDetails(stock)
    if not contracts:
        logger.error("No contract details found. Exiting.")
        ib.disconnect()
        return {}

    contract = contracts[0].contract
    logger.info(f"Stock contract details: {contract}")

    # Fetch option expirations
    # reqSecDefOptParams returns a list of SecDefOptParams objects.  Each element has an
    # "expirations" attribute containing expiration dates in YYYYMMDD format.  We must
    # patch asyncio to interoperate with the IB event loop before awaiting responses.
    sec_def_data = ib.reqSecDefOptParams(symbol, '', 'STK', contract.conId)
    util.patchAsyncio()
    # Use ib.sleep() instead of time.sleep() so we don't block the event loop
    ib.sleep(2)

    # Collect expiration dates from each SecDefOptParams object
    expirations = []
    for sec_def in sec_def_data:
        # each sec_def has an expirations attribute
        if hasattr(sec_def, 'expirations') and sec_def.expirations:
            expirations.extend(sec_def.expirations)

    if not expirations:
        logger.error("No expirations found. Exiting.")
        ib.disconnect()
        return {}

    # Find the expiration closest to 30 days out
    target_date = datetime.now() + timedelta(days=30)
    closest_expiry = min(
        expirations,
        key=lambda expiry: abs((datetime.strptime(expiry, '%Y%m%d') - target_date).days)
    )
    logger.info(f"Closest expiration to 30 days out: {closest_expiry}")

    # Define strikes to fetch (ATM and surrounding strikes) using the width parameter
    # width defines how far out-of-the-money we look; e.g. width=5 fetches strikes at
    # ATM, ATM-width and ATM+width
    strikes = [atm_strike - width, atm_strike, atm_strike + width]
    logger.info(f"Fetching data for strikes: {strikes}")

    # Fetch market data for each strike
    option_data = {}
    for strike in strikes:
        option = Option(
            symbol=symbol,
            lastTradeDateOrContractMonth=closest_expiry,
            strike=strike,
            right='C',
            exchange='SMART',
            currency='USD'
        )

        # Qualify the contract to get a valid contract ID
        try:
            option = ib.qualifyContracts(option)[0]
            logger.info(f"Option Contract: {option}")
        except Exception as e:
            logger.error(f"Failed to qualify contract for strike {strike}: {e}")
            continue

        # Request market data for the option.  We request generic ticks 101 and 106
        # which populate callOpenInterest/putOpenInterest (101) and impliedVolatility (106)
        # on the returned ticker.  See IB docs: generic tick ID 101 returns
        # callOpenInterest and putOpenInterest, and ID 106 returns implied volatility.
        ticker = ib.reqMktData(option, '101,106', False, False)
        # Allow some time for data to populate
        ib.sleep(1)

        # Extract implied volatility and call open interest from the ticker
        iv = getattr(ticker, 'impliedVolatility', None)
        oi = getattr(ticker, 'callOpenInterest', None)

        option_data[strike] = {
            'implied_volatility': iv,
            'open_interest': oi
        }
        logger.info(f"Strike: {strike}, IV: {iv}, Open Interest: {oi}")

    # Disconnect
    ib.disconnect()
    return option_data

# Example usage
option_data = pull_option_data('PEP')
print(option_data)