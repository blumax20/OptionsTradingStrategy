import math
from scipy.stats import norm

def black_scholes_call(self, S, K, T, r, sigma):
    """Calculate Black-Scholes call option price"""
    d1 = (math.log(S/K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    
    call_price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return call_price

def calculate_debit_spread_value(self, S, long_strike, short_strike, T, r, sigma):
    """Calculate theoretical debit spread value"""
    long_call = self.black_scholes_call(S, long_strike, T, r, sigma)
    short_call = self.black_scholes_call(S, short_strike, T, r, sigma)
    
    spread_value = long_call - short_call
    return spread_value

def generate_trade_signals(self, sector_data):
    """Generate buy/sell signals for debit spreads"""
    signals = []
    
    for symbol, data in sector_data.items():
        current_price = data['current_price']
        iv = data['implied_volatility']
        
        # Filter by IV < 40%
        if iv > 0.40:
            continue
            
        # Generate signal based on technical indicators
        signal = self.analyze_technical_setup(data)
        
        if signal['action'] in ['BUY', 'SELL']:
            # Calculate optimal strikes
            strikes = self.calculate_optimal_strikes(current_price, signal['direction'])
            
            # Calculate fair value using Black-Scholes
            fair_value = self.calculate_debit_spread_value(
                current_price, strikes['long'], strikes['short'],
                signal['dte']/365, 0.05, iv
            )
            
            signals.append({
                'symbol': symbol,
                'action': signal['action'],
                'long_strike': strikes['long'],
                'short_strike': strikes['short'],
                'expiration': signal['expiration'],
                'fair_value': fair_value,
                'limit_price': fair_value * 0.95  # 5% below fair value
            })
    
    return signals
