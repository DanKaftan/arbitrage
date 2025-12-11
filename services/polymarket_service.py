"""Polymarket service wrapping py_clob_client for Polymarket operations."""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple
from decimal import Decimal, ROUND_DOWN
import aiohttp

logger = logging.getLogger(__name__)

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, TradeParams
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError as e:
    # Fallback if py_clob_client is not available
    logger.warning(f"py_clob_client import failed: {e}")
    ClobClient = None
    OrderArgs = None
    OrderType = None
    TradeParams = None
    BUY = None
    SELL = None
    CLOB_AVAILABLE = False

from config import ExecutionConfig


class PolymarketServiceError(Exception):
    """Exception raised by Polymarket service."""
    pass


class PolymarketService:
    """Service for interacting with Polymarket API via py_clob_client."""
    
    def __init__(self, config: ExecutionConfig):
        """Initialize Polymarket service with config."""
        self.config = config
        self.client: Optional[ClobClient] = None
        self._initialize_client()
        self._latency_tracker: Dict[str, float] = {}  # order_id -> submission_time
        
    def _initialize_client(self):
        """Initialize the CLOB client."""
        if not CLOB_AVAILABLE or ClobClient is None:
            logger.error("py_clob_client not available. Cannot use real trading mode!")
            logger.error("Please install: pip install py-clob-client")
            self.client = None
            return
            
        try:
            # Parse API base URL - use full URL with scheme (matches working implementation)
            api_base = self.config.api_base_url
            if not api_base.startswith("http://") and not api_base.startswith("https://"):
                api_base = f"https://{api_base}"
            host = api_base.rstrip('/')
            
            # Strip quotes from private key if present
            private_key = None
            if self.config.private_key:
                private_key = self.config.private_key.strip('"').strip("'")
            
            if not private_key:
                logger.warning("⚠️ No private key configured - order submission will fail")
                logger.warning("⚠️ Client will still initialize for read operations (orderbook)")
            
            # Initialize ClobClient (matches working implementation)
            # Note: API key/secret are NOT required for initialization
            # They are derived from the private key using create_or_derive_api_creds()
            client_kwargs = {
                "host": host,  # Use full URL with scheme (e.g., "https://clob.polymarket.com")
                "key": private_key,  # Wallet private key (required for order submission)
                "chain_id": self.config.chain_id,
                "signature_type": 1,  # Required for Polymarket
            }
            
            # Add funder (proxy_address) if available (for Email/Magic accounts)
            if hasattr(self.config, 'wallet_address') and self.config.wallet_address:
                wallet_address = self.config.wallet_address.strip('"').strip("'")
                client_kwargs["funder"] = wallet_address
                logger.info(f"✅ Using proxy address (funder): {wallet_address[:10]}...")
            
            self.client = ClobClient(**client_kwargs)
            
            # Set API credentials (derived from private key - matches working implementation)
            # This creates or derives API credentials for authentication
            try:
                api_creds = self.client.create_or_derive_api_creds()
                if api_creds:
                    self.client.set_api_creds(api_creds)
                    logger.info("✅ API credentials derived and set successfully")
                else:
                    logger.warning("⚠️ Could not derive API credentials - order submission may fail")
            except Exception as creds_error:
                # API credentials derivation might fail, but orderbook should still work
                logger.warning(f"⚠️ Failed to derive API credentials: {creds_error}")
                logger.warning("⚠️ Order submission may fail, but read operations (orderbook) should still work")
                logger.debug(f"API credentials error details: {type(creds_error).__name__}: {creds_error}")
            
            logger.info(f"✅ Polymarket service initialized successfully - REAL TRADING MODE (host: {host})")
        except Exception as e:
            logger.error(f"❌ Failed to initialize CLOB client: {e}")
            logger.error("Falling back to mock mode. Check your API credentials and private key.")
            import traceback
            logger.error(traceback.format_exc())
            self.client = None
    
    def _round_price(self, price: float) -> float:
        """Round price to valid Polymarket tick size."""
        # Polymarket typically uses 0.01 tick size (1 cent)
        # Adjust based on actual requirements
        tick_size = 0.01
        return round(price / tick_size) * tick_size
    
    def _round_size(self, size: float) -> float:
        """Round size to valid precision."""
        return round(size, self.config.size_precision)
    
    async def _retry_operation(self, operation, *args, **kwargs):
        """Retry an operation with exponential backoff."""
        last_exception = None
        for attempt in range(self.config.max_retries):
            try:
                return await operation(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt < self.config.max_retries - 1:
                    delay = self.config.retry_delay_seconds * (2 ** attempt)
                    logger.warning(
                        f"Operation failed (attempt {attempt + 1}/{self.config.max_retries}): {e}. "
                        f"Retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Operation failed after {self.config.max_retries} attempts: {e}")
        
        raise PolymarketServiceError(f"Operation failed: {last_exception}")
    
    async def get_orderbook(self, token_id: str) -> Dict:
        """Fetch orderbook for a token.
        
        Args:
            token_id: Token ID (not condition ID) - required by Polymarket API
        """
        if self.client is None:
            # Mock response for testing
            logger.warning(f"Using MOCK orderbook data for token {token_id}")
            return {
                "bids": [{"price": "0.50", "size": "100"}],
                "asks": [{"price": "0.51", "size": "100"}],
            }
        
        async def _fetch():
            # Use asyncio.to_thread for cleaner async handling (matches working implementation)
            # Note: get_order_book doesn't require API credentials, just a valid client
            try:
                return await asyncio.to_thread(self.client.get_order_book, token_id)
            except Exception as e:
                # Log more details about the error
                logger.error(f"get_order_book failed for token {token_id[:20]}...: {type(e).__name__}: {e}")
                raise
        
        try:
            orderbook_obj = await self._retry_operation(_fetch)
            
            # Convert OrderBookSummary to dict format expected by trader
            # OrderBookSummary has bids/asks as lists of OrderSummary objects
            orderbook = {
                "bids": [],
                "asks": [],
                "min_order_size": None,  # Market-specific minimum order size
            }
            
            # Extract min_order_size from OrderBookSummary if available
            if hasattr(orderbook_obj, 'min_order_size') and orderbook_obj.min_order_size:
                try:
                    orderbook["min_order_size"] = float(orderbook_obj.min_order_size)
                except (ValueError, TypeError):
                    pass  # Keep as None if can't parse
            
            if orderbook_obj.bids:
                for bid in orderbook_obj.bids:
                    # OrderSummary has 'price' and 'size' attributes
                    orderbook["bids"].append({
                        "price": str(bid.price),
                        "size": str(bid.size),
                    })
            
            if orderbook_obj.asks:
                for ask in orderbook_obj.asks:
                    orderbook["asks"].append({
                        "price": str(ask.price),
                        "size": str(ask.size),
                    })
            
            # Log for debugging
            # Note: Orderbook is sorted reverse - best bid/ask are LAST elements
            if orderbook["bids"]:
                best_bid = float(orderbook['bids'][-1]['price'])  # Last element = highest bid
                best_ask = float(orderbook['asks'][-1]['price']) if orderbook['asks'] else None  # Last element = lowest ask
                spread = best_ask - best_bid if best_ask else None
                if best_ask:
                    logger.info(
                        f"Token {token_id[:20]}... - Best Bid: {best_bid*100:.0f} cents, "
                        f"Best Ask: {best_ask*100:.0f} cents, "
                        f"Spread: {spread*100:.0f} cents"
                    )
                else:
                    logger.info(f"Token {token_id[:20]}... - Best Bid: {best_bid*100:.0f} cents, No asks")
            elif orderbook["asks"]:
                best_ask = float(orderbook['asks'][-1]['price'])
                logger.info(f"Token {token_id[:20]}... - Best Ask: {best_ask*100:.0f} cents, No bids")
            else:
                logger.warning(f"Token {token_id[:20]}... - Empty orderbook")
            
            return orderbook
        except Exception as e:
            logger.error(f"Failed to fetch orderbook for token {token_id}: {e}")
            raise PolymarketServiceError(f"Orderbook fetch failed: {e}")
    
    async def submit_limit(
        self,
        side: str,  # "BUY" or "SELL"
        price: float,
        size: float,
        token_id: str,  # Token ID (not condition ID)
    ) -> str:
        """Submit a limit order and return order ID."""
        if self.client is None:
            # Mock order ID for testing
            order_id = f"mock_{int(time.time() * 1000)}"
            logger.info(f"Mock order: {side} {size} @ {price} for token {token_id}")
            return order_id
        
        rounded_price = self._round_price(price)
        rounded_size = self._round_size(size)
        
        async def _submit():
            # Use the working pattern: create_order + post_order (separate steps)
            # This matches the successful implementation
            
            if not CLOB_AVAILABLE or OrderArgs is None or OrderType is None or BUY is None or SELL is None:
                raise PolymarketServiceError("py_clob_client not properly imported")
            
            # Determine side constant (BUY or SELL from py_clob_client)
            order_side = BUY if side.upper() == "BUY" else SELL
            
            # Build order args
            order_args = OrderArgs(
                price=rounded_price,
                size=rounded_size,
                side=order_side,
                token_id=token_id
            )
            
            # Step 1: Create and sign order
            signed_order = await asyncio.to_thread(self.client.create_order, order_args)
            
            # Step 2: Post order as GTC (Good-Till-Cancelled)
            resp = await asyncio.to_thread(self.client.post_order, signed_order, OrderType.GTC)
            
            return resp
        
        try:
            submission_time = time.time()
            result = await self._retry_operation(_submit)
            
            # Extract order ID from result
            # post_order returns a dict with orderID field (capital ID)
            if isinstance(result, dict):
                # Try common order ID field names (orderID with capital ID is what Polymarket uses)
                order_id = (
                    result.get("orderID")  # Capital ID - this is what Polymarket actually returns
                    or result.get("orderId")  # camelCase variant
                    or result.get("order_id")  # snake_case variant
                    or result.get("id") 
                    or result.get("hash")
                    or result.get("orderHash")
                    or result.get("order_hash")
                )
            elif hasattr(result, "__dict__"):
                # If it's an object with attributes
                order_id = (
                    getattr(result, "orderID", None)  # Capital ID - this is what Polymarket actually returns
                    or getattr(result, "orderId", None)  # camelCase variant
                    or getattr(result, "order_id", None) 
                    or getattr(result, "id", None) 
                    or getattr(result, "hash", None)
                    or getattr(result, "orderHash", None)
                )
            else:
                order_id = None
            
            if not order_id:
                # Last resort: use a hash of the order data
                import hashlib
                order_str = f"{token_id}_{side}_{rounded_price}_{rounded_size}_{submission_time}"
                order_id = hashlib.sha256(order_str.encode()).hexdigest()[:16]
                logger.warning(
                    f"Could not extract order_id from result type {type(result)}, "
                    f"using hash: {order_id}. Result: {result}"
                )
            
            self._latency_tracker[order_id] = submission_time
            
            logger.info(
                f"Submitted {side} order: {order_id} - {rounded_size} @ {rounded_price} "
                f"for token {token_id}"
            )
            return str(order_id)
        except Exception as e:
            logger.error(f"Failed to submit {side} order: {e}")
            raise PolymarketServiceError(f"Order submission failed: {e}")
    
    async def get_order_status(self, order_id: str) -> Dict:
        """Get status of an order."""
        if self.client is None:
            # Mock status
            return {
                "order_id": order_id,
                "status": "OPEN",
                "filled_size": "0",
                "remaining_size": "100",
            }
        
        async def _fetch():
            # Use asyncio.to_thread for cleaner async handling
            return await asyncio.to_thread(self.client.get_order, order_id)
        
        try:
            status = await self._retry_operation(_fetch)
            
            # Handle None response (order might not exist or API error)
            if status is None:
                logger.warning(f"Order {order_id} not found or API returned None")
                return {
                    "order_id": order_id,
                    "status": "UNKNOWN",
                    "filled_size": "0",
                    "remaining_size": "0",
                }
            
            # Ensure status is a dict-like object
            if not isinstance(status, dict):
                # Convert to dict if it's an object with attributes
                if hasattr(status, "__dict__"):
                    status = status.__dict__
                else:
                    logger.warning(f"Order {order_id} status is not a dict: {type(status)}")
                    return {
                        "order_id": order_id,
                        "status": "UNKNOWN",
                        "filled_size": "0",
                        "remaining_size": "0",
                    }
            
            # Track latency if order is filled
            if order_id in self._latency_tracker and status.get("status") == "FILLED":
                latency = time.time() - self._latency_tracker[order_id]
                logger.info(f"Order {order_id} filled in {latency:.3f}s")
                del self._latency_tracker[order_id]
            
            return status
        except Exception as e:
            logger.error(f"Failed to get order status for {order_id}: {e}")
            raise PolymarketServiceError(f"Order status fetch failed: {e}")
    
    async def cancel(self, order_id: str) -> bool:
        """Cancel an order."""
        if self.client is None:
            logger.info(f"Mock cancel for order {order_id}")
            return True
        
        async def _cancel():
            # Use asyncio.to_thread for cleaner async handling
            return await asyncio.to_thread(self.client.cancel, order_id)
        
        try:
            result = await self._retry_operation(_cancel)
            logger.info(f"Cancelled order {order_id}")
            
            # Clean up latency tracker
            if order_id in self._latency_tracker:
                del self._latency_tracker[order_id]
            
            return result
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            raise PolymarketServiceError(f"Order cancellation failed: {e}")
    
    async def get_market_position(self, token_id: str) -> float:
        """Get position size for a specific token directly from Polymarket Data API.
        
        Uses the Polymarket Data API /positions endpoint to get the actual position
        for the token. This is more accurate and faster than calculating from trades.
        
        Args:
            token_id: Token ID to check position for
            
        Returns:
            Position size (positive = long, negative = short, 0 = flat)
        """
        if self.client is None:
            logger.debug(f"Position check for token {token_id} - client not available, returning 0.0")
            return 0.0
        
        try:
            # Get wallet address from client or config
            # For Polymarket, we might need the proxy wallet (funder) if using email/magic accounts
            wallet_address = None
            
            # Try to get proxy wallet (funder) first - this is what Polymarket uses for positions
            if hasattr(self.config, 'wallet_address') and self.config.wallet_address:
                wallet_address = self.config.wallet_address.strip('"').strip("'")
                logger.debug(f"Using wallet address from config: {wallet_address[:10]}...")
            
            # Fallback to client's address
            if not wallet_address and hasattr(self.client, 'get_address'):
                try:
                    wallet_address = await asyncio.to_thread(self.client.get_address)
                    logger.debug(f"Using wallet address from client: {wallet_address[:10]}...")
                except Exception as e:
                    logger.debug(f"Could not get address from client: {e}")
            
            if not wallet_address:
                logger.warning("Cannot get position: wallet address not available from client or config")
                return 0.0
            
            # Use Polymarket Data API to get positions directly
            async def _fetch_position():
                url = "https://data-api.polymarket.com/positions"
                params = {
                    "user": wallet_address,
                    "sizeThreshold": 0.0  # Get all positions, even small ones
                }
                
                logger.debug(f"Fetching positions from API for wallet {wallet_address[:10]}... and token {token_id[:20]}...")
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as response:
                        response.raise_for_status()
                        positions = await response.json()
                        logger.debug(f"Received {len(positions)} positions from API")
                        return positions
            
            positions = await self._retry_operation(_fetch_position)
            
            if not positions:
                logger.debug(f"No positions returned from API for wallet {wallet_address[:10]}...")
                return 0.0
            
            # Log positions for debugging (only if we have many positions or if debug is enabled)
            if len(positions) > 0:
                logger.debug(f"Searching for token {token_id[:20]}... in {len(positions)} positions")
                # Only log positions if debug level is enabled (to reduce noise)
                if logger.isEnabledFor(logging.DEBUG):
                    for pos in positions[:3]:  # Log first 3 for debugging
                        logger.debug(f"  Position asset: {pos.get('asset', 'N/A')[:20]}..., size: {pos.get('size', 'N/A')}")
            
            # Find position for this specific token
            # The asset field should match the token_id (token contract address)
            for position in positions:
                asset = position.get("asset", "")
                # Normalize both to lowercase for comparison (addresses are case-insensitive)
                asset_normalized = asset.lower().strip()
                token_id_normalized = token_id.lower().strip()
                
                # Check if this position matches our token_id
                if asset_normalized == token_id_normalized:
                    size = position.get("size", 0.0)
                    try:
                        position_size = float(size)
                        logger.info(f"✅ Found position for token {token_id[:20]}...: {position_size:.2f} shares")
                        return position_size
                    except (ValueError, TypeError):
                        logger.warning(f"Could not parse position size: {size}")
                        continue
            
            # Token not found in positions - this is normal if there's no position
            # Only log as debug (not warning) since having no position is a valid state
            logger.debug(
                f"Token {token_id[:20]}... not found in positions (position = 0). "
                f"Available assets: {[p.get('asset', '')[:20] for p in positions[:5]]}"
            )
            return 0.0
            
        except Exception as e:
            logger.warning(f"Failed to get position for token {token_id} from API: {e}")
            # Fall back to 0.0 - trader will rely on self-tracking
        return 0.0
    
    async def get_my_open_orders(self, token_id: str) -> List[Dict]:
        """Get my open orders for a specific token.
        
        Uses Polymarket's get_orders API to fetch all open orders and filters by token_id.
        
        Args:
            token_id: Token ID to filter orders for
            
        Returns:
            List of order dicts with: id, side, price, size, etc.
        """
        if self.client is None:
            logger.debug(f"get_my_open_orders for token {token_id} - client not available, returning empty list")
            return []
        
        try:
            from py_clob_client.clob_types import OpenOrderParams
            
            async def _fetch():
                # Fetch all open orders
                open_orders = await asyncio.to_thread(self.client.get_orders, OpenOrderParams())
                
                # Convert to list of dicts if needed
                orders_list = []
                if open_orders:
                    for order in open_orders:
                        # Convert order object to dict if needed
                        if not isinstance(order, dict):
                            if hasattr(order, "__dict__"):
                                order = order.__dict__
                            else:
                                # Try to extract common fields
                                order = {
                                    "id": getattr(order, "id", None) or getattr(order, "orderID", None),
                                    "token_id": getattr(order, "token_id", None) or getattr(order, "tokenId", None),
                                    "side": getattr(order, "side", None),
                                    "price": getattr(order, "price", None),
                                    "size": getattr(order, "size", None) or getattr(order, "original_size", None),
                                }
                        
                        # Filter by token_id
                        order_token_id = (
                            order.get("token_id") or 
                            order.get("tokenId") or 
                            order.get("token")
                        )
                        
                        # Normalize token IDs for comparison (case-insensitive)
                        if order_token_id and token_id:
                            if order_token_id.lower().strip() == token_id.lower().strip():
                                orders_list.append(order)
                
                return orders_list
            
            orders = await self._retry_operation(_fetch)
            logger.debug(f"Found {len(orders)} open orders for token {token_id[:20]}...")
            return orders
            
        except Exception as e:
            logger.warning(f"Failed to get my open orders for token {token_id}: {e}")
            return []
