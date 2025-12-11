# Trader Logic Documentation

## Overview

The trader implements a **market-making strategy** for a single Polymarket market. It uses **Polymarket as the single source of truth** - all trading decisions are made based on real-time data queried directly from Polymarket's API. There is no internal state tracking or caching.

### Design Philosophy

1. **Ground Truth = Polymarket**: Every trading step queries Polymarket for fresh data (orderbook, position, orders)
2. **No Internal State**: We don't track position or orders internally - Polymarket is the authoritative source
3. **Two-Sided Market Making**: The trader can simultaneously maintain both BUY and SELL orders
4. **Inventory-Based**: Trading is based on share inventory limits, not dollar budgets

---

## Configuration

Each trader is configured with:

- **`max_inventory`**: Maximum number of shares the trader can hold at any time
- **`spread_threshold`**: Minimum spread (in cents) required to place BUY orders
- **`price_improvement`**: Amount (in cents) to improve over the best bid/ask to get priority

### Example Configuration

```python
TraderConfig(
    market_id="0x123...",
    token_id="0xabc...",
    max_inventory=100.0,      # Can hold up to 100 shares
    spread_threshold=1.0,      # Need at least 1¢ spread to buy
    price_improvement=1.0,     # Place orders 1¢ better than best bid/ask
)
```

---

## MarketState: The Ground Truth

Every trading step starts by fetching a fresh `MarketState` from Polymarket. This contains all the data needed for trading decisions:

```python
@dataclass
class MarketState:
    # From orderbook
    best_bid_cents: float          # Best bid price in cents
    best_ask_cents: float          # Best ask price in cents
    best_bid_size: float           # Size at best bid
    best_ask_size: float           # Size at best ask
    second_best_bid_cents: float   # Second best bid price in cents
    second_best_ask_cents: float   # Second best ask price in cents
    min_order_size: float          # Market minimum order size
    
    # From position API
    current_inventory: float        # Current shares owned (from Polymarket)
    
    # From my open orders API
    my_bid_order_id: str           # Our active BUY order ID (if any)
    my_bid_order_price_cents: float # Our BUY order price in cents
    my_bid_order_size: float       # Our BUY order size
    my_bid_order_is_best_bid: bool # Is our order the best bid?
    
    my_ask_order_id: str           # Our active SELL order ID (if any)
    my_ask_order_price_cents: float # Our SELL order price in cents
    my_ask_order_size: float       # Our SELL order size
    my_ask_order_is_best_ask: bool # Is our order the best ask?
```

**Key Point**: This data is fetched fresh every step - no caching, no assumptions.

---

## Main Trading Loop

The `step()` method runs periodically and executes the following:

```
1. Fetch MarketState from Polymarket
   ├── Orderbook (best bid/ask, sizes)
   ├── Current position (inventory)
   └── My open orders (if any)

2. Calculate derived values
   ├── balance = max_inventory - current_inventory
   └── spread_cents = best_ask_cents - best_bid_cents

3. Execute SELL logic (always be best ask)

4. Execute BUY logic (be best bid if spread condition met)
```

---

## SELL Logic: Always Be the Best Ask

**Goal**: Always maintain the best ask order to maximize chances of selling.

**Strategy**: 
- Always propose to sell **ALL** inventory (read fresh from Polymarket)
- Default: Place orders at `best_ask - price_improvement` to be the best ask
- **Gap Closing**: If we're the **sole best ask** (only our order at that price) AND the gap to second best ask is > `price_improvement`, move closer to second best by placing at `second_best_ask - price_improvement`

### Decision Tree

