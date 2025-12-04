"""Supabase service for database operations.

This service handles all database operations including:
- Traders: Configuration and status management
- Fills: Trade execution tracking
- Logs: Trader log entries

It provides a clean interface for reading and writing data to Supabase.
"""

import logging
import asyncio
from typing import List, Optional, Dict, Any
from datetime import datetime

from config import TraderConfig
from trading.utils.slug_resolver import market_slug_resolver

logger = logging.getLogger(__name__)


class SupabaseService:
    """Service for managing trader data in Supabase."""
    
    def __init__(self, supabase_url: str, supabase_key: str):
        """Initialize Supabase service.
        
        Args:
            supabase_url: Supabase project URL
            supabase_key: Supabase anon/service key
        """
        self.url = supabase_url
        self.key = supabase_key
        self.client = None
        self.table_name = "traders"
        self._initialize_client()
    
    def _initialize_client(self) -> None:
        """Initialize Supabase client."""
        try:
            from supabase import create_client, Client
            self.client: Optional[Client] = create_client(self.url, self.key)
            logger.info("Supabase service initialized successfully")
        except ImportError:
            logger.warning("supabase-py not installed. SupabaseService will use mock mode.")
            self.client = None
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client: {e}")
            self.client = None
    
    def is_available(self) -> bool:
        """Check if Supabase is available.
        
        Returns:
            True if Supabase client is initialized, False otherwise
        """
        return self.client is not None
    
    # ============================================================================
    # Read Operations
    # ============================================================================
    
    async def load_all_traders(self, include_paused: bool = False) -> List[TraderConfig]:
        """Load all traders from Supabase.
        
        Resolves market_slug to market_id and token_id using the market resolver.
        
        Args:
            include_paused: If True, includes paused traders. If False, only active traders.
        
        Returns:
            List of TraderConfig objects
        """
        if not self.client:
            logger.warning("Supabase not available. Returning empty list.")
            return []
        
        try:
            query = self.client.table(self.table_name).select("*")
            if not include_paused:
                query = query.eq("status", "active")
            else:
                # Load active and paused, but not deleted
                query = query.in_("status", ["active", "paused"])
            
            response = query.execute()
            
            traders = []
            for row in response.data:
                try:
                    config = await self._row_to_config(row)
                    if config:
                        traders.append(config)
                except (KeyError, ValueError) as e:
                    logger.error(f"Failed to parse trader from DB: {e}, row: {row}")
                    continue
            
            logger.info(f"Loaded {len(traders)} traders from Supabase")
            return traders
            
        except Exception as e:
            logger.error(f"Failed to load traders from Supabase: {e}")
            return []
    
    def load_trader_by_slug(self, market_slug: str, include_paused: bool = False) -> Optional[TraderConfig]:
        """Load a single trader by market slug.
        
        Args:
            market_slug: Market slug to load
            include_paused: If True, includes paused traders. If False, only active traders.
            
        Returns:
            TraderConfig if found, None otherwise
        """
        if not self.client:
            return None
        
        try:
            query = (
                self.client.table(self.table_name)
                .select("*")
                .eq("market_slug", market_slug)
            )
            
            if not include_paused:
                query = query.eq("status", "active")
            else:
                # Load active and paused, but not deleted
                query = query.in_("status", ["active", "paused"])
            
            response = query.limit(1).execute()
            
            if response.data:
                return self._row_to_config(response.data[0])
            return None
            
        except Exception as e:
            logger.error(f"Failed to load trader {market_slug} from Supabase: {e}")
            return None
    
    def get_trader_status(self, market_slug: str) -> Optional[str]:
        """Get trader status from Supabase.
        
        Args:
            market_slug: Market slug to check
            
        Returns:
            Status string ('active', 'paused', 'deleted') or None if not found
        """
        if not self.client:
            return None
        
        try:
            response = (
                self.client.table(self.table_name)
                .select("status")
                .eq("market_slug", market_slug)
                .limit(1)
                .execute()
            )
            
            if response.data:
                return response.data[0].get("status", "active")
            return None
            
        except Exception as e:
            logger.error(f"Failed to get trader status from Supabase: {e}")
            return None
    
    # ============================================================================
    # Write Operations
    # ============================================================================
    
    def save_trader(self, config: TraderConfig) -> bool:
        """Save a trader configuration to Supabase (upsert).
        
        Uses market_slug as the identifier. Saves market_slug, min_gap, budget, price_improvement, and status.
        
        Args:
            config: TraderConfig to save
            
        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            logger.warning("Supabase not available. Skipping save.")
            return False
        
        if not config.market_slug:
            logger.error("Cannot save trader: market_slug is required")
            return False
        
        try:
            data = self._config_to_row(config)
            
            # Check if trader exists by market_slug
            existing = (
                self.client.table(self.table_name)
                .select("id")
                .eq("market_slug", config.market_slug)
                .execute()
            )
            
            if existing.data:
                # Update existing
                self.client.table(self.table_name).update(data).eq(
                    "market_slug", config.market_slug
                ).execute()
                logger.debug(f"Updated trader {config.market_slug} in Supabase")
            else:
                # Insert new
                data["created_at"] = datetime.utcnow().isoformat()
                self.client.table(self.table_name).insert(data).execute()
                logger.debug(f"Saved trader {config.market_slug} to Supabase")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to save trader to Supabase: {e}")
            return False
    
    def delete_trader(self, market_slug: str) -> bool:
        """Delete (soft delete) a trader from Supabase by setting status to 'deleted'.
        
        Args:
            market_slug: Market slug of trader to delete
            
        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            logger.warning("Supabase not available. Skipping delete.")
            return False
        
        try:
            self.client.table(self.table_name).update({
                "status": "deleted",
                "updated_at": datetime.utcnow().isoformat(),
            }).eq("market_slug", market_slug).execute()
            
            logger.info(f"Deleted trader {market_slug} from Supabase")
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete trader from Supabase: {e}")
            return False
    
    def update_trader_status(self, market_slug: str, status: str) -> bool:
        """Update status of a trader.
        
        Args:
            market_slug: Market slug of trader
            status: New status ('active', 'paused', 'deleted')
            
        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            return False
        
        if status not in ['active', 'paused', 'deleted']:
            logger.error(f"Invalid status: {status}. Must be 'active', 'paused', or 'deleted'")
            return False
        
        try:
            self.client.table(self.table_name).update({
                "status": status,
                "updated_at": datetime.utcnow().isoformat(),
            }).eq("market_slug", market_slug).execute()
            
            return True
        except Exception as e:
            logger.error(f"Failed to update trader status in Supabase: {e}")
            return False
    
    # ============================================================================
    # Fills Operations
    # ============================================================================
    
    def save_fill(
        self,
        trader_id: Optional[str],
        market_slug: str,
        side: str,
        price: float,
        size: float,
        order_id: str,
        pnl: Optional[float] = None,
    ) -> bool:
        """Save a fill (executed trade) to Supabase.
        
        Args:
            trader_id: UUID of the trader (from traders table), or None if not available
            market_slug: Market slug
            side: 'buy' or 'sell'
            price: Execution price
            size: Number of shares filled
            order_id: Order ID from exchange
            pnl: Optional realized profit/loss for this fill
            
        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            logger.warning("Supabase not available. Skipping fill save.")
            return False
        
        if side.upper() not in ['BUY', 'SELL']:
            logger.error(f"Invalid side: {side}. Must be 'BUY' or 'SELL'")
            return False
        
        try:
            data = {
                "trader_id": trader_id,
                "market_slug": market_slug,
                "side": side.lower(),  # Store as 'buy' or 'sell' per schema
                "price": float(price),
                "size": float(size),
                "order_id": order_id,
                "pnl": float(pnl) if pnl is not None else None,
                "created_at": datetime.utcnow().isoformat(),
            }
            
            self.client.table("fills").insert(data).execute()
            logger.debug(f"Saved fill to Supabase: {side} {size:.2f} @ {price:.4f} for {market_slug}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save fill to Supabase: {e}")
            return False
    
    def get_trader_id_by_slug(self, market_slug: str) -> Optional[str]:
        """Get trader UUID (id) by market_slug.
        
        Args:
            market_slug: Market slug to look up
            
        Returns:
            Trader UUID if found, None otherwise
        """
        if not self.client:
            return None
        
        try:
            response = (
                self.client.table(self.table_name)
                .select("id")
                .eq("market_slug", market_slug)
                .limit(1)
                .execute()
            )
            
            if response.data:
                return response.data[0].get("id")
            return None
            
        except Exception as e:
            logger.error(f"Failed to get trader_id for {market_slug}: {e}")
            return None
    
    # ============================================================================
    # Logs Operations
    # ============================================================================
    
    def save_log(
        self,
        trader_id: Optional[str],
        level: str,
        message: str,
    ) -> bool:
        """Save a log entry to Supabase.
        
        Args:
            trader_id: UUID of the trader (from traders table), or None if not available
            level: Log level ('info', 'warning', 'error', 'debug')
            message: Log message
            
        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            # Don't log warning for logs - it would create infinite loop
            return False
        
        if level.lower() not in ['info', 'warning', 'error', 'debug']:
            logger.error(f"Invalid log level: {level}. Must be 'info', 'warning', 'error', or 'debug'")
            return False
        
        try:
            data = {
                "trader_id": trader_id,
                "level": level.lower(),
                "message": str(message),
                "created_at": datetime.utcnow().isoformat(),
            }
            
            self.client.table("logs").insert(data).execute()
            return True
            
        except Exception as e:
            # Don't log error here - it would create infinite loop
            # Just silently fail
            return False
    
    # ============================================================================
    # Helper Methods
    # ============================================================================
    
    def _config_to_row(self, config: TraderConfig) -> Dict[str, Any]:
        """Convert TraderConfig to database row.
        
        Saves fields: market_slug, min_gap, budget, price_improvement, status.
        
        Args:
            config: TraderConfig to convert
            
        Returns:
            Dictionary representing database row
        """
        # Determine status from is_paused (if TraderConfig has it) or default to 'active'
        status = "paused" if getattr(config, 'is_paused', False) else "active"
        
        return {
            "market_slug": config.market_slug or "",
            "budget": config.budget,
            "min_gap": config.min_gap,
            "price_improvement": config.price_improvement,
            "status": status,
            "updated_at": datetime.utcnow().isoformat(),
        }
    
    async def _row_to_config(self, row: Dict[str, Any]) -> Optional[TraderConfig]:
        """Convert database row to TraderConfig.
        
        Resolves market_slug to market_id and token_id using the market resolver.
        
        Args:
            row: Database row dictionary
            
        Returns:
            TraderConfig object, or None if market resolution fails
        """
        market_slug = row.get("market_slug", "")
        if not market_slug:
            logger.error("Cannot load trader: market_slug is missing")
            return None
        
        # Resolve market_slug to market_id and token_id
        try:
            market_info = await market_slug_resolver(market_slug)
        except Exception as e:
            logger.error(f"Failed to resolve market_slug '{market_slug}': {e}")
            return None
        
        if not market_info:
            logger.error(f"Failed to resolve market_slug '{market_slug}' to market_id/token_id")
            return None
        
        # Extract market_id and token_id from resolved info
        if isinstance(market_info, dict):
            market_id = market_info.get("condition_id", "")
            token_id = market_info.get("yes_token_id", "")
        else:
            # Backward compatibility: if it returns a string (old behavior)
            market_id = market_info
            token_id = ""
        
        if not market_id:
            logger.error(f"Failed to get condition_id for market_slug '{market_slug}'")
            return None
        
        # Determine is_paused from status
        status = row.get("status", "active")
        is_paused = (status == "paused")
        
        return TraderConfig(
            market_id=market_id,
            token_id=token_id,
            market_slug=market_slug,
            name=market_slug,  # Use market_slug as name if not provided
            budget=float(row["budget"]),
            min_gap=float(row["min_gap"]),
            # Load price_improvement from database, default to 1.0 cent if not present
            price_improvement=float(row.get("price_improvement", 1.0)),
            max_retries=3,
            retry_delay_seconds=1.0,
        )
