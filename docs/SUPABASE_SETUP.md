# Supabase Setup for Trader Persistence

This guide explains how to set up Supabase to persist trader configurations.

## Why Supabase?

- **Persistence**: Traders survive bot restarts
- **Multi-Instance**: Multiple bot instances can share the same traders
- **Real-time**: Changes sync across instances

## Setup Steps

### 1. Create Supabase Project

1. Go to [supabase.com](https://supabase.com)
2. Create a new project
3. Note your project URL and anon key

### 2. Create Database Table

Run the SQL migration in `docs/supabase_migration.sql` in your Supabase SQL editor.

The schema includes:
- **traders**: Stores trader configurations (market_slug, budget, min_gap, status)
- **fills**: Stores trade fills/executions
- **logs**: Stores trader logs
- **settings**: Stores application settings

### 3. Configure Environment Variables

Add to your `.env` file:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_anon_key_here
```

### 4. Install Dependencies

```bash
pip install supabase
```

Or it will be installed automatically when you run:
```bash
pip install -r requirements.txt
```

## How It Works

### On Startup

1. Bot loads Supabase credentials from `.env`
2. Connects to Supabase
3. Loads all active traders from database
4. Creates traders locally
5. Bot starts trading

### Creating Traders

1. Add trader to Supabase `traders` table (via SQL or API)
2. Bot loads trader on startup
3. Trader starts trading automatically

### Deleting Traders

1. Mark trader as `is_active = false` in Supabase
2. Trader stops on next bot restart
3. Or remove trader from database entirely

### Fallback Behavior

- If Supabase is unavailable, bot continues with local traders
- If no traders in Supabase, falls back to local config
- All operations work locally first, then sync to DB

## Benefits

✅ **Fast**: Local operations are instant  
✅ **Resilient**: Works even if Supabase is down  
✅ **Persistent**: Traders survive restarts  
✅ **Scalable**: Multiple instances share same config  

## Troubleshooting

### Traders not loading

1. Check Supabase credentials in `.env`
2. Verify table exists and has correct schema
3. Check bot logs for Supabase errors
4. Verify RLS policies allow access

### Traders not saving

1. Check Supabase connection
2. Verify table permissions
3. Check logs for error messages
4. Operations still work locally even if save fails

### Multiple instances

- All instances load from same Supabase table
- Changes sync across instances (with small delay)
- Use pause/resume to coordinate if needed

