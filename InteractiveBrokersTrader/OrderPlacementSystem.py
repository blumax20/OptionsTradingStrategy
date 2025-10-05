def place_debit_spread_order(self, signal):
    """Place automated debit spread order"""
    
    # Create combo contract for debit spread
    combo_contract = Contract()
    combo_contract.symbol = signal['symbol']
    combo_contract.secType = "BAG"
    combo_contract.currency = "USD"
    combo_contract.exchange = "SMART"
    
    # Define legs
    leg1 = ComboLeg()
    leg1.conId = self.get_option_conid(signal['symbol'], signal['long_strike'], 
                                      signal['expiration'], "C")
    leg1.ratio = 1
    leg1.action = "BUY"
    leg1.exchange = "SMART"
    
    leg2 = ComboLeg()
    leg2.conId = self.get_option_conid(signal['symbol'], signal['short_strike'],
                                       signal['expiration'], "C")
    leg2.ratio = 1
    leg2.action = "SELL"
    leg2.exchange = "SMART"
    
    combo_contract.comboLegs = [leg1, leg2]
    
    # Create limit order
    order = Order()
    order.action = "BUY"
    order.orderType = "LMT"
    order.totalQuantity = 1
    order.lmtPrice = signal['limit_price']
    order.tif = "DAY"
    
    # Place order
    self.placeOrder(self.nextOrderId, combo_contract, order)
    self.nextOrderId += 1

def manage_existing_positions(self):
    """Monitor and manage existing debit spread positions"""
    for position in self.active_positions:
        days_in_trade = (datetime.now() - position['entry_date']).days
        current_value = self.get_position_value(position)
        
        # Check exit conditions
        if self.should_exit_position(position, current_value, days_in_trade):
            self.close_debit_spread(position)
