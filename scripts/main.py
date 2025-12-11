"""Entry point for the Polymarket market-maker/arbitrage bot."""

import asyncio
import logging
import sys
import os
from typing import List
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config import (
    TraderConfig,
    ExecutionConfig,
    ManagerConfig,
    SupabaseConfig,
    load_execution_config,
    load_manager_config,
    load_supabase_config,
    get_supabase_config,  # Legacy, for backward compatibility
)
from trading import ExecutionLayer, TraderManager
from services import SupabaseService


# Configure logging
import os
from pathlib import Path

# Create logs directory if it doesn't exist
log_dir = Path(__file__).parent.parent / "logs"
log_dir.mkdir(exist_ok=True)

# Create log file with timestamp
from datetime import datetime
log_filename = log_dir / f"trading_bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

# Configure logging with both file and console handlers
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # Console output
        logging.FileHandler(log_filename),  # File output
    ],
)

logger = logging.getLogger(__name__)


def load_market_configs() -> List[TraderConfig]:
    """Load trader configurations for target markets.
    
    This function can be customized to load market configurations
    from a file, database, or hardcoded values.
    
    Note: You can also configure traders via .env file using TRADER_MARKETS, etc.
    """
    # Example: Hardcoded configs (for CLI mode)
    # You can also load from .env using load_default_trader_configs()
    return []


async def main():
    """Main entry point."""
    logger.info("=" * 80)
    logger.info("Polymarket Market-Maker/Arbitrage Bot")
    logger.info("=" * 80)
    
    try:
        # Load configurations
        logger.info("Loading configurations...")
        execution_config = load_execution_config()
        manager_config = load_manager_config()
        
        # Initialize Supabase service
        supabase_url, supabase_key = get_supabase_config()
        supabase_service = None
        if supabase_url and supabase_key:
            supabase_service = SupabaseService(supabase_url, supabase_key)
            if supabase_service.is_available():
                logger.info("Supabase service initialized")
            else:
                logger.warning("Supabase service not available (check credentials)")
        else:
            logger.warning("Supabase credentials not found. Traders will not persist to database.")
        
        # Load traders from Supabase
        trader_configs = []
        if supabase_service and supabase_service.is_available():
            trader_configs = await supabase_service.load_all_traders()
            logger.info(f"Loaded {len(trader_configs)} traders from Supabase")
        
        # Fallback to local config if no traders in Supabase
        if not trader_configs:
            trader_configs = load_market_configs()
            if trader_configs:
                logger.info(f"Loaded {len(trader_configs)} traders from local config (fallback)")
        
        if not trader_configs:
            logger.warning("No trader configurations found. Configure traders via Supabase or .env file.")
            logger.info("Continuing with empty trader list...")
        
        # Validate API credentials
        if not execution_config.api_key or not execution_config.api_secret:
            logger.warning(
                "API credentials not found in environment variables. "
                "Set POLYMARKET_API_KEY and POLYMARKET_API_SECRET. "
                "Running in mock mode."
            )
        
        # Initialize execution layer
        logger.info("Initializing execution layer...")
        execution_layer = ExecutionLayer(execution_config)
        
        # Initialize manager with Supabase service
        logger.info("Initializing trader manager...")
        manager = TraderManager(
            execution_layer=execution_layer,
            config=manager_config,
            supabase_service=supabase_service,
        )
        
        # Add traders
        logger.info("Adding traders...")
        for trader_config in trader_configs:
            manager.add_trader(trader_config)
            logger.info(
                f"  Added trader for market {trader_config.market_id} "
                f"(max_inventory={trader_config.max_inventory}, spread_threshold={trader_config.spread_threshold}¢)"
            )
        
        # Start the manager
        logger.info("Starting trader manager...")
        logger.info("Press Ctrl+C to stop")
        await manager.run()
        
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Bot shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())

