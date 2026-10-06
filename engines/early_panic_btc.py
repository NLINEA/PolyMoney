"""Historical research signal functions. Thresholds require independent validation. No execution or performance claim."""


import logging


import time


from dataclasses import dataclass


from typing import Optional


logger = logging.getLogger("engine.early_panic_btc")


_loss_count = 0


_stopped = False


_active_entries: dict = {}  # market_id -> entry timestamp


_ENTRY_TTL = 300  # 5 min — enough for one market lifetime


_trend_cache: dict = {}


TIER_A_MIN = 0.05           # kept for reference only — no entries accepted in this band


TIER_A_MAX = 0.19


TIER_B_MIN = 0.20           # Config #10: min entry raised from 0.19 to 0.20 (kill Tier A)


TIER_B_MAX = 0.26


PANIC_PRICE_FLOOR = TIER_B_MIN  # effectively kills Tier A


PANIC_PRICE_MAX = TIER_B_MAX


TARGET_MULTIPLE = 2.0


def _panic_target(price: float):
    """Return (tier_name, target_price) if price in Tier B band, else None.
    Tier A disabled (Config #10, 2026-04-21): 9 historical Tier A trades 0W-9L."""
    if TIER_B_MIN <= price <= TIER_B_MAX:
        return ("B", min(0.99, round(price * TARGET_MULTIPLE, 4)))
    return None


TARGET_HOURS_MIN = 0.05     # 3 min remaining (skip if <3 min left)


TARGET_HOURS_MAX = 0.0833   # 5 min remaining (skip if >5 min — not BTC Up/Down)


BTC_TREND_THRESHOLD = 0.0015 # 0.15% — tightened 2026-04-23 from 0.35%. 100-trade BTC-enriched analysis: adverse 5m moves 0.1-0.3% killed profitability (-$7.58 on 32 conflict trades). 0.15% is knee of saved-$ curve (+$7.88 saved on 18 blocks) without killing winners.


BTC_TREND_30M_THRESHOLD = 0.003   # 0.30% — added 2026-04-24 to catch slow cumulative drift (10-loss streak 04:00-07:00 UTC during BTC +0.75%/4.5hr persistent rise).


MAX_ACTIVE_ENTRIES = 3      # Cap concurrent positions


CONSECUTIVE_LOSS_STOP = 3


def _fetch_btc_trend_5min() -> Optional[float]:
    from core import public_data as requests
    resp = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": "1m", "limit": 5},
        timeout=3,
    )
    candles = resp.json()
    if not isinstance(candles, list) or len(candles) < 5:
        return None
    first_open = float(candles[0][1])
    last_close = float(candles[-1][4])
    return (last_close - first_open) / first_open


def _btc_trend_5min() -> Optional[float]:
    """BTC 5-min trend shared across bots via the process-local research cache."""
    from core.shared_cache import get_or_fetch
    return get_or_fetch("btc_trend_5min_binance", _fetch_btc_trend_5min, 5)


def _fetch_btc_trend_30min() -> Optional[float]:
    """BTC 30-min cumulative trend — catches slow drift missed by 5m/1m filters.
    Added 2026-04-24 after 10 consecutive losses during slow BTC uptrend (+0.75% over 4.5hr
    but each 5m window <0.15% threshold)."""
    from core import public_data as requests
    resp = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": "1m", "limit": 30},
        timeout=3,
    )
    candles = resp.json()
    if not isinstance(candles, list) or len(candles) < 30:
        return None
    first_open = float(candles[0][1])
    last_close = float(candles[-1][4])
    return (last_close - first_open) / first_open


def _btc_trend_30min() -> Optional[float]:
    """BTC 30-min trend cached 30s (changes slowly)."""
    from core.shared_cache import get_or_fetch
    return get_or_fetch("btc_trend_30min_binance", _fetch_btc_trend_30min, 30)


def _fetch_btc_er_15min() -> Optional[float]:
    """Historical parameter hypothesis; requires independent validation."""
    from core import public_data as requests
    resp = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": "1m", "limit": 16},
        timeout=3,
    )
    c = resp.json()
    if not isinstance(c, list) or len(c) < 16:
        return None
    closes = [float(x[4]) for x in c]
    net = abs(closes[-1] - closes[0])
    sum_moves = sum(abs(closes[i] - closes[i-1]) for i in range(1, len(closes)))
    if sum_moves == 0:
        return 0.0
    return net / sum_moves


