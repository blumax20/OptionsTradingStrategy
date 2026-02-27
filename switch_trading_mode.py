#!/usr/bin/env python3
"""
switch_trading_mode.py — Switch between paper and live IB trading.

Usage:
    python switch_trading_mode.py paper   # Switch to paper trading (port 7497)
    python switch_trading_mode.py live    # Switch to live trading   (port 7496)
    python switch_trading_mode.py status  # Show current mode

Updates 4 files atomically:
    1. InteractiveBrokersTrader/ib_config.py         -- IB_PORT
    2. C:\\OptionsHistory\\bin\\IB_Watchdog.ps1      -- $IB_GW_PORT
    3. C:\\OptionsHistory\\bin\\Health.ps1           -- $IB_PORT
    4. C:\\IBC\\config.ini                           -- TradingMode, ApiPort, OverrideTwsApiPort

Then restarts IBGateway via NSSM so IBC auto-logs in to the new mode.
IBC handles all login dialogs automatically using saved credentials in config.ini.
"""

import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
SCRIPT_DIR   = Path(__file__).resolve().parent
IB_CONFIG_PY = SCRIPT_DIR / "InteractiveBrokersTrader" / "ib_config.py"
WATCHDOG_PS1 = Path(r"C:\OptionsHistory\bin\IB_Watchdog.ps1")
HEALTH_PS1   = Path(r"C:\OptionsHistory\bin\Health.ps1")
IBC_CONFIG   = Path(r"C:\IBC\config.ini")

# ---------------------------------------------------------------------------
# Mode definitions
# ---------------------------------------------------------------------------
MODES = {
    "paper": {
        "port":        7497,
        "trading_mode": "paper",
        "label":       "PAPER",
    },
    "live": {
        "port":        7496,
        "trading_mode": "live",
        "label":       "LIVE",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    print(f"  [OK] {path}")


def _sub(pattern: str, replacement: str, text: str, flags=0) -> str:
    new, n = re.subn(pattern, replacement, text, flags=flags)
    if n == 0:
        raise ValueError(f"Pattern not found: {pattern!r}")
    return new


def _current_port_from_ib_config() -> int | None:
    """Read IB_PORT from ib_config.py; return None if unreadable."""
    try:
        text = _read(IB_CONFIG_PY)
        m = re.search(r"^IB_PORT\s*[:=]\s*int\s*=\s*(\d+)", text, re.MULTILINE)
        if m:
            return int(m.group(1))
        # fallback: plain assignment
        m = re.search(r"^IB_PORT\s*=\s*(\d+)", text, re.MULTILINE)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Per-file update functions
# ---------------------------------------------------------------------------

def update_ib_config_py(port: int) -> None:
    text = _read(IB_CONFIG_PY)
    text = _sub(
        r"(^IB_PORT\s*:\s*int\s*=\s*)\d+",
        rf"\g<1>{port}",
        text,
        flags=re.MULTILINE,
    )
    _write(IB_CONFIG_PY, text)


def update_watchdog_ps1(port: int) -> None:
    text = _read(WATCHDOG_PS1)
    text = _sub(
        r"(\$IB_GW_PORT\s*=\s*)\d+",
        rf"\g<1>{port}",
        text,
    )
    _write(WATCHDOG_PS1, text)


def update_health_ps1(port: int) -> None:
    text = _read(HEALTH_PS1)
    text = _sub(
        r"(\$IB_PORT\s*=\s*)\d+",
        rf"\g<1>{port}",
        text,
    )
    _write(HEALTH_PS1, text)


def update_ibc_config(port: int, trading_mode: str) -> None:
    """Update TradingMode, ApiPort, and OverrideTwsApiPort in C:\\IBC\\config.ini."""
    text = _read(IBC_CONFIG)

    text = _sub(
        r"(?i)(^TradingMode\s*=\s*)\S+",
        rf"\g<1>{trading_mode}",
        text,
        flags=re.MULTILINE,
    )
    text = _sub(
        r"(?i)(^ApiPort\s*=\s*)\d+",
        rf"\g<1>{port}",
        text,
        flags=re.MULTILINE,
    )
    text = _sub(
        r"(?i)(^OverrideTwsApiPort\s*=\s*)\d+",
        rf"\g<1>{port}",
        text,
        flags=re.MULTILINE,
    )
    _write(IBC_CONFIG, text)


# ---------------------------------------------------------------------------
# IBGateway restart
# ---------------------------------------------------------------------------

def restart_ibgateway() -> None:
    """Restart the IBGateway Windows service via NSSM."""
    print("\nRestarting IBGateway service (IBC will auto-login)...")
    try:
        result = subprocess.run(
            ["nssm", "restart", "IBGateway"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print("  [OK] IBGateway service restarted")
        else:
            print(f"  [WARN] nssm restart returned code {result.returncode}")
            if result.stderr.strip():
                print(f"         {result.stderr.strip()}")
            print("  You may need to restart IBGateway manually.")
    except FileNotFoundError:
        print("  [WARN] nssm not found in PATH.")
        print("  Please restart the IBGateway service manually in Windows Services.")
    except subprocess.TimeoutExpired:
        print("  [WARN] nssm restart timed out. Restart IBGateway manually.")


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def show_status() -> None:
    port = _current_port_from_ib_config()
    if port is None:
        print("Could not determine current mode from ib_config.py")
        return
    if port == 7497:
        print(f"Current mode: PAPER (port {port})")
    elif port == 7496:
        print(f"Current mode: LIVE  (port {port})")
    else:
        print(f"Current mode: UNKNOWN (port {port})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1].lower() not in ("paper", "live", "status"):
        print(__doc__)
        sys.exit(1)

    action = sys.argv[1].lower()

    if action == "status":
        show_status()
        return

    mode = MODES[action]
    port = mode["port"]
    trading_mode = mode["trading_mode"]
    label = mode["label"]

    # Check if already in the requested mode
    current = _current_port_from_ib_config()
    if current == port:
        print(f"Already in {label} mode (port {port}). No changes made.")
        return

    print(f"\nSwitching to {label} trading (port {port})...\n")

    errors = []

    for name, fn, args in [
        ("ib_config.py",    update_ib_config_py, (port,)),
        ("IB_Watchdog.ps1", update_watchdog_ps1, (port,)),
        ("Health.ps1",      update_health_ps1,   (port,)),
        ("C:\\IBC\\config.ini", update_ibc_config, (port, trading_mode)),
    ]:
        try:
            fn(*args)
        except Exception as e:
            errors.append(f"  [FAIL] {name}: {e}")

    if errors:
        print("\nErrors encountered:")
        for e in errors:
            print(e)
        print("\nPlease fix the above before restarting IBGateway.")
        sys.exit(1)

    restart_ibgateway()

    print(f"\nDone. System is now configured for {label} trading.")
    if action == "live":
        print("""
LIVE TRADING CHECKLIST (verify before placing orders):
  1. IB Gateway shows 'Live Trading' in title bar (not 'Paper Trading')
  2. 'Read-Only API' is OFF in IB Gateway API settings
  3. Market data subscriptions are active (Error 354 = subscription missing)
  4. Run Health.ps1 to confirm all services healthy on port 7496
""")
    else:
        print("Paper trading active. Run Health.ps1 to confirm port 7497 healthy.")


if __name__ == "__main__":
    main()
