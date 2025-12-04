"""Trader manager for coordinating multiple trader agents."""

import asyncio
import logging
import time
from typing import Dict, Optional, Any
from datetime import datetime

from .trader import Trader
from .execution import ExecutionLayer
from config import TraderConfig, ManagerConfig


logger = logging.getLogger(__name__)


class TraderManager:
    """Manages multiple trader agents and monitors global risk."""
    
    def __init__(
        self,
        execution_layer: ExecutionLayer,
        config: ManagerConfig,
        supabase_service: Optional[Any] = None,  # SupabaseService, avoiding circular import
    ):
        """Initialize manager with execution layer and config.
        
        Args:
            execution_layer: Execution layer for order operations
            config: Manager configuration
            supabase_service: Optional Supabase service for trader persistence
        """
        self.execution = execution_layer
        self.config = config
        self.traders: Dict[str, Trader] = {}
        self.is_running = False
        self.start_time: Optional[float] = None
        self.last_status_update: float = 0.0
        self.last_supabase_sync: float = 0.0
        self.supabase_service = supabase_service
        
        logger.info("TraderManager initialized")
    
    def _sync_to_supabase(self, operation) -> None:
        """Helper to sync operations to Supabase without blocking.
        
        Args:
            operation: Callable that performs the Supabase operation
        """
        try:
            # Run in background thread to avoid blocking
            import threading
            thread = threading.Thread(target=operation, daemon=True)
            thread.start()
        except Exception as e:
            logger.warning(f"Failed to sync to Supabase (non-critical): {e}")
    
    def add_trader(self, config: TraderConfig) -> Trader:
        """Add a trader to the manager.
        
        Traders are loaded from Supabase on startup. This method creates the trader
        instance locally for trading operations.
        
        Args:
            config: Trader configuration (loaded from Supabase)
            
        Returns:
            Created Trader instance
        """
        if config.market_id in self.traders:
            logger.warning(f"Trader for market {config.market_id} already exists")
            return self.traders[config.market_id]
        
        # Create trader instance
        trader = Trader(
            market_id=config.market_id,
            config=config,
            execution_layer=self.execution,
            supabase_service=self.supabase_service,
        )
        
        self.traders[config.market_id] = trader
        logger.info(f"Added trader for market {config.market_id} (slug: {config.market_slug})")
        
        return trader
    
    def remove_trader(self, market_id: str) -> bool:
        """Remove a trader from the manager.
        
        Removes trader locally. To permanently delete, update status in Supabase to 'deleted'.
        
        Args:
            market_id: Market ID of trader to remove
            
        Returns:
            True if successful, False otherwise
        """
        if market_id not in self.traders:
            return False
        
        # Remove locally
        trader = self.traders[market_id]
        trader.stop()
        del self.traders[market_id]
        logger.info(f"Removed trader for market {market_id}")
        
        return True
    
    async def _sync_traders_from_supabase(self) -> None:
        """Sync traders from Supabase to detect changes made by frontend.
        
        This method:
        - Adds new traders (in DB but not locally)
        - Removes deleted traders (status='deleted' or missing from DB)
        - Updates pause/resume status for existing traders
        """
        if not self.supabase_service or not self.supabase_service.is_available():
            return
        
        try:
            # Load all traders (active and paused, but not deleted) from Supabase
            db_traders = await self.supabase_service.load_all_traders(include_paused=True)
            
            # Create a map of market_id -> config for quick lookup
            db_traders_map = {config.market_id: config for config in db_traders}
            db_market_ids = set(db_traders_map.keys())
            local_market_ids = set(self.traders.keys())
            
            # 1. Add new traders (in DB but not locally)
            for config in db_traders:
                if config.market_id not in local_market_ids:
                    logger.info(f"Detected new trader from Supabase: {config.market_slug} (market_id: {config.market_id[:20]}...)")
                    self.add_trader(config)
            
            # 2. Remove deleted traders (in local but not in DB, or status='deleted')
            traders_to_remove = []
            for market_id, trader in self.traders.items():
                if market_id not in db_market_ids:
                    # Trader exists locally but not in DB - mark for removal
                    traders_to_remove.append(market_id)
                else:
                    # Check if status is 'deleted' by querying directly
                    status = self._get_trader_status_from_db(trader.config.market_slug or "")
                    if status == "deleted":
                        traders_to_remove.append(market_id)
            
            for market_id in traders_to_remove:
                logger.info(f"Detected deleted trader from Supabase: {market_id[:20]}... - removing")
                self.remove_trader(market_id)
            
            # 3. Update pause/resume status for existing traders
            for market_id, trader in self.traders.items():
                if market_id in db_traders_map:
                    config = db_traders_map[market_id]
                    status = self._get_trader_status_from_db(config.market_slug or "")
                    
                    if status == "paused" and not trader.is_paused:
                        logger.info(f"Trader {config.market_slug} was paused in Supabase - pausing locally")
                        trader.pause()
                    elif status == "active" and trader.is_paused:
                        logger.info(f"Trader {config.market_slug} was resumed in Supabase - resuming locally")
                        trader.resume()
        
        except Exception as e:
            logger.error(f"Failed to sync traders from Supabase: {e}")
    
    def _get_trader_status_from_db(self, market_slug: str) -> Optional[str]:
        """Get trader status from Supabase.
        
        Args:
            market_slug: Market slug to check
            
        Returns:
            Status string ('active', 'paused', 'deleted') or None if not found
        """
        if not self.supabase_service or not self.supabase_service.is_available():
            return None
        
        return self.supabase_service.get_trader_status(market_slug)
    
    def pause_trader(self, market_id: str) -> bool:
        """Pause a specific trader.
        
        Updates local state only. Status changes are managed by the frontend in Supabase.
        """
        if market_id not in self.traders:
            return False
        
        trader = self.traders[market_id]
        trader.pause()
        
        return True
    
    def resume_trader(self, market_id: str) -> bool:
        """Resume a specific trader.
        
        Updates local state only. Status changes are managed by the frontend in Supabase.
        """
        if market_id not in self.traders:
            return False
        
        trader = self.traders[market_id]
        trader.resume()
        
        return True
    
    def pause_all(self) -> None:
        """Pause all traders."""
        for trader in self.traders.values():
            trader.pause()
        logger.info("All traders paused")
    
    def resume_all(self) -> None:
        """Resume all traders."""
        for trader in self.traders.values():
            trader.resume()
        logger.info("All traders resumed")
    
    async def _monitor_risk(self) -> bool:
        """Monitor global risk and return True if safe to continue."""
        try:
            # Calculate total exposure
            total_exposure = 0.0
            total_pnl = 0.0
            
            for trader in self.traders.values():
                status = trader.get_status()
                total_exposure += abs(status["position"])
                total_pnl += status["total_pnl"]
            
            # Check exposure limit
            if total_exposure > self.config.max_total_exposure:
                logger.warning(
                    f"Total exposure {total_exposure:.2f} exceeds limit "
                    f"{self.config.max_total_exposure:.2f}"
                )
                if self.config.enable_emergency_shutdown:
                    return False
            
            # Check P&L loss limit
            if total_pnl < self.config.max_total_pnl_loss:
                logger.error(
                    f"Total P&L {total_pnl:.2f} below loss threshold "
                    f"{self.config.max_total_pnl_loss:.2f}"
                )
                if self.config.enable_emergency_shutdown:
                    return False
            
            return True
            
        except Exception as e:
            logger.error(f"Risk monitoring error: {e}")
            return True  # Continue on error, but log it
    
    async def _print_status(self) -> None:
        """Print status update for all traders."""
        current_time = time.time()
        
        if current_time - self.last_status_update < self.config.status_update_interval_seconds:
            return
        
        self.last_status_update = current_time
        
        # Build status string for both console and log file
        status_lines = []
        status_lines.append("\n" + "=" * 80)
        status_lines.append(f"Trader Manager Status - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        status_lines.append("=" * 80)
        
        if self.start_time:
            uptime = current_time - self.start_time
            # Count only non-paused traders
            active_count = sum(1 for t in self.traders.values() if not t.is_paused and t.is_active)
            paused_count = sum(1 for t in self.traders.values() if t.is_paused)
            status_lines.append(f"Uptime: {uptime:.0f}s | Total Traders: {len(self.traders)} (Active: {active_count}, Paused: {paused_count})")
        
        total_exposure_shares = 0.0
        total_exposure_dollars = 0.0
        total_pnl = 0.0
        total_trades = 0
        
        for trader in self.traders.values():
            status = trader.get_status()
            total_exposure_shares += abs(status["position"])
            total_exposure_dollars += status.get("position_value", 0.0)
            total_pnl += status["total_pnl"]
            total_trades += status["total_trades"]
            
            # Display spread in both cents and percentage
            if status['spread_cents'] is not None and status['spread_pct'] is not None:
                spread_str = f"{status['spread_cents']:.2f}¢ ({status['spread_pct']:.2f}%)"
            elif status['spread_pct'] is not None:
                spread_str = f"{status['spread_pct']:.2f}%"
            elif status['spread_cents'] is not None:
                spread_str = f"{status['spread_cents']:.2f}¢"
            else:
                spread_str = "N/A"
            
            # Format best bid/ask
            best_bid_str = f"{status['best_bid']:.4f}" if status['best_bid'] is not None else "N/A"
            best_ask_str = f"{status['best_ask']:.4f}" if status['best_ask'] is not None else "N/A"
            
            # Format order details
            order_details_str = "None"
            if status['order_details']:
                order_parts = []
                for order in status['order_details']:
                    size = order.get('size', 0.0)
                    if size > 0:
                        order_parts.append(f"{order['side']} {size:.2f}@{order['price']:.4f}")
                    else:
                        order_parts.append(f"{order['side']}@{order['price']:.4f}")
                order_details_str = ", ".join(order_parts)
            
            # Main trader line
            status_lines.append(
                f"  Trader: {status['name'][:25]:<25} | "
                f"Market: {status['market_slug'][:30]:<30}"
            )
            # Get budget info
            budget = status.get('budget', 0.0)
            capital_used = status.get('capital_used', 0.0)
            available_budget = status.get('available_budget', 0.0)
            
            # Calculate position display (shares and dollars)
            position_shares = status['position']
            position_value = status.get('position_value', 0.0)
            position_display = f"{position_shares:>8.2f} shares (${position_value:>7.2f})"
            
            status_lines.append(
                f"    Status: {'⏸️ PAUSED' if status['is_paused'] else '✅ ACTIVE'} | "
                f"Position: {position_display} | "
                f"P&L: ${status['total_pnl']:>7.2f} | "
                f"Trades: {status['total_trades']:>4}"
            )
            status_lines.append(
                f"    Budget: ${budget:>7.2f} | "
                f"Used: ${capital_used:>7.2f} | "
                f"Available: ${available_budget:>7.2f}"
            )
            # Show prices in both decimal and percentage format
            best_bid_pct = f"({float(best_bid_str)*100:.2f}%)" if best_bid_str != "N/A" else ""
            best_ask_pct = f"({float(best_ask_str)*100:.2f}%)" if best_ask_str != "N/A" else ""
            
            status_lines.append(
                f"    Best Bid: {best_bid_str:>10} {best_bid_pct:>8} | "
                f"Best Ask: {best_ask_str:>10} {best_ask_pct:>8} | "
                f"Spread: {spread_str:>6}"
            )
            status_lines.append(
                f"    Active Orders ({status['active_orders']}): {order_details_str}"
            )
            
            # Display decision information
            if status.get('min_order_size') is not None:
                min_order_size = status.get('min_order_size', 0)
                min_order_value_buy = status.get('min_order_value_buy')
                min_order_value_sell = status.get('min_order_value_sell')
                min_gap_cents = status.get('min_gap_cents', 0)
                
                decision_lines = []
                decision_lines.append(f"    Market Requirements:")
                decision_lines.append(f"      Min gap: {min_gap_cents:.2f}¢ | Min order: {min_order_size:.0f} shares")
                if min_order_value_buy is not None:
                    decision_lines.append(f"      BUY minimum: ${min_order_value_buy:.2f} | SELL minimum: ${min_order_value_sell:.2f}")
                
                # Determine why orders are/aren't placed
                buy_reason = []
                sell_reason = []
                
                # BUY order reasoning
                if status.get('active_buy_order_id'):
                    buy_reason.append("✅ BUY order active")
                else:
                    if status.get('spread_cents', 0) < min_gap_cents:
                        buy_reason.append(f"❌ Spread {status.get('spread_cents', 0):.2f}¢ < min gap {min_gap_cents:.2f}¢")
                    if available_budget < (min_order_value_buy or 0):
                        buy_reason.append(f"❌ Budget ${available_budget:.2f} < BUY min ${min_order_value_buy:.2f}")
                    if not buy_reason:
                        buy_reason.append("⏳ Evaluating...")
                
                # SELL order reasoning
                if status.get('active_sell_order_id'):
                    sell_reason.append("✅ SELL order active")
                else:
                    if position_shares <= 0:
                        sell_reason.append("❌ No position to sell")
                    elif position_shares < min_order_size:
                        sell_reason.append(f"❌ Position {position_shares:.2f} < min {min_order_size:.0f} shares")
                    if not sell_reason:
                        sell_reason.append("⏳ Evaluating...")
                
                decision_lines.append(f"      BUY: {' | '.join(buy_reason)}")
                decision_lines.append(f"      SELL: {' | '.join(sell_reason)}")
                
                for line in decision_lines:
                    status_lines.append(line)
            
            status_lines.append("")  # Empty line for readability
        
        status_lines.append("-" * 80)
        status_lines.append(
            f"Total Exposure: {total_exposure_shares:.2f} shares (${total_exposure_dollars:.2f}) | "
            f"Total P&L: ${total_pnl:.2f} | "
            f"Total Trades: {total_trades}"
        )
        status_lines.append("=" * 80 + "\n")
        
        # Print to console and log to file
        status_text = "\n".join(status_lines)
        print(status_text)
        logger.info(f"\n{status_text}")
    
    async def run(self) -> None:
        """Main event loop for the manager."""
        self.is_running = True
        self.start_time = time.time()
        
        logger.info(f"Starting TraderManager with {len(self.traders)} traders")
        
        try:
            while self.is_running:
                # Check risk limits
                if not await self._monitor_risk():
                    logger.error("Risk limits exceeded. Shutting down.")
                    break
                
                # Sync traders from Supabase periodically to detect frontend changes
                current_time = time.time()
                if current_time - self.last_supabase_sync >= self.config.supabase_sync_interval_seconds:
                    await self._sync_traders_from_supabase()
                    self.last_supabase_sync = current_time
                
                # Run all trader steps in parallel
                tasks = [trader.step() for trader in self.traders.values() if trader.is_active]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                
                # Print status periodically
                await self._print_status()
                
                # Sleep before next iteration
                await asyncio.sleep(self.config.poll_interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt. Shutting down...")
        except Exception as e:
            logger.error(f"Manager error: {e}", exc_info=True)
        finally:
            await self.shutdown()
    
    async def shutdown(self) -> None:
        """Gracefully shutdown all traders."""
        logger.info("Shutting down TraderManager...")
        self.is_running = False
        
        # Stop all traders
        for trader in self.traders.values():
            trader.stop()
        
        # Cancel all active orders
        logger.info("Cancelling all active orders...")
        for trader in self.traders.values():
            for order_id in list(trader.state.active_order_ids):
                try:
                    await trader.execution.cancel(order_id)
                except Exception as e:
                    logger.error(f"Failed to cancel order {order_id}: {e}")
        
        logger.info("TraderManager shutdown complete")
    
    def stop(self) -> None:
        """Stop the manager (non-blocking)."""
        self.is_running = False

