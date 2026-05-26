#!/usr/bin/env python3
"""
Generate yearly wallet transaction reports for Ledger-held addresses.

Outputs:
- One Excel file per cryptocurrency with year-filtered transactions and USD values.
- One summary Excel file with opening and closing token balances for the selected year.

Supported assets:
- Bitcoin (BTC) via Blockchair, with mempool.space fallback
- Litecoin (LTC) via Blockchair, with litecoinspace.org fallback
- Ethereum (ETH) via Etherscan
- Polkadot (DOT) via Subscan
- Cardano (ADA) via Blockfrost
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
COINBASE_PRODUCTS = {
    "bitcoin": "BTC-USD",
    "litecoin": "LTC-USD",
    "ethereum": "ETH-USD",
    "polkadot": "DOT-USD",
    "cardano": "ADA-USD",
}
SUPPORTED_CHAINS = ["bitcoin", "litecoin", "ethereum", "polkadot", "cardano"]
ESPLORA_APIS = {
    "bitcoin": "https://mempool.space/api",
    "litecoin": "https://litecoinspace.org/api",
}
POLKADOT_NETWORKS = {
    "polkadot": "https://polkadot.api.subscan.io",
    "assethub-polkadot": "https://assethub-polkadot.api.subscan.io",
}


class BlockchairFetchError(RuntimeError):
    def __init__(self, chain: str, wallet: str, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.chain = chain
        self.wallet = wallet
        self.status_code = status_code


@dataclass
class TxRecord:
    chain: str
    network: str
    record_type: str
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
    def __init__(self, demo_api_key: str = "", pro_api_key: str = "") -> None:
        self.session = requests.Session()
        self.cache: Dict[str, Dict[str, float]] = {}
        self.base_url = "https://pro-api.coingecko.com/api/v3" if pro_api_key else "https://api.coingecko.com/api/v3"
        self.headers = {}
        if pro_api_key:
            self.headers["x-cg-pro-api-key"] = pro_api_key
        elif demo_api_key:
            self.headers["x-cg-demo-api-key"] = demo_api_key

    def _load_daily_prices(self, chain: str, start: datetime, end: datetime) -> Dict[str, float]:
        try:
            return self._load_coingecko_daily_prices(chain, start, end)
        except RuntimeError as exc:
            if chain not in COINBASE_PRODUCTS:
                raise
            print(f"{chain}: CoinGecko unavailable; trying Coinbase Exchange USD candle fallback.")
            try:
                return self._load_coinbase_daily_prices(chain, start, end)
            except requests.RequestException as fallback_exc:
                raise RuntimeError(
                    f"{exc}\nCoinbase Exchange fallback also failed for {chain}: {fallback_exc}"
                ) from fallback_exc

    def _load_coingecko_daily_prices(self, chain: str, start: datetime, end: datetime) -> Dict[str, float]:
        chain_id = COINGECKO_IDS[chain]
        url = f"{self.base_url}/coins/{chain_id}/market_chart/range"
        params = {"vs_currency": "usd", "from": dt_to_epoch(start), "to": dt_to_epoch(end)}
        try:
            resp = self.session.get(url, params=params, headers=self.headers, timeout=30)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            detail = (resp.text or "").strip().replace("\n", " ")[:300]
            hint = ""
            if resp.status_code == 401:
                hint = (
                    " CoinGecko requires valid authentication for this request. "
                    "For historical ranges older than 365 days, use a paid CoinGecko key via "
                    "api_keys.coingecko_pro / COINGECKO_PRO_API_KEY."
                )
            elif resp.status_code == 429:
                hint = " CoinGecko rate-limited this request; wait and rerun, or use an API key with more quota."
            raise RuntimeError(
                f"CoinGecko price fetch failed for {chain} with HTTP {resp.status_code}.{hint} "
                f"Response: {detail}"
            ) from exc
        data = resp.json()
        out: Dict[str, float] = {}
        for ts_ms, price in data.get("prices", []):
            day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()
            out[day] = float(price)
        return out

    def _load_coinbase_daily_prices(self, chain: str, start: datetime, end: datetime) -> Dict[str, float]:
        product_id = COINBASE_PRODUCTS[chain]
        out: Dict[str, float] = {}
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=299))
            url = f"https://api.exchange.coinbase.com/products/{product_id}/candles"
            params = {
                "granularity": 86400,
                "start": chunk_start.isoformat().replace("+00:00", "Z"),
                "end": chunk_end.isoformat().replace("+00:00", "Z"),
            }
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            for candle in resp.json():
                if len(candle) < 5:
                    continue
                day = datetime.fromtimestamp(int(candle[0]), tz=timezone.utc).date().isoformat()
                out[day] = float(candle[4])
            chunk_start = chunk_end + timedelta(seconds=1)
            time.sleep(0.2)
        if not out:
            raise RuntimeError(f"Coinbase Exchange returned no USD candles for {chain}.")
        return out

    def get_price_usd(self, chain: str, ts: datetime, start: datetime, end: datetime) -> Optional[float]:
        if chain not in self.cache:
            self.cache[chain] = self._load_daily_prices(chain, start, end)
            time.sleep(1.2)
        return self.cache[chain].get(ts.date().isoformat())


def fetch_btc_or_ltc_blockchair(chain: str, wallet: str, api_key: str = "") -> List[dict]:
    url = f"https://api.blockchair.com/{chain}/dashboards/address/{wallet}"
    params = {"transaction_details": "true", "limit": 10000}
    if api_key:
        params["key"] = api_key
    try:
        resp = requests.get(url, params=params, timeout=45)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = (resp.text or "").strip().replace("\n", " ")[:300]
        hint = ""
        if resp.status_code == 430:
            hint = (
                " Blockchair uses HTTP 430 when anonymous or high-cost API access is blocked. "
                "Add api_keys.blockchair to wallet_config.yaml, set BLOCKCHAIR_API_KEY, "
                "or wait for Blockchair access to reset."
            )
        raise BlockchairFetchError(
            chain=chain,
            wallet=wallet,
            status_code=resp.status_code,
            message=(
                f"Blockchair failed for {chain} address {wallet} "
                f"with HTTP {resp.status_code}.{hint} Response: {detail}"
            ),
        ) from exc
    except requests.RequestException as exc:
        raise BlockchairFetchError(
            chain=chain,
            wallet=wallet,
            message=f"Blockchair request failed for {chain} address {wallet}: {exc}",
        ) from exc
    payload = resp.json()
    data = payload.get("data", {}).get(wallet, {})
    return data.get("transactions", [])


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


def _esplora_to_blockchair_tx(tx: dict, wallet: str) -> Optional[dict]:
    status = tx.get("status") or {}
    block_time = status.get("block_time")
    if not block_time:
        return None
    balance_change, spent = _esplora_balance_change(tx, wallet)
    tx_time = datetime.fromtimestamp(int(block_time), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "hash": tx.get("txid", ""),
        "time": tx_time,
        "balance_change": balance_change,
        "fee": int(tx.get("fee", 0) or 0) if spent else 0,
    }


def fetch_btc_or_ltc_esplora(chain: str, wallet: str) -> List[dict]:
    base_url = ESPLORA_APIS[chain]
    session = requests.Session()
    out: List[dict] = []
    last_seen_txid: Optional[str] = None
    while True:
        url = f"{base_url}/address/{wallet}/txs/chain"
        if last_seen_txid:
            url = f"{url}/{last_seen_txid}"
        resp = session.get(url, timeout=45)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        for tx in rows:
            normalized = _esplora_to_blockchair_tx(tx, wallet)
            if normalized is not None:
                out.append(normalized)
        last_seen_txid = rows[-1].get("txid")
        if len(rows) < 25 or not last_seen_txid:
            break
        time.sleep(0.2)
    return out


def fetch_btc_or_ltc_transactions(chain: str, wallet: str, blockchair_api_key: str = "") -> List[dict]:
    try:
        return fetch_btc_or_ltc_blockchair(chain, wallet, blockchair_api_key)
    except BlockchairFetchError as exc:
        if exc.status_code not in {429, 430, 435, 503, None}:
            raise
        fallback_name = "mempool.space" if chain == "bitcoin" else "litecoinspace.org"
        print(f"{chain}: Blockchair unavailable for {wallet}; trying {fallback_name} fallback.")
        try:
            return fetch_btc_or_ltc_esplora(chain, wallet)
        except requests.RequestException as fallback_exc:
            raise RuntimeError(
                f"{exc}\nFallback via {fallback_name} also failed for {chain} address {wallet}: "
                f"{fallback_exc}"
            ) from fallback_exc


def parse_btc_ltc(chain: str, wallet: str, tx: dict) -> TxRecord:
    raw_delta = int(tx.get("balance_change", 0))
    amount_coin = abs(raw_delta) / 1e8
    signed_amount = raw_delta / 1e8
    direction = "in" if raw_delta >= 0 else "out"
    fee_coin = float(tx.get("fee", 0)) / 1e8 if tx.get("fee") is not None else None
    ts = to_utc(tx["time"])
    return TxRecord(
        chain=chain,
        network=chain,
        record_type="transaction",
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
        network="ethereum",
        record_type="transaction",
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


def fetch_polkadot(wallet: str, api_key: str, network: str = "polkadot") -> List[dict]:
    base_url = POLKADOT_NETWORKS[network]
    url = f"{base_url}/api/v2/scan/transfers"
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


def fetch_polkadot_staking_rewards(wallet: str, api_key: str) -> List[dict]:
    url = f"{POLKADOT_NETWORKS['polkadot']}/api/scan/account/reward_slash"
    headers = {"Content-Type": "application/json", "X-API-Key": api_key}
    out: List[dict] = []
    seen: set[str] = set()
    for is_stash in (True, False):
        page = 0
        while True:
            body = {
                "address": wallet,
                "category": "Reward",
                "is_stash": is_stash,
                "page": page,
                "row": 100,
            }
            resp = requests.post(url, headers=headers, json=body, timeout=45)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") not in {0, "0", None}:
                raise RuntimeError(
                    f"Subscan staking reward API error for Polkadot address {wallet}: "
                    f"{payload.get('message', payload)}"
                )
            rewards = (payload.get("data", {}) or {}).get("list", []) or []
            if not rewards:
                break
            for reward in rewards:
                reward_key = (
                    str(reward.get("id") or "")
                    or str(reward.get("event_id") or "")
                    or str(reward.get("event_index") or "")
                    or str(reward.get("extrinsic_hash") or "")
                )
                if reward_key in seen:
                    continue
                seen.add(reward_key)
                out.append(reward)
            if len(rewards) < 100:
                break
            page += 1
            time.sleep(0.2)
    return out


def parse_dot(wallet: str, tr: dict, network: str = "polkadot") -> TxRecord:
    ts = datetime.fromtimestamp(int(tr["block_timestamp"]), tz=timezone.utc)
    raw_amount = str(tr.get("amount", "0") or "0")
    try:
        amount_dot = int(raw_amount) / 1e10
    except ValueError:
        try:
            amount_dot = float(Decimal(raw_amount))
        except InvalidOperation as exc:
            raise ValueError(f"Unexpected Polkadot amount value: {raw_amount}") from exc
    from_addr = tr.get("from", "")
    to_addr = tr.get("to", "")
    is_out = from_addr == wallet
    direction = "out" if is_out else "in"
    signed_amount = -amount_dot if is_out else amount_dot
    return TxRecord(
        chain="polkadot",
        network=network,
        record_type="transaction",
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


def parse_dot_reward(wallet: str, reward: dict) -> TxRecord:
    ts = datetime.fromtimestamp(int(reward["block_timestamp"]), tz=timezone.utc)
    raw_amount = str(reward.get("amount", "0") or "0")
    try:
        amount_dot = int(raw_amount) / 1e10
    except ValueError:
        try:
            amount_dot = float(Decimal(raw_amount))
        except InvalidOperation as exc:
            raise ValueError(f"Unexpected Polkadot staking reward amount value: {raw_amount}") from exc
    return TxRecord(
        chain="polkadot",
        network="polkadot",
        record_type="staking_reward",
        wallet=wallet,
        tx_hash=reward.get("extrinsic_hash") or reward.get("event_id") or reward.get("event_index", ""),
        timestamp=ts,
        direction="in",
        amount_coin=amount_dot,
        signed_amount_coin=amount_dot,
        price_usd=None,
        amount_usd=None,
        fee_coin=0.0,
        fee_usd=None,
        from_address=reward.get("stash", ""),
        to_address=reward.get("account", wallet),
    )


def _ada_lovelace_for_addresses(entries: List[dict], wallets: set[str]) -> int:
    total = 0
    for item in entries:
        if item.get("address") not in wallets:
            continue
        for amt in item.get("amount", []):
            if amt.get("unit") == "lovelace":
                total += int(amt.get("quantity", "0"))
    return total


def fetch_cardano_account_addresses(wallet: str, project_id: str) -> List[str]:
    headers = {"project_id": project_id}
    base = "https://cardano-mainnet.blockfrost.io/api/v0"
    stake_address = fetch_cardano_stake_address(wallet, project_id)
    if not stake_address:
        return [wallet]

    out: List[str] = []
    page = 1
    while True:
        url = f"{base}/accounts/{stake_address}/addresses"
        params = {"order": "asc", "page": page, "count": 100}
        resp = requests.get(url, headers=headers, params=params, timeout=45)
        if resp.status_code == 404:
            return [wallet]
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        out.extend(row["address"] for row in rows if row.get("address"))
        if len(rows) < 100:
            break
        page += 1
        time.sleep(0.2)

    if wallet not in out:
        out.append(wallet)
    return out


def fetch_cardano(wallet: str, project_id: str) -> List[dict]:
    headers = {"project_id": project_id}
    base = "https://cardano-mainnet.blockfrost.io/api/v0"
    account_addresses = set(fetch_cardano_account_addresses(wallet, project_id))
    tx_hashes: List[str] = []
    seen_tx_hashes: set[str] = set()
    for account_address in account_addresses:
        page = 1
        while True:
            url = f"{base}/addresses/{account_address}/transactions"
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
                tx_hashes.append(tx_hash)
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
        tx["_wallet_input_lovelace"] = _ada_lovelace_for_addresses(utxos.get("inputs", []), account_addresses)
        tx["_wallet_output_lovelace"] = _ada_lovelace_for_addresses(utxos.get("outputs", []), account_addresses)
        out.append(tx)
        time.sleep(0.1)
    return out


def fetch_cardano_stake_address(wallet: str, project_id: str) -> Optional[str]:
    headers = {"project_id": project_id}
    base = "https://cardano-mainnet.blockfrost.io/api/v0"
    resp = requests.get(f"{base}/addresses/{wallet}", headers=headers, timeout=45)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("stake_address")


def fetch_cardano_staking_rewards(wallet: str, project_id: str) -> List[dict]:
    headers = {"project_id": project_id}
    base = "https://cardano-mainnet.blockfrost.io/api/v0"
    stake_address = fetch_cardano_stake_address(wallet, project_id)
    if not stake_address:
        return []

    epoch_times: Dict[int, datetime] = {}
    out: List[dict] = []
    page = 1
    while True:
        url = f"{base}/accounts/{stake_address}/rewards"
        params = {"order": "asc", "page": page, "count": 100}
        resp = requests.get(url, headers=headers, params=params, timeout=45)
        if resp.status_code == 404:
            return []
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
            out.append(reward)
        if len(rewards) < 100:
            break
        page += 1
        time.sleep(0.2)
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
        network="cardano",
        record_type="transaction",
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


def parse_ada_reward(wallet: str, reward: dict) -> TxRecord:
    amount_ada = int(reward.get("amount", "0") or "0") / 1e6
    stake_address = reward.get("_wallet_stake_address", "")
    reward_type = reward.get("type", "member")
    epoch = reward.get("epoch", "")
    return TxRecord(
        chain="cardano",
        network="cardano",
        record_type="staking_reward",
        wallet=wallet,
        tx_hash=f"{stake_address}:epoch:{epoch}:{reward_type}",
        timestamp=reward["_reward_timestamp"],
        direction="in",
        amount_coin=amount_ada,
        signed_amount_coin=amount_ada,
        price_usd=None,
        amount_usd=None,
        fee_coin=0.0,
        fee_usd=None,
        from_address=reward.get("pool_id", ""),
        to_address=stake_address,
    )


def enrich_with_usd(
    records: List[TxRecord],
    start: datetime,
    end: datetime,
    coingecko_demo_key: str = "",
    coingecko_pro_key: str = "",
) -> None:
    if not records:
        return
    prices = PriceService(coingecko_demo_key, coingecko_pro_key)
    for rec in records:
        px = prices.get_price_usd(rec.chain, rec.timestamp, start, end)
        rec.price_usd = px
        if px is None:
            rec.amount_usd = None
            rec.fee_usd = None
            continue
        rec.amount_usd = rec.amount_coin * px
        rec.fee_usd = (rec.fee_coin or 0.0) * px


def to_dataframe(records: List[TxRecord], include_prices: bool = True) -> pd.DataFrame:
    rows = []
    for r in records:
        row = {
            "chain": r.chain,
            "network": r.network,
            "record_type": r.record_type,
            "wallet": r.wallet,
            "tx_hash": r.tx_hash,
            "timestamp_utc": r.timestamp.isoformat(),
            "direction": r.direction,
            "amount_coin": r.amount_coin,
            "signed_amount_coin": r.signed_amount_coin,
            "fee_coin": r.fee_coin,
            "from_address": r.from_address,
            "to_address": r.to_address,
        }
        if include_prices:
            row.update(
                {
                    "price_usd": r.price_usd,
                    "amount_usd": r.amount_usd,
                    "fee_usd": r.fee_usd,
                }
            )
        rows.append(row)
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
    parser.add_argument(
        "--cryptos",
        type=str,
        default="all",
        help="Comma-separated list of cryptos to process (bitcoin,litecoin,ethereum,polkadot,cardano) or 'all'.",
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


def generate_reports(
    year: int,
    config_path: str,
    selected_chains: set[str],
    summary_basename: str = "yearly_balances_summary",
    include_prices: bool = True,
    include_summary: bool = True,
    include_staking_rewards: bool = False,
) -> None:
    invalid = sorted(selected_chains - set(SUPPORTED_CHAINS))
    if invalid:
        raise ValueError(f"Unsupported crypto values: {', '.join(invalid)}")

    config = load_config(config_path)
    wallet_addresses = read_wallet_addresses(config)
    start, end = year_boundaries_utc(year)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    api_keys = config.get("api_keys", {}) or {}
    etherscan_key = str(api_keys.get("etherscan", "") or "")
    subscan_key = str(api_keys.get("subscan", "") or "")
    blockfrost_key = str(api_keys.get("blockfrost_project_id", "") or "")
    blockchair_key = str(api_keys.get("blockchair", "") or os.environ.get("BLOCKCHAIR_API_KEY", "") or "")
    coingecko_demo_key = str(
        api_keys.get("coingecko_demo", "")
        or api_keys.get("coingecko", "")
        or os.environ.get("COINGECKO_DEMO_API_KEY", "")
        or ""
    )
    coingecko_pro_key = str(
        api_keys.get("coingecko_pro", "") or os.environ.get("COINGECKO_PRO_API_KEY", "") or ""
    )

    all_time_records_by_chain: Dict[str, List[TxRecord]] = {chain: [] for chain in SUPPORTED_CHAINS}

    if "bitcoin" in selected_chains:
        for wallet in wallet_addresses["bitcoin"]:
            txs = fetch_btc_or_ltc_transactions("bitcoin", wallet, blockchair_key)
            all_time_records_by_chain["bitcoin"].extend(parse_btc_ltc("bitcoin", wallet, tx) for tx in txs)

    if "litecoin" in selected_chains:
        for wallet in wallet_addresses["litecoin"]:
            txs = fetch_btc_or_ltc_transactions("litecoin", wallet, blockchair_key)
            all_time_records_by_chain["litecoin"].extend(parse_btc_ltc("litecoin", wallet, tx) for tx in txs)

    if "ethereum" in selected_chains and wallet_addresses["ethereum"] and not etherscan_key:
        raise RuntimeError("ETHERSCAN_API_KEY is required for Ethereum addresses.")
    if "ethereum" in selected_chains:
        for wallet in wallet_addresses["ethereum"]:
            txs = fetch_ethereum(wallet, etherscan_key)
            all_time_records_by_chain["ethereum"].extend(parse_eth(wallet, tx) for tx in txs)

    if "polkadot" in selected_chains and wallet_addresses["polkadot"] and not subscan_key:
        raise RuntimeError("SUBSCAN_API_KEY is required for Polkadot addresses.")
    if "polkadot" in selected_chains:
        for wallet in wallet_addresses["polkadot"]:
            for network in POLKADOT_NETWORKS:
                txs = fetch_polkadot(wallet, subscan_key, network)
                all_time_records_by_chain["polkadot"].extend(parse_dot(wallet, tx, network) for tx in txs)
            if include_staking_rewards:
                rewards = fetch_polkadot_staking_rewards(wallet, subscan_key)
                all_time_records_by_chain["polkadot"].extend(parse_dot_reward(wallet, reward) for reward in rewards)

    if "cardano" in selected_chains and wallet_addresses["cardano"] and not blockfrost_key:
        raise RuntimeError("BLOCKFROST_PROJECT_ID is required for Cardano addresses.")
    seen_cardano_tx_hashes: set[str] = set()
    seen_cardano_rewards: set[str] = set()
    if "cardano" in selected_chains:
        for wallet in wallet_addresses["cardano"]:
            txs = fetch_cardano(wallet, blockfrost_key)
            for tx in txs:
                tx_hash = tx.get("hash", "")
                if tx_hash in seen_cardano_tx_hashes:
                    continue
                seen_cardano_tx_hashes.add(tx_hash)
                all_time_records_by_chain["cardano"].append(parse_ada(wallet, tx))
            if include_staking_rewards:
                rewards = fetch_cardano_staking_rewards(wallet, blockfrost_key)
                for reward in rewards:
                    reward_key = (
                        f"{reward.get('_wallet_stake_address', '')}:"
                        f"{reward.get('epoch', '')}:"
                        f"{reward.get('type', '')}:"
                        f"{reward.get('pool_id', '')}:"
                        f"{reward.get('amount', '')}"
                    )
                    if reward_key in seen_cardano_rewards:
                        continue
                    seen_cardano_rewards.add(reward_key)
                    all_time_records_by_chain["cardano"].append(parse_ada_reward(wallet, reward))

    year_records_by_chain: Dict[str, List[TxRecord]] = {}
    for chain in SUPPORTED_CHAINS:
        if chain not in selected_chains:
            continue
        records = all_time_records_by_chain[chain]
        year_records = [r for r in records if start <= r.timestamp <= end]
        year_records_by_chain[chain] = year_records
        if include_prices:
            enrich_with_usd(year_records, start, end, coingecko_demo_key, coingecko_pro_key)
        price_suffix = "_usd" if include_prices else ""
        out_file = OUTPUT_DIR / f"{chain}_{year}_transactions{price_suffix}.xlsx"
        to_dataframe(year_records, include_prices=include_prices).to_excel(out_file, index=False)
        print(f"{chain}: {len(year_records)} year rows -> {out_file}")

    if not include_summary:
        return

    prices = PriceService(coingecko_demo_key, coingecko_pro_key)
    summary_rows = []
    for chain, wallet_list in wallet_addresses.items():
        if chain not in selected_chains:
            continue
        start_px = prices.get_price_usd(chain, start, start, end) if include_prices and wallet_list else None
        end_px = prices.get_price_usd(chain, end, start, end) if include_prices and wallet_list else None
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
    summary_file = OUTPUT_DIR / f"{summary_basename}_{year}.xlsx"
    summary_df.to_excel(summary_file, index=False)
    print(f"summary: {len(summary_df)} rows -> {summary_file}")


def run() -> None:
    args = parse_args()
    if args.cryptos.strip().lower() == "all":
        selected_chains = set(SUPPORTED_CHAINS)
    else:
        selected_chains = {c.strip().lower() for c in args.cryptos.split(",") if c.strip()}
    generate_reports(args.year, args.config, selected_chains)


if __name__ == "__main__":
    run()
