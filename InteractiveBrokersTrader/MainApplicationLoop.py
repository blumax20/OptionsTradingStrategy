def run_automated_system(self):
    """Main system execution loop"""
    
    # Connect to IBKR
    self.connect("127.0.0.1", 7497, clientId=1)  # Paper trading
    threading.Thread(target=self.run, daemon=True).start()
    
    # Wait for connection
    while not isinstance(self.nextOrderId, int):
        print("Waiting for connection...")
        time.sleep(1)
    
    print("System connected and running...")
    
    try:
        while True:
            current_time = datetime.now()
            
            # Daily cycle execution
            if current_time.hour == 16 and current_time.minute == 30:  # After close
                self.daily_trading_cycle()
            
            # Weekly maintenance
            if current_time.weekday() == 6 and current_time.hour == 10:  # Sunday
                self.weekly_maintenance()
            
            # Continuous monitoring during market hours
            if self.is_market_hours():
                self.manage_existing_positions()
                self.monitor_system_health()
            
            time.sleep(60)  # Check every minute
            
    except KeyboardInterrupt:
        print("Shutting down system...")
        self.disconnect()

# Initialize and run the system
if __name__ == "__main__":
    bot = DebitSpreadBot()
    bot.run_automated_system()
