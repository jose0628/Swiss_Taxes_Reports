#!/usr/bin/env python3
"""
Generate yearly Ethereum transaction reports.

Outputs:
- Transaction file with year-filtered transactions and USD values.
- Summary file with opening and closing balances.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import List
from datetime import datetime
import datetime
import pandas as pd
import requests
import yaml


OUTPUT_DIR = Path("reports")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate yearly Ethereum transaction reports."
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


def fetch_ethereum(wallet: str, api_key: str) -> List[dict]:
    """Fetch Ethereum transactions using Etherscan API."""
    url = "https://api.etherscan.io/v2/api"
    params = {
        "chainid": 1,
        "module": "account",
        "action": "txlist",
        "address": wallet,
        "startblock": 0,
        "endblock": 99999999,
        "sort": "asc",
        "apikey": api_key,
    }
    resp = requests.get(url, params=params, timeout=45)
    resp.raise_for_status()
    payload = resp.json()
    result = payload.get("result", [])
    if isinstance(result, str):
        if "No transactions found" in result:
            return []
        raise RuntimeError(
            f"Etherscan API error for Ethereum address {wallet}: "
            f"{payload.get('message', 'NOTOK')} - {result}"
        )
    if not isinstance(result, list):
        raise RuntimeError(f"Etherscan returned an unexpected response for Ethereum address {wallet}: {payload}")
    return result


def parse_ethereum_tx(wallet: str, tx: dict) -> dict:
    """Parse an Ethereum transaction into a record."""
    ts = datetime.fromtimestamp(int(tx["timeStamp"]), tz=datetime.timezone.utc)
    value_eth = int(tx["value"]) / 1e18
    from_addr = tx.get("from", "").lower()
    to_addr = tx.get("to", "").lower()
    is_out = from_addr == wallet.lower()
    direction = "out" if is_out else "in"
    signed_amount = -value_eth if is_out else value_eth
    gas_used = int(tx.get("gasUsed", "0"))
    gas_price = int(tx.get("gasPrice", "0"))
    fee_eth = (gas_used * gas_price) / 1e18 if is_out else 0.0
    
    return {
        "chain": "ethereum",
        "network": "ethereum",
        "record_type": "transaction",
        "wallet": wallet,
        "tx_hash": tx["hash"],
        "timestamp_utc": ts.isoformat(),
        "direction": direction,
        "amount_coin": value_eth,
        "signed_amount_coin": signed_amount,
        "fee_coin": fee_eth,
        "price_usd": None,
        "amount_usd": None,
        "fee_usd": None,
        "from_address": tx.get("from", ""),
        "to_address": tx.get("to", ""),
    }


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
        "chain": "ethereum",
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
    """Generate Ethereum transaction reports."""
    from datetime import datetime, timezone
    
    config = load_config(config_path)
    api_keys = config.get("api_keys", {}) or {}
    etherscan_key = str(api_keys.get("etherscan", "") or os.environ.get("ETHERSCAN_API_KEY", "") or "")
    coingecko_demo_key = str(
        api_keys.get("coingecko_demo", "")
        or api_keys.get("coingecko", "")
        or ""
    )
    coingecko_pro_key = str(api_keys.get("coingecko_pro", "") or "")
    wallet_addresses = config.get("wallet_addresses", {}) or {}
    ethereum_wallets = wallet_addresses.get("ethereum", []) or []
    
    if not ethereum_wallets:
        print("No Ethereum wallets configured.")
        return
    
    if not etherscan_key:
        raise RuntimeError("ETHERSCAN_API_KEY is required for Ethereum addresses.")
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    if all_years:
        # Get all years from transactions
        all_transactions: List[dict] = []
        wallet_data: dict[str, list] = {}
        
        for wallet in ethereum_wallets:
            txs = fetch_ethereum(wallet, etherscan_key)
            wallet_data[wallet] = txs
            
            for tx in txs:
                record = parse_ethereum_tx(wallet, tx)
                all_transactions.append(record)
        
        # Filter to year if specified
        year_transactions = all_transactions
        if year:
            year_str = str(year)
            year_transactions = [r for r in all_transactions if year_str in r["timestamp_utc"]]
        
        # Enrich with USD prices if requested
        if include_prices:
            for record in year_transactions:
                px = get_price_usd("ethereum", record["timestamp_utc"], coingecko_demo_key, coingecko_pro_key)
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
        tx_file = OUTPUT_DIR / f"ethereum_{year_output_str}_transactions{price_suffix}.xlsx"
        df_tx.to_excel(tx_file, index=False)
        print(f"Ethereum {year_output_str} transactions: {len(df_tx)} rows -> {tx_file}")
        
        # Generate summary for all years or specific year
        if year:
            # When year is specified, build summary for that year
            start = f"{year}-01-01T00:00:00+00:00"
            end = f"{year}-12-31T23:59:59+00:00"
        else:
            # Calculate overall summary for all years
            start = min((r["timestamp_utc"] for r in all_transactions), default="")
            end = max((r["timestamp_utc"] for r in all_transactions), default="")
        
        start_px = get_price_usd("ethereum", start, coingecko_demo_key, coingecko_pro_key) if include_prices else None
        end_px = get_price_usd("ethereum", end, coingecko_demo_key, coingecko_pro_key) if include_prices else None
        
        summary_rows = []
        for wallet in ethereum_wallets:
            txs = wallet_data[wallet]
            tx_records = [parse_ethereum_tx(wallet, tx) for tx in txs]
            
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
        summary_file = OUTPUT_DIR / f"ethereum_{year_output_str}_summary.xlsx"
        summary_df.to_excel(summary_file, index=False)
        print(f"Ethereum {year_output_str} summary: {len(summary_df)} rows -> {summary_file}")
        
        # Print totals
        if include_prices:
            total_tx = sum(r["amount_usd"] or 0 for r in year_transactions)
            print(f"Total Ethereum transactions (USD): ${total_tx:,.2f}")
        else:
            total_tx = sum(r["amount_coin"] for r in year_transactions)
            print(f"Total Ethereum transactions: {total_tx:.18f} ETH")
    else:
        if year is None:
            raise ValueError("--year or --all-years must be provided")
        
        start = f"{year}-01-01T00:00:00+00:00"
        end = f"{year}-12-31T23:59:59+00:00"
        
        # Fetch all data
        all_transactions: List[dict] = []
        wallet_data: dict[str, list] = {}
        
        for wallet in ethereum_wallets:
            txs = fetch_ethereum(wallet, etherscan_key)
            wallet_data[wallet] = txs
            
            for tx in txs:
                record = parse_ethereum_tx(wallet, tx)
                all_transactions.append(record)
        
        # Filter to year
        year_str = str(year)
        year_transactions = [r for r in all_transactions if year_str in r["timestamp_utc"]]
        
        # Enrich with USD prices if requested
        if include_prices:
            for record in year_transactions:
                px = get_price_usd("ethereum", record["timestamp_utc"], coingecko_demo_key, coingecko_pro_key)
                record["price_usd"] = px
                record["amount_usd"] = record["amount_coin"] * px if px else None
                record["fee_usd"] = record["fee_coin"] * px if px else None
        
        # Output transaction file
        df_tx = pd.DataFrame(year_transactions)
        if not df_tx.empty:
            df_tx = df_tx.sort_values("timestamp_utc").reset_index(drop=True)
        price_suffix = "_usd" if include_prices else ""
        tx_file = OUTPUT_DIR / f"ethereum_{year}_transactions{price_suffix}.xlsx"
        df_tx.to_excel(tx_file, index=False)
        print(f"Ethereum transactions: {len(df_tx)} rows -> {tx_file}")
        
        # Generate summary
        start_px = get_price_usd("ethereum", start, coingecko_demo_key, coingecko_pro_key) if include_prices else None
        end_px = get_price_usd("ethereum", end, coingecko_demo_key, coingecko_pro_key) if include_prices else None
        
        summary_rows = []
        for wallet in ethereum_wallets:
            txs = wallet_data[wallet]
            tx_records = [parse_ethereum_tx(wallet, tx) for tx in txs]
            
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
        summary_file = OUTPUT_DIR / f"ethereum_{year}_summary.xlsx"
        summary_df.to_excel(summary_file, index=False)
        print(f"Ethereum summary: {len(summary_df)} rows -> {summary_file}")
        
        # Print totals
        if include_prices:
            total_tx = sum(r["amount_usd"] or 0 for r in year_transactions)
            print(f"Total Ethereum transactions (USD): ${total_tx:,.2f}")
        else:
            total_tx = sum(r["amount_coin"] for r in year_transactions)
            print(f"Total Ethereum transactions: {total_tx:.18f} ETH")


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
