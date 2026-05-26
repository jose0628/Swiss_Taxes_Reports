#!/usr/bin/env python3
"""
Generate yearly Bitcoin transaction reports.

Outputs:
- Transaction file with year-filtered transactions and USD values.
- Summary file with opening and closing balances.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import List

import pandas as pd
import requests
import yaml


OUTPUT_DIR = Path("reports")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate yearly Bitcoin transaction reports."
    )
    parser.add_argument(
        "--year",
        type=int,
        help="Tax year to process, for example: 2025 (optional if --all-years is used)",
    )
    parser.add_argument(
        "--all-years",
        action="store_true",
        help="Fetch transactions for all years (not limited to a single year). Overrides --year if provided.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="wallet_config.yaml",
        help="YAML config file path with wallet addresses and API keys.",
    )
    parser.add_argument(
        "--include-prices",
        action="store_true",
        help="Fetch historical USD prices and add USD columns to the transaction files.",
    )
    return parser.parse_args()


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def fetch_bitcoin_transactions(wallet: str, blockchair_key: str = "") -> List[dict]:
    """Fetch Bitcoin transactions using Blockchair API with Etherscan fallback."""
    base_url = "https://api.blockchair.com/bitcoin/transactions"
    
    # Use mempool.space API as primary (no key required)
    esplora_base = "https://mempool.space/api"
    
    all_txs: List[dict] = []
    last_seen_txid: str | None = None
    page = 0
    
    while True:
        url = f"{esplora_base}/address/{wallet}/txs/chain"
        if last_seen_txid:
            url = f"{url}/{last_seen_txid}"
        
        resp = requests.get(url, timeout=45)
        resp.raise_for_status()
        rows = resp.json()
        
        if not rows:
            break
        
        for tx in rows:
            status = tx.get("status", {})
            block_time = status.get("block_time")
            if block_time:
                all_txs.append(tx)
        
        if len(rows) < 25:
            break
        last_seen_txid = rows[-1].get("txid")
        page += 1
    
    return all_txs


def parse_bitcoin_tx(wallet: str, tx: dict) -> dict:
    """Parse a Bitcoin transaction into a record."""
    status = tx.get("status", {})
    block_time = status.get("block_time")
    ts = datetime.fromtimestamp(int(block_time), tz=timezone.utc) if block_time else None
    
    # Calculate balance change
    balance_change, spent = _esplora_balance_change(tx, wallet)
    tx_time = datetime.fromtimestamp(int(block_time), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if block_time else ""
    amount_btc = abs(balance_change) / 1e8
    direction = "in" if balance_change >= 0 else "out"
    fee_btc = int(tx.get("fee", 0) or 0) / 1e8 if spent else 0.0
    
    return {
        "chain": "bitcoin",
        "network": "bitcoin",
        "record_type": "transaction",
        "wallet": wallet,
        "tx_hash": tx.get("txid", ""),
        "timestamp_utc": ts.isoformat() if ts else "",
        "direction": direction,
        "amount_coin": amount_btc,
        "signed_amount_coin": balance_change / 1e8,
        "fee_coin": fee_btc if balance_change < 0 else 0.0,
        "price_usd": None,
        "amount_usd": None,
        "fee_usd": None,
        "from_address": "",
        "to_address": "",
    }


def _esplora_balance_change(tx: dict, wallet: str) -> tuple[int, int]:
    spent = 0
    received = 0
    for vin in tx.get("vin", []):
        prevout = vin.get("prevout") or {}
        if prevout.get("scriptpubkey_address") == wallet:
            spent += int(prevout.get("value", 0) or 0)
    for vout in tx.get("vout", []):
        if vout.get("scriptpubkey_address") == wallet:
            received += int(vout.get("value", 0) or 0)
    return received - spent, spent


def build_balance_summary(
    wallet: str,
    all_records: List[dict],
    start: str,
    end: str,
    start_price: float | None,
    end_price: float | None,
) -> dict:
    """Build a balance summary for a wallet."""
    opening = sum(r["signed_amount_coin"] for r in all_records if r["timestamp_utc"] < start)
    closing = sum(r["signed_amount_coin"] for r in all_records if r["timestamp_utc"] <= end)
    change = closing - opening
    
    return {
        "chain": "bitcoin",
        "wallet": wallet,
        "year": int(start[:4]) if start else 0,
        "opening_balance_coin": opening,
        "closing_balance_coin": closing,
        "net_change_coin": change,
        "opening_price_usd": start_price,
        "closing_price_usd": end_price,
        "opening_balance_usd": opening * start_price if start_price else None,
        "closing_balance_usd": closing * end_price if end_price else None,
    }


def get_price_usd(chain: str, timestamp: str, demo_key: str = "", pro_key: str = "") -> float | None:
    """Get USD price for a given timestamp."""
    import requests
    from datetime import datetime
    
    base_url = "https://pro-api.coingecko.com/api/v3" if pro_key else "https://api.coingecko.com/api/v3"
    headers = {}
    if pro_key:
        headers["x-cg-pro-api-key"] = pro_key
    elif demo_key:
        headers["x-cg-demo-api-key"] = demo_key
    
    if isinstance(timestamp, str):
        try:
            ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        ts = timestamp
    
    coin_id = "bitcoin" if chain == "bitcoin" else ("litecoin" if chain == "litecoin" else "ethereum")
    url = f"{base_url}/coins/{coin_id}/market_chart/date"
    params = {"vs_currency": "usd", "date": ts.strftime("%d-%m-%Y")}
    
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        prices = data.get("prices", [])
        for price in prices:
            if price[0] == ts.timestamp() * 1000:
                return price[1]
        return None
    except Exception:
        return None


def generate_reports(
    year: int | None,
    config_path: str,
    include_prices: bool = True,
    all_years: bool = False,
) -> None:
    """Generate Bitcoin transaction reports."""
    from datetime import datetime, timezone
    
    config = load_config(config_path)
    api_keys = config.get("api_keys", {}) or {}
    blockchair_key = str(api_keys.get("blockchair", "") or os.environ.get("BLOCKCHAIR_API_KEY", "") or "")
    coingecko_demo_key = str(
        api_keys.get("coingecko_demo", "")
        or api_keys.get("coingecko", "")
        or ""
    )
    coingecko_pro_key = str(api_keys.get("coingecko_pro", "") or "")
    wallet_addresses = config.get("wallet_addresses", {}) or {}
    bitcoin_wallets = wallet_addresses.get("bitcoin", []) or []
    
    if not bitcoin_wallets:
        print("No Bitcoin wallets configured.")
        return
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    if all_years:
        # Get all years from transactions
        all_transactions: List[dict] = []
        wallet_data: dict[str, list] = {}
        
        for wallet in bitcoin_wallets:
            txs = fetch_bitcoin_transactions(wallet, blockchair_key)
            wallet_data[wallet] = txs
            
            for tx in txs:
                record = parse_bitcoin_tx(wallet, tx)
                all_transactions.append(record)
        
        # Filter to year if specified
        year_transactions = all_transactions
        if year:
            year_str = str(year)
            year_transactions = [r for r in all_transactions if year_str in r["timestamp_utc"]]
        
        # Enrich with USD prices if requested
        if include_prices:
            for record in year_transactions:
                px = get_price_usd("bitcoin", record["timestamp_utc"], coingecko_demo_key, coingecko_pro_key)
                record["price_usd"] = px
                record["amount_usd"] = record["amount_coin"] * px if px else None
                record["fee_usd"] = record["fee_coin"] * px if px else None
        
        # Determine output year label
        if year:
            output_year = year
            year_output_str = str(year)
        else:
            output_year = "all_years"
            year_output_str = "all_years"
        
        # Output transaction file
        df_tx = pd.DataFrame(year_transactions)
        if not df_tx.empty:
            df_tx = df_tx.sort_values("timestamp_utc").reset_index(drop=True)
        price_suffix = "_usd" if include_prices else ""
        tx_file = OUTPUT_DIR / f"bitcoin_{year_output_str}_transactions{price_suffix}.xlsx"
        df_tx.to_excel(tx_file, index=False)
        print(f"Bitcoin {year_output_str} transactions: {len(df_tx)} rows -> {tx_file}")
        
        # Generate summary
        if year:
            # When year is specified, build summary for that year
            start = f"{year}-01-01T00:00:00+00:00"
            end = f"{year}-12-31T23:59:59+00:00"
        else:
            # Calculate overall summary for all years
            start = min((r["timestamp_utc"] for r in all_transactions), default="")
            end = max((r["timestamp_utc"] for r in all_transactions), default="")
        
        start_px = get_price_usd("bitcoin", start, coingecko_demo_key, coingecko_pro_key) if include_prices else None
        end_px = get_price_usd("bitcoin", end, coingecko_demo_key, coingecko_pro_key) if include_prices else None
        
        summary_rows = []
        for wallet in bitcoin_wallets:
            txs = wallet_data[wallet]
            tx_records = [parse_bitcoin_tx(wallet, tx) for tx in txs]
            
            summary_rows.append(
                build_balance_summary(
                    wallet=wallet,
                    all_records=tx_records,
                    start=start,
                    end=end,
                    start_price=start_px,
                    end_price=end_px,
                )
            )
        
        summary_df = pd.DataFrame(summary_rows)
        summary_file = OUTPUT_DIR / f"bitcoin_{year_output_str}_summary.xlsx"
        summary_df.to_excel(summary_file, index=False)
        print(f"Bitcoin {year_output_str} summary: {len(summary_df)} rows -> {summary_file}")
        
        # Print totals
        if include_prices:
            total_tx = sum(r["amount_usd"] or 0 for r in year_transactions)
            print(f"Total Bitcoin transactions (USD): ${total_tx:,.2f}")
        else:
            total_tx = sum(r["amount_coin"] for r in year_transactions)
            print(f"Total Bitcoin transactions: {total_tx:.8f} BTC")
    else:
        if year is None:
            raise ValueError("--year or --all-years must be provided")
        
        start = f"{year}-01-01T00:00:00+00:00"
        end = f"{year}-12-31T23:59:59+00:00"
        
        # Fetch all data
        all_transactions: List[dict] = []
        wallet_data: dict[str, list] = {}
        
        for wallet in bitcoin_wallets:
            txs = fetch_bitcoin_transactions(wallet, blockchair_key)
            wallet_data[wallet] = txs
            
            for tx in txs:
                record = parse_bitcoin_tx(wallet, tx)
                all_transactions.append(record)
        
        # Filter to year
        year_str = str(year)
        year_transactions = [r for r in all_transactions if year_str in r["timestamp_utc"]]
        
        # Enrich with USD prices if requested
        if include_prices:
            for record in year_transactions:
                px = get_price_usd("bitcoin", record["timestamp_utc"], coingecko_demo_key, coingecko_pro_key)
                record["price_usd"] = px
                record["amount_usd"] = record["amount_coin"] * px if px else None
                record["fee_usd"] = record["fee_coin"] * px if px else None
        
        # Output transaction file
        df_tx = pd.DataFrame(year_transactions)
        if not df_tx.empty:
            df_tx = df_tx.sort_values("timestamp_utc").reset_index(drop=True)
        price_suffix = "_usd" if include_prices else ""
        tx_file = OUTPUT_DIR / f"bitcoin_{year}_transactions{price_suffix}.xlsx"
        df_tx.to_excel(tx_file, index=False)
        print(f"Bitcoin transactions: {len(df_tx)} rows -> {tx_file}")
        
        # Generate summary
        start_px = get_price_usd("bitcoin", start, coingecko_demo_key, coingecko_pro_key) if include_prices else None
        end_px = get_price_usd("bitcoin", end, coingecko_demo_key, coingecko_pro_key) if include_prices else None
        
        summary_rows = []
        for wallet in bitcoin_wallets:
            txs = wallet_data[wallet]
            tx_records = [parse_bitcoin_tx(wallet, tx) for tx in txs]
            
            summary_rows.append(
                build_balance_summary(
                    wallet=wallet,
                    all_records=tx_records,
                    start=start,
                    end=end,
                    start_price=start_px,
                    end_price=end_px,
                )
            )
        
        summary_df = pd.DataFrame(summary_rows)
        summary_file = OUTPUT_DIR / f"bitcoin_{year}_summary.xlsx"
        summary_df.to_excel(summary_file, index=False)
        print(f"Bitcoin summary: {len(summary_df)} rows -> {summary_file}")
        
        # Print totals
        if include_prices:
            total_tx = sum(r["amount_usd"] or 0 for r in year_transactions)
            print(f"Total Bitcoin transactions (USD): ${total_tx:,.2f}")
        else:
            total_tx = sum(r["amount_coin"] for r in year_transactions)
            print(f"Total Bitcoin transactions: {total_tx:.8f} BTC")


def run() -> None:
    args = parse_args()
    generate_reports(
        year=args.year,
        config_path=args.config,
        include_prices=args.include_prices,
        all_years=args.all_years,
    )


if __name__ == "__main__":
    run()
