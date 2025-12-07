#!/usr/bin/env python3
"""
Improved Health Queries for Interactive Brokers
Fixes issues with P/L and transaction data retrieval
"""

from ib_insync import IB, util, ExecutionFilter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import sys

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def get_pnl_data(ib: IB, account: str) -> dict:
    """
    Get P/L data using multiple methods to ensure we capture all available data

    Returns dict with:
    - day_realized: Day's realized P/L
    - day_unrealized: Day's unrealized P/L
    - netliq: Current net liquidation value
    - ytd_realized: YTD realized P/L (if available)
    """
    result = {}

    # Method 1: Account Summary (original approach)
    try:
        summary = ib.accountSummary(account)
        vals = {s.tag: s.value for s in summary}

        # Try multiple possible tag names for day P/L
        day_realized_tags = ['RealizedPnL', 'DailyPnL', 'DayPnL', 'RealizedDayPnL']
        day_unrealized_tags = ['UnrealizedPnL', 'UnrealizedDayPnL', 'DayUnrealizedPnL']

        for tag in day_realized_tags:
            if tag in vals and vals[tag]:
                try:
                    result['day_realized'] = float(vals[tag])
                    break
                except (ValueError, TypeError):
                    pass

        for tag in day_unrealized_tags:
            if tag in vals and vals[tag]:
                try:
                    result['day_unrealized'] = float(vals[tag])
                    break
                except (ValueError, TypeError):
                    pass

        # NetLiquidation
        if 'NetLiquidation' in vals:
            try:
                result['netliq'] = float(vals['NetLiquidation'])
            except (ValueError, TypeError):
                pass

    except Exception as e:
        print(f"Warning: accountSummary failed: {e}", file=sys.stderr)

    # Method 2: Account Values (alternative approach - more comprehensive)
    try:
        account_values = ib.accountValues(account)
        vals_dict = {av.tag: av.value for av in account_values if av.currency == 'USD' or av.currency == 'BASE'}

        # If we didn't get day_realized from summary, try from account values
        if 'day_realized' not in result:
            for tag in ['RealizedPnL', 'DailyPnL']:
                if tag in vals_dict:
                    try:
                        result['day_realized'] = float(vals_dict[tag])
                        break
                    except (ValueError, TypeError):
                        pass

        # If we didn't get day_unrealized from summary, try from account values
        if 'day_unrealized' not in result:
            for tag in ['UnrealizedPnL']:
                if tag in vals_dict:
                    try:
                        result['day_unrealized'] = float(vals_dict[tag])
                        break
                    except (ValueError, TypeError):
                        pass

        # NetLiquidation fallback
        if 'netliq' not in result and 'NetLiquidation' in vals_dict:
            try:
                result['netliq'] = float(vals_dict['NetLiquidation'])
            except (ValueError, TypeError):
                pass

    except Exception as e:
        print(f"Warning: accountValues failed: {e}", file=sys.stderr)

    # Calculate day_total if we have the components
    if 'day_realized' in result and 'day_unrealized' in result:
        result['day_total'] = result['day_realized'] + result['day_unrealized']
    elif 'day_realized' in result:
        result['day_total'] = result['day_realized']
    elif 'day_unrealized' in result:
        result['day_total'] = result['day_unrealized']

    return result


def get_recent_trades(ib: IB, days: int = 14) -> list:
    """
    Get recent trades (orders) with improved filtering

    Returns list of trade dicts with timing and details
    """
    try:
        # Request all open orders first
        try:
            ib.reqAllOpenOrders()
            ib.reqAutoOpenOrders(True)
            ib.sleep(1)
        except Exception:
            pass

        all_trades = ib.trades()
        results = []

        cutoff = datetime.now(UTC) - timedelta(days=days)

        for tr in all_trades:
            # Extract time - try multiple methods
            time_utc = None

            # Method 1: Check order status lastUpdateTime
            if hasattr(tr.orderStatus, 'lastUpdateTime') and tr.orderStatus.lastUpdateTime:
                try:
                    time_utc = util.parseIBDatetime(tr.orderStatus.lastUpdateTime)
                    if time_utc and time_utc.tzinfo is None:
                        time_utc = time_utc.replace(tzinfo=UTC)
                except Exception:
                    pass

            # Method 2: Check trade log for submitted time
            if not time_utc and tr.log:
                for log_entry in tr.log:
                    if 'submitted' in (log_entry.status or '').lower():
                        try:
                            time_utc = util.parseIBDatetime(log_entry.time)
                            if time_utc and time_utc.tzinfo is None:
                                time_utc = time_utc.replace(tzinfo=UTC)
                            break
                        except Exception:
                            pass

            # Method 3: Use any log entry time
            if not time_utc and tr.log and len(tr.log) > 0:
                try:
                    time_utc = util.parseIBDatetime(tr.log[0].time)
                    if time_utc and time_utc.tzinfo is None:
                        time_utc = time_utc.replace(tzinfo=UTC)
                except Exception:
                    pass

            # Filter by time
            if time_utc and time_utc >= cutoff:
                results.append({
                    'time_utc': time_utc,
                    'time_ny': time_utc.astimezone(NY),
                    'contract': tr.contract,
                    'order': tr.order,
                    'status': tr.orderStatus.status if tr.orderStatus else 'Unknown'
                })

        # Sort by time, most recent first
        results.sort(key=lambda x: x['time_utc'], reverse=True)
        return results[:20]  # Return top 20

    except Exception as e:
        print(f"Error getting recent trades: {e}", file=sys.stderr)
        return []