def _btc_er_15min() -> Optional[float]:
    """Cached 15s (balances freshness vs API calls)."""
    from core.shared_cache import get_or_fetch
    return get_or_fetch("btc_er_15min_binance", _fetch_btc_er_15min, 15)


BTC_REGIME_MILD_TREND_MIN = 0.35   # ER threshold: start of danger zone


BTC_REGIME_MILD_TREND_MAX = 0.55   # end of danger zone (above = small-sample trending wins)


def _in_danger_regime() -> bool:
    """True if BTC is in mild-trend zone (0.35-0.55 Efficiency Ratio) — EP loses here."""
    er = _btc_er_15min()
    if er is None:
        return False
    return BTC_REGIME_MILD_TREND_MIN <= er <= BTC_REGIME_MILD_TREND_MAX


def _fetch_btc_mom_1min() -> Optional[float]:
    """BTC 1-minute momentum: (curr 1m close - prev 1m close) / prev. Config #10 filter."""
    from core import public_data as requests
    resp = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": "1m", "limit": 2},
        timeout=3,
    )
    candles = resp.json()
    if not isinstance(candles, list) or len(candles) < 2:
        return None
    prev_close = float(candles[0][4])
    curr_close = float(candles[-1][4])
    if prev_close <= 0: return None
    return (curr_close - prev_close) / prev_close


def _btc_mom_1min() -> Optional[float]:
    """BTC 1-min momentum shared via cache (3s TTL — changes fast)."""
    from core.shared_cache import get_or_fetch
    return get_or_fetch("btc_mom_1min_binance", _fetch_btc_mom_1min, 3)


BTC_MOM_CONFLICT_THRESHOLD = 0.0003


from collections import deque as _deque_rt


BTC_REALTIME_ADVERSE_THRESHOLD = 0.0010   # 0.10% adverse in 60s real-time


BTC_REALTIME_WINDOW_SEC = 60


_btc_price_samples = _deque_rt(maxlen=40)   # (ts, price) — covers 120s with 3s scan interval


def _record_btc_spot(price: Optional[float]):
    """Called every scan — record BTC current price for real-time adverse check."""
    if price is None or price <= 0:
        return
    _btc_price_samples.append((time.time(), price))


def _btc_realtime_adverse(side: str) -> bool:
    """True if BTC moved adversely ≥0.10% in last 60s (real-time, not candle-based).
    YES side: BTC falling is adverse.
    NO side: BTC rallying is adverse."""
    if len(_btc_price_samples) < 5:
        return False  # insufficient history — don't claim adverse
    now_ts, now_p = _btc_price_samples[-1]
    target_ts = now_ts - BTC_REALTIME_WINDOW_SEC
    # Find oldest sample >= target_ts
    older_p = None
    for ts, p in _btc_price_samples:
        if ts >= target_ts:
            older_p = p
            break
    if older_p is None or older_p <= 0:
        return False
    move_pct = (now_p - older_p) / older_p
    if side == "YES" and move_pct < -BTC_REALTIME_ADVERSE_THRESHOLD:
        return True
    if side == "NO" and move_pct > BTC_REALTIME_ADVERSE_THRESHOLD:
        return True
    return False


def _mom_conflicts(side: str) -> bool:
    """True if BTC 1-min momentum matches panic direction (thesis likely wrong)."""
    m = _btc_mom_1min()
    if m is None: return False
    if side == "YES" and m < -BTC_MOM_CONFLICT_THRESHOLD:
        return True
    if side == "NO" and m > BTC_MOM_CONFLICT_THRESHOLD:
        return True
    return False


from collections import deque as _deque


_price_history: dict = {}


PANIC_DEPTH_WINDOW = 60      # seconds


PANIC_DEPTH_RATIO = 0.60     # current must be ≤ 60% of recent max (i.e. ≥40% drop)


def _log_analysis(event_type, market, side=None, filter_name=None, reason=None, btc_price=None):
    """Research diagnostics only; never opens the private runtime database."""
    logger.debug("%s market=%s side=%s filter=%s reason=%s",
                 event_type, market.market_id, side, filter_name, reason)


def _record_price(market_id: str, side: str, price: float):
    """Update per-side price history for panic depth check."""
    key = f"{market_id}:{side}"
    dq = _price_history.get(key)
    if dq is None:
        dq = _deque(maxlen=30)  # 30 samples × ~5s scan = ~150s history
        _price_history[key] = dq
    dq.append((time.time(), price))


