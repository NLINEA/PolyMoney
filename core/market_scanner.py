"""Market scanner — discovers and classifies Polymarket opportunities."""
import json
import math
import logging
import re
import time
from core import public_data as requests
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from core.config import Config

logger = logging.getLogger("scanner")


@dataclass
class Market:
    """A scanned Polymarket market with parsed metadata."""
    market_id: str
    condition_id: str
    question: str
    category: str           # crypto_price | sports | multi_outcome | other
    end_date: Optional[datetime]
    hours_to_resolve: float
    yes_token_id: str
    no_token_id: str
    yes_price: float        # midpoint
    no_price: float
    best_bid: float         # for YES side
    best_ask: float
    spread: float
    volume_24h: float
    liquidity: float
    neg_risk: bool
    # Parsed from question (crypto_price markets)
    crypto_coin: str = ""
    crypto_strike: float = 0.0
    crypto_direction: str = ""   # "above" or "below"
    # Multi-outcome
    event_id: str = ""
    event_title: str = ""
    event_outcomes: list = field(default_factory=list)

    @property
    def is_tradeable(self) -> bool:
        return (
            self.yes_token_id != ""
            and self.best_ask > 0
            and self.best_bid > 0
            and self.liquidity >= Config.MIN_LIQUIDITY
        )


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ── Question parsing ───────────────────────────────────────────────

_CRYPTO_PATTERN = re.compile(
    r"(?:will\s+(?:the\s+(?:price\s+of\s+)?)?)?"
    r"(?P<coin>\bbitcoin\b|\bbtc\b|\bethereum\b|\beth\b|\bsolana\b|\bsol\b"
    r"|\bdoge\b|\bdogecoin\b|\bxrp\b|\bmatic\b|\bavax\b|\bada\b|\bdot\b|\blink\b"
    r"|\bbnb\b|\bsui\b|\bapt\b|\baptos\b|\bton\b|\bnear\b|\barb\b)"
    r".*?"
    r"(?P<dir>above|below|over|under|exceed|hit|reach|drop below|fall below|dip to|drop to|up or down)"
    r".*?"
    r"\$?\s*(?P<price>[\d,]+(?:\.\d+)?)\s*(?:k|K)?"
    r"",
    re.IGNORECASE,
)

# Simpler pattern for daily markets: "Bitcoin above 68,000 on April 2"
_CRYPTO_DAILY_PATTERN = re.compile(
    r"^(?P<coin>Bitcoin|Ethereum|Solana|XRP|Dogecoin|BNB|SOL|BTC|ETH|DOGE)"
    r"\s+(?P<dir>above|below)"
    r"\s+(?P<price>[\d,]+(?:\.\d+)?)"
    r"",
    re.IGNORECASE,
)

_COIN_MAP = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
    "doge": "DOGE", "dogecoin": "DOGE",
    "xrp": "XRP", "matic": "MATIC",
    "avax": "AVAX", "ada": "ADA",
    "dot": "DOT", "link": "LINK",
    "bnb": "BNB", "sui": "SUI",
    "apt": "APT", "aptos": "APT",
    "ton": "TON", "near": "NEAR",
    "arb": "ARB",
}

_ABOVE_WORDS = {"above", "over", "exceed", "hit", "reach"}
_BELOW_WORDS = {"below", "under", "drop below", "fall below"}


def _parse_crypto_question(question: str) -> Optional[dict]:
    """Extract coin, direction, and strike price from a crypto market question."""
    # Try daily pattern first (simpler, no "$")
    m = _CRYPTO_DAILY_PATTERN.search(question)
    if m:
        coin_raw = m.group("coin").lower()
        coin = _COIN_MAP.get(coin_raw, coin_raw.upper())
        direction = "above" if m.group("dir").lower() in _ABOVE_WORDS else "below"
        price = float(m.group("price").replace(",", ""))
        return {"coin": coin, "direction": direction, "strike": price}

    # Try full pattern
    m = _CRYPTO_PATTERN.search(question)
    if not m:
        return None
    coin_raw = m.group("coin").lower()
    coin = _COIN_MAP.get(coin_raw, coin_raw.upper())
    direction_raw = m.group("dir").lower()
    if direction_raw in {"dip to", "drop to", "drop below", "fall below"} or direction_raw in _BELOW_WORDS:
        direction = "below"
    elif direction_raw in _ABOVE_WORDS or direction_raw in {"hit", "reach"}:
        direction = "above"
    elif direction_raw == "up or down":
        return None  # skip 5-min direction markets
    else:
        return None

    price_str = m.group("price").replace(",", "")
    price = float(price_str)
    # Handle "85K" → 85000
    rest = question[m.end("price"):m.end("price") + 2].lower().strip()
    if rest.startswith("k") or "k" in m.group(0).lower().split(price_str)[-1][:3]:
        if price < 1000:
            price *= 1000
    return {"coin": coin, "direction": direction, "strike": price}


def _classify_market(question: str, neg_risk: bool) -> str:
    """Classify a market by type."""
    q = question.lower()
    crypto = _parse_crypto_question(question)
    if crypto:
        return "crypto_price"
    if neg_risk:
        return "multi_outcome"
    # Sports keywords
    sports_kw = ["win", "vs.", "defeat", "nba", "nfl", "nhl", "mlb", "fifa",
                 "premier league", "champions league", "o/u", "over/under"]
    if any(kw in q for kw in sports_kw):
        return "sports"
    return "other"


# ── Main scanner ───────────────────────────────────────────────────

