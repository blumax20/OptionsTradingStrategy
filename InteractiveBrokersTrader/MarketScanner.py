def create_sector_scanner(self, sector_code):
    """Create scanner for specific sector with liquidity filters"""
    scanner = ScannerSubscription()
    scanner.instrument = "STK"
    scanner.locationCode = "STK.US.MAJOR"
    scanner.scanCode = "TOP_VOLUME_RATE"
    
    # Liquidity filters
    scan_options = []
    filter_options = [
        TagValue("volumeAbove", "1000000"),  # Min 1M daily volume
        TagValue("marketCapAbove", "1000000000"),  # Min 1B market cap
        TagValue("priceAbove", "10"),  # Minimum $10 stock price
        TagValue("sector", sector_code)  # Sector-specific filter
    ]
    
    return scanner, scan_options, filter_options

def scan_sector_candidates(self, sector_codes):
    """Scan multiple sectors for debit spread candidates"""
    for sector in sector_codes:
        scanner, scan_opts, filter_opts = self.create_sector_scanner(sector)
        req_id = len(self.scanner_results) + 1000
        self.reqScannerSubscription(req_id, scanner, scan_opts, filter_opts)
        time.sleep(2)  # Rate limiting
