#!/usr/bin/env python3
"""
Predict Market Maker - Conservative Market Making System

A conservative market maker for Predict.fun that uses Polymarket/Kalshi
prices as external reference to provide liquidity on Predict markets.

Features:
- External price feed polling (Polymarket, Kalshi)
- Automatic order placement based on strategy
- Risk management with automatic pause triggers
- Position tracking and auto-merge
- Interactive TUI for monitoring and control
- Persistent state across restarts

Usage:
    python main.py [options]

Options:
    --headless          Run without UI (daemon mode)
    --testnet           Use testnet environment
    --mainnet           Use mainnet environment
    --config FILE       Load configuration from YAML file
    --allowlist MARKETS Comma-separated market IDs to trade

Environment Variables:
    PMM_PREDICT_API_KEY     Predict API key
    PMM_WALLET_PRIVATE_KEY  Wallet private key
    PMM_ENVIRONMENT         Environment (testnet/mainnet)
"""

import asyncio
import argparse
import signal
import sys
from pathlib import Path
from typing import Optional

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import get_settings, update_settings, Settings, Environment
from src.engine import MarketMakerEngine
from src.utils import setup_logging, get_logger


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Predict Market Maker - Conservative Market Making System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without UI (daemon mode)"
    )

    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Use testnet environment"
    )

    parser.add_argument(
        "--mainnet",
        action="store_true",
        help="Use mainnet environment"
    )

    parser.add_argument(
        "--config",
        type=str,
        help="Configuration file path (YAML)"
    )

    parser.add_argument(
        "--allowlist",
        type=str,
        help="Comma-separated market IDs to trade"
    )

    parser.add_argument(
        "--db",
        type=str,
        help="Database file path"
    )

    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level"
    )

    parser.add_argument(
        "--log-file",
        type=str,
        help="Log file path"
    )

    return parser.parse_args()


def load_config(args: argparse.Namespace) -> Settings:
    """Load and configure settings."""
    settings = get_settings()

    # Override from config file
    if args.config:
        # TODO: Load YAML config
        pass

    # Override from command line
    if args.testnet:
        settings.environment = Environment.TESTNET
    elif args.mainnet:
        settings.environment = Environment.MAINNET

    if args.allowlist:
        settings.market_allowlist = [
            m.strip() for m in args.allowlist.split(",")
        ]

    if args.db:
        settings.database_path = args.db

    update_settings(settings)
    return settings


async def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Setup logging
    setup_logging(
        level=args.log_level,
        log_file=args.log_file
    )

    logger = get_logger(__name__)
    logger.info("Starting Predict Market Maker...")

    # Load configuration
    settings = load_config(args)
    logger.info(
        f"Environment: {settings.environment.value}",
        allowlist_count=len(settings.market_allowlist)
    )

    # Create engine
    engine = MarketMakerEngine()

    # Handle signals
    shutdown_event = asyncio.Event()

    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        if args.headless:
            # Run in headless mode
            logger.info("Running in headless mode")

            # Override shutdown event
            engine._shutdown_event = shutdown_event

            await engine.run_headless()
        else:
            # Run with TUI
            logger.info("Running with TUI")

            # Override shutdown event
            engine._shutdown_event = shutdown_event

            await engine.run_with_ui()

        return 0

    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        return 1

    finally:
        logger.info("Predict Market Maker stopped")


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)