def _is_real_panic(market_id: str, side: str, current_price: float) -> bool:
    """True if current price dropped ≥40% from max in last 60s (real panic, not drift)."""
    key = f"{market_id}:{side}"
    dq = _price_history.get(key)
    if not dq or len(dq) < 3:
        return False  # insufficient history — don't claim panic
    now = time.time()
    cutoff = now - PANIC_DEPTH_WINDOW
    recent_prices = [p for ts, p in dq if ts >= cutoff]
    if not recent_prices:
        return False
    max_recent = max(recent_prices)
    return current_price <= max_recent * PANIC_DEPTH_RATIO


def _acceptable_side_hour(side: str) -> bool:
    """Historical parameter hypothesis; requires independent validation."""
    from datetime import datetime
    from datetime import timezone, timedelta
    h = datetime.now(timezone(timedelta(hours=8))).hour
    if side == "NO" and 6 <= h < 12:
        return True
    if side == "YES" and 18 <= h < 24:
        return True
    return False


def _cleanup_price_history():
    """Drop entries older than 300s to prevent unbounded growth."""
    now = time.time()
    for key in list(_price_history.keys()):
        dq = _price_history[key]
        # Drop if all entries are stale
        if dq and (now - dq[-1][0]) > 300:
            del _price_history[key]


PANIC_RATE_MIN = 0.01   # 1% per second — retail panics are this fast


PANIC_RATE_MAX = 0.02   # 2% per second — above this = real crash


BLOCKED_HOUR_MIN = 18   # SGT 18:00 start blocking


BLOCKED_HOUR_MAX = 24   # through 24:00 (midnight)


def _panic_rate(market_id: str, side: str, current_price: float):
    """Drop rate (pct per sec) from 60s peak to current. Returns None if insufficient history."""
    key = f"{market_id}:{side}"
    dq = _price_history.get(key)
    if not dq or len(dq) < 3:
        return None
    now = time.time()
    cutoff = now - PANIC_DEPTH_WINDOW
    recent = [(ts, p) for ts, p in dq if ts >= cutoff]
    if not recent:
        return None
    peak60 = max(p for _, p in recent)
    if peak60 <= 0 or current_price >= peak60:
        return None
    peak_ts = max(ts for ts, p in recent if p == peak60)
    speed_sec = max(1.0, now - peak_ts)
    drop_pct = (peak60 - current_price) / peak60
    return drop_pct / speed_sec


def _rate_ok(market_id: str, side: str, current_price: float) -> bool:
    """True if panic rate is within 1-2%/s window (retail panic, not flash crash / drift)."""
    r = _panic_rate(market_id, side, current_price)
    if r is None:
        return False
    return PANIC_RATE_MIN <= r <= PANIC_RATE_MAX


def _in_trading_hours() -> bool:
    """Block only US hours (SGT 18-24). Host TZ assumed SGT."""
    from datetime import datetime
    hour = datetime.now().hour
    return not (BLOCKED_HOUR_MIN <= hour < BLOCKED_HOUR_MAX)


def _strong_trend(side: str) -> bool:
    """True if BTC is in strong directional move that would invalidate panic.
    Checks both 5m (fast) and 30m (slow drift) timeframes.
    """
    # 5m check — fast moves
    trend_5m = _btc_trend_5min()
    if trend_5m is not None:
        if side == "YES" and trend_5m < -BTC_TREND_THRESHOLD:
            logger.info(f"Early panic SKIP YES: BTC 5m trend {trend_5m*100:+.2f}% (strong down)")
            return True
        if side == "NO" and trend_5m > BTC_TREND_THRESHOLD:
            logger.info(f"Early panic SKIP NO: BTC 5m trend {trend_5m*100:+.2f}% (strong up)")
            return True
    # 30m check — slow drift (catches cumulative moves missed by 5m)
    trend_30m = _btc_trend_30min()
    if trend_30m is not None:
        if side == "YES" and trend_30m < -BTC_TREND_30M_THRESHOLD:
            logger.info(f"Early panic SKIP YES: BTC 30m drift {trend_30m*100:+.2f}% (slow down)")
            return True
        if side == "NO" and trend_30m > BTC_TREND_30M_THRESHOLD:
            logger.info(f"Early panic SKIP NO: BTC 30m drift {trend_30m*100:+.2f}% (slow up)")
            return True
    return False


