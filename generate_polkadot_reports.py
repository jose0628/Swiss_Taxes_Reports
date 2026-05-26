#!/usr/bin/env python3
"""
Generate yearly Polkadot transaction and staking reward reports.

Outputs:
- Transaction file with year-filtered transactions and USD values.
- Summary file with opening and closing balances.
- Rewards transactions file with staking rewards.
- Rewards summary file with staking rewards summary.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests
import yaml


OUTPUT_DIR = Path("reports")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate yearly Polkadot transaction and staking reward reports."
    )
    parser.add_argument(
        "--year",
        type=int,
        required=True,
        help="Tax year to process, for example: 2025",
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


def fetch_polkadot_data(wallet: str, subscan_key: str, network: str = "polkadot") -> tuple[List[dict], List[dict]]:
    """Fetch Polkadot transactions and staking rewards."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": subscan_key,
    }
    
    base_url = "https://polkadot.api.subscan.io" if network == "polkadot" else "https://assethub-polkadot.api.subscan.io"
    
    # Fetch transfers/transactions using correct Subscan endpoint
    all_txs: List[dict] = []
    page = 0
    
    while True:
        url = f"{base_url}/api/v2/scan/transfers"
        payload = {
            "address": wallet,
            "direction": "all",
            "row": 100,
            "page": page,
        }
        
        resp = requests.post(url, json=payload, headers=headers, timeout=45)
        # Handle 400 errors gracefully
        if resp.status_code != 200:
            print(f"Warning: Failed to fetch transfers for {wallet}: HTTP {resp.status_code}")
            break
        
        result = resp.json()
        
        if result.get("code") != 0:
            break
        
        transfers = result.get("data", {}).get("list", [])
        if not transfers:
            break
        
        for transfer in transfers:
            if transfer.get("block_num") and transfer.get("block_time"):
                transfer["_reward_timestamp"] = transfer.get("block_time", "")
                all_txs.append(transfer)
        
        if len(transfers) < 100:
            break
        page += 1
    
    # Fetch staking rewards
    all_rewards: List[dict] = []
    rewards_url = f"{base_url}/api/v2/scan/staking/rewards"
    page = 0
    
    while True:
        payload = {
            "address": wallet,
            "row": 100,
            "page": page,
        }
        
        resp = requests.post(rewards_url, json=payload, headers=headers, timeout=45)
        if resp.status_code != 200:
            print(f"Warning: Failed to fetch rewards for {wallet}: HTTP {resp.status_code}")
            break
        
        result = resp.json()
        
        if result.get("code") != 0:
            break
        
        rewards = result.get("data", {}).get("list", [])
        if not rewards:
            break
        
        for reward in rewards:
            reward["_reward_timestamp"] = reward.get("block_time", "")
            all_rewards.append(reward)
        
        if len(rewards) < 100:
            break
        page += 1
    
    return all_txs, all_rewards


def parse_polkadot_tx(wallet: str, tx: dict) -> dict:
    """Parse a Polkadot transaction into a record."""
    amount_dot = float(tx.get("amount", "0") or "0") / 1e10
    block_time = tx.get("block_time", "")
    direction = "in" if tx.get("direction", "") == "ingress" else "out"
    fee_dot = float(tx.get("fee", "0") or "0") / 1e10
    
    return {
        "chain": "polkadot",
        "network": "polkadot",
        "record_type": "transaction",
        "wallet": wallet,
        "tx_hash": tx.get("tx_hash", ""),
        "timestamp_utc": block_time,
        "direction": direction,
        "amount_coin": amount_dot,
        "signed_amount_coin": amount_dot if direction == "in" else -amount_dot,
        "fee_coin": fee_dot,
        "price_usd": None,
        "amount_usd": None,
        "fee_usd": None,
        "from_address": tx.get("from", ""),
        "to_address": tx.get("to", ""),
    }


def parse_polkadot_reward(wallet: str, reward: dict) -> dict:
    """Parse a Polkadot staking reward into a record."""
    amount_dot = float(reward.get("amount", "0") or "0") / 1e10
    era = reward.get("era", "")
    reward_type = reward.get("reward_type", "Nomination")
    block_time = reward.get("block_time", "")
    
    return {
        "chain": "polkadot",
        "network": "polkadot",
        "record_type": "staking_reward",
        "wallet": wallet,
        "tx_hash": f"{wallet}:era:{era}:{reward_type}",
        "timestamp_utc": block_time,
        "direction": "in",
        "amount_coin": amount_dot,
        "signed_amount_coin": amount_dot,
        "fee_coin": 0.0,
        "price_usd": None,
        "amount_usd": None,
        "fee_usd": None,
        "from_address": f"era:{era}",
        "to_address": wallet,
    }


