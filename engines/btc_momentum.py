"""Historical research signal functions. Thresholds require independent validation. No execution or performance claim."""


import logging


import time


from dataclasses import dataclass


from typing import Optional


from engines.early_panic_btc import (
    _strong_trend, _is_real_panic, _record_price,
    TIER_A_MIN, TIER_B_MAX,
    MAX_ACTIVE_ENTRIES,
)


logger = logging.getLogger("engine.btc_momentum")


_loss_count = 0


_stopped = False


_active_entries: dict = {}  # market_id -> entry timestamp


_ENTRY_TTL = 300


WINNING_SIDE_MIN = 1.0 - TIER_B_MAX  # 0.74


WINNING_SIDE_MAX = 0.85


CONSECUTIVE_LOSS_STOP = 3


@dataclass
class BtcMomentumSignal:
    market_id: str
    question: str
    side: str
    token_id: str
    market_price: float  # winning-side price at entry
    edge: float
    target_price: float
    stop_price: float
    hours_to_resolve: float
    strategy: str = "btc_momentum"


def reset_btc_momentum():
    global _loss_count, _stopped
    _loss_count = 0
    _stopped = False
    logger.info("BTC momentum reset")


def record_btc_momentum_win():
    global _loss_count
    if _loss_count > 0:
        logger.info(f"BTC momentum consecutive losses reset ({_loss_count} -> 0)")
    _loss_count = 0


def record_btc_momentum_loss():
    """Record loss counter only. No auto-stop (2026-04-22: parity with DryRun).
    Safety net is bot-level edge_decay freeze."""
    global _loss_count
    _loss_count += 1
    if _loss_count >= CONSECUTIVE_LOSS_STOP:
        logger.info(f"BTC momentum {_loss_count} consecutive losses (NOT stopping — bot edge_decay freeze is the safety net)")


def _cleanup_active():
    now = time.time()
    for k in [k for k, v in _active_entries.items() if now - v > _ENTRY_TTL]:
        del _active_entries[k]


def find_btc_momentum_signals(markets: list) -> list:
    """For each panic detected on losing side, return signal to buy winning side."""
    global _stopped
    if _stopped:
        return []
    _cleanup_active()
    if len(_active_entries) >= MAX_ACTIVE_ENTRIES:
        return []

    signals = []
    for m in markets:
        q = (m.question or "").lower()
        if "up or down" not in q:
            continue
        if "bitcoin" not in q and "btc" not in q:
            continue

        # Same time window as Early Panic (first 2 min of new market)
        if m.hours_to_resolve < 0.05 or m.hours_to_resolve > 0.0833:
            continue

        # Maintain price history (shares deque with Early Panic via import)
        _record_price(m.market_id, "YES", m.yes_price)
        _record_price(m.market_id, "NO", m.no_price)

        if m.market_id in _active_entries:
            continue

        # Check if there's a panic on either side
        # If YES is in panic band → winning side is NO (buy NO)
        # If NO is in panic band → winning side is YES (buy YES)
        losing_side = None
        if TIER_A_MIN <= m.yes_price <= TIER_B_MAX:
            losing_side = "YES"
            winning_side = "NO"
            winning_price = m.no_price
            winning_token = m.no_token_id
        elif TIER_A_MIN <= m.no_price <= TIER_B_MAX:
            losing_side = "NO"
            winning_side = "YES"
            winning_price = m.yes_price
            winning_token = m.yes_token_id
        else:
            continue

        # Require real panic on losing side (not drift)
        if not _is_real_panic(m.market_id, losing_side, m.yes_price if losing_side == "YES" else m.no_price):
            continue

        # Sanity check winning-side price
        if not (WINNING_SIDE_MIN <= winning_price <= WINNING_SIDE_MAX):
            continue

        # Skip if BTC trend against momentum direction (strong counter-trend)
        if _strong_trend(winning_side):
            continue

        target = min(0.99, round(winning_price * 2.0, 4))
        edge = target - winning_price
        signals.append(BtcMomentumSignal(
            market_id=m.market_id,
            question=m.question,
            side=winning_side,
            token_id=winning_token,
            market_price=winning_price,
            edge=edge,
            target_price=target,
            stop_price=0.0,
            hours_to_resolve=m.hours_to_resolve,
        ))
        _active_entries[m.market_id] = time.time()
        logger.info(
            f"BTC MOMENTUM BUY {winning_side}: {m.question[:50]} @ ${winning_price:.3f} "
            f"(loser {losing_side} @ ${m.yes_price if losing_side=='YES' else m.no_price:.3f}) "
            f"target=${target:.3f}"
        )

    if signals:
        logger.info(f"BTC MOMENTUM: {len(signals)} signals found")

    return signals
