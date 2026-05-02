#!/usr/bin/env python3
"""
Generate yearly wallet transaction reports for Ledger-held addresses.

Outputs:
- One Excel file per cryptocurrency with year-filtered transactions and USD values.
- One summary Excel file with opening and closing token balances for the selected year.

Supported assets:
- Bitcoin (BTC) via Blockchair
- Litecoin (LTC) via Blockchair
- Ethereum (ETH) via Etherscan
- Polkadot (DOT) via Subscan
- Cardano (ADA) via Blockfrost
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests
import yaml


OUTPUT_DIR = Path("reports")

COINGECKO_IDS = {
    "bitcoin": "bitcoin",
    "litecoin": "litecoin",
    "ethereum": "ethereum",
    "polkadot": "polkadot",
    "cardano": "cardano",
}


@dataclass
class TxRecord:
    chain: str
    wallet: str
    tx_hash: str
    timestamp: datetime
    direction: str
    amount_coin: float
    signed_amount_coin: float
    price_usd: Optional[float]
    amount_usd: Optional[float]
    fee_coin: Optional[float]
    fee_usd: Optional[float]
    from_address: str
    to_address: str


def dt_to_epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def to_utc(dt_string: str) -> datetime:
    if "T" in dt_string:
        parsed = datetime.fromisoformat(dt_string.replace("Z", "+00:00"))
    else:
        parsed = datetime.strptime(dt_string, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def year_boundaries_utc(year: int) -> tuple[datetime, datetime]:
    start = datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    return start, end


class PriceService:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.cache: Dict[str, Dict[str, float]] = {}

    def _load_daily_prices(self, chain: str, start: datetime, end: datetime) -> Dict[str, float]:
        chain_id = COINGECKO_IDS[chain]
        url = f"https://api.coingecko.com/api/v3/coins/{chain_id}/market_chart/range"
        params = {"vs_currency": "usd", "from": dt_to_epoch(start), "to": dt_to_epoch(end)}
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        out: Dict[str, float] = {}
        for ts_ms, price in data.get("prices", []):
            day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()
            out[day] = float(price)
        return out

    def get_price_usd(self, chain: str, ts: datetime, start: datetime, end: datetime) -> Optional[float]:
        if chain not in self.cache:
            self.cache[chain] = self._load_daily_prices(chain, start, end)
            time.sleep(1.2)
        return self.cache[chain].get(ts.date().isoformat())


def fetch_btc_or_ltc_blockchair(chain: str, wallet: str) -> List[dict]:
    url = f"https://api.blockchair.com/{chain}/dashboards/address/{wallet}"
    params = {"transaction_details": "true", "limit": 10000}
    resp = requests.get(url, params=params, timeout=45)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data", {}).get(wallet, {})
    return data.get("transactions", [])


def parse_btc_ltc(chain: str, wallet: str, tx: dict) -> TxRecord:
    raw_delta = int(tx.get("balance_change", 0))
    amount_coin = abs(raw_delta) / 1e8
    signed_amount = raw_delta / 1e8
    direction = "in" if raw_delta >= 0 else "out"
    fee_coin = float(tx.get("fee", 0)) / 1e8 if tx.get("fee") is not None else None
    ts = to_utc(tx["time"])
    return TxRecord(
        chain=chain,
        wallet=wallet,
        tx_hash=tx["hash"],
        timestamp=ts,
        direction=direction,
        amount_coin=amount_coin,
        signed_amount_coin=signed_amount,
        price_usd=None,
        amount_usd=None,
        fee_coin=fee_coin,
        fee_usd=None,
        from_address="",
        to_address="",
    )


def fetch_ethereum(wallet: str, api_key: str) -> List[dict]:
    url = "https://api.etherscan.io/api"
    params = {
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
    return resp.json().get("result", [])


def parse_eth(wallet: str, tx: dict) -> TxRecord:
    ts = datetime.fromtimestamp(int(tx["timeStamp"]), tz=timezone.utc)
    value_eth = int(tx["value"]) / 1e18
    from_addr = tx.get("from", "").lower()
    to_addr = tx.get("to", "").lower()
    is_out = from_addr == wallet.lower()
    direction = "out" if is_out else "in"
    signed_amount = -value_eth if is_out else value_eth
    gas_used = int(tx.get("gasUsed", "0"))
    gas_price = int(tx.get("gasPrice", "0"))
    fee_eth = (gas_used * gas_price) / 1e18
    return TxRecord(
        chain="ethereum",
        wallet=wallet,
        tx_hash=tx["hash"],
        timestamp=ts,
        direction=direction,
        amount_coin=value_eth,
        signed_amount_coin=signed_amount,
        price_usd=None,
        amount_usd=None,
        fee_coin=fee_eth if is_out else 0.0,
        fee_usd=None,
        from_address=tx.get("from", ""),
        to_address=tx.get("to", ""),
    )


def fetch_polkadot(wallet: str, api_key: str) -> List[dict]:
    url = "https://polkadot.api.subscan.io/api/v2/scan/transfers"
    headers = {"Content-Type": "application/json", "X-API-Key": api_key}
    out: List[dict] = []
    page = 0
    while True:
        body = {"address": wallet, "page": page, "row": 100}
        resp = requests.post(url, headers=headers, json=body, timeout=45)
        resp.raise_for_status()
        transfers = (resp.json().get("data", {}) or {}).get("transfers", []) or []
        if not transfers:
            break
        out.extend(transfers)
        page += 1
        time.sleep(0.2)
    return out


def parse_dot(wallet: str, tr: dict) -> TxRecord:
    ts = datetime.fromtimestamp(int(tr["block_timestamp"]), tz=timezone.utc)
    amount_dot = int(tr.get("amount", "0")) / 1e10
    from_addr = tr.get("from", "")
    to_addr = tr.get("to", "")
    is_out = from_addr == wallet
    direction = "out" if is_out else "in"
    signed_amount = -amount_dot if is_out else amount_dot
    return TxRecord(
        chain="polkadot",
        wallet=wallet,
        tx_hash=tr.get("extrinsic_hash", ""),
        timestamp=ts,
        direction=direction,
        amount_coin=amount_dot,
        signed_amount_coin=signed_amount,
        price_usd=None,
        amount_usd=None,
        fee_coin=None,
        fee_usd=None,
        from_address=from_addr,
        to_address=to_addr,
    )


def _ada_lovelace_for_address(entries: List[dict], wallet: str) -> int:
    total = 0
    for item in entries:
        if item.get("address") != wallet:
            continue
        for amt in item.get("amount", []):
            if amt.get("unit") == "lovelace":
                total += int(amt.get("quantity", "0"))
    return total


def fetch_cardano(wallet: str, project_id: str) -> List[dict]:
    headers = {"project_id": project_id}
    base = "https://cardano-mainnet.blockfrost.io/api/v0"
    tx_hashes: List[str] = []
    page = 1
    while True:
        url = f"{base}/addresses/{wallet}/transactions"
        params = {"order": "desc", "page": page, "count": 100}
        resp = requests.get(url, headers=headers, params=params, timeout=45)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        tx_hashes.extend([r["tx_hash"] for r in rows])
        page += 1
        time.sleep(0.2)
        if len(rows) < 100:
            break

    out: List[dict] = []
    for txh in tx_hashes:
        tx_resp = requests.get(f"{base}/txs/{txh}", headers=headers, timeout=45)
        tx_resp.raise_for_status()
        utxo_resp = requests.get(f"{base}/txs/{txh}/utxos", headers=headers, timeout=45)
        utxo_resp.raise_for_status()
        tx = tx_resp.json()
        utxos = utxo_resp.json()
        tx["_wallet_input_lovelace"] = _ada_lovelace_for_address(utxos.get("inputs", []), wallet)
        tx["_wallet_output_lovelace"] = _ada_lovelace_for_address(utxos.get("outputs", []), wallet)
        out.append(tx)
        time.sleep(0.1)
    return out


def parse_ada(wallet: str, tx: dict) -> TxRecord:
    ts = datetime.fromtimestamp(int(tx["block_time"]), tz=timezone.utc)
    input_lovelace = int(tx.get("_wallet_input_lovelace", 0))
    output_lovelace = int(tx.get("_wallet_output_lovelace", 0))
    signed_amount = (output_lovelace - input_lovelace) / 1e6
    amount_ada = abs(signed_amount)
    direction = "in" if signed_amount >= 0 else "out"
    fee_ada = int(tx.get("fees", 0)) / 1e6
    return TxRecord(
        chain="cardano",
        wallet=wallet,
        tx_hash=tx.get("hash", ""),
        timestamp=ts,
        direction=direction,
        amount_coin=amount_ada,
        signed_amount_coin=signed_amount,
        price_usd=None,
        amount_usd=None,
        fee_coin=fee_ada if signed_amount < 0 else 0.0,
        fee_usd=None,
        from_address="",
        to_address="",
    )


def enrich_with_usd(records: List[TxRecord], start: datetime, end: datetime) -> None:
    if not records:
        return
    prices = PriceService()
    for rec in records:
        px = prices.get_price_usd(rec.chain, rec.timestamp, start, end)
        rec.price_usd = px
        if px is None:
            rec.amount_usd = None
            rec.fee_usd = None
            continue
        rec.amount_usd = rec.amount_coin * px
        rec.fee_usd = (rec.fee_coin or 0.0) * px


def to_dataframe(records: List[TxRecord]) -> pd.DataFrame:
    rows = []
    for r in records:
        rows.append(
            {
                "chain": r.chain,
                "wallet": r.wallet,
                "tx_hash": r.tx_hash,
                "timestamp_utc": r.timestamp.isoformat(),
                "direction": r.direction,
                "amount_coin": r.amount_coin,
                "signed_amount_coin": r.signed_amount_coin,
                "price_usd": r.price_usd,
                "amount_usd": r.amount_usd,
                "fee_coin": r.fee_coin,
                "fee_usd": r.fee_usd,
                "from_address": r.from_address,
                "to_address": r.to_address,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("timestamp_utc").reset_index(drop=True)
    return df


def build_balance_summary(
    chain: str,
    wallet: str,
    all_records: List[TxRecord],
    start: datetime,
    end: datetime,
    start_price: Optional[float],
    end_price: Optional[float],
) -> Dict[str, Optional[float]]:
    opening = sum(r.signed_amount_coin for r in all_records if r.timestamp < start)
    closing = sum(r.signed_amount_coin for r in all_records if r.timestamp <= end)
    change = closing - opening
    return {
        "chain": chain,
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate yearly crypto wallet tax reports.")
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
    return parser.parse_args()


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Create it from wallet_config.example.yaml."
        )
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def read_wallet_addresses(config: dict) -> Dict[str, List[str]]:
    wallets = config.get("wallet_addresses", {}) or {}
    return {
        "bitcoin": wallets.get("bitcoin", []) or [],
        "litecoin": wallets.get("litecoin", []) or [],
        "ethereum": wallets.get("ethereum", []) or [],
        "polkadot": wallets.get("polkadot", []) or [],
        "cardano": wallets.get("cardano", []) or [],
    }


def run() -> None:
    args = parse_args()
    config = load_config(args.config)
    wallet_addresses = read_wallet_addresses(config)
    start, end = year_boundaries_utc(args.year)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    api_keys = config.get("api_keys", {}) or {}
    etherscan_key = str(api_keys.get("etherscan", "") or "")
    subscan_key = str(api_keys.get("subscan", "") or "")
    blockfrost_key = str(api_keys.get("blockfrost_project_id", "") or "")

    all_time_records_by_chain: Dict[str, List[TxRecord]] = {
        "bitcoin": [],
        "litecoin": [],
        "ethereum": [],
        "polkadot": [],
        "cardano": [],
    }

    for wallet in wallet_addresses["bitcoin"]:
        txs = fetch_btc_or_ltc_blockchair("bitcoin", wallet)
        all_time_records_by_chain["bitcoin"].extend(parse_btc_ltc("bitcoin", wallet, tx) for tx in txs)

    for wallet in wallet_addresses["litecoin"]:
        txs = fetch_btc_or_ltc_blockchair("litecoin", wallet)
        all_time_records_by_chain["litecoin"].extend(parse_btc_ltc("litecoin", wallet, tx) for tx in txs)

    if wallet_addresses["ethereum"] and not etherscan_key:
        raise RuntimeError("ETHERSCAN_API_KEY is required for Ethereum addresses.")
    for wallet in wallet_addresses["ethereum"]:
        txs = fetch_ethereum(wallet, etherscan_key)
        all_time_records_by_chain["ethereum"].extend(parse_eth(wallet, tx) for tx in txs)

    if wallet_addresses["polkadot"] and not subscan_key:
        raise RuntimeError("SUBSCAN_API_KEY is required for Polkadot addresses.")
    for wallet in wallet_addresses["polkadot"]:
        txs = fetch_polkadot(wallet, subscan_key)
        all_time_records_by_chain["polkadot"].extend(parse_dot(wallet, tx) for tx in txs)

    if wallet_addresses["cardano"] and not blockfrost_key:
        raise RuntimeError("BLOCKFROST_PROJECT_ID is required for Cardano addresses.")
    for wallet in wallet_addresses["cardano"]:
        txs = fetch_cardano(wallet, blockfrost_key)
        all_time_records_by_chain["cardano"].extend(parse_ada(wallet, tx) for tx in txs)

    year_records_by_chain: Dict[str, List[TxRecord]] = {}
    for chain, records in all_time_records_by_chain.items():
        year_records = [r for r in records if start <= r.timestamp <= end]
        year_records_by_chain[chain] = year_records
        enrich_with_usd(year_records, start, end)
        out_file = OUTPUT_DIR / f"{chain}_{args.year}_transactions_usd.xlsx"
        to_dataframe(year_records).to_excel(out_file, index=False)
        print(f"{chain}: {len(year_records)} year rows -> {out_file}")

    prices = PriceService()
    summary_rows = []
    for chain, wallet_list in wallet_addresses.items():
        start_px = prices.get_price_usd(chain, start, start, end) if wallet_list else None
        end_px = prices.get_price_usd(chain, end, start, end) if wallet_list else None
        for wallet in wallet_list:
            wallet_records = [r for r in all_time_records_by_chain[chain] if r.wallet == wallet]
            summary_rows.append(
                build_balance_summary(
                    chain=chain,
                    wallet=wallet,
                    all_records=wallet_records,
                    start=start,
                    end=end,
                    start_price=start_px,
                    end_price=end_px,
                )
            )

    summary_df = pd.DataFrame(summary_rows)
    summary_file = OUTPUT_DIR / f"yearly_balances_summary_{args.year}.xlsx"
    summary_df.to_excel(summary_file, index=False)
    print(f"summary: {len(summary_df)} rows -> {summary_file}")


if __name__ == "__main__":
    run()
