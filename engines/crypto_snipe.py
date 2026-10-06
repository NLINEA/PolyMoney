"""Historical research signal functions. Thresholds require independent validation. No execution or performance claim."""


from research.signal_state import get_signal_store
import logging


import time


from dataclasses import dataclass


from datetime import datetime, timezone, timedelta


from typing import Optional


logger = logging.getLogger("engine.crypto_snipe")


_loss_count = 0


_stopped = False


_atr_cache: dict = {}


_ATR_CACHE_TTL = 5  # seconds — short to catch vol spikes near entry


def _get_btc_atr() -> Optional[float]:
    """Fetch BTC 5-min ATR from Binance. Returns ATR as fraction of price."""
    now = time.time()
    if _atr_cache.get("ts") and now - _atr_cache["ts"] < _ATR_CACHE_TTL:
        return _atr_cache.get("atr")
    try:
        from core import public_data as requests
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": 14},
            timeout=5,
        )
        candles = resp.json()
        if not isinstance(candles, list) or len(candles) < 14:
            return None
        # candle: [open_time, open, high, low, close, ...]
        ranges = [float(c[2]) - float(c[3]) for c in candles]
        last_close = float(candles[-1][4])
        atr = (sum(ranges) / len(ranges)) / last_close
        _atr_cache["atr"] = atr
        _atr_cache["ts"] = now
        return atr
    except Exception as e:
        logger.debug(f"ATR fetch failed: {e}")
        return _atr_cache.get("atr")  # stale fallback


ATR_THRESHOLD = 0.003  # skip if BTC 5-min ATR > 0.3% (dormant)


TREND_THRESHOLD = 0.005  # kept for backward-compat reference


_trend_cache: dict = {}


def _btc_trend_1h() -> Optional[float]:
    """BTC net % change over last 2 hours (24 × 5-min candles). Cached 5s.
    Naming kept as '1h' for historical compat but now measures 2h per backtest."""
    now = time.time()
    if _trend_cache.get("ts") and now - _trend_cache["ts"] < 5:
        return _trend_cache.get("trend")
    try:
        from core import public_data as requests
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": 24},
            timeout=5,
        )
        candles = resp.json()
        if not isinstance(candles, list) or len(candles) < 24:
            return None
        first_open = float(candles[0][1])
        last_close = float(candles[-1][4])
        trend = (last_close - first_open) / first_open
        _trend_cache["trend"] = trend
        _trend_cache["ts"] = now
        return trend
    except Exception as e:
        logger.debug(f"Trend fetch failed: {e}")
        return _trend_cache.get("trend")


def _volatility_ok() -> bool:
    """Return True if BTC volatility is within acceptable range for sniping."""
    atr = _get_btc_atr()
    if atr is None:
        return True  # if data unavailable, don't block (fail-open)
    if atr > ATR_THRESHOLD:
        logger.info(f"Crypto snipe SKIP: ATR {atr*100:.2f}% > {ATR_THRESHOLD*100:.2f}%")
        return False
    return True


_current_candle_cache: dict = {}


def _btc_current_move() -> Optional[float]:
    """% change in current forming 5-min candle (open → latest). 3s cache."""
    now = time.time()
    if _current_candle_cache.get("ts") and now - _current_candle_cache["ts"] < 3:
        return _current_candle_cache.get("move")
    try:
        from core import public_data as requests
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": 1},
            timeout=3,
        )
        c = resp.json()[0]
        open_p = float(c[1])
        close_p = float(c[4])  # latest close within forming candle
        move = (close_p - open_p) / open_p
        _current_candle_cache["move"] = move
        _current_candle_cache["ts"] = now
        return move
    except Exception as e:
        logger.debug(f"Current candle fetch failed: {e}")
        return _current_candle_cache.get("move")


