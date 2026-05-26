# Swiss Taxes Reports

Python tooling to generate crypto transaction reports for tax declarations.

## What This Project Does

- Pulls transactions for Ledger wallet addresses (BTC, LTC, ETH, DOT, ADA).
- Converts transaction values to USD.
- Exports one Excel file per cryptocurrency.
- Exports a yearly summary with opening and closing balances.

## Requirements

- Conda (Miniconda or Anaconda)
- API keys for:
  - Etherscan (Ethereum)
  - Subscan (Polkadot)
  - Blockfrost (Cardano)
  - Blockchair (optional, recommended for Bitcoin/Litecoin if the free API returns HTTP 430)
  - CoinGecko (optional, preferred USD historical price source)

## 1) Create Conda Environment

```bash
conda create -n swiss-tax-reports python=3.11 -y
conda activate swiss-tax-reports
pip install -r requirements.txt
```

## 2) Get API Keys

### Etherscan (Ethereum)

1. Create an account at [Etherscan](https://etherscan.io/).
2. Go to API keys in your account dashboard.
3. Create a key and copy it.
4. Put it in `wallet_config.yaml` under `api_keys.etherscan`.

### Subscan (Polkadot)

1. Create an account at [Subscan](https://subscan.io/).
2. Open the API/key management area in your account.
3. Create an API key and copy it.
4. Put it in `wallet_config.yaml` under `api_keys.subscan`.

### Blockfrost (Cardano)

1. Create an account at [Blockfrost](https://blockfrost.io/).
2. Create a Cardano mainnet project.
3. Copy the project ID.
4. Put it in `wallet_config.yaml` under `api_keys.blockfrost_project_id`.

### Blockchair (Bitcoin/Litecoin, Optional)

Bitcoin and Litecoin use Blockchair first. If Blockchair returns HTTP 430, the script falls back to public Esplora-compatible explorers:

- Bitcoin: [mempool.space](https://mempool.space/)
- Litecoin: [litecoinspace.org](https://litecoinspace.org/)

For more reliable Blockchair access, add a Blockchair API key under `api_keys.blockchair` or set the `BLOCKCHAIR_API_KEY` environment variable.

### CoinGecko / Coinbase (USD Prices)

CoinGecko is used first for USD historical prices. If CoinGecko is unavailable or the requested range is outside the public plan's historical limit, the script falls back to Coinbase Exchange daily USD candles.

Create a CoinGecko Demo API key and put it under `api_keys.coingecko_demo`, or set `COINGECKO_DEMO_API_KEY`. For historical ranges older than 365 days, use a paid CoinGecko key under `api_keys.coingecko_pro` or set `COINGECKO_PRO_API_KEY`.

## 3) Get Wallet Addresses From Ledger

Use Ledger Live and copy the **public receiving address** for each account:

1. Open Ledger Live.
2. Go to **Accounts** and select the account for the cryptocurrency you want to report.
3. Click **Receive**.
4. Choose the correct account if Ledger Live asks again.
5. Connect and unlock your Ledger device.
6. Open the matching app on the Ledger device:
   - Bitcoin app for Bitcoin
   - Litecoin app for Litecoin
   - Ethereum app for Ethereum
   - Polkadot app for Polkadot
   - Cardano app for Cardano
7. Let Ledger Live show the public receiving address.
8. Verify the address on the Ledger device screen.
9. Copy the address and paste it into the matching section of `wallet_config.yaml`.

Only public addresses are needed. Never store private keys or seed phrases in this project.

## 4) Configure `wallet_config.yaml`

Copy the template:

```bash
cp wallet_config.example.yaml wallet_config.yaml
```

Edit `wallet_config.yaml`:

```yaml
api_keys:
  etherscan: "YOUR_ETHERSCAN_KEY"
  subscan: "YOUR_SUBSCAN_KEY"
  blockfrost_project_id: "YOUR_BLOCKFROST_PROJECT_ID"
  blockchair: ""
  coingecko_demo: "YOUR_COINGECKO_DEMO_KEY"
  coingecko_pro: ""

wallet_addresses:
  bitcoin:
    - "bc1qexample..."
  litecoin:
    - "ltc1qexample..."
  ethereum:
    - "0xExampleAddress..."
  polkadot:
    - "1ExampleAddress..."
  cardano:
    - "addr1example..."
```

How to place Ledger addresses in the file:

- Add Bitcoin Ledger addresses under `wallet_addresses.bitcoin`
- Add Litecoin Ledger addresses under `wallet_addresses.litecoin`
- Add Ethereum Ledger addresses under `wallet_addresses.ethereum`
- Add Polkadot Ledger addresses under `wallet_addresses.polkadot`
- Add Cardano Ledger addresses under `wallet_addresses.cardano`

Example with multiple Ledger accounts:

```yaml
wallet_addresses:
  bitcoin:
    - "bc1qfirstledgeraddress..."
    - "bc1qsecondledgeraddress..."
  litecoin:
    - "ltc1qledgeraddress..."
  ethereum:
    - "0xFirstLedgerAddress..."
    - "0xSecondLedgerAddress..."
  polkadot:
    - "1FirstLedgerAddress..."
  cardano:
    - "addr1firstledgeraddress..."
```

If you use more than one Ledger account for the same coin, add each public address as a new item in that coin list.

## 5) Run The Report

Default (all supported cryptos) for tax year 2025:

```bash
python3 generate_wallet_reports.py --year 2025
```

Run only selected cryptos:

```bash
python3 generate_wallet_reports.py --year 2025 --cryptos bitcoin,ethereum,cardano
```

Optional custom config path:

```bash
python3 generate_wallet_reports.py --year 2025 --cryptos litecoin --config /absolute/path/to/wallet_config.yaml
```

`--cryptos` accepts:

- `all` (default)
- Comma-separated values from: `bitcoin,litecoin,ethereum,polkadot,cardano`

Cardano only, including staking rewards:

```bash
python3 generate_cardano_reports.py --year 2025
```

Polkadot only, including staking rewards:

```bash
python3 generate_polkadot_reports.py --year 2025
```

These fetch transactions plus staking reward rows and write:

- `reports/cardano_2025_transactions.xlsx`
- `reports/polkadot_2025_transactions.xlsx`

For Cardano, the script uses the configured address to find the linked stake address, then checks all payment addresses associated with that stake account.
For Polkadot, the script checks both the relay-chain network (`polkadot`) and Asset Hub (`assethub-polkadot`). Staking reward rows are fetched from the relay-chain reward endpoint.

Add historical USD prices only when needed, for example:

```bash
python3 generate_cardano_reports.py --year 2025 --include-prices
```

With a custom config path:

```bash
python3 generate_polkadot_reports.py --year 2025 --config /absolute/path/to/wallet_config.yaml
```

## Output Files

Generated in `reports/`:

- `bitcoin_2025_transactions_usd.xlsx`
- `litecoin_2025_transactions_usd.xlsx`
- `ethereum_2025_transactions_usd.xlsx`
- `polkadot_2025_transactions_usd.xlsx`
- `cardano_2025_transactions_usd.xlsx`
- `yearly_balances_summary_2025.xlsx`
- `polkadot_2025_transactions.xlsx` when using the dedicated Polkadot script without `--include-prices`
- `cardano_2025_transactions.xlsx` when using the dedicated Cardano script without `--include-prices`

## Security Notes

- `wallet_config.yaml` is ignored by git and should stay private.
- Do not commit API keys.
- Do not include private keys or seed phrases.
