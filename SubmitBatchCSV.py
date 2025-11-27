import pandas as pd
import json
import requests
import os
import argparse
import sys
from typing import List
import time
import random
from urllib.parse import urlparse, urlunparse
from datetime import datetime
import hashlib

def _yy_mm_dd_today_ny() -> str:
    try:
        # Local time is fine; caller provides explicit paths when needed
        return datetime.now().strftime("%y_%m_%d")
    except Exception:
        return datetime.utcnow().strftime("%y_%m_%d")

def send_batch_webhook(csv_file: str,
                       webhook_urls: List[str],
                       *,
                       chunk_size: int = 4,
                       timeout_s: int = 90,
                       inter_chunk_sleep_s: float = 0.5,
                       health_probe: bool = True,
                       strict: bool = False,
                       max_retries: int = 2,
                       retry_backoff: float = 1.5,
                       retry_jitter: float = 0.25,
                       health_url: str = None) -> bool:
    """Send all descriptions as a batch JSON to webhook, in chunks.

    - Splits the CSV rows into chunks (default 8) to avoid very long single requests that can
      hit Cloudflare/Tunnel timeouts when the server has to fetch IB data serially.
    - Tries each URL in order for each chunk.
    - `timeout_s` controls the per-request HTTP timeout.
    - `inter_chunk_sleep_s` adds a small delay between chunks to reduce load bursts.
    """
    # Derive a /health endpoint (best-effort) from the first URL, to sanity-check after timeouts.
    probe_url = None
    if health_url:
        probe_url = health_url
    else:
        try:
            if webhook_urls:
                u = urlparse(webhook_urls[0])
                probe_url = urlunparse((u.scheme, u.netloc, "/health", "", "", ""))
        except Exception:
            probe_url = None

    def _probe_health() -> bool:
        if not (health_probe and probe_url):
            return False
        try:
            r = requests.get(probe_url, timeout=5)
            return r.status_code == 200
        except requests.exceptions.RequestException:
            return False

    # Read the CSV file
    df = pd.read_csv(csv_file)

    # Build messages list
    messages = []
    for _, row in df.iterrows():
        # Robust field extraction (accept string/hash Alert IDs; tolerate NaNs)
        desc = row.get("Description")
        aid  = row.get("Alert ID")
        tick = row.get("Ticker")
        ts   = row.get("Time")

        # Skip rows that have no description or ticker
        if pd.isna(desc) or pd.isna(tick):
            continue

        # Use string form of Alert ID without forcing int; allow hash IDs like '22478bbad7'
        alert_id = None if pd.isna(aid) else str(aid).strip()

        message_data = {
            "message": str(desc).strip(),
            "alert_id": alert_id,
            "ticker": str(tick).strip(),
            "timestamp": None if pd.isna(ts) else str(ts).strip()
        }
        messages.append(message_data)

    accepted_total = 0
    rejected_total = 0

    if not messages:
        print("✖ No rows found in CSV; nothing to send.")
        return False

    def _post_chunk(chunk_idx: int, total_chunks: int, payload: dict) -> tuple[str, str | None]:
        """
        Returns: (status, last_err)
          status in {"ok","warn","fail"}
          - ok   : got HTTP 200 from some URL
          - warn : only timeouts/530s; server may still process
          - fail : hard HTTP errors (non-200, non-530) or connection errors
        """
        nonlocal accepted_total, rejected_total
        last_err = None
        only_edge_or_timeout = True  # stays True while we only see 530/timeout
        for u_idx, url in enumerate(webhook_urls, 1):
            for rtry in range(max_retries + 1):
                try:
                    print(f"[chunk {chunk_idx}/{total_chunks}] Attempt {u_idx}/{len(webhook_urls)} (retry {rtry}/{max_retries}) → POST {url}")
                    response = requests.post(
                        url,
                        json=payload,
                        headers={'Content-Type': 'application/json'},
                        timeout=timeout_s
                    )
                    txt = (response.text or "").strip()
                    if response.status_code == 530 or ('Cloudflare Tunnel error' in txt):
                        print(f"⚠︎ Cloudflare/Tunnel issue (status {response.status_code}). Response trimmed:\n{txt[:200]}…")
                        last_err = f"cloudflare_530@{url}"
                        # retry this URL
                        if rtry < max_retries:
                            sleep_s = inter_chunk_sleep_s * (retry_backoff ** rtry) + random.uniform(0, retry_jitter)
                            time.sleep(sleep_s)
                            continue
                        break
                    if response.status_code != 200:
                        print(f"✖ Batch webhook failed - Status: {response.status_code}")
                        print(f"Response: {txt[:400]}…")
                        last_err = f"http_{response.status_code}@{url}"
                        only_edge_or_timeout = False
                        # non-200 non-530 → do not retry further on this URL
                        break
                    # 200 OK
                    c_ok = c_bad = 0
                    try:
                        data = response.json()
                        if isinstance(data, dict) and isinstance(data.get("results"), list):
                            for item in data["results"]:
                                if isinstance(item, dict):
                                    if item.get("_error") is False:
                                        c_ok += 1
                                    elif item.get("error"):
                                        c_bad += 1
                    except Exception:
                        pass
                    accepted_total += c_ok
                    rejected_total += c_bad
                    print(f"✔ Chunk {chunk_idx} accepted. (accepted={c_ok}, rejected={c_bad}) Response (trimmed): {txt[:400]}…")
                    return ("ok", None)
                except requests.exceptions.ReadTimeout as e:
                    print(f"⚠︎ Chunk {chunk_idx} timed out waiting for response from {url}: {e}")
                    last_err = f"timeout@{url}"
                    # retry on timeout
                    if rtry < max_retries:
                        sleep_s = inter_chunk_sleep_s * (retry_backoff ** rtry) + random.uniform(0, retry_jitter)
                        time.sleep(sleep_s)
                        continue
                    # exhausted retries on this URL
                    break
                except requests.exceptions.RequestException as e:
                    print(f"✖ Chunk {chunk_idx} request failed for {url}: {e}")
                    last_err = str(e)
                    only_edge_or_timeout = False
                    # do not retry different exception types
                    break
        # No URL returned 200
        if only_edge_or_timeout:
            # Probe health to give operator confidence
            healthy = _probe_health()
            if healthy:
                print(f"⚠︎ Chunk {chunk_idx}: no immediate 200 (timeouts/530), but /health is OK — likely still processing server-side.")
            else:
                print(f"⚠︎ Chunk {chunk_idx}: no immediate 200 (timeouts/530), and /health could not be confirmed.")
            return ("warn", last_err)
        else:
            print(f"✖ Chunk {chunk_idx} failed for all URLs.")
            if last_err:
                print(f"  Last error: {last_err}")
            return ("fail", last_err)

    # Split into chunks and send
    total = len(messages)
    chunks = [messages[i:i+chunk_size] for i in range(0, total, chunk_size)]
    print(f"Submitting {total} messages in {len(chunks)} chunk(s) of ≤ {chunk_size} row(s) each (timeout {timeout_s}s)…")

    print(f"Retry policy: max_retries={max_retries}, backoff={retry_backoff}, jitter≤{retry_jitter}s; health_probe={'on' if health_probe else 'off'}")

    any_ok = False
    any_warn = False
    any_fail = False

    for i, chunk in enumerate(chunks, start=1):
        batch_payload = {"data": chunk}
        status, _ = _post_chunk(i, len(chunks), batch_payload)
        if status == "ok":
            any_ok = True
        elif status == "warn":
            any_warn = True
        else:
            any_fail = True

        if i < len(chunks) and inter_chunk_sleep_s > 0:
            time.sleep(inter_chunk_sleep_s)

    print(f"Summary across all chunks: accepted={accepted_total} rejected={rejected_total}")

    if any_fail and (strict or not any_ok):
        print("✖ One or more chunks failed (strict failure).")
        return False
    if any_warn:
        print("⚠︎ Completed with warnings (timeouts/edge 530). Rows may still be processing on the server.")
    if any_ok and not any_fail:
        print("✔ All chunks accepted.")
    return True

