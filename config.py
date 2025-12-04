"""Configuration management for the trading bot."""

import os
from dataclasses import dataclass
from typing import List, Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


# ============================================================================
# Supabase Configuration
# ============================================================================

@dataclass
class SupabaseConfig:
    """Configuration for Supabase database."""
    url: str
    key: str
    table_name: str = "traders"  # Table name for traders


def load_supabase_config() -> Optional[SupabaseConfig]:
    """Load Supabase configuration from environment variables.
    
    Returns:
        SupabaseConfig if URL and key are provided, None otherwise
    """
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    table_name = os.getenv("SUPABASE_TABLE_NAME", "traders")
    
    if url and key:
        return SupabaseConfig(url=url, key=key, table_name=table_name)
    return None


def get_supabase_config() -> tuple[Optional[str], Optional[str]]:
    """Get Supabase configuration from environment (legacy function for backward compatibility).
    
    Returns:
        Tuple of (supabase_url, supabase_key)
    """
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    return url, key


@dataclass
class ExecutionConfig:
    """Configuration for the execution layer.
    
    Note: This is just the data structure. To load from .env, use load_execution_config().
    Values are loaded from environment variables (see load_execution_config() below).
    """
    api_key: str  # From POLYMARKET_API_KEY in .env
    api_secret: str  # From POLYMARKET_API_SECRET in .env
    api_passphrase: str = ""  # From POLYMARKET_PASSPHRASE in .env
    private_key: str = ""  # From POLYMARKET_PRIVATE_KEY in .env
    wallet_address: str = ""  # From POLYMARKET_ADDRESS in .env
    chain_id: int = 137  # From POLYMARKET_CHAIN_ID in .env (default: 137)
    api_base_url: str = "https://clob.polymarket.com"  # From POLYMARKET_API_BASE_URL in .env
    max_retries: int = 3  # From EXECUTION_MAX_RETRIES in .env
    retry_delay_seconds: float = 0.5  # From EXECUTION_RETRY_DELAY in .env
    request_timeout_seconds: int = 10  # From EXECUTION_TIMEOUT in .env
    price_precision: int = 4  # From EXECUTION_PRICE_PRECISION in .env
    size_precision: int = 2  # From EXECUTION_SIZE_PRECISION in .env


@dataclass
class TraderConfig:
    """Configuration for a single trader agent."""
    market_id: str  # Condition ID (for identification)
    token_id: str = ""  # Token ID for YES outcome (for API calls - required for orderbook/orders)
    market_slug: str = ""  # Market slug (e.g., "russia-x-ukraine-ceasefire-in-2025")
    name: str = ""  # Friendly name for the trader (defaults to market_id if not provided)
    budget: float = 10.0  # Max capital in dollars (total amount trader can spend)
    min_gap: float = 1  # Minimum spread threshold in cents
    price_improvement: float = 1  # Price improvement in cents
    max_retries: int = 3
    retry_delay_seconds: float = 1.0


@dataclass
class ManagerConfig:
    """Configuration for the trader manager.
    
    Note: To load from .env, use load_manager_config().
    Values are loaded from environment variables (see load_manager_config() below).
    """
    poll_interval_seconds: float = 3.0  # From MANAGER_POLL_INTERVAL in .env
    max_total_exposure: float = 10000.0  # From MANAGER_MAX_EXPOSURE in .env
    max_total_pnl_loss: float = -1000.0  # From MANAGER_MAX_PNL_LOSS in .env
    status_update_interval_seconds: float = 5.0  # From MANAGER_STATUS_INTERVAL in .env
    supabase_sync_interval_seconds: float = 30.0  # From MANAGER_SUPABASE_SYNC_INTERVAL in .env
    enable_emergency_shutdown: bool = True  # From MANAGER_EMERGENCY_SHUTDOWN in .env


# ============================================================================
# Configuration Loaders
# ============================================================================

