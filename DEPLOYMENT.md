# Deployment Guide for Render

This guide explains how to deploy the trading bot to Render as a background worker.

## Prerequisites

1. A Render account (sign up at [render.com](https://render.com))
2. Your repository pushed to GitHub/GitLab/Bitbucket
3. All required environment variables ready

## Step 1: Prepare Your Repository

Make sure your code is committed and pushed to your Git repository.

## Step 2: Create a Background Worker on Render

1. Go to your Render dashboard
2. Click "New +" → "Background Worker"
3. Connect your repository
4. Configure the service:
   - **Name**: `trading-bot` (or your preferred name)
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python scripts/main.py`
   - **Plan**: Choose based on your needs (Free tier works for testing)

## Step 3: Install py-clob-client (Optional but Recommended)

If you're doing real trading (not just mock mode), you'll need to install `py-clob-client`. 

**Option 1: Add to requirements.txt** (recommended)
Uncomment or add this line to `requirements.txt`:
```
py-clob-client>=0.1.0
```

**Option 2: Install via build command**
Update the build command in Render to:
```
pip install -r requirements.txt && pip install py-clob-client
```

**Note**: The bot works in mock mode without this package, but you won't be able to place real orders.

## Step 4: Set Environment Variables

In the Render dashboard, go to your service → Environment tab and add:

### Required Variables

```bash
# Polymarket API Credentials
POLYMARKET_API_KEY=your_api_key_here
POLYMARKET_API_SECRET=your_api_secret_here

# Supabase Configuration
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_anon_key
```

### Optional Variables

```bash
# Polymarket Additional Credentials (if using real trading)
POLYMARKET_PASSPHRASE=your_passphrase
POLYMARKET_PRIVATE_KEY=your_private_key
POLYMARKET_ADDRESS=your_wallet_address
POLYMARKET_CHAIN_ID=137  # Polygon mainnet (or 80001 for testnet)

# Manager Configuration
MANAGER_POLL_INTERVAL=1.0
MANAGER_MAX_EXPOSURE=10000.0
MANAGER_MAX_PNL_LOSS=-1000.0
MANAGER_STATUS_INTERVAL=5.0
MANAGER_SUPABASE_SYNC_INTERVAL=30.0
MANAGER_EMERGENCY_SHUTDOWN=true

# Execution Configuration
EXECUTION_MAX_RETRIES=3
EXECUTION_RETRY_DELAY=0.5
EXECUTION_TIMEOUT=10
EXECUTION_PRICE_PRECISION=4
EXECUTION_SIZE_PRECISION=2

# Logging
LOG_LEVEL=INFO
PYTHON_VERSION=3.11.0
```

## Step 5: Deploy

1. Click "Create Background Worker"
2. Render will build and deploy your service
3. Check the logs to ensure it starts correctly

## Step 6: Monitor

- **Logs**: View real-time logs in the Render dashboard
- **Metrics**: Monitor resource usage
- **Status**: Check service health

## Troubleshooting

### Service Won't Start

1. Check logs for errors
2. Verify all required environment variables are set
3. Ensure `requirements.txt` is up to date
4. Check Python version compatibility

### Connection Issues

1. Verify Supabase credentials are correct
2. Check network connectivity
3. Ensure API keys are valid

### Trading Not Working

1. Verify Polymarket API credentials
2. Check token allowances are set (for real trading)
3. Ensure traders are configured in Supabase
4. Review logs for specific errors

## Important Notes

- **Free Tier**: Render free tier sleeps after 15 minutes of inactivity. Consider upgrading for 24/7 operation
- **Logs**: Logs are stored in Render and can be downloaded
- **Secrets**: Never commit API keys or secrets to Git. Always use Render environment variables
- **Restarts**: The service will automatically restart on crashes or code updates

## Alternative: Using render.yaml

If you prefer infrastructure-as-code, you can use the `render.yaml` file:

1. Push `render.yaml` to your repository
2. In Render dashboard, select "Apply render.yaml"
3. Render will automatically create the service from the YAML configuration

## Updating the Service

1. Push changes to your repository
2. Render will automatically detect and deploy updates
3. Or manually trigger a deploy from the dashboard

