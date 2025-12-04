"""Verification script to check if the environment is set up correctly."""

import sys
import os
import asyncio
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def check_dependencies():
    """Check if required dependencies are installed."""
    print("🔍 Checking dependencies...")
    required = [
        ("dotenv", "python-dotenv"),
        ("streamlit", "streamlit"),
        ("supabase", "supabase"),
        ("aiohttp", "aiohttp"),
    ]
    
    missing = []
    for import_name, package_name in required:
        try:
            __import__(import_name)
            print(f"  ✅ {package_name}")
        except ImportError:
            print(f"  ❌ {package_name} - MISSING")
            missing.append(package_name)
    
    return len(missing) == 0

def check_env_file():
    """Check if .env file exists."""
    print("\n🔍 Checking .env file...")
    env_path = project_root / ".env"
    if env_path.exists():
        print("  ✅ .env file exists")
        return True
    else:
        print("  ⚠️  .env file not found")
        print("     Copy .env.example to .env and fill in your values")
        return False

def check_env_vars():
    """Check if required environment variables are set."""
    print("\n🔍 Checking environment variables...")
    
    from dotenv import load_dotenv
    load_dotenv()
    
    required = {
        "POLYMARKET_API_KEY": "Polymarket API key",
        "POLYMARKET_API_SECRET": "Polymarket API secret",
    }
    
    optional = {
        "SUPABASE_URL": "Supabase URL (optional but recommended)",
        "SUPABASE_KEY": "Supabase key (optional but recommended)",
    }
    
    all_good = True
    
    for var, desc in required.items():
        value = os.getenv(var)
        if value:
            print(f"  ✅ {var} - Set")
        else:
            print(f"  ❌ {var} - MISSING ({desc})")
            all_good = False
    
    for var, desc in optional.items():
        value = os.getenv(var)
        if value:
            print(f"  ✅ {var} - Set")
        else:
            print(f"  ⚠️  {var} - Not set ({desc})")
    
    return all_good

def check_supabase():
    """Check Supabase connection if configured."""
    print("\n🔍 Checking Supabase connection...")
    
    from dotenv import load_dotenv
    load_dotenv()
    
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    
    if not url or not key:
        print("  ⚠️  Supabase not configured (optional)")
        return True
    
    try:
        from services import SupabaseService
        service = SupabaseService(url, key)
        if service.is_available():
            print("  ✅ Supabase connection successful")
            # Run async function in sync context
            traders = asyncio.run(service.load_all_traders())
            print(f"  ✅ Found {len(traders)} traders in database")
            return True
        else:
            print("  ❌ Supabase connection failed")
            return False
    except Exception as e:
        print(f"  ❌ Supabase check failed: {e}")
        return False

def check_config():
    """Check if config loads correctly."""
    print("\n🔍 Checking configuration...")
    
    try:
        from config import (
            load_execution_config,
            load_manager_config,
            load_supabase_config,
        )
        
        exec_config = load_execution_config()
        print("  ✅ Execution config loaded")
        
        manager_config = load_manager_config()
        print("  ✅ Manager config loaded")
        
        supabase_config = load_supabase_config()
        if supabase_config:
            print("  ✅ Supabase config loaded")
        else:
            print("  ⚠️  Supabase config not available (optional)")
        
        return True
    except Exception as e:
        print(f"  ❌ Config check failed: {e}")
        return False

def main():
    """Run all verification checks."""
    print("=" * 60)
    print("🚀 Arbitrage Bot - Setup Verification")
    print("=" * 60)
    
    results = []
    
    results.append(("Dependencies", check_dependencies()))
    results.append(("Environment File", check_env_file()))
    results.append(("Environment Variables", check_env_vars()))
    results.append(("Configuration", check_config()))
    results.append(("Supabase", check_supabase()))
    
    print("\n" + "=" * 60)
    print("📊 Summary")
    print("=" * 60)
    
    all_passed = True
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} - {name}")
        if not passed and name != "Supabase":  # Supabase is optional
            all_passed = False
    
    print("\n" + "=" * 60)
    if all_passed:
        print("✅ All critical checks passed! You're ready to go.")
        print("\nNext steps:")
        print("  1. Run the dashboard: python scripts/run_dashboard.py")
        print("  2. Or run the bot: python scripts/main.py")
    else:
        print("❌ Some checks failed. Please fix the issues above.")
        print("\nSee README.md for setup instructions.")
    print("=" * 60)
    
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())

