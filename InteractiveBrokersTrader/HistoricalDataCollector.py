def collect_historical_data(self, contract, days_back=365):
    """Collect historical price and options data"""
    req_id = self.get_next_req_id()
    end_date = datetime.now().strftime("%Y%m%d %H:%M:%S")
    
    self.reqHistoricalData(
        req_id, contract, end_date, f"{days_back} D", 
        "1 day", "ADJUSTED_LAST", 1, 1, False, []
    )
    
def organize_sector_data(self):
    """Organize collected data by sector"""
    sectors = {
        'TECH': [], 'FINANCE': [], 'HEALTHCARE': [], 
        'ENERGY': [], 'CONSUMER': []
    }
    
    for symbol, data in self.historical_data.items():
        sector = self.get_stock_sector(symbol)
        if sector in sectors:
            sectors[sector].append({
                'symbol': symbol,
                'data': data,
                'volatility': self.calculate_historical_volatility(data),
                'liquidity_score': self.calculate_liquidity_score(symbol)
            })
    
    return sectors
