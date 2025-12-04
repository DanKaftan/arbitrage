"""Market-making trader for a single market."""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, Any

from .execution import ExecutionLayer, ExecutionError
from config import TraderConfig

logger = logging.getLogger(__name__)

PRICE_UPDATE_THRESHOLD = 0.0001
MIN_ORDER_SIZE = 5.0  # Polymarket minimum order size in shares


@dataclass
class TraderState:
    """Trader state tracking.
    
    Tracks the current state of the trader including:
    - Position: Current position in shares (positive = long, negative = short)
    - Market data: Last seen bid/ask prices and spread
    - Active orders: We can have both a BUY and SELL order simultaneously (two-sided market making)
    """
    current_position: float = 0.0  # Position in shares (0 = flat, >0 = long, <0 = short)
    total_pnl: float = 0.0  # Cumulative profit/loss
    total_trades: int = 0  # Number of completed trades
    last_best_bid: Optional[float] = None  # Last seen best bid price (highest buy offer)
    last_best_ask: Optional[float] = None  # Last seen best ask price (lowest sell offer)
    last_spread: Optional[float] = None  # Last calculated spread (ask - bid)
    min_order_size: Optional[float] = None  # Market-specific minimum order size (from orderbook)
    # Active order tracking (can have both BUY and SELL orders simultaneously)
    active_buy_order_id: Optional[str] = None  # ID of active BUY order (if any)
    active_buy_order_price: Optional[float] = None  # Price of active BUY order
    active_buy_order_size: Optional[float] = None  # Size of active BUY order (if any)
    active_sell_order_id: Optional[str] = None  # ID of active SELL order (if any)
    active_sell_order_price: Optional[float] = None  # Price of active SELL order
    active_sell_order_size: Optional[float] = None  # Size of active SELL order (if any)