```
IF inventory <= 0:
    → Cancel existing ask order (if any)
    → Return (nothing to sell)

IF inventory < min_order_size:
    → Return (can't place order below minimum)

Calculate target_price:
    IF we're sole best ask (our size == best_ask_size) AND second_best_ask exists:
        gap = best_ask - second_best_ask
        IF gap > price_improvement:
            → target_price = second_best_ask - price_improvement (move closer)
        ELSE:
            → target_price = best_ask - price_improvement (stay at best)
    ELSE:
        → target_price = best_ask - price_improvement (default)

IF no open ask order:
    → Place new SELL order at target_price with ALL inventory

ELSE IF our order IS the best ask:
    IF inventory > current_order_size:
        → Cancel + replace order with new size (all inventory) at target_price
    ELSE IF price needs update (sole best ask moving closer):
        → Cancel + replace order at new target_price
    ELSE:
        → Keep order as is (already selling all we have at correct price)

ELSE IF our order is BELOW best ask:
    → Cancel + replace order at target_price with ALL inventory
```

### Example Scenarios

**Scenario 1: No Ask Order, Have Inventory**
- Market: Best ask = 50¢
- Inventory: 25 shares
- Action: Place SELL order at 49¢ (50¢ - 1¢) for 25 shares

**Scenario 2: Order is Best Ask, Inventory Increased**
- Current order: 20 shares @ 49¢ (best ask)
- New inventory: 30 shares (from filled buy order)
- Action: Cancel + replace with 30 shares @ 49¢

**Scenario 3: Order Below Best Ask**
- Our order: 25 shares @ 48¢
- Market: Best ask = 50¢
- Action: Cancel + replace with 25 shares @ 49¢ (50¢ - 1¢)

