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

## 3) Get Wallet Addresses From Ledger

Use Ledger Live and copy the **public receiving address** for each account:

1. Open Ledger Live.
2. Open each crypto account (Bitcoin, Litecoin, Ethereum, Polkadot, Cardano).
3. Click **Receive**.
4. Verify the address on device.
5. Copy the address and paste it into `wallet_config.yaml`.

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

## 5) Run The Report

Example for tax year 2025:

```bash
python3 generate_wallet_reports.py --year 2025
```

Optional custom config path:

```bash
python3 generate_wallet_reports.py --year 2025 --config /absolute/path/to/wallet_config.yaml
```

## Output Files

Generated in `reports/`:

- `bitcoin_2025_transactions_usd.xlsx`
- `litecoin_2025_transactions_usd.xlsx`
- `ethereum_2025_transactions_usd.xlsx`
- `polkadot_2025_transactions_usd.xlsx`
- `cardano_2025_transactions_usd.xlsx`
- `yearly_balances_summary_2025.xlsx`

## Security Notes

- `wallet_config.yaml` is ignored by git and should stay private.
- Do not commit API keys.
- Do not include private keys or seed phrases.