def _cleanup_active_entries():
    """Remove entries older than TTL (positions should have closed)."""
    now = time.time()
    expired = [k for k, v in _active_entries.items() if now - v > _ENTRY_TTL]
    for k in expired:
        del _active_entries[k]


@dataclass
class EarlyPanicSignal:
    market_id: str
    question: str
    side: str  # YES or NO
    token_id: str
    market_price: float  # the panic price we're buying at
    edge: float  # estimated edge (target - current)
    target_price: float  # exit target
    stop_price: float  # exit stop
    hours_to_resolve: float
    strategy: str = "early_panic_btc"


def reset_early_panic():
    """Reset loss counter."""
    global _loss_count, _stopped
    _loss_count = 0
    _stopped = False
    logger.info("Early panic reset")


def record_early_panic_win():
    """Reset consecutive loss counter on win."""
    global _loss_count
    if _loss_count > 0:
        logger.info(f"Early panic consecutive losses reset ({_loss_count} -> 0)")
    _loss_count = 0


def record_early_panic_loss():
    """Historical parameter hypothesis; requires independent validation."""
    global _loss_count
    _loss_count += 1
    if _loss_count >= CONSECUTIVE_LOSS_STOP:
        logger.info(f"Early panic {_loss_count} consecutive losses (NOT stopping — bot edge_decay freeze is the safety net)")


