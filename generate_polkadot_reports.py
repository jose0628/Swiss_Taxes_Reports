#!/usr/bin/env python3
"""
Generate yearly Polkadot transaction and staking reward reports.
"""

from __future__ import annotations

import argparse


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


def run() -> None:
    args = parse_args()
    from generate_wallet_reports import generate_reports

    generate_reports(
        year=args.year,
        config_path=args.config,
        selected_chains={"polkadot"},
        include_prices=args.include_prices,
        include_summary=True,
        include_staking_rewards=True,
    )


if __name__ == "__main__":
    run()
