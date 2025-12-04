# Architecture Overview

## Codebase Structure

```
/
├── services/                    # External service integrations
│   ├── __init__.py
│   └── supabase_service.py     # Supabase database service
│
├── trading/                    # Core trading logic
│   ├── core/                   # Core trading components
│   │   ├── trader.py          # Trader agent
│   │   ├── manager.py          # Manager coordinating traders
│   │   └── execution.py        # Execution layer
│   └── utils/                  # Trading utilities
│       └── market_resolver.py  # Market slug resolver
│
├── scripts/                    # Entry points
│   └── main.py                # CLI entry point
│
├── config.py                   # Configuration management
├── .env                        # Environment variables
└── requirements.txt            # Dependencies
```

## Data Flow

### Startup Flow

```
1. Load config from .env
   ↓
2. Initialize SupabaseService
   ↓
3. Load traders from Supabase
   ↓
4. Create Trader objects from configs
   ↓
5. Add traders to TraderManager
   ↓
6. Start trading loop
```

### Creating a Trader

```
Trader added to Supabase (via SQL/API)
   ↓
Bot loads trader on startup
   ↓
Trader starts trading automatically
```

### Deleting a Trader

```
Trader marked inactive in Supabase
   ↓
Bot stops trader on next restart
   ↓
Or remove trader from database entirely
```

## Service Layer

### SupabaseService

**Location**: `services/supabase_service.py`

**Responsibilities**:
- Read traders from Supabase
- Write traders to Supabase
- Update trader state (pause/resume)
- Delete traders (soft delete)

**Methods**:
- `load_all_traders()` → List[TraderConfig]
- `load_trader(market_id)` → Optional[TraderConfig]
- `save_trader(config)` → bool
- `delete_trader(market_id)` → bool
- `update_trader_pause_state(market_id, is_paused)` → bool

**Key Features**:
- Synchronous operations (no async complexity)
- Graceful degradation if Supabase unavailable
- Clean separation of concerns

## Trading Layer

### TraderManager

**Location**: `trading/core/manager.py`

**Responsibilities**:
- Manage multiple Trader instances
- Coordinate trading operations
- Monitor global risk
- Sync state to Supabase

**Integration**:
- Accepts `SupabaseService` in constructor
- Syncs trader operations to Supabase automatically
- Non-blocking: DB operations don't slow trading

## Benefits of This Structure

1. **Clean Separation**: Services layer separate from business logic
2. **Testability**: Easy to mock SupabaseService for testing
3. **Maintainability**: Clear responsibilities for each component
4. **Scalability**: Easy to add more services (Redis, etc.)
5. **Resilience**: Works even if Supabase is down

## Database Schema

See `docs/supabase_migration.sql` for the complete schema.

Key table: `traders`
- Stores all trader configurations
- Soft delete via `is_active` flag
- Tracks pause state
- Auto-updates `updated_at` timestamp

