"""Market resolution checker.

Polls Gamma API to check if markets have resolved,
then closes positions with actual outcomes.
"""
import logging
import json
from core import public_data as requests
from typing import Optional
from core.config import Config

logger = logging.getLogger("resolver")


def check_market_resolved(market_id: str) -> Optional[dict]:
    """
    Check if a Polymarket market has resolved.

    Returns None if not resolved, or:
    {
        "resolved": True,
        "outcome": "YES" or "NO",
        "yes_price": float (1.0 or 0.0 after resolution),
    }
    """
    try:
        resp = requests.get(
            f"{Config.GAMMA_HOST}/markets/{market_id}",
            timeout=10,
        )
        if resp.status_code != 200:
            # Fallback: try by condition_id
            resp = requests.get(
                f"{Config.GAMMA_HOST}/markets",
                params={"id": market_id, "limit": 1},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            markets = resp.json()
            if not markets:
                return None
            market = markets[0] if isinstance(markets, list) else markets
        else:
            market = resp.json()

        if isinstance(market, list):
            market = market[0] if market else {}

        closed = market.get("closed", False)
        resolved = market.get("umaResolutionStatus") == "resolved"

        if not (closed and resolved):
            return None

        # Get resolution outcome
        prices_raw = market.get("outcomePrices", "")
        try:
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        except Exception:
            prices = []

        if not prices or len(prices) < 2:
            return None

        labels_raw = market.get("outcomes", "")
        try:
            labels = json.loads(labels_raw) if isinstance(labels_raw, str) else labels_raw
            labels = [str(label).lower() for label in labels]
        except (TypeError, ValueError):
            return None
        if labels not in (["yes", "no"], ["up", "down"], ["no", "yes"], ["down", "up"]):
            return None
        if len(prices) != 2:
            return None
        if labels[0] in {"no", "down"}:
            prices = list(reversed(prices))
        yes_price = float(prices[0])

        # After resolution: YES price is ~1.0 (YES won) or ~0.0 (NO won)
        if yes_price == 1.0 and float(prices[1]) == 0.0:
            outcome = "YES"
        elif yes_price == 0.0 and float(prices[1]) == 1.0:
            outcome = "NO"
        else:
            return None  # not clearly resolved yet

        return {
            "resolved": True,
            "outcome": outcome,
            "yes_price": yes_price,
        }

    except Exception as e:
        logger.debug(f"Resolution check failed for {market_id}: {e}")
        return None


def calculate_payout(position_side: str, outcome: str, shares: float) -> float:
    """
    Calculate payout for a position given the market outcome.

    If we bought YES and market resolved YES → payout = shares * $1
    If we bought YES and market resolved NO → payout = $0
    If we bought NO and market resolved NO → payout = shares * $1
    If we bought NO and market resolved YES → payout = $0
    """
    won = (position_side == outcome)
    return shares if won else 0.0
