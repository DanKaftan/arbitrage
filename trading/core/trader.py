"""Market-making trader for a single market."""

import logging
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional, Any, List

from services import PolymarketService, PolymarketServiceError
from config import TraderConfig

logger = logging.getLogger(__name__)

MIN_ORDER_SIZE = 5.0  # Polymarket minimum order size in shares
PRICE_UPDATE_THRESHOLD = 0.0001  # Threshold for price comparisons (in decimal, not cents)


@dataclass
class MarketState:
    """Real-time market data from Polymarket - always fresh, never cached.
    
    This is the ground truth - all data comes directly from Polymarket API.
    No internal tracking, no caching - Polymarket is the source of truth.
    """
    # From orderbook
    best_bid_cents: Optional[float] = None  # Best bid in cents
    best_ask_cents: Optional[float] = None  # Best ask in cents
    best_bid_size: Optional[float] = None  # Size at best bid
    best_ask_size: Optional[float] = None  # Size at best ask
    second_best_bid_cents: Optional[float] = None  # Second best bid in cents
    second_best_ask_cents: Optional[float] = None  # Second best ask in cents
    min_order_size: Optional[float] = None  # Market-specific minimum order size
    
    # From position API
    current_inventory: float = 0.0  # Current position in shares (from Polymarket)
    
    # From get_my_open_orders(token_id) API
    my_bid_order_id: Optional[str] = None
    my_bid_order_price_cents: Optional[float] = None  # Price in cents
    my_bid_order_size: Optional[float] = None  # Size in shares
    my_bid_order_is_best_bid: bool = False  # Is it the current best bid?
    
    my_ask_order_id: Optional[str] = None
    my_ask_order_price_cents: Optional[float] = None  # Price in cents
    my_ask_order_size: Optional[float] = None  # Size in shares
    my_ask_order_is_best_ask: bool = False  # Is it the current best ask?