def find_early_panic_signals(markets: list) -> list:
    """Return list of EarlyPanicSignal for markets matching the panic pattern."""
    global _stopped
    if _stopped:
        return []

    _cleanup_active_entries()

    # Cap concurrent positions
    if len(_active_entries) >= MAX_ACTIVE_ENTRIES:
        return []

    # NEGATIVE value on 39-trade dataset. Removed. Only BTC 1-min momentum + Tier B +
    # panic_depth signal trigger retained.

    signals = []
    _cleanup_price_history()

    # Get BTC price once per scan for logging efficiency
    btc_now = None
    try:
        from engines.crypto_scanner import get_btc_price
        btc_now = get_btc_price()
    except Exception:
        pass

    # Record real-time BTC spot for adverse-move detection (2026-04-24)
    _record_btc_spot(btc_now)

    for m in markets:
        # Filter to BTC Up/Down only
        q = (m.question or "").lower()
        if "up or down" not in q:
            continue
        if "bitcoin" not in q and "btc" not in q:
            continue

        # Log scan snapshot event (tick logging moved to bot.py 2s exit loop)
        _log_analysis("scan", m, btc_price=btc_now)

        # Time window: only first 2 min of new BTC Up/Down market
        if m.hours_to_resolve < TARGET_HOURS_MIN or m.hours_to_resolve > TARGET_HOURS_MAX:
            continue

        # Record price history for panic depth detection (always, for both sides)
        _record_price(m.market_id, "YES", m.yes_price)
        _record_price(m.market_id, "NO", m.no_price)

        # Skip if already entered
        if m.market_id in _active_entries:
            continue

        # Config #10: only Tier B (0.20-0.26). Tier A disabled.
        # Check YES side panic
        yes_tier = _panic_target(m.yes_price)
        if yes_tier is not None:
            _log_analysis("candidate", m, side="YES", btc_price=btc_now, reason=f"price {m.yes_price:.3f} in band")
            if _strong_trend("YES"):
                _log_analysis("filter_skip", m, side="YES", btc_price=btc_now, filter_name="trend_5min", reason="strong downtrend")
                continue
            # Panic depth filter: require real crash, not slow drift
            if not _is_real_panic(m.market_id, "YES", m.yes_price):
                _log_analysis("filter_skip", m, side="YES", btc_price=btc_now, filter_name="panic_depth", reason="drift not crash")
                logger.debug(f"Early panic SKIP YES: no recent crash for {m.market_id} (current {m.yes_price:.3f})")
                continue
            if _mom_conflicts("YES"):
                _log_analysis("filter_skip", m, side="YES", btc_price=btc_now, filter_name="btc_mom_1min", reason="BTC 1m momentum conflicts YES")
                continue
            # Regime filter (2026-04-24): skip if BTC in mild-trend confused zone (ER 0.35-0.55)
            if _in_danger_regime():
                er = _btc_er_15min()
                _log_analysis("filter_skip", m, side="YES", btc_price=btc_now, filter_name="btc_regime", reason=f"ER 15m={er:.3f} mild trend")
                logger.info(f"Early panic SKIP YES: BTC ER 15m {er:.3f} (danger zone 0.35-0.55)")
                continue
            # Real-time adverse check (2026-04-24): catch intra-minute crashes missed by candle filter
            if _btc_realtime_adverse("YES"):
                _log_analysis("filter_skip", m, side="YES", btc_price=btc_now, filter_name="btc_realtime", reason="live 60s adverse ≥0.10%")
                continue
            # Side×Hour filter (2026-04-24): only trade YES at SGT 18-24 night
            if not _acceptable_side_hour("YES"):
                _log_analysis("filter_skip", m, side="YES", btc_price=btc_now, filter_name="side_hour", reason="YES outside 18-24 SGT night window")
                continue
            _log_analysis("filter_pass", m, side="YES", btc_price=btc_now, reason="all filters passed")
            tier_name, target = yes_tier
            edge = target - m.yes_price
            signals.append(EarlyPanicSignal(
                market_id=m.market_id,
                question=m.question,
                side="YES",
                token_id=m.yes_token_id,
                market_price=m.yes_price,
                edge=edge,
                target_price=target,
                stop_price=0.0,
                hours_to_resolve=m.hours_to_resolve,
            ))
            _active_entries[m.market_id] = time.time()
            logger.info(
                f"EARLY PANIC BUY YES [{tier_name}]: {m.question[:50]} @ ${m.yes_price:.3f} "
                f"target=${target:.3f} edge=${edge:.3f} (tier {tier_name})"
            )
            continue

        # Check NO side panic
        no_tier = _panic_target(m.no_price)
        if no_tier is not None:
            _log_analysis("candidate", m, side="NO", btc_price=btc_now, reason=f"price {m.no_price:.3f} in band")
            if _strong_trend("NO"):
                _log_analysis("filter_skip", m, side="NO", btc_price=btc_now, filter_name="trend_5min", reason="strong uptrend")
                continue
            if not _is_real_panic(m.market_id, "NO", m.no_price):
                _log_analysis("filter_skip", m, side="NO", btc_price=btc_now, filter_name="panic_depth", reason="drift not crash")
                logger.debug(f"Early panic SKIP NO: no recent crash for {m.market_id} (current {m.no_price:.3f})")
                continue
            if _mom_conflicts("NO"):
                _log_analysis("filter_skip", m, side="NO", btc_price=btc_now, filter_name="btc_mom_1min", reason="BTC 1m momentum conflicts NO")
                continue
            # Regime filter (2026-04-24): skip mild-trend zone
            if _in_danger_regime():
                er = _btc_er_15min()
                _log_analysis("filter_skip", m, side="NO", btc_price=btc_now, filter_name="btc_regime", reason=f"ER 15m={er:.3f} mild trend")
                logger.info(f"Early panic SKIP NO: BTC ER 15m {er:.3f} (danger zone 0.35-0.55)")
                continue
            # Real-time adverse check (2026-04-24): catch intra-minute rallies missed by candle filter
            if _btc_realtime_adverse("NO"):
                _log_analysis("filter_skip", m, side="NO", btc_price=btc_now, filter_name="btc_realtime", reason="live 60s adverse ≥0.10%")
                continue
            # Side×Hour filter (2026-04-24): only trade NO at SGT 6-12 morning
            if not _acceptable_side_hour("NO"):
                _log_analysis("filter_skip", m, side="NO", btc_price=btc_now, filter_name="side_hour", reason="NO outside 6-12 SGT morning window")
                continue
            _log_analysis("filter_pass", m, side="NO", btc_price=btc_now, reason="all filters passed")
            tier_name, target = no_tier
            edge = target - m.no_price
            signals.append(EarlyPanicSignal(
                market_id=m.market_id,
                question=m.question,
                side="NO",
                token_id=m.no_token_id,
                market_price=m.no_price,
                edge=edge,
                target_price=target,
                stop_price=0.0,
                hours_to_resolve=m.hours_to_resolve,
            ))
            _active_entries[m.market_id] = time.time()
            logger.info(
                f"EARLY PANIC BUY NO [{tier_name}]: {m.question[:50]} @ ${m.no_price:.3f} "
                f"target=${target:.3f} edge=${edge:.3f} (tier {tier_name})"
            )

    if signals:
        logger.info(f"EARLY PANIC: {len(signals)} signals found")

    return signals