**Scenario 4: Sole Best Ask, Gap Closing**
- Our order: 100 shares @ 50¢ (sole best ask - we're the only one)
- Market: Second best ask = 52¢
- Price improvement: 1¢
- Gap: `52 - 50 = 2¢ > 1¢` ✅ (gap is wide enough)
- Action: Move order to 51¢ (52¢ - 1¢) to close gap while staying best ask

---

## BUY Logic: Be Best Bid (If Spread Condition Met)

**Goal**: Be the best bid order, but only if the spread is wide enough to be profitable.

**Spread Condition**: 
```
effective_spread = (best_ask - best_bid - price_improvement)
IF effective_spread >= spread_threshold:
    → Place/maintain BUY order
ELSE:
    → Cancel BUY order (don't replace)
```

**Price Strategy**:
- Default: Place orders at `best_bid + price_improvement` to be the best bid
- **Gap Closing**: If we're the **sole best bid** (only our order at that price) AND the gap to second best bid is > `price_improvement`, move closer to second best by placing at `second_best_bid + price_improvement`

**Why this condition?**
- We buy at `best_bid + price_improvement`
- After placing our buy order, the effective spread becomes `best_ask - (best_bid + price_improvement)`
- We only want to buy if this remaining spread is >= `spread_threshold`

### Decision Tree

```
IF spread condition NOT met:
    → Cancel existing buy order (if any)
    → Return (spread too narrow)

IF balance <= 0:
    → Cancel existing buy order (if any)
    → Return (no capacity to buy)

IF balance < min_order_size:
    → Return (can't place order below minimum)

Calculate target_price:
    IF we're sole best bid (our size == best_bid_size) AND second_best_bid exists:
        gap = best_bid - second_best_bid
        IF gap > price_improvement:
            → target_price = second_best_bid + price_improvement (move closer)
        ELSE:
            → target_price = best_bid + price_improvement (stay at best)
    ELSE:
        → target_price = best_bid + price_improvement (default)

IF no open bid order:
    → Place new BUY order at target_price with balance shares

ELSE IF our order IS the best bid:
    IF balance > current_order_size:
        → Cancel + replace order with new size (all balance) at target_price
    ELSE IF price needs update (sole best bid moving closer):
        → Cancel + replace order at new target_price
    ELSE:
        → Keep order as is (already buying all we can at correct price)

ELSE IF our order is NOT the best bid:
    → Cancel + replace order at target_price with balance shares
```

### Example Scenarios

**Scenario 1: Spread Condition Met, No Bid Order**
- Market: Best bid = 45¢, Best ask = 50¢
- Spread threshold: 1¢
- Price improvement: 1¢
- Balance: 50 shares
- Calculation: `effective_spread = (50 - 45 - 1) = 4¢ >= 1¢` ✅
- Action: Place BUY order at 46¢ (45¢ + 1¢) for 50 shares

**Scenario 2: Spread Condition NOT Met**
- Market: Best bid = 45¢, Best ask = 46.5¢
- Spread threshold: 1¢
- Price improvement: 1¢
- Calculation: `effective_spread = (46.5 - 45 - 1) = 0.5¢ < 1¢` ❌
- Action: Cancel existing buy order (if any), don't place new one

**Scenario 3: Order is Best Bid, Balance Increased**
- Current order: 30 shares @ 46¢ (best bid)
- New balance: 50 shares (from sold inventory)
- Action: Cancel + replace with 50 shares @ 46¢

**Scenario 4: Order NOT Best Bid**
- Our order: 50 shares @ 45¢
- Market: Best bid = 46¢
- Action: Cancel + replace with 50 shares @ 47¢ (46¢ + 1¢)

**Scenario 5: Sole Best Bid, Gap Closing**
- Our order: 50 shares @ 45¢ (sole best bid - we're the only one)
- Market: Second best bid = 43¢
- Price improvement: 1¢
- Gap: `45 - 43 = 2¢ > 1¢` ✅
- Action: Move order to 44¢ (43¢ + 1¢) to close gap while staying best bid

---

## Key Concepts

### Price Improvement

**Purpose**: Get priority in the order book by placing orders slightly better than the current best bid/ask.

- **BUY orders**: Place at `best_bid + price_improvement` (e.g., if best bid is 45¢, place at 46¢)
- **SELL orders**: Place at `best_ask - price_improvement` (e.g., if best ask is 50¢, place at 49¢)

This ensures our orders are at the top of the book and more likely to fill.

**Gap Closing Strategy**: When we're the **sole best bid/ask** (no other traders at that price) and there's a wide gap to the second best price (> `price_improvement`), we move closer to the second best:

- **SELL**: If gap to second best ask is wide, place at `second_best_ask - price_improvement` instead of `best_ask - price_improvement`
- **BUY**: If gap to second best bid is wide, place at `second_best_bid + price_improvement` instead of `best_bid + price_improvement`

This strategy closes unnecessary gaps while maintaining our position as the best bid/ask.

### Balance Calculation

```
balance = max_inventory - current_inventory
```

- **Balance**: Remaining shares we can buy (buying capacity)
- **Current Inventory**: Shares we currently own (from Polymarket)
- **Max Inventory**: Maximum shares we're allowed to hold

Example:
- `max_inventory = 100`
- `current_inventory = 30` (we own 30 shares)
- `balance = 70` (we can buy 70 more shares)

### Spread Condition

The spread condition ensures we only place BUY orders when there's enough profit opportunity:

```
effective_spread = (best_ask - best_bid - price_improvement)
spread_condition_met = (effective_spread >= spread_threshold)
```

**Why subtract price_improvement?**
- We place our buy order at `best_bid + price_improvement`
- After our order is placed, the remaining spread is `best_ask - (best_bid + price_improvement)`
- We want this remaining spread to be at least `spread_threshold` to be profitable

**Example**:
- Best bid: 45¢
- Best ask: 50¢
- Price improvement: 1¢
- Spread threshold: 1¢
- Effective spread: `50 - 45 - 1 = 4¢` ✅ (condition met)

### Order Updates

Polymarket doesn't support in-place order size updates. To "add shares" to an existing order:

1. Cancel the old order
2. Wait briefly (0.1s for SELL orders to ensure position is unlocked)
3. Place a new order with the updated size at the same price

**Note**: This cancel+replace approach may cause temporary priority loss if another trader places an order at the same price during the cancellation window. This is a limitation of Polymarket's API which doesn't support in-place order amendments.

---

## Price Units

**Internal Storage**: All prices are stored and compared in **cents** for clarity and precision.

**API Calls**: Prices are converted to **decimal** (0.01 = 1 cent) when calling Polymarket API.

**Example**:
- Internal: `best_bid_cents = 50.0` (50 cents)
- API call: `price = 0.50` (decimal representation)

---

## Error Handling

The trader handles errors gracefully:

- **API failures**: Log warning, continue with next step
- **Missing data**: Skip step if orderbook is incomplete
- **Order failures**: Log error, retry on next step

The trader never crashes - it logs errors and continues operating.

---

## Status Reporting

The `get_status()` method fetches fresh data from Polymarket and returns:

- Current position (inventory)
- Active orders (BUY and/or SELL)
- Market prices (best bid/ask)
- Spread information
- Balance and capacity
- Trading statistics (P&L, trade count)

**Note**: Status is fetched fresh every time - no cached values.

---

## Example Trading Session

Let's trace through a complete trading session:

### Initial State
- `max_inventory = 100`
- `spread_threshold = 1¢`
- `price_improvement = 1¢`
- `current_inventory = 0` (flat)
- Market: Best bid = 45¢, Best ask = 50¢

### Step 1: First Trading Step

**MarketState fetched**:
- Best bid: 45¢, Best ask: 50¢
- Current inventory: 0 shares
- No open orders

**Calculations**:
- Balance: `100 - 0 = 100 shares`
- Spread: `50 - 45 = 5¢`
- Effective spread: `5 - 1 = 4¢ >= 1¢` ✅

**SELL Logic**: No inventory → no action

**BUY Logic**: 
- Spread condition met ✅
- No bid order → Place BUY at 46¢ (45¢ + 1¢) for 100 shares

**Result**: BUY order placed at 46¢ for 100 shares

### Step 2: After BUY Order Fills

**MarketState fetched**:
- Best bid: 46¢, Best ask: 50¢
- Current inventory: 100 shares (from filled buy)
- No open orders (buy order filled)

**Calculations**:
- Balance: `100 - 100 = 0 shares`
- Spread: `50 - 46 = 4¢`

**SELL Logic**:
- Inventory: 100 shares
- No ask order → Place SELL at 49¢ (50¢ - 1¢) for 100 shares

**BUY Logic**:
- Balance: 0 → Cancel any buy order (none exists)

**Result**: SELL order placed at 49¢ for 100 shares

### Step 3: After SELL Order Fills

**MarketState fetched**:
- Best bid: 46¢, Best ask: 49¢
- Current inventory: 0 shares (sold everything)
- No open orders

**Calculations**:
- Balance: `100 - 0 = 100 shares`
- Spread: `49 - 46 = 3¢`
- Effective spread: `3 - 1 = 2¢ >= 1¢` ✅

**SELL Logic**: No inventory → no action

**BUY Logic**:
- Spread condition met ✅
- No bid order → Place BUY at 47¢ (46¢ + 1¢) for 100 shares

**Result**: BUY order placed at 47¢ for 100 shares

### Step 4: Spread Narrows

**MarketState fetched**:
- Best bid: 47¢, Best ask: 48.5¢
- Current inventory: 0 shares
- BUY order: 100 shares @ 47¢

**Calculations**:
- Balance: 100 shares
- Spread: `48.5 - 47 = 1.5¢`
- Effective spread: `1.5 - 1 = 0.5¢ < 1¢` ❌

**SELL Logic**: No inventory → no action

**BUY Logic**:
- Spread condition NOT met ❌
- Cancel existing buy order

**Result**: BUY order cancelled (spread too narrow)

---

## Summary

The trader implements a simple but effective market-making strategy:

1. **SELL**: Always be the best ask, sell all inventory
2. **BUY**: Be the best bid, but only if spread is wide enough
3. **Ground Truth**: Always query Polymarket - no assumptions
4. **Inventory-Based**: Trading based on share limits, not dollar budgets
5. **Price Improvement**: Place orders slightly better than best bid/ask for priority

The strategy is designed to:
- Capture spreads when they're wide enough
- Maintain competitive prices (best bid/ask)
- Avoid trading when spreads are too narrow
- Always reflect the true state from Polymarket
