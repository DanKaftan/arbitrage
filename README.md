# Polymarket Market-Maker/Arbitrage Bot

A production-grade Python bot for automated market-making and arbitrage on Polymarket using an object-oriented, multi-agent architecture.

## Features

- **Multi-Agent Architecture**: Each trader manages a single market independently
- **Async/Await**: Fully asynchronous implementation using asyncio
- **Risk Management**: Per-trader and global risk limits
- **Order Management**: Automatic stale order cancellation and position tracking
- **Spread Detection**: Configurable minimum spread thresholds
- **Price Improvement**: Places orders with configurable price improvement
- **Production Ready**: Comprehensive error handling, logging, and monitoring

## Architecture

```
/trading/
    /core/             # Core trading components
        trader.py      # Individual trader agents (1 per market)
        manager.py     # Manager coordinating all traders
        execution.py   # Wrapper around py_clob_client
    /utils/            # Trading utilities
        market_resolver.py  # Resolve market slugs to IDs

/scripts/              # Entry points
    main.py            # CLI entry point

config.py              # Configuration management (loads from .env)
```

## Quick Start

1. **Clone and setup:**
```bash
git clone <repository-url>
cd arbitrage
python3.11+ -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

2. **Configure environment:**
```bash
cp .env.example .env
# Edit .env and add your credentials
```

3. **Verify setup:**
```bash
python scripts/verify_setup.py
```

4. **Configure traders:**
   - Add traders via Supabase (recommended for persistence)
   - Or configure via `.env` file using `TRADER_MARKETS`, `TRADER_BUDGETS`, etc.
   - See Configuration section below for details

5. **Run the bot:**
```bash
python scripts/main.py
```

That's it! Your traders will start automatically.

## Installation

### Step 1: Clone and Setup

```bash
git clone <repository-url>
cd arbitrage
python3.11+ -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Step 2: Configure Environment

```bash
# Copy the example env file
cp .env.example .env

# Edit .env and add your API credentials
# Required:
POLYMARKET_API_KEY=your_key
POLYMARKET_API_SECRET=your_secret

# Optional but recommended (for trader persistence):
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_key
```

### Step 3: (Optional) Set up Supabase

For trader persistence across restarts:

1. Create a Supabase project at https://supabase.com
2. Run the SQL migration in `docs/supabase_migration.sql`
3. Add `SUPABASE_URL` and `SUPABASE_KEY` to your `.env`

**Note**: If Supabase is not configured, traders can be configured via `.env` file but won't persist across restarts.

### Step 4: Verify Setup

```bash
python scripts/verify_setup.py
```

This will check:
- ✅ Dependencies are installed
- ✅ .env file exists
- ✅ Required environment variables are set
- ✅ Configuration loads correctly
- ✅ Supabase connection (if configured)

All configuration is done via the `.env` file. See `.env.example` for all available options.

## Configuration

All configuration is managed through the `.env` file. The configuration system uses:

- **`config.py`**: Defines configuration dataclasses and loaders
- **`.env`**: Contains all environment-specific values (API keys, settings, etc.)
- **`.env.example`**: Template showing all available configuration options
- **Supabase**: Trader configurations are persisted in Supabase (optional but recommended)

### Configuration Structure

1. **API Configuration** (`POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, etc.)
2. **Execution Layer** (`EXECUTION_MAX_RETRIES`, `EXECUTION_TIMEOUT`, etc.)
3. **Manager Settings** (`MANAGER_POLL_INTERVAL`, `MANAGER_MAX_EXPOSURE`, etc.)
4. **Trader Defaults** (`TRADER_DEFAULT_BUDGET`, `TRADER_DEFAULT_MIN_GAP`, etc.)
5. **Pre-configured Traders** (Optional: `TRADER_MARKETS`, `TRADER_BUDGETS`, etc.)

### Trader Configuration

Each trader is configured with:
- `market_id`: The Polymarket market ID to trade
- `market_slug`: Human-readable market slug (e.g., "russia-x-ukraine-ceasefire-in-2025")
- `name`: Friendly name for the trader
- `budget`: Maximum capital per market
- `min_gap`: Minimum spread threshold in cents (absolute price difference, e.g., 0.01 = 1 cent)
- `price_improvement`: Price improvement in cents (absolute amount, e.g., 0.001 = 0.1 cent)
- `order_timeout_seconds`: Cancel orders older than this

### Manager Configuration

The manager monitors:
- `max_total_exposure`: Maximum total capital across all traders
- `max_total_pnl_loss`: Emergency shutdown threshold
- `poll_interval_seconds`: How often to run trader steps
- `status_update_interval_seconds`: How often to print status

## Usage

### Running the Bot

1. Configure traders via Supabase (recommended) or `.env` file:
   - **Via Supabase**: Add traders to your Supabase `traders` table
   - **Via .env**: Set `TRADER_MARKETS`, `TRADER_BUDGETS`, etc. (see Configuration section)

2. Run the bot:
```bash
python scripts/main.py
```

**On startup, the bot automatically loads all active traders from Supabase** (if configured). If no traders are found in Supabase, it falls back to `.env` configuration.

## How It Works

1. **Trader Step Loop**: Each trader periodically:
   - Fetches the orderbook
   - Calculates spread (ask - bid)
   - If spread (in cents) >= min_gap and no position: places buy order above best bid
   - If holding position: places sell order below best ask
   - Cleans stale orders
   - Updates position tracking

2. **Manager Coordination**: The manager:
   - Runs all traders in parallel
   - Monitors global risk (total exposure, total P&L)
   - Prints status updates
   - Handles emergency shutdowns

3. **Execution Layer**: Wraps py_clob_client with:
   - Async operations
   - Retry logic
   - Price/size rounding
   - Latency tracking

## Risk Management

- **Per-Trader Limits**: Budget, max position size, order timeouts
- **Global Limits**: Total exposure, total P&L loss threshold
- **Stale Order Handling**: Automatic cancellation of old orders
- **Emergency Shutdown**: Automatic shutdown if risk limits exceeded

## Logging

The bot logs to stdout with timestamps. Log levels:
- `INFO`: Normal operations, order submissions, status updates
- `WARNING`: Failed operations, stale orders
- `ERROR`: Critical errors, risk limit violations

## Development

### Code Structure

- **Type Hints**: All functions have type hints
- **Dataclasses**: Used for configuration and state
- **Async/Await**: No blocking calls
- **Error Handling**: Try/except on all network operations
- **Modular Design**: Each component is independently testable

### Testing

To test individual components:
```python
from trading.execution import ExecutionLayer
from config import ExecutionConfig

config = ExecutionConfig(api_key="...", api_secret="...")
execution = ExecutionLayer(config)
```

## Future Enhancements

- WebSocket orderbook listener for real-time updates
- SQLite database for P&L tracking
- Auto-recovery after crashes
- Per-trader log files in `/logs/market_id.log`
- Charts and visualizations for P&L over time

## Disclaimer

This bot is for educational purposes. Trading involves risk. Always test thoroughly before using real funds.

## License

[Your License Here]

# arbitrage