class Trader:
    """Market-making trader for a single market.
    
    Uses Polymarket as the single source of truth - no internal state tracking.
    All decisions are made based on real-time data from Polymarket API.
    """
    
    def __init__(
        self,
        market_id: str,
        config: TraderConfig,
        execution_layer: PolymarketService,
        supabase_service: Optional[Any] = None,  # SupabaseService, avoiding circular import
    ):
        self.market_id = market_id
        self.token_id = config.token_id or ""
        self.config = config
        self.execution = execution_layer
        self.supabase_service = supabase_service
        self.is_active = True
        self.is_paused = False
        self._trader_id: Optional[str] = None  # Cached trader UUID from DB
        
        # Statistics (for reporting only - not used for trading decisions)
        self.total_trades: int = 0
        self.total_pnl: float = 0.0
        
        if not config.name:
            config.name = f"Trader-{market_id[:8]}"
        
        if not self.token_id:
            logger.warning(
                f"Trader '{config.name}' missing token_id - API calls will fail"
            )
        
        logger.info(
            f"Trader '{config.name}' initialized "
            f"(token_id={'SET' if self.token_id else 'MISSING'}, "
            f"max_inventory={config.max_inventory}, spread_threshold={config.spread_threshold}¢, "
            f"price_improvement={config.price_improvement}¢)"
        )
    
    async def step(self) -> None:
        """Execute one trading step.
        
        Main trading loop:
        1. Fetch ALL real-time data from Polymarket (orderbook, position, my orders)
        2. Calculate derived values (balance, spread)
        3. Execute SELL logic (always be best ask)
        4. Execute BUY logic (be best bid only if spread condition met)
        """
        if not self.is_active or self.is_paused:
            return
        
        try:
            # Step 1: Fetch all real-time data from Polymarket
            market = await self._fetch_market_state()
            if not market.best_bid_cents or not market.best_ask_cents:
                logger.debug(f"Trader {self.market_id}: Skipping step - missing bid/ask prices")
                return
            
            # Step 2: Calculate derived values
            balance = self.config.max_inventory - market.current_inventory
            spread_cents = market.best_ask_cents - market.best_bid_cents
            
            # Step 3: Execute trading logic
            await self._handle_sell_logic(market, balance)
            await self._handle_buy_logic(market, balance, spread_cents)
            
        except PolymarketServiceError as e:
            logger.error(f"Trader {self.market_id} Polymarket service error: {e}")
        except Exception as e:
            logger.error(f"Trader {self.market_id} error: {e}", exc_info=True)
    
    async def _fetch_market_state(self) -> MarketState:
        """Fetch all real-time data from Polymarket.
        
        This is the single source of truth - always query Polymarket directly.
        No caching, no internal tracking - Polymarket is ground truth.
        """
        state = MarketState()
        
        # 1. Fetch orderbook
        try:
            orderbook = await self.execution.get_orderbook(self.token_id)
            if orderbook:
                best_bid, best_ask, best_bid_size, best_ask_size, second_best_bid, second_best_ask = self._extract_best_prices(orderbook)
                state.best_bid_cents = best_bid * 100 if best_bid else None  # Convert to cents
                state.best_ask_cents = best_ask * 100 if best_ask else None  # Convert to cents
                state.best_bid_size = best_bid_size
                state.best_ask_size = best_ask_size
                state.second_best_bid_cents = second_best_bid * 100 if second_best_bid else None  # Convert to cents
                state.second_best_ask_cents = second_best_ask * 100 if second_best_ask else None  # Convert to cents
                state.min_order_size = orderbook.get("min_order_size")
        except Exception as e:
            logger.warning(f"Trader {self.market_id} failed to fetch orderbook: {e}")
        
        # 2. Fetch current position
        try:
            state.current_inventory = await self.execution.get_market_position(self.token_id)
        except Exception as e:
            logger.warning(f"Trader {self.market_id} failed to fetch position: {e}")
        
        # 3. Fetch my open orders
        try:
            my_orders = await self.execution.get_my_open_orders(self.token_id)
            for order in my_orders:
                side = order.get("side", "").upper()
                order_id = order.get("id") or order.get("orderID") or order.get("order_id")
                price = self._extract_price(order)
                size = self._extract_size(order)
                
                if side == "BUY":
                    state.my_bid_order_id = order_id
                    state.my_bid_order_price_cents = price * 100 if price else None  # Convert to cents
                    state.my_bid_order_size = size
                    # Check if it's the best bid
                    if state.best_bid_cents and state.my_bid_order_price_cents:
                        price_diff = abs(state.my_bid_order_price_cents - state.best_bid_cents)
                        state.my_bid_order_is_best_bid = price_diff < (self.config.price_improvement + 0.01)  # Within price_improvement
                elif side == "SELL":
                    state.my_ask_order_id = order_id
                    state.my_ask_order_price_cents = price * 100 if price else None  # Convert to cents
                    state.my_ask_order_size = size
                    # Check if it's the best ask
                    if state.best_ask_cents and state.my_ask_order_price_cents:
                        price_diff = abs(state.my_ask_order_price_cents - state.best_ask_cents)
                        state.my_ask_order_is_best_ask = price_diff < (self.config.price_improvement + 0.01)  # Within price_improvement
        except Exception as e:
            logger.warning(f"Trader {self.market_id} failed to fetch my orders: {e}")
        
        return state
    
    def _extract_best_prices(self, orderbook: Dict) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
        """Extract best bid/ask, second best bid/ask, and sizes from orderbook.
        
        Polymarket API returns:
        - Bids sorted lowest->highest (best bid = last element = highest price)
        - Asks sorted highest->lowest (best ask = last element = lowest price)
        
        Returns: (best_bid, best_ask, best_bid_size, best_ask_size, second_best_bid, second_best_ask) in decimal
        """
        try:
            bids = orderbook.get("bids", [])
            asks = orderbook.get("asks", [])
            
            # Best bid (last element = highest price)
            best_bid = float(bids[-1]["price"]) if bids else None
            best_bid_size = float(bids[-1].get("size", 0)) if bids else None
            
            # Second best bid (second-to-last element, if exists)
            second_best_bid = float(bids[-2]["price"]) if len(bids) >= 2 else None
            
            # Best ask (last element = lowest price)
            best_ask = float(asks[-1]["price"]) if asks else None
            best_ask_size = float(asks[-1].get("size", 0)) if asks else None
            
            # Second best ask (second-to-last element, if exists)
            second_best_ask = float(asks[-2]["price"]) if len(asks) >= 2 else None
            
            return best_bid, best_ask, best_bid_size, best_ask_size, second_best_bid, second_best_ask
        except (KeyError, IndexError, ValueError) as e:
            logger.warning(f"Trader {self.market_id} failed to parse orderbook: {e}")
            return None, None, None, None, None, None
    
    def _extract_price(self, order: Dict) -> Optional[float]:
        """Extract price from order dict."""
        price = order.get("price") or order.get("Price") or order.get("PRICE")
        if price is not None:
            try:
                return float(price)
            except (ValueError, TypeError):
                pass
        return None
    
    def _extract_size(self, order: Dict) -> Optional[float]:
        """Extract size from order dict."""
        size = (
            order.get("size") or 
            order.get("Size") or 
            order.get("original_size") or 
            order.get("originalSize") or
            order.get("remaining_size") or
            order.get("remainingSize")
        )
        if size is not None:
            try:
                return float(size)
            except (ValueError, TypeError):
                pass
        return None
    
    async def _handle_sell_logic(self, market: MarketState, balance: float) -> None:
        """SELL logic: Always be the best ask.
        
        Strategy:
        1. Always propose to sell ALL inventory (read from Polymarket)
        2. Always be the best ask order
        
        Cases:
        - No open ask order → create at (best_ask - price_improvement) with all inventory
        - Have order AND it equals best_ask → add shares if inventory > order.size
        - Have order AND it's below best_ask → cancel + create new at (best_ask - price_improvement)
        """
        if not market.best_ask_cents:
            return
        
        # Always query fresh inventory from market state
        inventory = market.current_inventory
        
        if inventory <= 0:
            # No inventory to sell - cancel existing order if any
            if market.my_ask_order_id:
                logger.info(f"Trader {self.market_id}: No inventory to sell, cancelling ask order")
                try:
                    await self.execution.cancel(market.my_ask_order_id)
                except Exception as e:
                    logger.error(f"Trader {self.market_id}: Failed to cancel ask order: {e}")
            return
        
        min_order_size = market.min_order_size or MIN_ORDER_SIZE
        if inventory < min_order_size:
            logger.debug(f"Trader {self.market_id}: Inventory {inventory:.2f} < min order size {min_order_size:.0f}")
            return
        
        # Calculate target price
        # Strategy: Be price_improvement better than second best ask if we're sole best ask and gap is wide enough
        # Otherwise: Be price_improvement better than best ask
        
        # Check if we're the sole best ask (our order size equals best ask size)
        is_sole_best_ask = (
            market.my_ask_order_is_best_ask and
            market.my_ask_order_size is not None and
            market.best_ask_size is not None and
            abs(market.my_ask_order_size - market.best_ask_size) < 0.01  # Allow small floating point differences
        )
        
        # If we're sole best ask and second best exists and gap is > price_improvement, move closer
        if is_sole_best_ask and market.second_best_ask_cents is not None:
            gap_to_second_best = market.best_ask_cents - market.second_best_ask_cents
            if gap_to_second_best > self.config.price_improvement:
                # Move to be price_improvement better than second best
                target_price_cents = market.second_best_ask_cents - self.config.price_improvement
                logger.info(
                    f"Trader {self.market_id}: Sole best ask, moving closer to second best "
                    f"(gap: {gap_to_second_best:.2f}¢ > {self.config.price_improvement:.2f}¢, "
                    f"target: {target_price_cents:.2f}¢)"
                )
            else:
                # Stay at best ask - price_improvement
                target_price_cents = market.best_ask_cents - self.config.price_improvement
        else:
            # Default: Be price_improvement better than best ask
            target_price_cents = market.best_ask_cents - self.config.price_improvement
        
        target_price_decimal = target_price_cents / 100.0  # Convert to decimal for API
        
        # Case 1: No open ask order
        if not market.my_ask_order_id:
            logger.info(f"Trader {self.market_id}: No ask order, creating at {target_price_cents:.2f}¢ with {inventory:.2f} shares")
            await self._place_order("SELL", target_price_decimal, inventory)
            return
        
        # Case 2: Have order AND it equals best_ask (or we're sole best ask)
        if market.my_ask_order_is_best_ask:
            # Check if we need to add more shares
            current_order_size = market.my_ask_order_size or 0.0
            if inventory > current_order_size:
                additional_shares = inventory - current_order_size
                logger.info(
                    f"Trader {self.market_id}: Ask order is best ask, adding {additional_shares:.2f} shares "
                    f"(current: {current_order_size:.2f}, inventory: {inventory:.2f})"
                )
                # Cancel and replace with new size (Polymarket doesn't support in-place updates)
                await self._replace_order(market.my_ask_order_id, "SELL", target_price_decimal, inventory, market)
            # Check if price needs to be updated (if we're sole best and should move closer)
            elif is_sole_best_ask and market.second_best_ask_cents is not None:
                gap_to_second_best = market.best_ask_cents - market.second_best_ask_cents
                if gap_to_second_best > self.config.price_improvement:
                    # Price should be updated to move closer to second best
                    new_target_price_cents = market.second_best_ask_cents - self.config.price_improvement
                    if abs(new_target_price_cents - market.my_ask_order_price_cents) > 0.01:  # Price changed
                        logger.info(
                            f"Trader {self.market_id}: Updating ask order price to move closer to second best "
                            f"(from {market.my_ask_order_price_cents:.2f}¢ to {new_target_price_cents:.2f}¢)"
                        )
                        await self._replace_order(market.my_ask_order_id, "SELL", new_target_price_cents / 100.0, inventory, market)
            # If inventory <= current_order_size and price is correct, keep order as is
            return
        
        # Case 3: Have order AND it's below best_ask
        logger.info(
            f"Trader {self.market_id}: Ask order at {market.my_ask_order_price_cents:.2f}¢ is below best ask "
            f"{market.best_ask_cents:.2f}¢, replacing"
        )
        await self._replace_order(market.my_ask_order_id, "SELL", target_price_decimal, inventory, market)
    
    async def _handle_buy_logic(self, market: MarketState, balance: float, spread_cents: float) -> None:
        """BUY logic: Be best bid only if spread condition is met.
        
        Spread condition: (best_ask - best_bid - price_improvement) >= spread_threshold
        
        Strategy:
        - If spread condition NOT met → cancel existing buy order (don't replace)
        - If spread condition met:
          - No open bid order → create at (best_bid + price_improvement) with balance shares
          - Have order AND it's best bid → add shares if balance > order.size
          - Have order AND it's NOT best bid → cancel + create new at (best_bid + price_improvement)
        """
        if not market.best_bid_cents:
            return
        
        # Check spread condition
        # Condition: (best_ask - best_bid - price_improvement) >= spread_threshold
        effective_spread = spread_cents - self.config.price_improvement
        spread_condition_met = effective_spread >= self.config.spread_threshold
        
        if not spread_condition_met:
            # Spread condition NOT met - cancel existing buy order if any
            if market.my_bid_order_id:
                logger.info(
                    f"Trader {self.market_id}: Spread condition not met "
                    f"(effective_spread: {effective_spread:.2f}¢ < threshold: {self.config.spread_threshold:.2f}¢), "
                    f"cancelling buy order"
                )
                try:
                    await self.execution.cancel(market.my_bid_order_id)
                except Exception as e:
                    logger.error(f"Trader {self.market_id}: Failed to cancel buy order: {e}")
            return
        
        # Spread condition met - proceed with buy logic
        if balance <= 0:
            # No balance to buy - cancel existing order if any
            if market.my_bid_order_id:
                logger.info(f"Trader {self.market_id}: No balance to buy, cancelling bid order")
                try:
                    await self.execution.cancel(market.my_bid_order_id)
                except Exception as e:
                    logger.error(f"Trader {self.market_id}: Failed to cancel bid order: {e}")
            return
        
        min_order_size = market.min_order_size or MIN_ORDER_SIZE
        if balance < min_order_size:
            logger.debug(f"Trader {self.market_id}: Balance {balance:.2f} < min order size {min_order_size:.0f}")
            return
        
        # Calculate target price
        # Strategy: Be price_improvement better than second best bid if we're sole best bid and gap is wide enough
        # Otherwise: Be price_improvement better than best bid
        
        # Check if we're the sole best bid (our order size equals best bid size)
        is_sole_best_bid = (
            market.my_bid_order_is_best_bid and
            market.my_bid_order_size is not None and
            market.best_bid_size is not None and
            abs(market.my_bid_order_size - market.best_bid_size) < 0.01  # Allow small floating point differences
        )
        
        # If we're sole best bid and second best exists and gap is > price_improvement, move closer
        if is_sole_best_bid and market.second_best_bid_cents is not None:
            gap_to_second_best = market.best_bid_cents - market.second_best_bid_cents  # Best bid is higher than second best
            if gap_to_second_best > self.config.price_improvement:
                # Move to be price_improvement better than second best
                target_price_cents = market.second_best_bid_cents + self.config.price_improvement
                logger.info(
                    f"Trader {self.market_id}: Sole best bid, moving closer to second best "
                    f"(gap: {gap_to_second_best:.2f}¢ > {self.config.price_improvement:.2f}¢, "
                    f"target: {target_price_cents:.2f}¢)"
                )
            else:
                # Stay at best bid + price_improvement
                target_price_cents = market.best_bid_cents + self.config.price_improvement
        else:
            # Default: Be price_improvement better than best bid
            target_price_cents = market.best_bid_cents + self.config.price_improvement
        
        target_price_decimal = target_price_cents / 100.0  # Convert to decimal for API
        
        # Case 1: No open bid order
        if not market.my_bid_order_id:
            logger.info(
                f"Trader {self.market_id}: No bid order, creating at {target_price_cents:.2f}¢ "
                f"with {balance:.2f} shares (spread condition met: {effective_spread:.2f}¢ >= {self.config.spread_threshold:.2f}¢)"
            )
            await self._place_order("BUY", target_price_decimal, balance)
            return
        
        # Case 2: Have order AND it's best bid (or we're sole best bid)
        if market.my_bid_order_is_best_bid:
            # Check if we need to add more shares
            current_order_size = market.my_bid_order_size or 0.0
            if balance > current_order_size:
                additional_shares = balance - current_order_size
                logger.info(
                    f"Trader {self.market_id}: Bid order is best bid, adding {additional_shares:.2f} shares "
                    f"(current: {current_order_size:.2f}, balance: {balance:.2f})"
                )
                # Cancel and replace with new size
                await self._replace_order(market.my_bid_order_id, "BUY", target_price_decimal, balance, market)
            # Check if price needs to be updated (if we're sole best and should move closer)
            elif is_sole_best_bid and market.second_best_bid_cents is not None:
                gap_to_second_best = market.best_bid_cents - market.second_best_bid_cents  # Best bid is higher than second best
                if gap_to_second_best > self.config.price_improvement:
                    # Price should be updated to move closer to second best
                    new_target_price_cents = market.second_best_bid_cents + self.config.price_improvement
                    if abs(new_target_price_cents - market.my_bid_order_price_cents) > 0.01:  # Price changed
                        logger.info(
                            f"Trader {self.market_id}: Updating bid order price to move closer to second best "
                            f"(from {market.my_bid_order_price_cents:.2f}¢ to {new_target_price_cents:.2f}¢)"
                        )
                        await self._replace_order(market.my_bid_order_id, "BUY", new_target_price_cents / 100.0, balance, market)
            # If balance <= current_order_size and price is correct, keep order as is
            return
        
        # Case 3: Have order AND it's NOT best bid
        logger.info(
            f"Trader {self.market_id}: Bid order at {market.my_bid_order_price_cents:.2f}¢ is not best bid "
            f"{market.best_bid_cents:.2f}¢, replacing"
        )
        await self._replace_order(market.my_bid_order_id, "BUY", target_price_decimal, balance, market)
    
    async def _place_order(self, side: str, price: float, size: float) -> None:
        """Place a limit order.
        
        Args:
            side: "BUY" or "SELL"
            price: Price in decimal (e.g., 0.50 for 50 cents)
            size: Size in shares
        """
        try:
            order_id = await self.execution.submit_limit(
                side=side, price=price, size=size, token_id=self.token_id
            )
            logger.info(
                f"Trader {self.market_id}: Placed {side} order {order_id[:20]}... "
                f"({size:.2f} shares @ {price:.4f} = {price*100:.2f}¢)"
            )
        except Exception as e:
            logger.error(f"Trader {self.market_id} failed to place {side} order: {e}")
    
    async def _replace_order(self, old_order_id: str, side: str, new_price: float, new_size: float, market: MarketState) -> None:
        """Replace an order by cancelling old and placing new.
        
        Args:
            old_order_id: ID of order to cancel
            side: "BUY" or "SELL"
            new_price: New price in decimal
            new_size: New size in shares
            market: Current market state (for validation)
        """
        try:
            # Cancel old order
            await self.execution.cancel(old_order_id)
            logger.info(f"Trader {self.market_id}: Cancelled {side} order {old_order_id[:20]}...")
            
            # Small delay to ensure cancellation is processed (especially for SELL orders)
            if side == "SELL":
                await asyncio.sleep(0.1)
            
            # Place new order
            await self._place_order(side, new_price, new_size)
            
        except Exception as e:
            logger.error(f"Trader {self.market_id} failed to replace {side} order {old_order_id[:20]}...: {e}")
    
    def pause(self) -> None:
        """Pause the trader."""
        self.is_paused = True
        logger.info(f"Trader {self.market_id} paused")
    
    def resume(self) -> None:
        """Resume the trader."""
        self.is_paused = False
        logger.info(f"Trader {self.market_id} resumed")
    
    def stop(self) -> None:
        """Stop the trader."""
        self.is_active = False
        logger.info(f"Trader {self.market_id} stopped")
    
    async def get_status(self) -> Dict:
        """Get current trader status for monitoring.
        
        Fetches fresh data from Polymarket since we don't track state internally.
        """
        try:
            # Fetch fresh market state
            market = await self._fetch_market_state()
            
            # Calculate derived values
            balance = self.config.max_inventory - market.current_inventory
            spread_cents = market.best_ask_cents - market.best_bid_cents if (market.best_ask_cents and market.best_bid_cents) else None
            spread_decimal = spread_cents / 100.0 if spread_cents else None
            
            # Count active orders
            active_orders = 0
            order_details = []
            if market.my_bid_order_id:
                active_orders += 1
                order_details.append({
                    "id": market.my_bid_order_id,
                    "side": "BUY",
                    "price": market.my_bid_order_price_cents / 100.0 if market.my_bid_order_price_cents else 0.0,
                    "size": market.my_bid_order_size or 0.0,
                })
            if market.my_ask_order_id:
                active_orders += 1
                order_details.append({
                    "id": market.my_ask_order_id,
                    "side": "SELL",
                    "price": market.my_ask_order_price_cents / 100.0 if market.my_ask_order_price_cents else 0.0,
                    "size": market.my_ask_order_size or 0.0,
                })
            
            # Calculate position value
            position_value = abs(market.current_inventory * (market.best_bid_cents / 100.0 if market.best_bid_cents else 0.0))
            
            return {
                "name": self.config.name,
                "market_id": self.market_id,
                "market_slug": self.config.market_slug or self.market_id[:20] + "...",
                "position": market.current_inventory,
                "position_value": position_value,
                "active_orders": active_orders,
                "order_details": order_details,
                "best_bid": market.best_bid_cents / 100.0 if market.best_bid_cents else None,
                "best_ask": market.best_ask_cents / 100.0 if market.best_ask_cents else None,
                "spread": spread_decimal,
                "spread_cents": spread_cents,
                "spread_pct": (spread_cents / market.best_bid_cents * 100) if (spread_cents and market.best_bid_cents) else None,
                "total_pnl": self.total_pnl,
                "total_trades": self.total_trades,
                "is_paused": self.is_paused,
                "is_active": self.is_active,
                "max_inventory": self.config.max_inventory,
                "balance": balance,
                "spread_threshold": self.config.spread_threshold,
                "min_order_size": market.min_order_size,
                "price_improvement": self.config.price_improvement,
            }
        except Exception as e:
            logger.warning(f"Trader {self.market_id} failed to get status: {e}")
            # Return basic info on error
            return {
                "name": self.config.name,
                "market_id": self.market_id,
                "market_slug": self.config.market_slug or self.market_id[:20] + "...",
                "is_paused": self.is_paused,
                "is_active": self.is_active,
                "error": str(e),
            }