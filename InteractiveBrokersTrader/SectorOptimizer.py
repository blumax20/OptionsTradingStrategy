def optimize_sector_strategy(self, sector_data, lookback_days=252):
    """Optimize debit spread parameters per sector"""
    optimization_results = {}
    
    for sector, stocks in sector_data.items():
        best_params = {
            'dte_range': (25, 35),
            'delta_target': 0.5,
            'profit_target': 0.25,
            'stop_loss': 0.50,
            'max_positions': 5
        }
        
        # Backtest different parameter combinations
        for dte in range(20, 45, 5):
            for delta in [0.4, 0.5, 0.6]:
                for profit_target in [0.20, 0.25, 0.30]:
                    results = self.backtest_parameters(
                        stocks, dte, delta, profit_target, lookback_days
                    )
                    
                    if results['win_rate'] > best_params.get('win_rate', 0):
                        best_params.update(results)
        
        optimization_results[sector] = best_params
    
    return optimization_results
