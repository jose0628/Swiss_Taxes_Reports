#!/usr/bin/env python3
"""
Generate yearly Cardano transaction and staking reward reports.

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
import yaml


OUTPUT_DIR = Path("reports")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate yearly Cardano transaction and staking reward reports."
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


def to_utc(dt_string: str) -> str:
    """Convert ISO datetime string to UTC format."""
    if "T" in dt_string:
        return dt_string.replace("Z", "+00:00")
    return dt_string


def fetch_cardano_data(wallet: str, project_id: str) -> tuple[List[dict], List[dict]]:
    """Fetch Cardano transactions and staking rewards."""
    import requests
    import time
    from datetime import datetime, timezone

    headers = {"project_id": project_id}
    base = "https://cardano-mainnet.blockfrost.io/api/v0"
    
    # Fetch stake address
    resp = requests.get(f"{base}/addresses/{wallet}", headers=headers, timeout=45)
    stake_address = None
    if resp.status_code != 404:
        resp.raise_for_status()
        stake_address = resp.json().get("stake_address")
    
    # Fetch account addresses if we have a stake address
    account_addresses = set([wallet])
    if stake_address:
        url = f"{base}/accounts/{stake_address}/addresses"
        page = 1
        while True:
            params = {"order": "asc", "page": page, "count": 100}
            resp = requests.get(url, headers=headers, params=params, timeout=45)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            for row in rows:
                if row.get("address"):
                    account_addresses.add(row["address"])
            if len(rows) < 100:
                break
            page += 1
            time.sleep(0.2)
    
    # Fetch transactions for all addresses
    seen_tx_hashes: set[str] = set()
    all_txs: List[dict] = []
    
    for account_address in account_addresses:
        url = f"{base}/addresses/{account_address}/transactions"
        page = 1
        while True:
            params = {"order": "desc", "page": page, "count": 100}
            resp = requests.get(url, headers=headers, params=params, timeout=45)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            for row in rows:
                tx_hash = row["tx_hash"]
                if tx_hash in seen_tx_hashes:
                    continue
                seen_tx_hashes.add(tx_hash)
                
                # Fetch full tx details
                tx_resp = requests.get(f"{base}/txs/{tx_hash}", headers=headers, timeout=45)
                tx_resp.raise_for_status()
                tx = tx_resp.json()
                
                # Fetch utxos
                utxo_resp = requests.get(f"{base}/txs/{tx_hash}/utxos", headers=headers, timeout=45)
                utxo_resp.raise_for_status()
                utxos = utxo_resp.json()
                
                # Calculate wallet input/output lovelace
                wallet_input = 0
                wallet_output = 0
                for inp in utxos.get("inputs", []):
                    if inp.get("address") in account_addresses:
                        for amt in inp.get("amount", []):
                            if amt.get("unit") == "lovelace":
                                wallet_input += int(amt.get("quantity", "0"))
                for out in utxos.get("outputs", []):
                    if out.get("address") in account_addresses:
                        for amt in out.get("amount", []):
                            if amt.get("unit") == "lovelace":
                                wallet_output += int(amt.get("quantity", "0"))
                
                tx["_wallet_input_lovelace"] = wallet_input
                tx["_wallet_output_lovelace"] = wallet_output
                tx["_wallet_address"] = account_address
                all_txs.append(tx)
            page += 1
            time.sleep(0.2)
            if len(rows) < 100:
                break
    
    # Fetch staking rewards
    all_rewards: List[dict] = []
    if stake_address:
        url = f"{base}/accounts/{stake_address}/rewards"
        page = 1
        epoch_times: Dict[int, datetime] = {}
        while True:
            params = {"order": "asc", "page": page, "count": 100}
            resp = requests.get(url, headers=headers, params=params, timeout=45)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            rewards = resp.json()
            if not rewards:
                break
            for reward in rewards:
                epoch = int(reward["epoch"])
                if epoch not in epoch_times:
                    epoch_resp = requests.get(f"{base}/epochs/{epoch}", headers=headers, timeout=45)
                    epoch_resp.raise_for_status()
                    epoch_data = epoch_resp.json()
                    reward_time = epoch_data.get("end_time") or epoch_data.get("start_time")
                    epoch_times[epoch] = datetime.fromtimestamp(int(reward_time), tz=timezone.utc)
                reward["_wallet_stake_address"] = stake_address
                reward["_reward_timestamp"] = epoch_times[epoch]
                all_rewards.append(reward)
            if len(rewards) < 100:
                break
            page += 1
            time.sleep(0.2)
    
    return all_txs, all_rewards


def parse_cardano_tx(wallet: str, tx: dict) -> dict:
    """Parse a Cardano transaction into a record."""
    ts = datetime.fromtimestamp(int(tx["block_time"]), tz=timezone.utc)
    input_lovelace = int(tx.get("_wallet_input_lovelace", 0))
    output_lovelace = int(tx.get("_wallet_output_lovelace", 0))
    signed_amount = (output_lovelace - input_lovelace) / 1e6
    amount_ada = abs(signed_amount)
    direction = "in" if signed_amount >= 0 else "out"
    fee_ada = int(tx.get("fees", 0)) / 1e6
    
    return {
        "chain": "cardano",
        "network": "cardano",
        "record_type": "transaction",
        "wallet": wallet,
        "tx_hash": tx.get("hash", ""),
        "timestamp_utc": ts.isoformat(),
        "direction": direction,
        "amount_coin": amount_ada,
        "signed_amount_coin": signed_amount,
        "fee_coin": fee_ada if signed_amount < 0 else 0.0,
        "price_usd": None,
        "amount_usd": None,
        "fee_usd": None,
        "from_address": "",
        "to_address": "",
    }


def parse_cardano_reward(wallet: str, reward: dict) -> dict:
    """Parse a Cardano staking reward into a record."""
    amount_ada = int(reward.get("amount", "0") or "0") / 1e6
    stake_address = reward.get("_wallet_stake_address", "")
    reward_type = reward.get("type", "member")
    epoch = reward.get("epoch", "")
    pool_id = reward.get("pool_id", "")
    
    return {
        "chain": "cardano",
        "network": "cardano",
        "record_type": "staking_reward",
        "wallet": wallet,
        "tx_hash": f"{stake_address}:epoch:{epoch}:{reward_type}",
        "timestamp_utc": reward["_reward_timestamp"].isoformat(),
        "direction": "in",
        "amount_coin": amount_ada,
        "signed_amount_coin": amount_ada,
        "fee_coin": 0.0,
        "price_usd": None,
        "amount_usd": None,
        "fee_usd": None,
        "from_address": pool_id,
        "to_address": stake_address,
    }


def build_balance_summary(
    wallet: str,
    all_tx_records: List[dict],
    all_reward_records: List[dict],
    start: datetime,
    end: datetime,
    start_price: float | None,
    end_price: float | None,
) -> dict:
    """Build a balance summary for a wallet."""
    # Calculate balances from all records (tx + rewards)
    all_records = all_tx_records + all_reward_records
    
    opening = sum(r["signed_amount_coin"] for r in all_records if r["timestamp_utc"] < start.isoformat())
    closing = sum(r["signed_amount_coin"] for r in all_records if r["timestamp_utc"] <= end.isoformat())
    change = closing - opening
    
    return {
        "chain": "cardano",
        "wallet": wallet,
        "year": start.year,
        "opening_balance_coin": opening,
        "closing_balance_coin": closing,
        "net_change_coin": change,
        "opening_price_usd": start_price,
        "closing_price_usd": end_price,
        "opening_balance_usd": opening * start_price if start_price is not None else None,
        "closing_balance_usd": closing * end_price if end_price is not None else None,
    }


def generate_reports(
    year: int | None,
    config_path: str,
    include_prices: bool = True,
    all_years: bool = False,
) -> None:
    """Generate Cardano transaction and reward reports."""
    import requests
    from datetime import datetime, timezone
    
    config = load_config(config_path)
    api_keys = config.get("api_keys", {}) or {}
    blockfrost_key = str(api_keys.get("blockfrost_project_id", "") or "")
    coingecko_demo_key = str(
        api_keys.get("coingecko_demo", "")
        or api_keys.get("coingecko", "")
        or ""
    )
    wallet_addresses = config.get("wallet_addresses", {}) or {}
    cardano_wallets = wallet_addresses.get("cardano", []) or []
    
    if not cardano_wallets:
        print("No Cardano wallets configured.")
        return
    
    if not blockfrost_key:
        raise RuntimeError("BLOCKFROST_PROJECT_ID is required for Cardano addresses.")
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    if all_years:
        # No specific year filtering - get all transactions
        start = ""
        end = ""
        year = None
    elif year is None:
        raise ValueError("--year or --all-years must be provided")
    else:
        start, end = (
            datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        )
    
    # Fetch all data
    all_transactions: List[dict] = []
    all_rewards: List[dict] = []
    wallet_data: Dict[str, tuple[List[dict], List[dict]]] = {}
    
    for wallet in cardano_wallets:
        txs, rewards = fetch_cardano_data(wallet, blockfrost_key)
        wallet_data[wallet] = (txs, rewards)
        
        # Parse transactions
        for tx in txs:
            record = parse_cardano_tx(wallet, tx)
            all_transactions.append(record)
        
        # Parse rewards
        for reward in rewards:
            record = parse_cardano_reward(wallet, reward)
            all_rewards.append(record)
    
    # Determine output year label and filter transactions
    if all_years:
        year_transactions = all_transactions
        year_rewards = all_rewards
        if year:
            # When year is specified, filter to that year
            year_str = str(year)
            year_transactions = [r for r in all_transactions if year_str in r.get("timestamp_utc", "")]
            year_rewards = [r for r in all_rewards if year_str in r.get("timestamp_utc", "")]
            year_output = year
        else:
            year_output = "all_years"
    else:
        year_str = str(year)
        year_transactions = [r for r in all_transactions if year_str in r.get("timestamp_utc", "")]
        year_rewards = [r for r in all_rewards if year_str in r.get("timestamp_utc", "")]
        year_output = year
    
    # Enrich with USD prices if requested
    if include_prices:
        prices = PriceService(coingecko_demo_key, "")
        for record in year_transactions + year_rewards:
            px = prices.get_price_usd("cardano", record["timestamp_utc"], start, end)
            record["price_usd"] = px
            record["amount_usd"] = record["amount_coin"] * px if px else None
            record["fee_usd"] = record["fee_coin"] * px if px else None
    
    # Output transaction file
    df_tx = pd.DataFrame(year_transactions)
    if not df_tx.empty:
        df_tx = df_tx.sort_values("timestamp_utc").reset_index(drop=True)
    price_suffix = "_usd" if include_prices else ""
    tx_file = OUTPUT_DIR / f"cardano_{year_output}_transactions{price_suffix}.xlsx"
    df_tx.to_excel(tx_file, index=False)
    print(f"Cardano transactions: {len(df_tx)} rows -> {tx_file}")
    
    # Output rewards transactions file
    df_rewards = pd.DataFrame(year_rewards)
    if not df_rewards.empty:
        df_rewards = df_rewards.sort_values("timestamp_utc").reset_index(drop=True)
    rewards_tx_file = OUTPUT_DIR / f"cardano_{year_output}_rewards_transactions{price_suffix}.xlsx"
    df_rewards.to_excel(rewards_tx_file, index=False)
    print(f"Cardano rewards transactions: {len(df_rewards)} rows -> {rewards_tx_file}")
    
    # Generate summary
    prices = PriceService(coingecko_demo_key, "")
    start_px = prices.get_price_usd("cardano", start.isoformat(), start, end) if include_prices else None
    end_px = prices.get_price_usd("cardano", end.isoformat(), start, end) if include_prices else None
    
    summary_rows = []
    for wallet in cardano_wallets:
        txs, rewards = wallet_data[wallet]
        tx_records = [parse_cardano_tx(wallet, tx) for tx in txs]
        reward_records = [parse_cardano_reward(wallet, reward) for reward in rewards]
        
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
    summary_file = OUTPUT_DIR / f"cardano_{year_output}_summary.xlsx"
    summary_df.to_excel(summary_file, index=False)
    print(f"Cardano summary: {len(summary_df)} rows -> {summary_file}")
    
    # Print totals
    if all_years:
        print(f"Cardano: {len(year_transactions)} total transactions")
    else:
        print(f"Cardano: {len(year_transactions)} transactions for year {year}")
    
    if include_prices:
        total_tx = sum(r["amount_usd"] or 0 for r in year_transactions)
        total_rewards = sum(r["amount_usd"] or 0 for r in year_rewards)
        print(f"Total Cardano transactions (USD): ${total_tx:,.2f}")
        print(f"Total Cardano rewards (USD): ${total_rewards:,.2f}")
    else:
        total_tx = sum(r["amount_coin"] for r in year_transactions)
        total_rewards = sum(r["amount_coin"] for r in year_rewards)
        print(f"Total Cardano transactions: {total_tx:.6f} ADA")
        print(f"Total Cardano rewards: {total_rewards:.6f} ADA")


class PriceService:
    def __init__(self, demo_api_key: str = "", pro_api_key: str = "") -> None:
        self.session = requests.Session()
        self.base_url = "https://pro-api.coingecko.com/api/v3" if pro_api_key else "https://api.coingecko.com/api/v3"
        self.headers = {}
        if pro_api_key:
            self.headers["x-cg-pro-api-key"] = pro_api_key
        elif demo_api_key:
            self.headers["x-cg-demo-api-key"] = demo_api_key

    def get_price_usd(self, chain: str, timestamp: str, start: datetime, end: datetime) -> float | None:
        """Get USD price for a given timestamp."""
        from datetime import datetime
        
        if isinstance(timestamp, str):
            try:
                ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            ts = timestamp
        
        # Get daily price for the timestamp
        url = f"{self.base_url}/coins/cardano/market_chart/date"
        params = {"vs_currency": "usd", "date": ts.strftime("%d-%m-%Y")}
        
        try:
            resp = self.session.get(url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            prices = data.get("prices", [])
            for price in prices:
                if price[0] == ts.timestamp() * 1000:
                    return price[1]
            return None
        except Exception:
            return None


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