def build_balance_summary(
    wallet: str,
    all_tx_records: List[dict],
    all_reward_records: List[dict],
    start: str,
    end: str,
    start_price: float | None,
    end_price: float | None,
) -> dict:
    """Build a balance summary for a wallet."""
    all_records = all_tx_records + all_reward_records
    
    opening = sum(r["signed_amount_coin"] for r in all_records if r["timestamp_utc"] < start)
    closing = sum(r["signed_amount_coin"] for r in all_records if r["timestamp_utc"] <= end)
    change = closing - opening
    
    return {
        "chain": "polkadot",
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
    
    coin_id = "polkadot" if chain == "polkadot" else "cardano"
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
    year: int,
    config_path: str,
    include_prices: bool = True,
) -> None:
    """Generate Polkadot transaction and reward reports."""
    config = load_config(config_path)
    api_keys = config.get("api_keys", {}) or {}
    subscan_key = str(api_keys.get("subscan", "") or "")
    coingecko_demo_key = str(
        api_keys.get("coingecko_demo", "")
        or api_keys.get("coingecko", "")
        or ""
    )
    coingecko_pro_key = str(
        api_keys.get("coingecko_pro", "")
        or ""
    )
    wallet_addresses = config.get("wallet_addresses", {}) or {}
    polkadot_wallets = wallet_addresses.get("polkadot", []) or []
    
    if not polkadot_wallets:
        print("No Polkadot wallets configured.")
        return
    
    if not subscan_key:
        raise RuntimeError("SUBSCAN_API_KEY is required for Polkadot addresses.")
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    start = f"{year}-01-01T00:00:00+00:00"
    end = f"{year}-12-31T23:59:59+00:00"
    
    # Fetch all data
    all_transactions: List[dict] = []
    all_rewards: List[dict] = []
    wallet_data: Dict[str, tuple[List[dict], List[dict]]] = {}
    
    for wallet in polkadot_wallets:
        txs, rewards = fetch_polkadot_data(wallet, subscan_key)
        wallet_data[wallet] = (txs, rewards)
        
        for tx in txs:
            record = parse_polkadot_tx(wallet, tx)
            all_transactions.append(record)
        
        for reward in rewards:
            record = parse_polkadot_reward(wallet, reward)
            all_rewards.append(record)
    
    # Filter to year
    year_str = str(year)
    year_transactions = [r for r in all_transactions if year_str in r["timestamp_utc"]]
    year_rewards = [r for r in all_rewards if year_str in r["timestamp_utc"]]
    
    # Enrich with USD prices if requested
    if include_prices:
        for record in year_transactions + year_rewards:
            px = get_price_usd("polkadot", record["timestamp_utc"], coingecko_demo_key, coingecko_pro_key)
            record["price_usd"] = px
            record["amount_usd"] = record["amount_coin"] * px if px else None
            record["fee_usd"] = record["fee_coin"] * px if px else None
    
    # Output transaction file
    df_tx = pd.DataFrame(year_transactions)
    if not df_tx.empty:
        df_tx = df_tx.sort_values("timestamp_utc").reset_index(drop=True)
    price_suffix = "_usd" if include_prices else ""
    tx_file = OUTPUT_DIR / f"polkadot_{year}_transactions{price_suffix}.xlsx"
    df_tx.to_excel(tx_file, index=False)
    print(f"Polkadot transactions: {len(df_tx)} rows -> {tx_file}")
    
    # Output rewards transactions file
    df_rewards = pd.DataFrame(year_rewards)
    if not df_rewards.empty:
        df_rewards = df_rewards.sort_values("timestamp_utc").reset_index(drop=True)
    rewards_tx_file = OUTPUT_DIR / f"polkadot_{year}_rewards_transactions{price_suffix}.xlsx"
    df_rewards.to_excel(rewards_tx_file, index=False)
    print(f"Polkadot rewards transactions: {len(df_rewards)} rows -> {rewards_tx_file}")
    
    # Generate summary
    start_px = get_price_usd("polkadot", start, coingecko_demo_key, coingecko_pro_key) if include_prices else None
    end_px = get_price_usd("polkadot", end, coingecko_demo_key, coingecko_pro_key) if include_prices else None
    
    summary_rows = []
    for wallet in polkadot_wallets:
        txs, rewards = wallet_data[wallet]
        tx_records = [parse_polkadot_tx(wallet, tx) for tx in txs]
        reward_records = [parse_polkadot_reward(wallet, reward) for reward in rewards]
        
        summary_rows.append(
            build_balance_summary(
                wallet=wallet,
                all_tx_records=tx_records,
                all_reward_records=reward_records,
                start=start,
                end=end,
                start_price=start_px,
                end_price=end_px,
            )
        )
    
    summary_df = pd.DataFrame(summary_rows)
    summary_file = OUTPUT_DIR / f"polkadot_{year}_summary.xlsx"
    summary_df.to_excel(summary_file, index=False)
    print(f"Polkadot summary: {len(summary_df)} rows -> {summary_file}")
    
    # Print totals
    if include_prices:
        total_tx = sum(r["amount_usd"] or 0 for r in year_transactions)
        total_rewards = sum(r["amount_usd"] or 0 for r in year_rewards)
        print(f"Total Polkadot transactions (USD): ${total_tx:,.2f}")
        print(f"Total Polkadot rewards (USD): ${total_rewards:,.2f}")
    else:
        total_tx = sum(r["amount_coin"] for r in year_transactions)
        total_rewards = sum(r["amount_coin"] for r in year_rewards)
        print(f"Total Polkadot transactions: {total_tx:.6f} DOT")
        print(f"Total Polkadot rewards: {total_rewards:.6f} DOT")


def run() -> None:
    args = parse_args()
    generate_reports(
        year=args.year,
        config_path=args.config,
        include_prices=args.include_prices,
    )


if __name__ == "__main__":
    run()