def _side_allowed(side: str) -> bool:
    """Combined filter:
    1. Trend gate (2h): skip counter-trend side
    2. Spot confirm: BTC current 5-min candle must move in bot's direction ≥ 0.05%
    """
    # Layer 1: Trend filter
    trend = _btc_trend_1h()
    try:
        from core.config import crypto_trend_threshold
        threshold = crypto_trend_threshold()
    except Exception:
        threshold = TREND_THRESHOLD
    if trend is not None:
        if trend > threshold and side == "NO":
            logger.info(f"Crypto snipe SKIP NO: 2h trend +{trend*100:.2f}% (uptrend)")
            return False
        if trend < -threshold and side == "YES":
            logger.info(f"Crypto snipe SKIP YES: 2h trend {trend*100:.2f}% (downtrend)")
            return False

    # Layer 2: Spot confirmation (current 5-min candle direction)
    move = _btc_current_move()
    CONFIRM_THRESHOLD = 0.0005  # 0.05%
    if move is not None:
        if side == "YES" and move < CONFIRM_THRESHOLD:
            logger.info(f"Crypto snipe SKIP YES: BTC current candle {move*100:+.3f}% (not up enough)")
            return False
        if side == "NO" and move > -CONFIRM_THRESHOLD:
            logger.info(f"Crypto snipe SKIP NO: BTC current candle {move*100:+.3f}% (not down enough)")
            return False

    return True


@dataclass
class CryptoSnipeSignal:
    market_id: str
    question: str
    side: str
    token_id: str
    market_price: float
    edge: float
    hours_to_resolve: float
    strategy: str = "crypto_snipe"


def reset_crypto_snipe():
    """Reset process-local research loss state."""
    global _loss_count, _stopped
    _loss_count, _stopped = 0, False
    get_signal_store().set_kv("crypto_snipe_stopped", "0")


def record_crypto_win():
    """Reset consecutive loss counter on win."""
    global _loss_count
    if _loss_count > 0:
        logger.info(f"Crypto consecutive losses reset ({_loss_count} -> 0)")
    _loss_count = 0


def record_crypto_loss():
    """Stop after consecutive losses; no notifications or private state access."""
    global _loss_count, _stopped
    from core.config import crypto_consecutive_losses_stop
    _loss_count += 1
    if _loss_count >= crypto_consecutive_losses_stop():
        _stopped = True
        get_signal_store().set_kv("crypto_snipe_stopped", "1")


def find_crypto_snipe_signals(markets: list) -> list[CryptoSnipeSignal]:
    """Find crypto up/down markets at 90-98¢ with <45 seconds to resolve.
    Gated by BTC trend filter (counter-trend side skipped) via _side_allowed."""
    global _stopped, _loss_count
    # _volatility_ok() / _get_btc_atr() kept as dormant helpers for future use.
    # if not _volatility_ok():
    #     return []
    # Check persistent stop from DB
    if get_signal_store().get_kv("crypto_snipe_stopped", "0") == "1":
        _stopped = True
    if _stopped:
        return []

    signals = []
    CRYPTO_KEYWORDS = ["up or down"]

    try:
        from core.config import crypto_window_seconds
        max_hours = crypto_window_seconds() / 3600.0
    except Exception:
        max_hours = 0.0125  # 45s default
    for m in markets:
        if m.hours_to_resolve > max_hours or m.hours_to_resolve < 0:
            continue

        q = (m.question or "").lower()
        if not any(kw in q for kw in CRYPTO_KEYWORDS):
            continue

        # Only BTC (ETH pending validation)
        if not any(coin in q for coin in ["bitcoin"]):
            continue

        # YES / NO entry range live-tunable via settings
        try:
            from core.config import crypto_entry_min, crypto_entry_max
            emin = crypto_entry_min()
            emax = crypto_entry_max()
        except Exception:
            emin, emax = 0.90, 0.98

        if emin <= m.yes_price <= emax:
            if not _side_allowed("YES"):
                continue
            edge = (1.0 - m.yes_price) * 0.5
            signals.append(CryptoSnipeSignal(
                market_id=m.market_id, question=m.question,
                side="YES", token_id=m.yes_token_id,
                market_price=m.yes_price, edge=edge,
                hours_to_resolve=m.hours_to_resolve,
            ))

        elif m.yes_price <= (1.0 - emin):
            if not _side_allowed("NO"):
                continue
            no_price = m.no_price
            if emin <= no_price <= emax:
                edge = (1.0 - no_price) * 0.5
                signals.append(CryptoSnipeSignal(
                    market_id=m.market_id, question=m.question,
                    side="NO", token_id=m.no_token_id,
                    market_price=no_price, edge=edge,
                    hours_to_resolve=m.hours_to_resolve,
                ))

    if signals:
        logger.info(f"CRYPTO SNIPE: {len(signals)} signals ({signals[0].market_price:.2f})")

    return signals
