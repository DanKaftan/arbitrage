"""Utility to resolve Polymarket market slugs to market IDs.

Uses the Gamma Markets API: https://gamma-api.polymarket.com/markets?slug=...
"""

import logging
import aiohttp
import json
from typing import Optional, Dict

logger = logging.getLogger(__name__)


async def market_slug_resolver(slug: str) -> Optional[Dict[str, str]]:
    """
    Resolve a Polymarket market slug to market IDs (conditionId and tokenIds).
    
    Uses the Gamma Markets API: https://gamma-api.polymarket.com/markets?slug=...
    
    Args:
        slug: Market slug (e.g., "will-israel-strike-lebanon-on-november-14")
    
    Returns:
        Dictionary with 'condition_id', 'yes_token_id', 'no_token_id', or None if not found
        For backward compatibility, also returns just condition_id as string if called with old signature
    """
    # If it's already a hex address (0x...), return as-is (backward compatibility)
    if slug.startswith("0x") and len(slug) == 42:
        logger.debug(f"Input is already a market ID: {slug}")
        return slug
    
    url = "https://gamma-api.polymarket.com/markets"
    params = {"slug": slug}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                response.raise_for_status()
                
                markets = await response.json()
                
                if not markets:
                    logger.warning(f"No market found for slug '{slug}'")
                    return None
                
                market = markets[0]
                condition_id = market.get("conditionId")
                
                if not condition_id or not isinstance(condition_id, str) or not condition_id.startswith("0x"):
                    logger.warning(f"Market found for slug '{slug}' but no valid conditionId")
                    return None
                
                # Extract token IDs
                clob_token_ids = market.get("clobTokenIds")
                outcomes = market.get("outcomes")
                
                result = {
                    "condition_id": condition_id,
                }
                
                if clob_token_ids:
                    try:
                        token_ids = json.loads(clob_token_ids)
                        outcome_list = json.loads(outcomes) if outcomes else []
                        
                        if len(token_ids) >= 2:
                            # Typically first token is YES, second is NO
                            result["yes_token_id"] = token_ids[0]
                            result["no_token_id"] = token_ids[1]
                        elif len(token_ids) >= 1:
                            # Some markets might only have one outcome
                            result["yes_token_id"] = token_ids[0]
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"Failed to parse token IDs for '{slug}': {e}")
                
                logger.info(f"Resolved slug '{slug}' to condition_id: {condition_id}, tokens: {result.get('yes_token_id', 'N/A')}")
                return result
                
    except aiohttp.ClientError as e:
        logger.error(f"Error fetching market for slug '{slug}': {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error resolving slug '{slug}': {e}")
        return None

