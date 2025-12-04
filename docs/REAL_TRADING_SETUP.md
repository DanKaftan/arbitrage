# Real Trading Setup

## Current Status

**⚠️ The bot is currently in MOCK MODE** - it will NOT send real orders to Polymarket.

## To Enable Real Trading

### Step 1: Upgrade Python (REQUIRED)

`py-clob-client` requires **Python >=3.9.10**. 

**Check your Python version:**
```bash
python --version
```

**If you have Python <3.9.10, you need to upgrade:**

**Option A: Use Python 3.10+ (Recommended)**
```bash
# Create new venv with Python 3.10+
python3.10 -m venv venv
# or
python3.11 -m venv venv

source venv/bin/activate
pip install -r requirements.txt
```

**Option B: Upgrade Python 3.9 to 3.9.10+**
- Use pyenv or similar tool to install Python 3.9.10+
- Or upgrade to Python 3.10+ (recommended)

### Step 2: Install py-clob-client

```bash
pip install py-clob-client
```

**Note:** Package name is `py-clob-client` (with hyphen), not `py_clob_client`.

### Step 3: Configure API Credentials

Make sure your `.env` file has valid Polymarket API credentials:

```env
POLYMARKET_API_KEY=your_real_api_key_here
POLYMARKET_API_SECRET=your_real_api_secret_here
```

### Step 4: Verify Setup

Run the verification script:
```bash
python scripts/verify_setup.py
```

Check the logs when starting the bot - you should see:
- ✅ "Execution layer initialized successfully" (real mode)
- ❌ "py_clob_client not available. Using mock client." (mock mode)

## How to Check Current Mode

**Mock Mode Indicators:**
- Logs show "Mock order: ..." when placing orders
- Order IDs start with "mock_"
- No actual API calls are made

**Real Trading Mode Indicators:**
- Logs show "Submitted BUY order: [real_order_id]"
- Order IDs are real Polymarket order IDs
- Orders appear in your Polymarket account

## ⚠️ IMPORTANT WARNINGS

1. **Real Money**: When enabled, the bot will use REAL money from your Polymarket account
2. **Test First**: Start with small budgets and test thoroughly
3. **Monitor Closely**: Watch the logs when first enabling
4. **Risk Limits**: Make sure your risk limits are set appropriately in `.env`

## Testing Safely

Before enabling real trading:
1. Test in mock mode first
2. Verify the logic works correctly
3. Start with minimal budget ($10-50)
4. Monitor closely for the first few trades
5. Gradually increase budget as confidence grows

