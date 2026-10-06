"""Crypto market scanner — fetch BTC/ETH 5-min markets via slug pattern.

Gamma API's general search misses these ultra-short markets.
This scanner queries them directly by slug: btc-updown-5m-{timestamp}
"""
import logging
import time
from core import public_data as requests
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger("crypto_scanner")

from core.shared_cache import get_or_fetch

_CACHE_TTL = 10  # market scan TTL
_BTC_CACHE_TTL = 5


def _fetch_btc_price() -> float:
    resp = requests.get(
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
        timeout=5,
    )
    price = float(resp.json().get("bitcoin", {}).get("usd", 0) or 0)
    if price <= 0:
        # Raise so shared_cache falls back to last stale value instead of caching 0
        raise ValueError("CoinGecko returned non-positive price (likely rate-limited)")
    return price


def get_btc_price() -> float:
    """Shared across bots via the process-local research cache."""
    price = get_or_fetch("btc_price_coingecko", _fetch_btc_price, _BTC_CACHE_TTL)
    return price or 0.0


def _clob_midpoint(token_id: str):
    """CLOB REST midpoint for a token. Returns float or None. 3s shared cache."""
    if not token_id:
        return None
    def _fetch():
        r = requests.get(
            f"https://clob.polymarket.com/midpoint?token_id={token_id}",
            timeout=4,
        )
        if r.status_code != 200:
            raise ValueError(f"CLOB midpoint HTTP {r.status_code}")
        mid = float(r.json().get("mid", 0) or 0)
        if mid <= 0:
            raise ValueError("CLOB midpoint non-positive")
        return mid
    try:
        return get_or_fetch(f"clob_mid_{token_id}", _fetch, 3)
    except Exception:
        return None


@dataclass
class CryptoMarket:
    market_id: str
    question: str
    yes_price: float
    no_price: float
    yes_token_id: str
    no_token_id: str
    hours_to_resolve: float
    btc_price: float = 0
    liquidity: float = 0
    volume: float = 0
    spread: float = 0
    category: str = "crypto_price"


def _fetch_gamma_events() -> list:
    """Network-bound part only — shared across bots. Returns raw Gamma event list."""
    now_ts = int(time.time())
    base = now_ts - (now_ts % 300)
    out = []
    for i in range(3):
        ts = base + (i * 300)
        slug = f"btc-updown-5m-{ts}"
        try:
            resp = requests.get(
                f"https://gamma-api.polymarket.com/events?slug={slug}",
                timeout=5,
            )
            if resp.status_code == 200:
                evs = resp.json()
                if evs:
                    out.append(evs[0])
        except Exception as e:
            logger.debug(f"Gamma fetch error {slug}: {e}")
    return out


def scan_crypto_markets() -> list[CryptoMarket]:
    """Fetch active BTC 5-min up/down markets via slug pattern. Gamma data shared across bots."""
    markets = []
    btc_price = get_btc_price()

    events = get_or_fetch("gamma_btc_5min_events", _fetch_gamma_events, _CACHE_TTL) or []

    for event in events:
        try:
            for m in event.get("markets", []):
                if not m.get("active") or m.get("closed"):
                    continue

                question = m.get("question", "")
                end_str = m.get("endDate", "")
                if not end_str:
                    continue

                # Parse end date
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    seconds_left = (end_dt - datetime.now(timezone.utc)).total_seconds()
                    hours_left = seconds_left / 3600
                except Exception:
                    continue

                if hours_left <= 0:
                    continue

                # Parse prices — WebSocket → CLOB REST midpoint → Gamma static fallback.
                # Gamma outcomePrices is stale (opening-fair-value), not live. So we prefer
                # CLOB REST midpoint when WS cache is empty (WS dropouts seen 2026-04-20).
                try:
                    import json
                    tokens_raw = m.get("clobTokenIds", "")
                    token_list_tmp = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
                    yt = token_list_tmp[0] if len(token_list_tmp) > 0 else ""
                    nt = token_list_tmp[1] if len(token_list_tmp) > 1 else ""

                    # Try WebSocket prices first
                    from engines.crypto_ws import get_live_price, subscribe_tokens
                    subscribe_tokens([yt, nt])
                    ws_yes = get_live_price(yt)
                    ws_no = get_live_price(nt)

                    if ws_yes is not None and ws_no is not None:
                        yes_price = ws_yes
                        no_price = ws_no
                    else:
                        # CLOB REST midpoint — fresh, but costs 1-2 HTTP per market
                        yes_price = _clob_midpoint(yt)
                        no_price = _clob_midpoint(nt)
                        if yes_price is None or no_price is None:
                            # Last-resort Gamma static (only valid at market open)
                            prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
                            if yes_price is None:
                                yes_price = float(prices[0])
                            if no_price is None:
                                no_price = float(prices[1])
                except Exception:
                    try:
                        prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
                        yes_price = float(prices[0])
                        no_price = float(prices[1])
                    except Exception:
                        continue

                # Get token IDs
                tokens = m.get("clobTokenIds", "")
                try:
                    token_list = json.loads(tokens) if isinstance(tokens, str) else tokens
                    yes_token = token_list[0] if len(token_list) > 0 else ""
                    no_token = token_list[1] if len(token_list) > 1 else ""
                except Exception:
                    continue

                if not yes_token or not no_token:
                    continue

                try:
                    vol = float(m.get("volume", 0) or 0)
                    liq = float(m.get("liquidity", 0) or 0)
                except Exception:
                    vol = liq = 0
                markets.append(CryptoMarket(
                    market_id=str(m.get("id", "")),
                    question=question,
                    yes_price=yes_price,
                    no_price=no_price,
                    yes_token_id=yes_token,
                    no_token_id=no_token,
                    hours_to_resolve=hours_left,
                    btc_price=btc_price,
                    liquidity=liq,
                    volume=vol,
                ))

        except Exception as e:
            logger.debug(f"Crypto scan parse error: {e}")

    if markets:
        logger.debug(f"Crypto scanner: {len(markets)} BTC markets")

    return markets
