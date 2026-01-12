# Predict Market Maker

A conservative market maker for [Predict.fun](https://predict.fun) that uses Polymarket/Kalshi prices as external reference to provide liquidity on Predict markets.

## Features

- **External Price Reference**: Uses Polymarket and Kalshi orderbooks as reference prices
- **Conservative Strategy**: Default sell-only mode to minimize risk exposure
- **Risk Management**: Automatic pause on price jumps, fetch failures, or position thresholds
- **Position Merging**: Automatic merge of dual-side positions to reclaim collateral
- **Interactive TUI**: Rich terminal interface for monitoring and control
- **Persistent State**: SQLite database for crash recovery and state persistence
- **Rate Limiting**: Built-in rate limiting for all API calls

## Requirements

- Python 3.10+
- Predict.fun account (with API key for mainnet)
- Wallet with funds for trading

## Installation

1. Clone the repository:
```bash
git clone https://github.com/your-repo/predict-market-maker.git
cd predict-market-maker
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure environment:
```bash
cp .env.example .env
# Edit .env with your API keys and wallet
```

## Configuration

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `PMM_ENVIRONMENT` | `testnet` or `mainnet` | No (default: testnet) |
| `PMM_PREDICT_API_KEY` | Predict API key | No (recommended for mainnet) |
| `PMM_WALLET_PRIVATE_KEY` | Wallet private key | Yes (for transactions) |
| `PMM_MARKET_ALLOWLIST` | Comma-separated market IDs | No |

### Configuration File

Copy `config.example.yaml` to `config.yaml` and customize:

```yaml
environment: testnet

strategy:
  pricing_mode: sell_only  # Conservative default
  offset: "0.01"           # 1 cent below external ask
  order_size: "10"         # Shares per order
  max_new_shares: 1000     # Max accumulation before pause

risk:
  jump_threshold: "0.02"   # 2 cent jump triggers pause
  max_consecutive_failures: 3
```

## Usage

### Interactive Mode (TUI)

```bash
python main.py
```

### Headless Mode

```bash
python main.py --headless
```

### Command Line Options

```bash
python main.py --help

Options:
  --headless          Run without UI (daemon mode)
  --testnet           Use testnet environment
  --mainnet           Use mainnet environment
  --config FILE       Configuration file path
  --allowlist MARKETS Comma-separated market IDs
  --db PATH           Database file path
  --log-level LEVEL   Log level (DEBUG, INFO, WARNING, ERROR)
  --log-file PATH     Log file path
```

## TUI Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `s` | Start all markets |
| `p` | Pause all markets |
| `c` | Cancel all orders |
| `a` | Activate selected market |
| `w` | Pause selected market |
| `r` | Refresh market data |
| `q` | Quit |

## Architecture

```
src/
├── config/          # Configuration management
├── api/             # API clients
│   ├── predict.py   # Predict.fun API
│   ├── polymarket.py# Polymarket CLOB API
│   └── kalshi.py    # Kalshi API
├── market/          # Market discovery and mapping
├── price/           # External price feed engine
├── order/           # Order management
├── risk/            # Risk management with state machine
├── position/        # Position tracking and auto-merge
├── persistence/     # SQLite persistence
├── ui/              # Textual TUI application
├── utils/           # Utilities (types, logging, rate limiting)
└── engine.py        # Main engine integrating all components
```

## Market States

Each market can be in one of three states:

- **ACTIVE**: Running, polling prices, placing orders
- **WATCH**: Paused, no orders, waiting for manual resume
- **DISABLED**: Cannot be auto-restored (market closed)

### State Transitions

```
                    ┌─────────────────────────────────┐
                    │                                 │
                    ▼                                 │
              ┌──────────┐   Risk Event    ┌─────────┴──┐
   Start ────►│  ACTIVE  │────────────────►│   WATCH    │
              └────┬─────┘                 └─────┬──────┘
                   │                             │
                   │ Market                      │ Manual
                   │ Closed                      │ Disable
                   │                             │
                   ▼                             ▼
              ┌──────────────────────────────────────┐
              │             DISABLED                  │
              └──────────────────────────────────────┘
```

## Risk Events

The system automatically pauses markets (moves to WATCH) when:

1. **Price Jump**: External price changes by more than `jump_threshold`
2. **Fetch Failures**: 3+ consecutive price fetch failures
3. **Position Threshold**: New shares accumulated exceed `max_new_shares`

## Pricing Modes

### Sell-Only (Default)

Conservative mode that only places sell orders. References external best ask price and undercuts by `offset`.

```
Predict Ask = External Ask - offset
```

This mode minimizes risk by not actively buying, only providing sell liquidity.

### Two-Sided

Places both buy and sell orders. More aggressive but requires tighter risk management.

```
Predict Bid = External Bid - offset
Predict Ask = External Ask - offset
```

## Position Merging

When holding both YES and NO positions in the same market, the system can automatically merge them to reclaim collateral:

```
YES Position: 100 shares
NO Position: 80 shares
Mergeable: 80 shares → Returns 80 units of collateral
```

## Price Conventions

### Predict
- Orderbook stored in YES price terms
- NO prices calculated as complement: `NO = 1 - YES`

### Polymarket
- Binary markets with YES/NO tokens
- Prices satisfy: `YES + NO = $1`

### Kalshi
- Prices in cents (1-99)
- Only bids returned in orderbook
- Asks derived: `YES ask @ X = NO bid @ (100-X)`

## Safety Features

1. **Safe Exit**: Cancel all orders on shutdown (enabled by default)
2. **No Auto-Resume**: Markets start in WATCH state on restart
3. **Rate Limiting**: Respects API rate limits (240 rpm for Predict)
4. **Fast Removal Warning**: Dangerous mode disabled by default

## License

MIT License - See LICENSE file for details.

## Disclaimer

This software is provided for educational and informational purposes only. Trading prediction markets involves financial risk. Use at your own risk. The authors are not responsible for any financial losses incurred through the use of this software.
