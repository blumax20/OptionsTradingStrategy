from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.scanner import ScannerSubscription
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import threading
import time

class DebitSpreadBot(EClient, EWrapper):
    def __init__(self):
        EClient.__init__(self, self)
        self.nextOrderId = None
        self.scanner_results = []
        self.historical_data = {}
        self.sector_data = {}
        self.filtered_candidates = []
        
    def error(self, reqId, errorCode, errorString, advancedOrderReject=""):
        print(f"Error {errorCode}: {errorString}")
        
    def nextValidId(self, orderId):
        super().nextValidId(orderId)
        self.nextOrderId = orderId
        print(f"Connected. Next valid order ID: {orderId}")