def generate_alert_csv_from_combined(combined_csv_path: str, out_csv_path: str | None = None) -> str:
    """
    Build a TradingView-like alert CSV (columns: Alert ID, Ticker, Time, Description)
    from a listener-style combined CSV that includes 'symbol', 'signal_type', 'expiration',
    'atm_strike', 'otm_strike_call', 'otm_strike_put', 'timestamp_ny'.

    Returns the path to the generated CSV.
    """
    import pandas as _pd
    import numpy as _np
    from pathlib import Path as _Path

    src = _Path(combined_csv_path)
    if not src.exists():
        raise FileNotFoundError(f"Combined CSV not found: {src}")

    df = _pd.read_csv(src)

    # Normalize column names we rely on
    cols_needed = ["symbol","signal_type","expiration","atm_strike","otm_strike_call","otm_strike_put","timestamp_ny"]
    for c in cols_needed:
        if c not in df.columns:
            # Create missing columns as NaN; we will handle gracefully
            df[c] = _np.nan

    # Keep only rows that actually carry a signal we understand
    valid_signals = {"CALL_OPEN","PUT_OPEN","CLOSE","CALL_CLOSE","PUT_CLOSE"}
    s_series = df["signal_type"].astype(str).str.upper()
    mask = s_series.isin(valid_signals)
    sig = df.loc[mask].copy()

    if sig.empty:
        # Create an empty output with the correct headers for a clean no-op
        out = _Path(out_csv_path) if out_csv_path else (src.parent / "TradingView_Alerts_for_batch.csv")
        _pd.DataFrame(columns=["Alert ID","Ticker","Time","Description"]).to_csv(out, index=False)
        return str(out)

    # Coerce useful fields
    sig["symbol"] = sig["symbol"].astype(str).str.strip().str.upper()
    # Timestamps
    def _ts(val):
        if isinstance(val, str) and val.strip():
            return val.strip()
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Row → Description generator
    def _desc(row):
        sym  = row.get("symbol", "")
        st   = str(row.get("signal_type","")).upper()
        exp  = str(row.get("expiration","")).strip()
        atm  = row.get("atm_strike")
        kC   = row.get("otm_strike_call")
        kP   = row.get("otm_strike_put")

        def _fmt(x):
            try:
                f = float(x)
                return f"{f:.2f}".rstrip('0').rstrip('.')
            except Exception:
                return ""

        if st == "CALL_OPEN":
            return f"{sym}: OPEN CALL debit spread { _fmt(atm) }/{ _fmt(kC) } exp {exp}"
        if st == "PUT_OPEN":
            return f"{sym}: OPEN PUT debit spread { _fmt(atm) }/{ _fmt(kP) } exp {exp}"
        if st in ("CLOSE","CALL_CLOSE","PUT_CLOSE"):
            side = "CALL" if st == "CALL_CLOSE" else "PUT" if st == "PUT_CLOSE" else "ANY"
            # try to include whichever leg is present
            legs = f"{_fmt(atm)}/{_fmt(kC) if _fmt(kC) else _fmt(kP)}"
            return f"{sym}: CLOSE {side} spread {legs} exp {exp}"
        return f"{sym}: SIGNAL {st}"

    # Build output frame
    out_rows = []
    for idx, row in sig.iterrows():
        sym = row.get("symbol","")
        ts  = _ts(row.get("timestamp_ny"))
        desc= _desc(row)
        # Build a stable Alert ID using a tiny hash
        h = hashlib.md5(f"{sym}|{ts}|{desc}".encode("utf-8")).hexdigest()[:10]
        out_rows.append({
            "Alert ID": h,
            "Ticker": sym,
            "Time": ts,
            "Description": desc,
        })

    out_df = _pd.DataFrame(out_rows, columns=["Alert ID","Ticker","Time","Description"])

    out_path = _Path(out_csv_path) if out_csv_path else (src.parent / "TradingView_Alerts_for_batch.csv")
    out_df.to_csv(out_path, index=False)
    return str(out_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Submit TradingView CSV alerts as a batch to /webhook_batch.")
    parser.add_argument("--csv", dest="csv_file", default=os.environ.get("ALERT_CSV", ""), help="Path to TradingView CSV (default: $ALERT_CSV)")
    parser.add_argument("--url", dest="urls", action="append", help="Webhook URL (can be passed multiple times). If omitted, uses $BATCH_WEBHOOK_URLS or default cloud URL.")
    parser.add_argument("--chunk-size", type=int, default=8, help="Number of rows per chunk (default 8).")
    parser.add_argument("--timeout", type=int, default=45, help="HTTP timeout in seconds per request (default 45).")
    parser.add_argument("--sleep", type=float, default=0.5, help="Delay in seconds between chunk submissions (default 0.5).")
    parser.add_argument("--local", action="store_true", help="Use local webhook URL http://127.0.0.1:5001/webhook_batch")
    parser.add_argument("--strict", action="store_true", help="Treat any non-200 as failure (old behavior).")
    parser.add_argument("--no-health-probe", action="store_true", help="Disable health checks after timeouts.")
    parser.add_argument("--from-combined", dest="from_combined", default="", help="Path to combined_listener_spreads.csv to auto-build a TradingView alerts CSV.")
    parser.add_argument("--out-csv", dest="out_csv", default="", help="Optional path to write the generated alerts CSV (used with --from-combined).")
    parser.add_argument("--max-retries", type=int, default=2, help="Max retries per URL on timeouts/edge errors (default 2).")
    parser.add_argument("--retry-backoff", type=float, default=1.5, help="Exponential backoff multiplier between retries (default 1.5).")
    parser.add_argument("--retry-jitter", type=float, default=0.25, help="Random jitter seconds added to retry sleeps (default 0.25).")
    parser.add_argument("--health-url", dest="health_url", default=os.environ.get("BATCH_HEALTH_URL", ""), help="Override /health probe URL.")
    args = parser.parse_args()

    # If the user supplied a combined CSV, generate a TradingView-style alert CSV first.
    if args.from_combined:
        try:
            gen_path = generate_alert_csv_from_combined(args.from_combined, args.out_csv or "")
            print(f"✔ Built alerts CSV from combined: {gen_path}")
            # If --csv was not supplied, use the generated path for submission.
            if not args.csv_file:
                args.csv_file = gen_path
        except Exception as e:
            print(f"✖ Failed to build alerts CSV from combined: {e}")
            sys.exit(3)

    # Build URL list: --url (multi) > env var > default
    urls: List[str] = []
    if args.urls:
        urls = args.urls
    else:
        env_urls = os.environ.get("BATCH_WEBHOOK_URLS", "").strip()
        if env_urls:
            urls = [u.strip() for u in env_urls.split(",") if u.strip()]
    if not urls:
        # Default to public Cloudflare hostname
        urls = ["https://signals.hyperbukit.com/webhook_batch"]

    if args.local:
        urls = ["http://127.0.0.1:5001/webhook_batch"]

    # Auto-tune defaults for Cloudflare/public runs unless user overrode
    is_local = args.local or any(u.startswith("http://127.0.0.1") for u in urls)
    if not is_local:
        # If user didn't override from defaults, bump timeout and shrink chunk size for reliability
        if args.timeout == 45:
            print("ℹ︎ Auto-tuning timeout from 45 → 90s for Cloudflare/public endpoint.")
            args.timeout = 90
        if args.chunk_size == 8:
            print("ℹ︎ Auto-tuning chunk size from 8 → 4 for Cloudflare/public endpoint.")
            args.chunk_size = 4
        if args.sleep == 0.5:
            print("ℹ︎ Auto-tuning inter-chunk sleep from 0.5 → 0.75s for Cloudflare/public endpoint.")
            args.sleep = 0.75

    csv_path = args.csv_file or \
               os.environ.get("ALERT_CSV", "/Users/maximilian-alexanderneidhardt/Downloads/TradingView_Alerts_Log_latest.csv")

    if not os.path.exists(csv_path):
        print(f"✖ CSV not found: {csv_path}")
        sys.exit(2)

    ok = send_batch_webhook(
        csv_path,
        urls,
        chunk_size=args.chunk_size,
        timeout_s=args.timeout,
        inter_chunk_sleep_s=args.sleep,
        health_probe=not args.no_health_probe,
        strict=args.strict,
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
        retry_jitter=args.retry_jitter,
        health_url=(args.health_url or None)
    )
    sys.exit(0 if ok else 1)