def load_execution_config() -> ExecutionConfig:
    """Load execution config from environment variables."""
    return ExecutionConfig(
        api_key=os.getenv("POLYMARKET_API_KEY", ""),
        api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
        api_passphrase=os.getenv("POLYMARKET_PASSPHRASE", "") or os.getenv("POLYMARKET_API_PASSPHRASE", ""),  # Support both names
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY", ""),  # Wallet private key for signing orders
        wallet_address=os.getenv("POLYMARKET_ADDRESS", ""),  # Wallet address (optional)
        chain_id=int(os.getenv("POLYMARKET_CHAIN_ID", "137")),  # Polygon mainnet (137) or Mumbai testnet (80001)
        api_base_url=os.getenv("POLYMARKET_API_BASE_URL", "https://clob.polymarket.com"),
        max_retries=int(os.getenv("EXECUTION_MAX_RETRIES", "3")),
        retry_delay_seconds=float(os.getenv("EXECUTION_RETRY_DELAY", "0.5")),
        request_timeout_seconds=int(os.getenv("EXECUTION_TIMEOUT", "10")),
        price_precision=int(os.getenv("EXECUTION_PRICE_PRECISION", "4")),
        size_precision=int(os.getenv("EXECUTION_SIZE_PRECISION", "2")),
    )


def load_manager_config() -> ManagerConfig:
    """Load manager config from environment variables."""
    return ManagerConfig(
        poll_interval_seconds=float(os.getenv("MANAGER_POLL_INTERVAL", "1.0")),
        max_total_exposure=float(os.getenv("MANAGER_MAX_EXPOSURE", "10000.0")),
        max_total_pnl_loss=float(os.getenv("MANAGER_MAX_PNL_LOSS", "-1000.0")),
        status_update_interval_seconds=float(os.getenv("MANAGER_STATUS_INTERVAL", "5.0")),
        supabase_sync_interval_seconds=float(os.getenv("MANAGER_SUPABASE_SYNC_INTERVAL", "30.0")),
        enable_emergency_shutdown=os.getenv("MANAGER_EMERGENCY_SHUTDOWN", "true").lower() == "true",
    )


def load_default_trader_configs() -> List[TraderConfig]:
    """Load default trader configurations from environment or return empty list.
    
    To configure traders via .env, use:
    TRADER_MARKETS=market1,market2,market3
    TRADER_BUDGETS=1000,2000,1500
    etc.
    
    Or configure them via Supabase.
    """
    markets_str = os.getenv("TRADER_MARKETS", "")
    if not markets_str:
        return []
    
    markets = [m.strip() for m in markets_str.split(",") if m.strip()]
    if not markets:
        return []
    
    # Parse other trader configs
    budgets_str = os.getenv("TRADER_BUDGETS", "")
    budgets = [float(b.strip()) for b in budgets_str.split(",")] if budgets_str else []
    
    min_gaps_str = os.getenv("TRADER_MIN_GAPS", "")
    min_gaps = [float(g.strip()) for g in min_gaps_str.split(",")] if min_gaps_str else []
    
    names_str = os.getenv("TRADER_NAMES", "")
    names = [n.strip() for n in names_str.split(",")] if names_str else []
    
    configs = []
    for idx, market in enumerate(markets):
        configs.append(TraderConfig(
            market_id=market,  # Will be resolved from slug if needed
            market_slug=market,  # Assume it's a slug initially
            name=names[idx] if idx < len(names) else "",
            budget=budgets[idx] if idx < len(budgets) else float(os.getenv("TRADER_DEFAULT_BUDGET", "1000.0")),
            min_gap=min_gaps[idx] if idx < len(min_gaps) else float(os.getenv("TRADER_DEFAULT_MIN_GAP", "0.01")),
            price_improvement=float(os.getenv("TRADER_DEFAULT_PRICE_IMPROVEMENT", "0.001")),
            order_timeout_seconds=int(os.getenv("TRADER_DEFAULT_TIMEOUT", "30")),
        ))
    
    return configs

