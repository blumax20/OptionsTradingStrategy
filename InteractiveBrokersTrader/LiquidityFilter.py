def get_options_chain(self, stock_contract):
    """Retrieve options chain for liquidity analysis"""
    # Get option parameters
    req_id = self.get_next_req_id()
    self.reqSecDefOptParams(req_id, stock_contract.symbol, "", 
                           stock_contract.secType, stock_contract.conId)
    
def filter_liquid_options(self, symbol, strikes, expirations):
    """Filter options by liquidity criteria"""
    liquid_options = []
    
    for expiry in expirations[:3]:  # Focus on first 3 expirations
        for strike in strikes:
            # Create call option contract
            call_contract = Contract()
            call_contract.symbol = symbol
            call_contract.secType = "OPT"
            call_contract.exchange = "SMART"
            call_contract.currency = "USD"
            call_contract.lastTradeDateOrContractMonth = expiry
            call_contract.strike = strike
            call_contract.right = "C"
            
            # Request market data to check liquidity
            req_id = self.get_next_req_id()
            self.reqMktData(req_id, call_contract, "", False, False, [])
            
    return liquid_options
