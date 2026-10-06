"""Process-local state for legacy signal loss counters. Never reads account DBs."""
from functools import lru_cache
import atexit
from core.state_store import StateStore


@lru_cache(maxsize=1)
def get_signal_store():
    return StateStore(":memory:")


def _close():
    if get_signal_store.cache_info().currsize:
        get_signal_store().close()


atexit.register(_close)