def scan_markets(max_pages: int = 10) -> list[Market]:
    """Scan Gamma API for tradeable markets resolving within MAX_RESOLVE_DAYS."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=Config.MAX_RESOLVE_DAYS)
    markets: list[Market] = []

    for page in range(max_pages):
        offset = page * Config.SCAN_BATCH_SIZE
        try:
            resp = requests.get(
                f"{Config.GAMMA_HOST}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": Config.SCAN_BATCH_SIZE,
                    "offset": offset,
                    "order": "volume24hr",
                    "ascending": "false",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
        except Exception as e:
            logger.warning(f"Gamma API error at offset {offset}: {e}")
            break

        for raw in batch:
            mkt = _parse_market(raw, now, cutoff)
            if mkt and mkt.is_tradeable:
                markets.append(mkt)

    logger.info(f"Scanned {len(markets)} tradeable markets (max_resolve={Config.MAX_RESOLVE_DAYS}d)")
    return markets


def scan_multi_outcome_events(max_pages: int = 5) -> list[Market]:
    """Scan negRisk events for multi-outcome arb opportunities."""
    markets: list[Market] = []
    now = datetime.now(timezone.utc)

    for page in range(max_pages):
        offset = page * 50
        try:
            resp = requests.get(
                f"{Config.GAMMA_HOST}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 50,
                    "offset": offset,
                    "order": "volume24hr",
                    "ascending": "false",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                break
            events = resp.json()
            if not isinstance(events, list) or not events:
                break
        except Exception as e:
            logger.warning(f"Gamma events API error: {e}")
            break

        for ev in events:
            ev_markets = ev.get("markets", [])
            if len(ev_markets) < 3:
                continue
            if not any(m.get("negRisk") for m in ev_markets):
                continue
            if len(ev_markets) > Config.ARB_MAX_OUTCOMES:
                continue

            event_id = str(ev.get("id", ""))
            event_title = ev.get("title") or ev.get("description") or ""

            outcomes = []
            for raw in ev_markets:
                mkt = _parse_market(raw, now, None)
                if mkt:
                    mkt.event_id = event_id
                    mkt.event_title = event_title
                    mkt.category = "multi_outcome"
                    outcomes.append(mkt)

            if len(outcomes) >= 3:
                for o in outcomes:
                    o.event_outcomes = outcomes
                    markets.append(o)

    logger.info(f"Scanned {len(markets)} multi-outcome markets")
    return markets


def _parse_market(raw: dict, now: datetime, cutoff: Optional[datetime]) -> Optional[Market]:
    """Parse a raw Gamma API market dict into a Market dataclass."""
    # Parse token IDs
    clob_ids_raw = raw.get("clobTokenIds", "")
    try:
        clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
    except Exception:
        return None
    if not isinstance(clob_ids, list) or len(clob_ids) < 2:
        return None

    # Only binary YES/NO (or crypto Up/Down) markets have this representation.
    try:
        outcomes = raw.get("outcomes", [])
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        labels = [str(x).lower() for x in outcomes]
        if labels not in (["yes", "no"], ["up", "down"], ["no", "yes"], ["down", "up"]):
            return None
        if len(clob_ids) != 2:
            return None
    except (TypeError, ValueError):
        return None

    # Parse prices; never substitute invented prices for malformed upstream data.
    prices_raw = raw.get("outcomePrices", "")
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        if len(prices) != 2:
            return None
        yes_price, no_price = float(prices[0]), float(prices[1])
        if not all(math.isfinite(p) and 0 <= p <= 1 for p in (yes_price, no_price)):
            return None
    except (TypeError, ValueError):
        return None
    if labels[0] in {"no", "down"}:
        clob_ids = list(reversed(clob_ids))
        yes_price, no_price = no_price, yes_price

    # Parse end date
    end_date = _parse_dt(raw.get("endDate") or raw.get("end_date"))
    if cutoff and end_date:
        if end_date > cutoff or end_date <= now:
            return None  # too far out or already expired
    hours_to_resolve = ((end_date - now).total_seconds() / 3600) if end_date else 9999

    # Best bid/ask (from Gamma — YES side)
    try:
        best_bid = float(raw.get("bestBid") or 0)
        best_ask = float(raw.get("bestAsk") or 0)
        spread = float(raw.get("spread") or 0)
        volume = float(raw.get("volume24hr") or 0)
        liquidity = float(raw.get("liquidity") or 0)
        if not all(math.isfinite(v) and v >= 0 for v in (best_bid, best_ask, spread, volume, liquidity)):
            return None
    except (TypeError, ValueError):
        return None
    if labels[0] in {"no", "down"}:
        best_bid, best_ask = (1 - best_ask if best_ask else 0), (1 - best_bid if best_bid else 0)

    question = raw.get("question") or ""
    neg_risk = bool(raw.get("negRisk"))
    category = _classify_market(question, neg_risk)

    mkt = Market(
        market_id=str(raw.get("id") or ""),
        condition_id=str(raw.get("conditionId") or ""),
        question=question,
        category=category,
        end_date=end_date,
        hours_to_resolve=hours_to_resolve,
        yes_token_id=str(clob_ids[0]),
        no_token_id=str(clob_ids[1]),
        yes_price=yes_price,
        no_price=no_price,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        volume_24h=volume,
        liquidity=liquidity,
        neg_risk=neg_risk,
    )

    # Enrich crypto markets
    if category == "crypto_price":
        parsed = _parse_crypto_question(question)
        if parsed:
            mkt.crypto_coin = parsed["coin"]
            mkt.crypto_strike = parsed["strike"]
            mkt.crypto_direction = parsed["direction"]

    return mkt