def get_recent_executions(ib: IB, days: int = 7) -> list:
    """
    Get recent executions (closed trades) with P/L

    Returns list of execution dicts with P/L data
    """
    try:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        since_str = cutoff.strftime("%Y%m%d-%H:%M:%S")

        # Request executions
        fills = ib.reqExecutions(ExecutionFilter(time=since_str))

        results = []
        for fill in fills:
            contract = fill.contract
            execution = fill.execution

            # Only include options
            if getattr(contract, 'secType', '') != 'OPT':
                continue

            # Parse execution time
            exec_time_str = getattr(execution, 'time', '')
            try:
                exec_time = util.parseIBDatetime(exec_time_str)
                if exec_time.tzinfo is None:
                    exec_time = exec_time.replace(tzinfo=UTC)
            except Exception:
                exec_time = None

            # Get P/L if available
            pnl = 0.0
            try:
                exec_id = getattr(execution, 'execId', None)
                if exec_id:
                    comm_report = ib.reqCommissionReport(exec_id)
                    ib.sleep(0.1)  # Small delay to get commission report
                    if comm_report and hasattr(comm_report, 'realizedPNL') and comm_report.realizedPNL is not None:
                        pnl = float(comm_report.realizedPNL)
            except Exception:
                pass

            results.append({
                'time_utc': exec_time,
                'time_ny': exec_time.astimezone(NY) if exec_time else None,
                'symbol': getattr(contract, 'symbol', ''),
                'exp': getattr(contract, 'lastTradeDateOrContractMonth', ''),
                'strike': float(getattr(contract, 'strike', 0.0)),
                'right': getattr(contract, 'right', ''),
                'side': getattr(execution, 'side', ''),
                'shares': float(getattr(execution, 'shares', 0.0)),
                'price': float(getattr(execution, 'price', 0.0)),
                'pnl': pnl,
                'open_close': getattr(execution, 'openClose', '')
            })

        # Sort by time, most recent first
        results.sort(key=lambda x: x['time_utc'] if x['time_utc'] else datetime.min.replace(tzinfo=UTC), reverse=True)
        return results[:20]  # Return top 20

    except Exception as e:
        print(f"Error getting recent executions: {e}", file=sys.stderr)
        return []


def main():
    """Test all queries and output as JSON"""
    ib = IB()

    try:
        # Connect to IB Gateway
        ib.connect('127.0.0.1', 7497, clientId=950, timeout=10)

        # Get account
        accounts = ib.managedAccounts()
        if not accounts:
            print(json.dumps({'error': 'No managed accounts found'}))
            return

        account = accounts[0]

        # Get all data
        result = {
            'ok': True,
            'account': account,
            'timestamp': datetime.now(NY).isoformat()
        }

        # P/L data
        pnl_data = get_pnl_data(ib, account)
        result['pnl'] = pnl_data

        # Recent trades
        trades = get_recent_trades(ib, days=14)
        result['recent_trades_count'] = len(trades)
        result['recent_trades'] = [
            {
                'time': t['time_ny'].strftime('%Y-%m-%d %H:%M:%S %Z'),
                'symbol': getattr(t['contract'], 'symbol', ''),
                'status': t['status'],
                'order_type': getattr(t['order'], 'orderType', ''),
                'action': getattr(t['order'], 'action', ''),
            }
            for t in trades[:5]  # Just show first 5 in summary
        ]

        # Recent executions
        executions = get_recent_executions(ib, days=7)
        result['recent_executions_count'] = len(executions)
        result['recent_executions'] = [
            {
                'time': e['time_ny'].strftime('%Y-%m-%d %H:%M:%S %Z') if e['time_ny'] else 'Unknown',
                'symbol': e['symbol'],
                'side': e['side'],
                'shares': e['shares'],
                'price': e['price'],
                'pnl': e['pnl'],
                'open_close': e['open_close']
            }
            for e in executions[:5]  # Just show first 5 in summary
        ]

        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)}), file=sys.stderr)
        return 1
    finally:
        try:
            ib.disconnect()
        except:
            pass

    return 0


if __name__ == '__main__':
    sys.exit(main())