class Trader:
    """Market-making trader for a single market."""
    
    def __init__(
        self,
        market_id: str,
        config: TraderConfig,
        execution_layer: ExecutionLayer,
        supabase_service: Optional[Any] = None,  # SupabaseService, avoiding circular import
    ):
        self.market_id = market_id
        self.token_id = config.token_id or ""
        self.config = config
        self.execution = execution_layer
        self.supabase_service = supabase_service
        self.state = TraderState()
        self.is_active = True
        self.is_paused = False
        self._trader_id: Optional[str] = None  # Cached trader UUID from DB
        self._avg_cost_basis: Optional[float] = None  # Average cost per share for PnL calculation
        
        if not config.name:
            config.name = f"Trader-{market_id[:8]}"
        
        if not self.token_id:
            logger.warning(
                f"Trader '{config.name}' missing token_id - API calls will fail"
            )
        
        logger.info(
            f"Trader '{config.name}' initialized "
            f"(token_id={'SET' if self.token_id else 'MISSING'}, "
            f"budget={config.budget}, min_gap={config.min_gap})"
        )
        
        # Sync position from API on startup and periodically
        # This ensures we have the correct position even after a restart or manual trades
        self._position_synced = False
        self._position_sync_counter = 0
        self._position_last_sync_time = 0.0
        self._position_sync_interval = 5  # Re-sync every 5 steps (more frequent)
        
        # Store last decision info for status display
        self._last_decision_info = None
        self._position_sync_time_interval = 30.0  # Or every 30 seconds, whichever comes first
    
    async def step(self) -> None:
        """Execute one trading step.
        
        Main trading loop that runs periodically. Flow:
        1. Check status of existing orders (clean up filled/cancelled)
        2. Fetch latest market data (orderbook)
        3. Update internal state with new prices
        4. Decide on action:
           - If we have active orders: update them to stay competitive
           - If flat (no position): try to enter if spread is good
           - If we have a position: try to exit
        """
        if not self.is_active or self.is_paused:
            return
        
        try:
            # Step 1: Clean up orders that were filled or cancelled
            # This ensures our state matches reality on the exchange
            await self._check_order_status()
            
            # Step 2: Get current market prices
            orderbook = await self._fetch_orderbook()
            if not orderbook:
                return
            
            best_bid, best_ask, second_best_bid, second_best_ask, best_bid_size, best_ask_size = self._extract_best_prices(orderbook)
            if best_bid is None or best_ask is None:
                logger.debug(f"Trader {self.market_id}: Skipping step - missing bid/ask prices")
                return
            
            # Step 3: Update our view of the market
            min_order_size = orderbook.get("min_order_size")
            self._update_market_state(best_bid, best_ask, min_order_size)
            await self._update_position()
            
            # Step 4: Trading decision logic
            # We can have both BUY and SELL orders simultaneously (two-sided market making)
            # - Update existing orders to stay competitive
            # - Check and cancel invalid orders (conditions changed)
            # - Place missing orders if conditions are met
            await self._update_orders(best_bid, best_ask, second_best_bid, second_best_ask, best_bid_size, best_ask_size)
            
            # Log current state for decision making
            position_value = abs(self.state.current_position * (self.state.last_best_bid or best_bid))
            capital_used = position_value
            available_budget = self.config.budget - capital_used
            min_order_size = self.state.min_order_size or MIN_ORDER_SIZE
            
            # Calculate minimum order value in dollars
            min_order_value_buy = min_order_size * best_bid if best_bid else None
            min_order_value_sell = min_order_size * best_ask if best_ask else None
            
            # Store decision info for status display
            self._last_decision_info = {
                "min_order_size": min_order_size,
                "min_order_value_buy": min_order_value_buy,
                "min_order_value_sell": min_order_value_sell,
                "min_gap_cents": self.config.min_gap,
            }
            
            # Check and cancel invalid orders before placing new ones
            await self._check_and_cancel_invalid_orders(best_bid, best_ask)
            
            # Place orders if we don't have them yet
            if not self.state.active_buy_order_id:
                await self._try_place_buy_order(best_bid, best_ask)
            else:
                logger.info(
                    f"Trader {self.market_id}: BUY order already active (ID: {self.state.active_buy_order_id[:20]}...), skipping placement"
                )
            
            if not self.state.active_sell_order_id:
                await self._try_place_sell_order(best_bid, best_ask)
            else:
                logger.info(
                    f"Trader {self.market_id}: SELL order already active (ID: {self.state.active_sell_order_id[:20]}...), skipping placement"
                )
                
        except ExecutionError as e:
            logger.error(f"Trader {self.market_id} execution error: {e}")
        except Exception as e:
            logger.error(f"Trader {self.market_id} error: {e}", exc_info=True)
    
    def pause(self) -> None:
        self.is_paused = True
        logger.info(f"Trader {self.market_id} paused")
    
    def resume(self) -> None:
        self.is_paused = False
        logger.info(f"Trader {self.market_id} resumed")
    
    def stop(self) -> None:
        self.is_active = False
        logger.info(f"Trader {self.market_id} stopped")
    
    def get_status(self) -> Dict:
        """Get current trader status."""
        spread_pct = None
        spread_cents = None
        if self.state.last_best_bid and self.state.last_best_bid > 0 and self.state.last_spread:
            spread_pct = (self.state.last_spread / self.state.last_best_bid) * 100
            spread_cents = self.state.last_spread * 100  # Convert decimal to cents
        
        # Calculate budget information
        position_value = abs(self.state.current_position * (self.state.last_best_bid or 0.0))
        capital_used = position_value
        available_budget = self.config.budget - capital_used
        
        order_details = []
        if self.state.active_buy_order_id:
            order_details.append({
                "id": self.state.active_buy_order_id,
                "side": "BUY",
                "price": self.state.active_buy_order_price or 0.0,
                "size": self.state.active_buy_order_size or 0.0,
            })
        if self.state.active_sell_order_id:
            order_details.append({
                "id": self.state.active_sell_order_id,
                "side": "SELL",
                "price": self.state.active_sell_order_price or 0.0,
                "size": self.state.active_sell_order_size or 0.0,
            })
        
        # Include decision info if available
        decision_info = self._last_decision_info or {}
        
        return {
            "name": self.config.name,
            "market_id": self.market_id,
            "market_slug": self.config.market_slug or self.market_id[:20] + "...",
            "position": self.state.current_position,  # Position in shares
            "position_value": position_value,  # Position value in dollars
            "active_orders": (1 if self.state.active_buy_order_id else 0) + (1 if self.state.active_sell_order_id else 0),
            "order_details": order_details,
            "best_bid": self.state.last_best_bid,
            "best_ask": self.state.last_best_ask,
            "spread": self.state.last_spread,
            "spread_pct": spread_pct,
            "spread_cents": spread_cents,  # Spread in cents (absolute value)
            "total_pnl": self.state.total_pnl,
            "total_trades": self.state.total_trades,
            "is_paused": self.is_paused,
            "is_active": self.is_active,
            "budget": self.config.budget,
            "capital_used": capital_used,
            "available_budget": available_budget,
            # Decision info
            "min_order_size": decision_info.get("min_order_size"),
            "min_order_value_buy": decision_info.get("min_order_value_buy"),
            "min_order_value_sell": decision_info.get("min_order_value_sell"),
            "min_gap_cents": decision_info.get("min_gap_cents"),
        }
    
    def _update_market_state(self, best_bid: float, best_ask: float, min_order_size: Optional[float] = None) -> None:
        """Update market state from current prices.
        
        Stores the latest market prices and calculates spread.
        Spread = ask - bid (the gap we can potentially profit from).
        Also updates market-specific minimum order size if provided.
        """
        self.state.last_best_bid = best_bid
        self.state.last_best_ask = best_ask
        self.state.last_spread = best_ask - best_bid  # Profit opportunity
        if min_order_size is not None:
            self.state.min_order_size = min_order_size
    
    async def _fetch_orderbook(self) -> Optional[Dict]:
        """Fetch orderbook."""
        if not self.token_id:
            logger.error(f"Trader {self.market_id} cannot fetch orderbook: token_id missing")
            return None
        
        try:
            return await self.execution.get_orderbook(self.token_id)
        except Exception as e:
            logger.warning(f"Trader {self.market_id} failed to fetch orderbook: {e}")
            return None
    
    def _extract_best_prices(self, orderbook: Dict) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
        """Extract best bid/ask and second-best bid/ask from orderbook, along with sizes.
        
        Best Bid = Highest price someone will BUY at (we can sell to them)
        Best Ask = Lowest price someone will SELL at (we can buy from them)
        Second-best bid/ask = Next best price after the best (helps detect gaps)
        
        Polymarket API returns:
        - Bids sorted lowest->highest (best bid = last element = highest price)
        - Asks sorted highest->lowest (best ask = last element = lowest price)
        
        Returns: (best_bid, best_ask, second_best_bid, second_best_ask, best_bid_size, best_ask_size)
        """
        try:
            bids = orderbook.get("bids", [])
            asks = orderbook.get("asks", [])
            # Best bid = highest buy offer = last element (sorted low->high)
            best_bid = float(bids[-1]["price"]) if bids else None
            best_bid_size = float(bids[-1].get("size", 0)) if bids else None
            # Second-best bid = second-to-last element (if exists)
            second_best_bid = float(bids[-2]["price"]) if len(bids) >= 2 else None
            # Best ask = lowest sell offer = last element (sorted high->low)
            best_ask = float(asks[-1]["price"]) if asks else None
            best_ask_size = float(asks[-1].get("size", 0)) if asks else None
            # Second-best ask = second-to-last element (if exists)
            second_best_ask = float(asks[-2]["price"]) if len(asks) >= 2 else None
            return best_bid, best_ask, second_best_bid, second_best_ask, best_bid_size, best_ask_size
        except (KeyError, IndexError, ValueError) as e:
            logger.warning(f"Trader {self.market_id} failed to parse orderbook: {e}")
            return None, None, None, None, None, None
    
    async def _update_position(self) -> None:
        """Update position from execution layer.
        
        Syncs position from API:
        - On first call after startup
        - Periodically (every N steps or N seconds) to catch manual trades
        - Otherwise relies on self-tracking via order fills for performance
        """
        import time
        current_time = time.time()
        
        try:
            # Determine if we should sync from API
            should_sync = False
            time_since_sync = current_time - self._position_last_sync_time
            
            if not self._position_synced:
                # First sync on startup
                should_sync = True
            elif self._position_sync_counter >= self._position_sync_interval:
                # Periodic sync every N steps
                should_sync = True
            elif time_since_sync >= self._position_sync_time_interval:
                # Periodic sync every N seconds
                should_sync = True
            
            if should_sync:
                api_position = await self.execution.get_market_position(self.token_id)
                
                # Check if position has changed (any difference, not just > 0.1)
                position_diff = abs(api_position - self.state.current_position)
                old_position = self.state.current_position
                
                # Always update position from API when syncing
                was_first_sync = not self._position_synced
                self.state.current_position = api_position
                self._position_synced = True
                self._position_sync_counter = 0
                self._position_last_sync_time = current_time
                
                # Log sync - always log if position changed significantly or on first sync
                if position_diff > 0.01:  # Changed by more than 0.01 shares
                    logger.info(
                        f"Trader {self.market_id}: Position synced from API: {api_position:.2f} shares "
                        f"(was {old_position:.2f}, diff: {position_diff:.2f})"
                    )
                elif was_first_sync or old_position == 0.0:
                    logger.info(
                        f"Trader {self.market_id}: Initial position sync from API: {api_position:.2f} shares"
                    )
                else:
                    logger.debug(
                        f"Trader {self.market_id}: Periodic position sync from API: {api_position:.2f} shares"
                    )
            else:
                # Increment counter for periodic sync
                self._position_sync_counter += 1
                # After initial sync, we rely on self-tracking (updated when orders fill)
                # This is more efficient than querying API every iteration
                
        except Exception as e:
            logger.warning(f"Trader {self.market_id} failed to update position: {e}")
            # If API sync fails, continue with self-tracking
            self._position_synced = True
    
    async def _check_and_cancel_invalid_orders(self, best_bid: float, best_ask: float) -> None:
        """Check if active orders should be cancelled because conditions changed.
        
        This ensures we cancel orders that are no longer valid:
        - BUY orders when spread < min_gap or budget insufficient
        - SELL orders when position is too small or doesn't exist
        """
        # Calculate current spread from current market prices
        current_spread = best_ask - best_bid if best_bid and best_ask else None
        
        # Check BUY order
        if self.state.active_buy_order_id:
            should_cancel_buy = False
            reason = ""
            
            # Check 1: Spread too small (use current spread, not last_spread)
            # Convert min_gap from cents to decimal for comparison (spread is in decimal)
            min_gap_decimal = self.config.min_gap / 100.0
            if current_spread is None or current_spread < min_gap_decimal:
                should_cancel_buy = True
                spread_cents = current_spread * 100 if current_spread else 0
                reason = f"Spread {spread_cents:.2f}¢ < min gap {self.config.min_gap:.2f}¢"
            
            # Check 2: Budget insufficient
            if not should_cancel_buy:
                position_value = abs(self.state.current_position * (self.state.last_best_bid or best_bid))
                capital_used = position_value
                available_budget = self.config.budget - capital_used
                min_order_size = self.state.min_order_size or MIN_ORDER_SIZE
                min_order_value_buy = min_order_size * best_bid if best_bid else 0
                
                if available_budget < min_order_value_buy:
                    should_cancel_buy = True
                    reason = f"Budget ${available_budget:.2f} < BUY min ${min_order_value_buy:.2f}"
            
            if should_cancel_buy:
                logger.info(
                    f"Trader {self.market_id}: Cancelling BUY order {self.state.active_buy_order_id[:20]}... - {reason}"
                )
                try:
                    await self.execution.cancel(self.state.active_buy_order_id)
                    self._unregister_order("BUY")
                    logger.info(f"Trader {self.market_id}: ✅ Successfully cancelled BUY order")
                except Exception as e:
                    logger.error(f"Trader {self.market_id}: Failed to cancel BUY order: {e}")
        
        # Check SELL order
        if self.state.active_sell_order_id:
            should_cancel_sell = False
            reason = ""
            
            # Check 1: Position too small or doesn't exist
            min_order_size = self.state.min_order_size or MIN_ORDER_SIZE
            safety_buffer = 0.1
            available_to_sell = max(0, self.state.current_position - safety_buffer)
            
            if self.state.current_position <= 0.0:
                should_cancel_sell = True
                reason = f"Position {self.state.current_position:.2f} <= 0 shares"
            elif available_to_sell < min_order_size:
                should_cancel_sell = True
                reason = f"Position {self.state.current_position:.2f} < min {min_order_size:.0f} shares (after buffer)"
            
            if should_cancel_sell:
                logger.info(
                    f"Trader {self.market_id}: Cancelling SELL order {self.state.active_sell_order_id[:20]}... - {reason}"
                )
                try:
                    await self.execution.cancel(self.state.active_sell_order_id)
                    self._unregister_order("SELL")
                    logger.info(f"Trader {self.market_id}: ✅ Successfully cancelled SELL order")
                except Exception as e:
                    logger.error(f"Trader {self.market_id}: Failed to cancel SELL order: {e}")
    
    async def _try_place_buy_order(self, best_bid: float, best_ask: float) -> None:
        """Try to place a BUY order if conditions are met.
        
        BUY order logic:
        1. Check if spread is wide enough (>= min_gap)
        2. Check if we have available budget
        3. Calculate order size based on available capital
        4. Place buy order slightly above best bid (price_improvement)
        
        Strategy: Buy at best_bid + price_improvement, hoping to sell later at best_ask - price_improvement
        to capture the spread.
        """
        logger.info(f"Trader {self.market_id}: Evaluating BUY order placement...")
        
        # Check 1: Spread must be wide enough to be profitable
        # Convert min_gap from cents to decimal for comparison (last_spread is in decimal)
        min_gap_decimal = self.config.min_gap / 100.0
        if not self.state.last_spread or self.state.last_spread < min_gap_decimal:
            logger.info(
                f"Trader {self.market_id}: ❌ Skipping BUY - spread {self.state.last_spread*100:.2f}¢ ({self.state.last_spread/self.state.last_best_bid*100:.2f}%) < min_gap {self.config.min_gap:.2f}¢"
            )
            return
        logger.info(
            f"Trader {self.market_id}: ✅ Spread check passed: {self.state.last_spread*100:.2f}¢ >= {self.config.min_gap:.2f}¢"
        )
        
        # Check 2: Calculate how much capital we've already used
        # current_position is in shares, so convert to dollars using current price
        # For simplicity, use last_best_bid as proxy for position value
        position_value = abs(self.state.current_position * (self.state.last_best_bid or best_bid))
        capital_used = position_value
        available_budget = self.config.budget - capital_used
        
        if available_budget <= 0:
            logger.info(
                f"Trader {self.market_id}: ❌ Skipping BUY - no budget available "
                f"(budget: ${self.config.budget:.2f}, used: ${capital_used:.2f}, available: ${available_budget:.2f})"
            )
            return  # No budget left
        logger.info(
            f"Trader {self.market_id}: ✅ Budget check passed: ${available_budget:.2f} available (${capital_used:.2f} used of ${self.config.budget:.2f} budget)"
        )
        
        # Check 3: Calculate buy price (best_bid + price_improvement)
        # This makes our order slightly better than the current best bid, so we get priority
        # Convert price_improvement from cents to decimal (best_bid is in decimal)
        price_improvement_decimal = self.config.price_improvement / 100.0
        buy_price = best_bid + price_improvement_decimal
        
        # Check 4: Calculate how many shares we can buy with available budget at the actual buy price
        # order_size = available_dollars / actual_price_per_share
        # IMPORTANT: Use buy_price (not best_bid) to ensure we don't exceed budget
        order_size = available_budget / buy_price
        if order_size <= 0:
            logger.info(
                f"Trader {self.market_id}: ❌ Skipping BUY - calculated order size {order_size:.2f} <= 0 "
                f"(available_budget: ${available_budget:.2f}, buy_price: {buy_price:.4f})"
            )
            return
        logger.info(
            f"Trader {self.market_id}: ✅ Order size calculated: {order_size:.2f} shares (${available_budget:.2f} / {buy_price:.4f})"
        )
        
        # Check 5: Verify order cost doesn't exceed available budget (safety check)
        order_cost = order_size * buy_price
        if order_cost > available_budget:
            logger.warning(
                f"Trader {self.market_id}: ❌ Skipping BUY - order cost ${order_cost:.2f} > available budget ${available_budget:.2f} "
                f"(order_size: {order_size:.2f}, buy_price: {buy_price:.4f})"
            )
            # Recalculate with a small safety margin
            order_size = (available_budget * 0.999) / buy_price  # 0.1% safety margin
            order_cost = order_size * buy_price
            if order_cost > available_budget:
                logger.error(f"Trader {self.market_id}: CRITICAL - Cannot place order within budget even with safety margin")
                return
        
        # Check 6: Order size must meet market-specific minimum (or default 5 shares)
        min_order_size = self.state.min_order_size or MIN_ORDER_SIZE
        min_order_value = min_order_size * buy_price  # Use buy_price for minimum calculation
        if order_size < min_order_size:
            logger.info(
                f"Trader {self.market_id}: ❌ Skipping BUY - order size {order_size:.2f} < market minimum {min_order_size:.0f} shares "
                f"(available_budget: ${available_budget:.2f}, buy_price: {buy_price:.4f}, "
                f"market requires ${min_order_value:.2f} for minimum order, have ${available_budget:.2f}, "
                f"need ${min_order_value - available_budget:.2f} more)"
            )
            return
        logger.info(
            f"Trader {self.market_id}: ✅ Minimum order size check passed: {order_size:.2f} >= {min_order_size:.0f} shares"
        )
        
        # Final safety check: ensure order cost is within budget
        final_order_cost = order_size * buy_price
        if final_order_cost > available_budget:
            logger.error(
                f"Trader {self.market_id}: ❌ CRITICAL - Final order cost ${final_order_cost:.2f} > available budget ${available_budget:.2f}. "
                f"Reducing order size to fit budget."
            )
            order_size = available_budget / buy_price
            final_order_cost = order_size * buy_price
        
        logger.info(
            f"Trader {self.market_id}: ✅ ALL CHECKS PASSED - Placing BUY order: "
            f"{order_size:.2f} shares @ {buy_price:.4f} (${final_order_cost:.2f}), "
            f"Budget: ${self.config.budget:.2f} (Used: ${capital_used:.2f}, Available: ${available_budget:.2f})"
        )
        await self._place_order("BUY", buy_price, order_size, self.state.last_spread)
    
    async def _try_place_sell_order(self, best_bid: float, best_ask: float) -> None:
        """Try to place a SELL order if conditions are met.
        
        IMPORTANT: We only sell shares we currently own. We never short sell.
        
        SELL order logic:
        - Only place if we have a position (current_position > 0 means we own shares)
        - Sell at best_ask - price_improvement (slightly better than best ask)
        - Sell exactly the number of shares we currently hold (no more, no less)
        """
        logger.info(f"Trader {self.market_id}: Evaluating SELL order placement...")
        
        # Only sell if we actually own shares
        # Force a position sync before placing SELL orders to ensure we have accurate position
        if self.state.current_position <= 0.0:
            # Try to sync position one more time in case it's stale
            try:
                api_position = await self.execution.get_market_position(self.token_id)
                if api_position > 0.0 and abs(api_position - self.state.current_position) > 0.01:
                    logger.info(
                        f"Trader {self.market_id}: Position was stale ({self.state.current_position:.2f}), "
                        f"synced from API: {api_position:.2f} shares"
                    )
                    self.state.current_position = api_position
            except Exception as e:
                logger.debug(f"Trader {self.market_id}: Failed to sync position before SELL: {e}")
        
        if self.state.current_position <= 0.0:
            logger.info(
                f"Trader {self.market_id}: ❌ Skipping SELL - no position to sell "
                f"(current_position: {self.state.current_position:.2f} shares, we don't short sell)"
            )
            return  # No shares to sell - we don't short sell
        
        logger.info(
            f"Trader {self.market_id}: ✅ Position check passed: {self.state.current_position:.2f} shares available to sell"
        )
        
        # Sell at best_ask - price_improvement (slightly better than current best ask)
        # This makes our order competitive and likely to fill
        # Convert price_improvement from cents to decimal (best_ask is in decimal)
        price_improvement_decimal = self.config.price_improvement / 100.0
        sell_price = best_ask - price_improvement_decimal
        
        # Sell the shares we own (current_position is positive when we own shares)
        # IMPORTANT: Account for any active sell order that might be locking shares
        # If we have an active sell order, those shares are locked and can't be used
        available_position = self.state.current_position
        if self.state.active_sell_order_id and self.state.active_sell_order_size:
            # Subtract the size of the active sell order (those shares are locked)
            available_position = available_position - self.state.active_sell_order_size
            logger.debug(
                f"Trader {self.market_id}: Active SELL order locks {self.state.active_sell_order_size:.2f} shares, "
                f"available: {available_position:.2f} of {self.state.current_position:.2f} total"
            )
        
        if available_position <= 0:
            logger.info(
                f"Trader {self.market_id}: ❌ Skipping SELL - no available position "
                f"(total: {self.state.current_position:.2f}, locked in orders: {self.state.active_sell_order_size or 0:.2f})"
            )
            return
        
        # Apply safety buffer: reduce order size slightly to account for rounding/precision issues
        # This prevents "not enough balance / allowance" errors
        # Use a percentage-based buffer (1%) or minimum 0.1 shares, whichever is larger
        safety_buffer = max(0.1, available_position * 0.01)  # 1% or 0.1 shares minimum
        order_size = max(0, available_position - safety_buffer)
        
        if order_size <= 0:
            logger.info(
                f"Trader {self.market_id}: ❌ Skipping SELL - position too small after safety buffer "
                f"(original: {self.state.current_position:.2f} shares)"
            )
            return
        
        # Check: Order size must meet market-specific minimum (or default 5 shares)
        min_order_size = self.state.min_order_size or MIN_ORDER_SIZE
        if order_size < min_order_size:
            logger.info(
                f"Trader {self.market_id}: ❌ Skipping SELL - position {order_size:.2f} < minimum {min_order_size:.0f} shares "
                f"(original position: {self.state.current_position:.2f}, cannot place order below market minimum)"
            )
            return
        logger.info(
            f"Trader {self.market_id}: ✅ Minimum order size check passed: {order_size:.2f} >= {min_order_size:.0f} shares "
            f"(original position: {self.state.current_position:.2f})"
        )
        
        logger.info(
            f"Trader {self.market_id}: ✅ ALL CHECKS PASSED - Placing SELL order: "
            f"{order_size:.2f} shares @ {sell_price:.4f} (${order_size * sell_price:.2f}) "
            f"(from position: {self.state.current_position:.2f} shares)"
        )
        await self._place_order("SELL", sell_price, order_size, self.state.last_spread)
    
    async def _place_order(self, side: str, price: float, size: float, spread: Optional[float] = None) -> None:
        """Place an order and track it.
        
        Places a limit order on the exchange and registers it in our state
        so we can track and update it later.
        """
        try:
            # Submit order to exchange
            order_id = await self.execution.submit_limit(
                side=side, price=price, size=size, token_id=self.token_id
            )
            # Register in our state so we can track it
            self._register_order(order_id, side, price, size)
            
            msg = f"Trader {self.market_id}: {side} order {order_id} ({size:.2f} @ {price:.4f})"
            if spread is not None:
                msg += f", spread={spread:.4f} cents"
            logger.info(msg)
        except Exception as e:
            error_msg = str(e)
            if "not enough balance / allowance" in error_msg.lower():
                if side == "SELL":
                    logger.error(
                        f"Trader {self.market_id} failed to place {side} order: {e}\n"
                        f"  This usually means: (1) Token allowance not set, (2) Position locked in another order, "
                        f"or (3) Position size mismatch. Current position: {self.state.current_position:.2f} shares, "
                        f"trying to sell: {size:.2f} shares"
                    )
                    # Force position sync on next iteration
                    self._position_synced = False
                else:
                    logger.error(
                        f"Trader {self.market_id} failed to place {side} order: {e}\n"
                        f"  Insufficient balance/allowance. Available budget may be incorrect."
                    )
            else:
                logger.error(f"Trader {self.market_id} failed to place {side} order: {e}")
    
    def _register_order(self, order_id: str, side: str, price: float, size: float) -> None:
        """Register an active order in state."""
        if side == "BUY":
            self.state.active_buy_order_id = order_id
            self.state.active_buy_order_price = price
            self.state.active_buy_order_size = size
        else:  # SELL
            self.state.active_sell_order_id = order_id
            self.state.active_sell_order_price = price
            self.state.active_sell_order_size = size
    
    def _unregister_order(self, side: str) -> None:
        """Remove an active order from state."""
        if side == "BUY":
            self.state.active_buy_order_id = None
            self.state.active_buy_order_price = None
            self.state.active_buy_order_size = None
        else:  # SELL
            self.state.active_sell_order_id = None
            self.state.active_sell_order_price = None
            self.state.active_sell_order_size = None
    
    def _extract_filled_size(self, status: Dict, original_size: Optional[float]) -> float:
        """Extract filled size from order status.
        
        Tries multiple field names and fallback methods to get the filled size.
        Returns 0.0 if unable to determine.
        """
        # Try common field names for filled size
        filled_size = (
            status.get("filled_size")
            or status.get("filledSize")
            or status.get("filled")
            or status.get("filledAmount")
            or status.get("filled_amount")
        )
        
        if filled_size is not None:
            try:
                return float(filled_size)
            except (ValueError, TypeError):
                pass
        
        # Fallback: calculate from size - remaining_size if we have original size
        if original_size is not None:
            remaining_size = (
                status.get("remaining_size")
                or status.get("remainingSize")
                or status.get("remaining")
                or status.get("remainingAmount")
                or status.get("remaining_amount")
            )
            
            if remaining_size is not None:
                try:
                    remaining = float(remaining_size)
                    filled = original_size - remaining
                    if filled > 0:
                        return filled
                except (ValueError, TypeError):
                    pass
        
        # Last resort: if order is FILLED and we have original size, assume full fill
        if status.get("status", "").upper() == "FILLED" and original_size is not None:
            logger.warning(f"Could not extract filled_size from status, assuming full fill: {original_size}")
            return original_size
        
        # If we can't determine, log and return 0
        logger.warning(f"Could not extract filled_size from order status: {status}")
        return 0.0
    
    async def _check_order_status(self) -> None:
        """Check order status and clean up filled/cancelled orders.
        
        We can have both BUY and SELL orders, so we check both.
        This ensures our internal state matches the exchange:
        - If an order was filled, remove it from tracking
        - If an order was cancelled/rejected, remove it from tracking
        - This prevents us from trying to update orders that no longer exist
        """
        # Check BUY order
        if self.state.active_buy_order_id:
            try:
                status = await self.execution.get_order_status(self.state.active_buy_order_id)
                order_status = status.get("status", "").upper()
                
                if order_status == "FILLED":
                    # Update position: BUY order filled means we now own more shares
                    filled_size = self._extract_filled_size(status, self.state.active_buy_order_size)
                    fill_price = self.state.active_buy_order_price or 0.0
                    if filled_size > 0:
                        # Update average cost basis
                        if self._avg_cost_basis is None:
                            self._avg_cost_basis = fill_price
                        else:
                            # Weighted average: (old_cost * old_size + new_cost * new_size) / total_size
                            old_position = self.state.current_position
                            total_cost = (self._avg_cost_basis * old_position) + (fill_price * filled_size)
                            self._avg_cost_basis = total_cost / (old_position + filled_size)
                        
                        self.state.current_position += filled_size
                        logger.info(
                            f"Trader {self.market_id}: BUY order {self.state.active_buy_order_id} filled "
                            f"({filled_size:.2f} shares @ {fill_price:.4f}). Position: {self.state.current_position:.2f}"
                        )
                        
                        # Save fill to Supabase
                        self._save_fill("BUY", fill_price, filled_size, self.state.active_buy_order_id, None)
                    
                    self._unregister_order("BUY")
                    self.state.total_trades += 1
                elif order_status in ("CANCELLED", "REJECTED"):
                    self._unregister_order("BUY")
                    logger.debug(f"Trader {self.market_id}: BUY order {self.state.active_buy_order_id} {order_status.lower()}")
            except Exception as e:
                logger.warning(f"Trader {self.market_id} failed to check BUY order {self.state.active_buy_order_id}: {e}")
        
        # Check SELL order
        if self.state.active_sell_order_id:
            try:
                status = await self.execution.get_order_status(self.state.active_sell_order_id)
                order_status = status.get("status", "").upper()
                
                if order_status == "FILLED":
                    # Update position: SELL order filled means we now own fewer shares
                    filled_size = self._extract_filled_size(status, self.state.active_sell_order_size)
                    fill_price = self.state.active_sell_order_price or 0.0
                    if filled_size > 0:
                        # Calculate realized PnL if we have a cost basis
                        realized_pnl = None
                        if self._avg_cost_basis is not None:
                            realized_pnl = (fill_price - self._avg_cost_basis) * filled_size
                            self.state.total_pnl += realized_pnl
                        
                        self.state.current_position -= filled_size
                        logger.info(
                            f"Trader {self.market_id}: SELL order {self.state.active_sell_order_id} filled "
                            f"({filled_size:.2f} shares @ {fill_price:.4f}). Position: {self.state.current_position:.2f}"
                            + (f", PnL: ${realized_pnl:.2f}" if realized_pnl is not None else "")
                        )
                        
                        # Save fill to Supabase
                        self._save_fill("SELL", fill_price, filled_size, self.state.active_sell_order_id, realized_pnl)
                        
                        # Reset cost basis if position is flat
                        if self.state.current_position <= 0:
                            self._avg_cost_basis = None
                    
                    self._unregister_order("SELL")
                    self.state.total_trades += 1
                elif order_status in ("CANCELLED", "REJECTED"):
                    self._unregister_order("SELL")
                    logger.debug(f"Trader {self.market_id}: SELL order {self.state.active_sell_order_id} {order_status.lower()}")
            except Exception as e:
                logger.warning(f"Trader {self.market_id} failed to check SELL order {self.state.active_sell_order_id}: {e}")
    
    async def _update_orders(self, best_bid: float, best_ask: float, second_best_bid: Optional[float] = None, second_best_ask: Optional[float] = None, best_bid_size: Optional[float] = None, best_ask_size: Optional[float] = None) -> None:
        """Update active orders to stay competitive.
        
        Market prices change constantly. This method ensures our orders stay at the top of the book:
        - If market moved, our orders might no longer be the best
        - We cancel old orders and place new ones at updated competitive prices
        - This keeps us at: best_bid + price_improvement (for buys) or best_ask - price_improvement (for sells)
        
        IMPORTANT: We check if our order is already the best bid/ask to prevent infinite price increases.
        If best_bid is close to our buy order price, our order IS the best bid - don't update upward.
        If best_ask is close to our sell order price, our order IS the best ask - don't update downward.
        
        We can have both BUY and SELL orders, so we update both independently.
        """
        # Update BUY order if we have one
        if self.state.active_buy_order_id and self.state.active_buy_order_price is not None:
            our_buy_price = self.state.active_buy_order_price
            # Convert price_improvement from cents to decimal (best_bid is in decimal)
            price_improvement_decimal = self.config.price_improvement / 100.0
            target_price = best_bid + price_improvement_decimal
            
            # CRITICAL: Check if our order is the best bid
            # If our order is the best bid, then best_bid from orderbook should equal our order price
            # We need to account for the fact that our order might be at best_bid + price_improvement
            # So check if our price is within price_improvement of the best_bid, or if best_bid equals our price
            # Also check if our price is >= best_bid (we're at or above the best bid)
            price_diff = abs(our_buy_price - best_bid)
            is_our_order_best_bid = (
                price_diff < PRICE_UPDATE_THRESHOLD or  # Our price equals best_bid (exact match)
                (our_buy_price >= best_bid and price_diff <= price_improvement_decimal + PRICE_UPDATE_THRESHOLD)  # Our price is within price_improvement of best_bid
            )
            
            if is_our_order_best_bid:
                # Our order is the best bid - stick close to second best bid to stay competitive
                # Strategy: Stay at second_best_bid + price_improvement to remain top while minimizing gap
                # BUT: Only move down if we're the ONLY one at the best bid
                # Check: If best_bid_size matches our order size, we're the only one
                our_order_size = self.state.active_buy_order_size or 0.0
                is_sole_best_bid = (best_bid_size is not None and 
                                   abs(best_bid_size - our_order_size) < 0.01)  # Allow small floating point differences
                
                if second_best_bid is not None and is_sole_best_bid:
                    # Calculate target based on second-best bid (stay just above it)
                    gap_target_price = second_best_bid + price_improvement_decimal
                    # Update if:
                    # 1. It's different from our current price (within threshold)
                    # 2. It's at or above the second_best_bid (sanity check - we should be above it)
                    # Note: We can move below our current price to close the gap, as long as we stay above second_best
                    if (abs(gap_target_price - our_buy_price) > PRICE_UPDATE_THRESHOLD and 
                        gap_target_price >= second_best_bid):
                        await self._replace_order(
                            self.state.active_buy_order_id, "BUY", gap_target_price, best_bid, best_ask
                        )
                elif second_best_bid is None:
                    # No second best bid - check if we need to update
                    # If our price already equals best_bid (we're the best bid), don't update
                    # Only update if market moved and we're no longer at the right price
                    if abs(our_buy_price - best_bid) > PRICE_UPDATE_THRESHOLD:
                        # Market moved - update to stay at best_bid + price_improvement
                        if abs(target_price - our_buy_price) > PRICE_UPDATE_THRESHOLD:
                            await self._replace_order(
                                self.state.active_buy_order_id, "BUY", target_price, best_bid, best_ask
                            )
                    # If our price equals best_bid (we're the best bid), don't update - we're already correct
                # If we're not the sole best bid, don't move down (others are at our price level)
                # Also don't update if we're already at the best bid price
                elif not is_sole_best_bid:
                    # Others are at our price - only update if we're not at best_bid
                    if abs(our_buy_price - best_bid) > PRICE_UPDATE_THRESHOLD:
                        # We're not at best_bid, update to get there
                        if abs(target_price - our_buy_price) > PRICE_UPDATE_THRESHOLD:
                            await self._replace_order(
                                self.state.active_buy_order_id, "BUY", target_price, best_bid, best_ask
                            )
            else:
                # Someone else has a better bid - offer best_bid + price_improvement to become competitive
                # target_price is already best_bid + price_improvement_decimal
                # Always update to beat the current best bid
                if abs(our_buy_price - target_price) > PRICE_UPDATE_THRESHOLD:
                    await self._replace_order(
                        self.state.active_buy_order_id, "BUY", target_price, best_bid, best_ask
                    )
        
        # Update SELL order if we have one
        if self.state.active_sell_order_id and self.state.active_sell_order_price is not None:
            our_sell_price = self.state.active_sell_order_price
            # Convert price_improvement from cents to decimal (best_ask is in decimal)
            price_improvement_decimal = self.config.price_improvement / 100.0
            target_price = best_ask - price_improvement_decimal
            
            # CRITICAL: Check if our order is the best ask
            # If our order is the best ask, then best_ask from orderbook should equal our order price
            # We need to account for the fact that our order might be at best_ask - price_improvement
            # So check if our price is within price_improvement of the best_ask, or if best_ask equals our price
            # Also check if our price is <= best_ask (we're at or below the best ask)
            price_diff = abs(our_sell_price - best_ask)
            is_our_order_best_ask = (
                price_diff < PRICE_UPDATE_THRESHOLD or  # Our price equals best_ask (exact match)
                (our_sell_price <= best_ask and price_diff <= price_improvement_decimal + PRICE_UPDATE_THRESHOLD)  # Our price is within price_improvement of best_ask
            )
            
            if is_our_order_best_ask:
                # Our order is the best ask - stick close to second best ask to stay competitive
                # Strategy: Stay at second_best_ask - price_improvement to remain top while minimizing gap
                # BUT: Only move up if we're the ONLY one at the best ask
                # Check: If best_ask_size matches our order size, we're the only one
                our_order_size = self.state.active_sell_order_size or 0.0
                is_sole_best_ask = (best_ask_size is not None and 
                                   abs(best_ask_size - our_order_size) < 0.01)  # Allow small floating point differences
                
                if second_best_ask is not None and is_sole_best_ask:
                    # Calculate target based on second-best ask (stay just below it)
                    gap_target_price = second_best_ask - price_improvement_decimal
                    # Update if:
                    # 1. It's different from our current price (within threshold)
                    # 2. It's at or below the second_best_ask (sanity check - we should be below it)
                    # Note: We can move above our current price to close the gap, as long as we stay below second_best
                    if (abs(gap_target_price - our_sell_price) > PRICE_UPDATE_THRESHOLD and 
                        gap_target_price <= second_best_ask):
                        await self._replace_order(
                            self.state.active_sell_order_id, "SELL", gap_target_price, best_bid, best_ask
                        )
                elif second_best_ask is None:
                    # No second best ask - check if we need to update
                    # If our price already equals best_ask (we're the best ask), don't update
                    # Only update if market moved and we're no longer at the right price
                    if abs(our_sell_price - best_ask) > PRICE_UPDATE_THRESHOLD:
                        # Market moved - update to stay at best_ask - price_improvement
                        if abs(target_price - our_sell_price) > PRICE_UPDATE_THRESHOLD:
                            await self._replace_order(
                                self.state.active_sell_order_id, "SELL", target_price, best_bid, best_ask
                            )
                    # If our price equals best_ask (we're the best ask), don't update - we're already correct
                # If we're not the sole best ask, don't move up (others are at our price level)
                # Also don't update if we're already at the best ask price
                elif not is_sole_best_ask:
                    # Others are at our price - only update if we're not at best_ask
                    if abs(our_sell_price - best_ask) > PRICE_UPDATE_THRESHOLD:
                        # We're not at best_ask, update to get there
                        if abs(target_price - our_sell_price) > PRICE_UPDATE_THRESHOLD:
                            await self._replace_order(
                                self.state.active_sell_order_id, "SELL", target_price, best_bid, best_ask
                            )
            else:
                # Someone else has a better ask - offer best_ask - price_improvement to become competitive
                # target_price is already best_ask - price_improvement_decimal
                # Update if our price is different from the target (to beat the current best ask)
                if abs(our_sell_price - target_price) > PRICE_UPDATE_THRESHOLD:
                    await self._replace_order(
                        self.state.active_sell_order_id, "SELL", target_price, best_bid, best_ask
                    )
    
    async def _replace_order(self, old_order_id: str, side: str, new_price: float, best_bid: float, best_ask: float) -> None:
        """Replace an active order with updated price.
        
        When market moves, we need to update our order:
        1. Cancel the old order
        2. Calculate new order size (may have changed if we're buying)
        3. Place new order at updated competitive price
        
        For BUY orders: Recalculate size based on available budget
        For SELL orders: Use current position size (sell what we have)
        """
        try:
            # Step 1: Cancel the old order
            await self.execution.cancel(old_order_id)
            self._unregister_order(side)
            
            # Step 2: Calculate new order size
            if side == "BUY":
                # For buy orders, recalculate based on available budget
                # (budget might have changed if we filled other orders)
                capital_used = abs(self.state.current_position * (self.state.last_best_bid or best_bid))
                available_budget = self.config.budget - capital_used
                if available_budget <= 0:
                    return  # No budget left, don't place new order
                order_size = available_budget / best_bid
            else:  # SELL
                # IMPORTANT: For sell orders, only sell shares we currently own
                # We never short sell - if position is 0 or negative, don't place order
                if self.state.current_position <= 0.0:
                    logger.info(
                        f"Trader {self.market_id}: Cannot replace SELL order - no position "
                        f"(current_position: {self.state.current_position:.2f})"
                    )
                    return  # No shares to sell - don't place order
                
                # Apply same safety buffer as in _try_place_sell_order to prevent rounding issues
                safety_buffer = 0.1  # Reduce by 0.1 shares as safety margin
                order_size = max(0, self.state.current_position - safety_buffer)
                
                if order_size <= 0:
                    logger.info(
                        f"Trader {self.market_id}: Cannot replace SELL order - position too small after buffer "
                        f"(original: {self.state.current_position:.2f} shares)"
                    )
                    return
                
                # Check minimum order size
                min_order_size = self.state.min_order_size or MIN_ORDER_SIZE
                if order_size < min_order_size:
                    logger.info(
                        f"Trader {self.market_id}: Cannot replace SELL order - size {order_size:.2f} < min {min_order_size:.0f} "
                        f"(original position: {self.state.current_position:.2f})"
                    )
                    return
            
            # Step 3: Place new order at updated price
            # For SELL orders, add a small delay after cancellation to ensure position is unlocked
            if side == "SELL":
                import asyncio
                await asyncio.sleep(0.1)  # Small delay to ensure cancellation is processed
            
            if order_size > 0:
                new_order_id = await self.execution.submit_limit(
                    side=side, price=new_price, size=order_size, token_id=self.token_id
                )
                self._register_order(new_order_id, side, new_price, order_size)
                logger.info(
                    f"Trader {self.market_id}: Replaced {side} order {old_order_id[:20]}... "
                    f"with {new_order_id[:20]}... ({order_size:.2f} @ {new_price:.4f})"
                )
        except Exception as e:
            error_msg = str(e)
            if "not enough balance / allowance" in error_msg.lower() and side == "SELL":
                logger.error(
                    f"Trader {self.market_id} failed to replace {side} order {old_order_id[:20]}...: {e}\n"
                    f"  Position: {self.state.current_position:.2f} shares, trying to sell: {order_size:.2f} shares. "
                    f"This may be a token allowance issue or position temporarily locked after cancellation."
                )
                # Force position sync on next iteration
                self._position_synced = False
            else:
                logger.error(f"Trader {self.market_id} failed to replace order {old_order_id[:20]}...: {e}")
    
    def _save_fill(self, side: str, price: float, size: float, order_id: str, pnl: Optional[float]) -> None:
        """Save a fill to Supabase (non-blocking).
        
        Args:
            side: 'BUY' or 'SELL'
            price: Fill price
            size: Fill size
            order_id: Order ID
            pnl: Realized PnL (None for BUY orders)
        """
        if not self.supabase_service or not self.supabase_service.is_available():
            return
        
        # Get trader_id (cache it to avoid repeated DB calls)
        if self._trader_id is None:
            self._trader_id = self.supabase_service.get_trader_id_by_slug(
                self.config.market_slug or ""
            )
        
        # Save fill in background thread (non-blocking)
        import threading
        thread = threading.Thread(
            target=lambda: self.supabase_service.save_fill(
                trader_id=self._trader_id,
                market_slug=self.config.market_slug or "",
                side=side,
                price=price,
                size=size,
                order_id=order_id,
                pnl=pnl,
            ),
            daemon=True
        )
        thread.start()
    
    def save_log_to_db(self, level: str, message: str) -> None:
        """Save a log entry to Supabase (non-blocking).
        
        Args:
            level: Log level ('info', 'warning', 'error', 'debug')
            message: Log message
        """
        if not self.supabase_service or not self.supabase_service.is_available():
            return
        
        # Get trader_id (cache it to avoid repeated DB calls)
        if self._trader_id is None:
            self._trader_id = self.supabase_service.get_trader_id_by_slug(
                self.config.market_slug or ""
            )
        
        # Save log in background thread (non-blocking)
        import threading
        thread = threading.Thread(
            target=lambda: self.supabase_service.save_log(
                trader_id=self._trader_id,
                level=level,
                message=message,
            ),
            daemon=True
        )
        thread.start()
