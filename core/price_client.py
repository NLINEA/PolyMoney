"""Price client — CLOB orderbook + crypto spot prices."""
import logging
import time
from core import public_data as requests
from typing import Optional
from core.config import Config

logger = logging.getLogger("price_client")

# In-memory cache: {key: (value, expiry_ts)}
_cache: dict[str, tuple] = {}


def _cached(key: str, ttl: float = 5.0):
    """Simple TTL cache decorator helper."""
    entry = _cache.get(key)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None


def _set_cache(key: str, value, ttl: float = 5.0):
    _cache[key] = (value, time.time() + ttl)


# ── CLOB prices ────────────────────────────────────────────────────

def get_clob_price(token_id: str, side: str = "buy") -> Optional[float]:
    """Get best executable price from CLOB. side='buy' → best ask, 'sell' → best bid."""
    key = f"clob:{token_id}:{side}"
    cached = _cached(key, ttl=3.0)
    if cached is not None:
        return cached
    try:
        resp = requests.get(
            f"{Config.CLOB_HOST}/price",
            params={"token_id": token_id, "side": side},
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        price = float(resp.json().get("price", 0))
        if price > 0:
            _set_cache(key, price, ttl=3.0)
            return price
    except Exception as e:
        logger.debug(f"CLOB price error: {e}")
    return None


def get_clob_midpoint(token_id: str) -> Optional[float]:
    key = f"clob_mid:{token_id}"
    cached = _cached(key, ttl=3.0)
    if cached is not None:
        return cached
    try:
        resp = requests.get(
            f"{Config.CLOB_HOST}/midpoint",
            params={"token_id": token_id},
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        mid = float(resp.json().get("mid", 0))
        if mid > 0:
            _set_cache(key, mid, ttl=3.0)
            return mid
    except Exception as e:
        logger.debug(f"CLOB midpoint error: {e}")
    return None


def get_clob_spread(token_id: str) -> Optional[float]:
    try:
        resp = requests.get(
            f"{Config.CLOB_HOST}/spread",
            params={"token_id": token_id},
            timeout=8,
        )
        if resp.status_code == 200:
            return float(resp.json().get("spread", 0))
    except Exception:
        pass
    return None


# ── Crypto spot prices (CoinGecko) ─────────────────────────────────

_COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "MATIC": "matic-network",
    "AVAX": "avalanche-2",
    "ADA": "cardano",
    "DOT": "polkadot",
    "LINK": "chainlink",
}


_BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "DOGE": "DOGEUSDT", "XRP": "XRPUSDT", "AVAX": "AVAXUSDT",
    "ADA": "ADAUSDT", "DOT": "DOTUSDT", "LINK": "LINKUSDT",
    "MATIC": "MATICUSDT",
}


def _get_binance_price(symbol: str) -> Optional[float]:
    """Fallback: get price from Binance public API."""
    pair = _BINANCE_SYMBOLS.get(symbol.upper())
    if not pair:
        return None
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": pair},
            timeout=8,
        )
        if resp.status_code == 200:
            return float(resp.json().get("price", 0))
    except Exception:
        pass
    return None


def get_crypto_price(symbol: str) -> Optional[float]:
    """Get current USD price. Tries CoinGecko first, falls back to Binance."""
    sym = symbol.upper()
    key = f"crypto:{sym}"
    cached = _cached(key, ttl=30.0)
    if cached is not None:
        return cached

    # Try CoinGecko first
    cg_id = _COINGECKO_IDS.get(sym)
    if cg_id:
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": cg_id, "vs_currencies": "usd"},
                timeout=10,
            )
            if resp.status_code == 200:
                price = resp.json().get(cg_id, {}).get("usd")
                if price:
                    price = float(price)
                    _set_cache(key, price, ttl=30.0)
                    return price
        except Exception as e:
            logger.debug(f"CoinGecko error for {sym}: {e}")

    # Fallback to Binance
    price = _get_binance_price(sym)
    if price and price > 0:
        _set_cache(key, price, ttl=30.0)
        return price

    return None


def _binance_klines_vol(symbol: str, days: int = 30) -> Optional[float]:
    """Get annualized vol from Binance klines (fallback)."""
    pair = _BINANCE_SYMBOLS.get(symbol.upper())
    if not pair:
        return None
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": pair, "interval": "1d", "limit": days},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        klines = resp.json()
        if len(klines) < 5:
            return None
        closes = [float(k[4]) for k in klines]  # close price at index 4
        import math
        returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        if not returns:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        daily_vol = variance ** 0.5
        return daily_vol * (365 ** 0.5)
    except Exception:
        return None


def get_crypto_volatility(symbol: str, days: int = 30) -> Optional[float]:
    """Get annualized historical volatility. Tries CoinGecko, falls back to Binance."""
    sym = symbol.upper()
    key = f"vol:{sym}:{days}"
    cached = _cached(key, ttl=3600.0)
    if cached is not None:
        return cached

    # Try CoinGecko
    cg_id = _COINGECKO_IDS.get(sym)
    if cg_id:
        try:
            resp = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart",
                params={"vs_currency": "usd", "days": days, "interval": "daily"},
                timeout=15,
            )
            if resp.status_code == 200:
                prices = resp.json().get("prices", [])
                if len(prices) >= 5:
                    closes = [p[1] for p in prices]
                    import math
                    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
                    if returns:
                        mean = sum(returns) / len(returns)
                        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
                        daily_vol = variance ** 0.5
                        annual_vol = daily_vol * (365 ** 0.5)
                        _set_cache(key, annual_vol, ttl=3600.0)
                        return annual_vol
        except Exception as e:
            logger.debug(f"CoinGecko vol error for {sym}: {e}")

    # Fallback to Binance
    vol = _binance_klines_vol(sym, days)
    if vol and vol > 0:
        _set_cache(key, vol, ttl=3600.0)
        return vol

    return None


def get_crypto_recent_trend(symbol: str) -> Optional[dict]:
    """Get 7-day price trend (momentum) for a crypto asset.
    Returns {change_7d, change_24h, direction} or None."""
    sym = symbol.upper()
    key = f"trend:{sym}"
    cached = _cached(key, ttl=600.0)
    if cached is not None:
        return cached

    cg_id = _COINGECKO_IDS.get(sym)
    if not cg_id:
        return None
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart",
            params={"vs_currency": "usd", "days": 7},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        prices = resp.json().get("prices", [])
        if len(prices) < 10:
            return None
        closes = [p[1] for p in prices]
        current = closes[-1]
        week_ago = closes[0]
        day_ago = closes[-min(24, len(closes))]  # ~24 hours ago
        change_7d = (current - week_ago) / week_ago
        change_24h = (current - day_ago) / day_ago
        direction = "up" if change_7d > 0.02 else ("down" if change_7d < -0.02 else "flat")
        result = {"change_7d": change_7d, "change_24h": change_24h, "direction": direction}
        _set_cache(key, result, ttl=600.0)
        return result
    except Exception as e:
        logger.debug(f"Trend error for {sym}: {e}")
    return None
