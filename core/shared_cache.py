"""Process-local research cache for public API data.

Trade decisions stay independent per bot. Only raw upstream data
(Gamma market scans, CoinGecko BTC price, Binance klines) is deduped
to reduce API load and rate-limit risk.
"""
import json
import os
import sqlite3
import threading
import time
from typing import Callable, Optional

_DB_PATH = ":memory:"
_conn: Optional[sqlite3.Connection] = None
_conn_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # check_same_thread=False lets the WS callback thread write tick updates.
        # WAL + autocommit + our _conn_lock keep concurrency safe.
        _conn = sqlite3.connect(_DB_PATH, timeout=5, isolation_level=None, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL)"
        )
    return _conn


def set(key: str, value, ttl: float):
    """Write-through: store a value with TTL, no fetcher. Safe from any thread."""
    try:
        with _conn_lock:
            _get_conn().execute(
                "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), time.time() + ttl),
            )
    except Exception:
        pass


def get(key: str):
    """Read-only: return JSON-deserialized value if fresh, else None."""
    try:
        with _conn_lock:
            row = _get_conn().execute(
                "SELECT value, expires_at FROM cache WHERE key=?", (key,)
            ).fetchone()
        if row and row[1] > time.time():
            return json.loads(row[0])
    except Exception:
        pass
    return None


def get_or_fetch(key: str, fetcher: Callable, ttl: float):
    """Return cached value if fresh, else call fetcher(), cache, return.

    fetcher() return value must be JSON-serialisable. On fetcher exception,
    returns the last stale value if present, else None.
    """
    now = time.time()
    try:
        with _conn_lock:
            row = _get_conn().execute(
                "SELECT value, expires_at FROM cache WHERE key=?", (key,)
            ).fetchone()
    except Exception:
        row = None

    if row and row[1] > now:
        try:
            return json.loads(row[0])
        except Exception:
            pass

    # Cache miss or expired — fetch fresh
    try:
        fresh = fetcher()
        with _conn_lock:
            _get_conn().execute(
                "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                (key, json.dumps(fresh), now + ttl),
            )
        return fresh
    except Exception:
        # Fall back to stale cache if fetcher failed
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None
