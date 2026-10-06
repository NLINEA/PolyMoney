"""
SQLite-backed runtime state store.
Persists: account state, positions, health, timeline, whale registry, bankroll history.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class StateStore:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = str(Path(db_path))
        self._lock = RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self.current_run_id: Optional[str] = None
        self._init_schema()

    def close(self):
        """Release the SQLite connection owned by this research store."""
        with self._lock:
            self._conn.close()

    def _ensure_column(self, table: str, column: str, ddl: str):
        cols = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {str(c["name"]) for c in cols}
        if column not in names:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _init_schema(self):
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS account_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash REAL NOT NULL, daily_trades INTEGER NOT NULL,
                    daily_pnl REAL NOT NULL, trade_date TEXT NOT NULL,
                    total_pnl REAL NOT NULL, is_stopped INTEGER NOT NULL,
                    next_position_id INTEGER NOT NULL,
                    day_start_equity REAL NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY, token_id TEXT NOT NULL,
                    market TEXT NOT NULL, side TEXT NOT NULL, shares REAL NOT NULL,
                    entry_price REAL NOT NULL, cost REAL NOT NULL, engine TEXT NOT NULL,
                    expected_payout REAL, settle_at TEXT, opened_at TEXT NOT NULL,
                    status TEXT NOT NULL, payout REAL, pnl REAL, closed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);
                CREATE TABLE IF NOT EXISTS source_health (
                    source TEXT PRIMARY KEY, ok_count INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    consecutive_errors INTEGER NOT NULL DEFAULT 0,
                    last_ok_at TEXT, last_error_at TEXT, last_latency_ms REAL,
                    last_error_message TEXT
                );
                CREATE TABLE IF NOT EXISTS health_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
                    source TEXT NOT NULL, status TEXT NOT NULL,
                    latency_ms REAL, error_message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_he_src_ts ON health_events(source, ts_utc);
                CREATE TABLE IF NOT EXISTS timeline_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
                    engine TEXT NOT NULL, event_type TEXT NOT NULL, level TEXT NOT NULL,
                    reason_code TEXT, message TEXT, payload_json TEXT, run_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tl_ts ON timeline_events(ts_utc);
                CREATE INDEX IF NOT EXISTS idx_tl_run ON timeline_events(run_id);
                CREATE TABLE IF NOT EXISTS runtime_kv (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS whale_wallets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL UNIQUE,
                    label TEXT, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bankroll_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
                    bankroll REAL NOT NULL, cash REAL NOT NULL, locked REAL NOT NULL,
                    total_pnl REAL NOT NULL, daily_pnl REAL NOT NULL,
                    positions_count INTEGER NOT NULL DEFAULT 0, run_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_bh_ts ON bankroll_history(ts_utc);
            """)
            self._ensure_column("source_health", "consecutive_errors", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("positions", "question", "TEXT DEFAULT ''")
            self._ensure_column("positions", "model_prob", "REAL DEFAULT 0")
            self._ensure_column("positions", "edge", "REAL DEFAULT 0")
            self._conn.commit()

    def set_run_id(self, run_id: Optional[str]):
        self.current_run_id = run_id
        if run_id:
            self.set_kv("last_run_id", run_id)

    # ─── Account State ──────────────────────
    def load_account_state(self) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM account_state WHERE id = 1").fetchone()
            return dict(row) if row else None

    def save_account_state(self, data: dict):
        with self._lock:
            p = {
                "cash": float(data["cash"]), "daily_trades": int(data["daily_trades"]),
                "daily_pnl": float(data["daily_pnl"]), "trade_date": str(data["trade_date"]),
                "total_pnl": float(data["total_pnl"]),
                "is_stopped": 1 if data["is_stopped"] else 0,
                "next_position_id": int(data["next_position_id"]),
                "day_start_equity": float(data["day_start_equity"]),
                "updated_at": data.get("updated_at", utc_now_iso()),
            }
            self._conn.execute("""
                INSERT INTO account_state (id, cash, daily_trades, daily_pnl, trade_date,
                    total_pnl, is_stopped, next_position_id, day_start_equity, updated_at)
                VALUES (1, :cash, :daily_trades, :daily_pnl, :trade_date,
                    :total_pnl, :is_stopped, :next_position_id, :day_start_equity, :updated_at)
                ON CONFLICT(id) DO UPDATE SET
                    cash=excluded.cash, daily_trades=excluded.daily_trades,
                    daily_pnl=excluded.daily_pnl, trade_date=excluded.trade_date,
                    total_pnl=excluded.total_pnl, is_stopped=excluded.is_stopped,
                    next_position_id=excluded.next_position_id,
                    day_start_equity=excluded.day_start_equity, updated_at=excluded.updated_at
            """, p)
            self._conn.commit()

    # ─── Positions ──────────────────────────
    def load_open_positions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY opened_at ASC").fetchall()
            return [dict(r) for r in rows]

    def upsert_open_position(self, pos: dict):
        with self._lock:
            self._conn.execute("""
                INSERT INTO positions (position_id, token_id, market, side, shares,
                    entry_price, cost, engine, expected_payout, settle_at, opened_at, status,
                    question, model_prob, edge)
                VALUES (:position_id, :token_id, :market, :side, :shares,
                    :entry_price, :cost, :engine, :expected_payout, :settle_at, :opened_at, 'OPEN',
                    :question, :model_prob, :edge)
                ON CONFLICT(position_id) DO UPDATE SET
                    token_id=excluded.token_id, market=excluded.market, side=excluded.side,
                    shares=excluded.shares, entry_price=excluded.entry_price, cost=excluded.cost,
                    engine=excluded.engine, expected_payout=excluded.expected_payout,
                    settle_at=excluded.settle_at, opened_at=excluded.opened_at, status='OPEN',
                    question=excluded.question, model_prob=excluded.model_prob, edge=excluded.edge
            """, pos)
            self._conn.commit()

    def close_position(self, position_id: str, payout: float, pnl: float, closed_at: Optional[str] = None):
        with self._lock:
            self._conn.execute(
                "UPDATE positions SET status='CLOSED', payout=?, pnl=?, closed_at=? WHERE position_id=?",
                (float(payout), float(pnl), closed_at or utc_now_iso(), position_id))
            self._conn.commit()

    # ─── Bankroll History (NEW) ─────────────
    def record_bankroll_snapshot(self, bankroll: float, cash: float, locked: float,
                                 total_pnl: float, daily_pnl: float, positions_count: int = 0):
        with self._lock:
            self._conn.execute("""
                INSERT INTO bankroll_history (ts_utc, bankroll, cash, locked, total_pnl,
                    daily_pnl, positions_count, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (utc_now_iso(), bankroll, cash, locked, total_pnl,
                  daily_pnl, positions_count, self.current_run_id))
            self._conn.commit()

    def get_bankroll_history(self, hours: int = 24, limit: int = 500) -> list[dict]:
        """Return most-recent N rows within `hours` window, sorted ascending.
        2026-04-22 fix: previous ASC+LIMIT dropped LATEST rows when entries exceeded limit
        → chart showed stale data up to 1-2 days behind. Now DESC+LIMIT then reverse."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._lock:
            rows = self._conn.execute("""
                SELECT ts_utc, bankroll, cash, locked, total_pnl, daily_pnl, positions_count
                FROM bankroll_history WHERE ts_utc >= ? ORDER BY ts_utc DESC LIMIT ?
            """, (cutoff, limit)).fetchall()
            return [dict(r) for r in reversed(rows)]

    # ─── Timeline ──────────────────────────
    def add_timeline_event(self, *, engine: str, event_type: str, level: str = "INFO",
                           reason_code: str = "", message: str = "",
                           payload: Optional[dict[str, Any]] = None,
                           run_id: Optional[str] = None, ts_utc: Optional[str] = None) -> int:
        payload_json = None
        if payload is not None:
            try:
                payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
            except Exception:
                payload_json = json.dumps({"raw": str(payload)}, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute("""
                INSERT INTO timeline_events (ts_utc, engine, event_type, level,
                    reason_code, message, payload_json, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts_utc or utc_now_iso(), (engine or "system")[:32],
                  (event_type or "unknown")[:64], (level or "INFO")[:16],
                  (reason_code or "")[:80], (message or "")[:400],
                  payload_json, run_id or self.current_run_id))
            self._conn.commit()
            return int(cur.lastrowid)

    def query_timeline(self, *, limit=200, engine="", event_type="",
                       level="", only_errors=False) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        clauses, params = [], []
        if engine:
            clauses.append("engine = ?"); params.append(engine)
        if event_type:
            clauses.append("event_type = ?"); params.append(event_type)
        if only_errors:
            clauses.append("level = 'ERROR'")
        elif level:
            clauses.append("level = ?"); params.append(level)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(f"""
                SELECT id, ts_utc, engine, event_type, level, reason_code,
                       message, payload_json, run_id
                FROM timeline_events {where} ORDER BY id DESC LIMIT ?
            """, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            raw = item.get("payload_json")
            if raw:
                try:
                    item["payload"] = json.loads(raw)
                except Exception:
                    item["payload"] = {"raw": raw}
            else:
                item["payload"] = None
            out.append(item)
        return out

    def get_recent_risk_rejects(self, limit=5) -> list[dict]:
        return self.query_timeline(limit=limit, event_type="risk_reject")

    def get_latest_run_id(self) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("""
                SELECT run_id FROM timeline_events
                WHERE run_id IS NOT NULL AND run_id != ''
                ORDER BY id DESC LIMIT 1
            """).fetchone()
            if row and row["run_id"]:
                return str(row["run_id"])
        return self.get_kv("last_run_id")

    # ─── Health ─────────────────────────────
    def record_health_ok(self, source: str, latency_ms: Optional[float] = None):
        ts = utc_now_iso()
        with self._lock:
            self._conn.execute("""
                INSERT INTO source_health (source, ok_count, error_count, consecutive_errors,
                    last_ok_at, last_latency_ms)
                VALUES (?, 1, 0, 0, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    ok_count = source_health.ok_count + 1, consecutive_errors = 0,
                    last_ok_at = excluded.last_ok_at, last_latency_ms = excluded.last_latency_ms
            """, (source, ts, latency_ms))
            self._conn.execute(
                "INSERT INTO health_events (ts_utc, source, status, latency_ms) VALUES (?, ?, 'OK', ?)",
                (ts, source, latency_ms))
            self._conn.commit()

    def record_health_error(self, source: str, error_message: str):
        ts = utc_now_iso()
        short = (error_message or "")[:500]
        with self._lock:
            self._conn.execute("""
                INSERT INTO source_health (source, ok_count, error_count, consecutive_errors,
                    last_error_at, last_error_message)
                VALUES (?, 0, 1, 1, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    error_count = source_health.error_count + 1,
                    consecutive_errors = source_health.consecutive_errors + 1,
                    last_error_at = excluded.last_error_at,
                    last_error_message = excluded.last_error_message
            """, (source, ts, short))
            self._conn.execute(
                "INSERT INTO health_events (ts_utc, source, status, error_message) VALUES (?, ?, 'ERROR', ?)",
                (ts, source, short))
            self._conn.commit()

    def evaluate_health_gate(self, window_minutes: int = 15) -> dict:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
        with self._lock:
            sources = self._conn.execute(
                "SELECT * FROM source_health ORDER BY source").fetchall()
            win_rows = self._conn.execute("""
                SELECT source,
                       SUM(CASE WHEN status='OK' THEN 1 ELSE 0 END) AS ok_15m,
                       SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) AS err_15m,
                       COUNT(*) AS total_15m
                FROM health_events WHERE ts_utc >= ? GROUP BY source
            """, (cutoff,)).fetchall()

        win_map = {r["source"]: dict(r) for r in win_rows}
        now = datetime.now(timezone.utc)
        out_sources = []
        for row in sources:
            item = dict(row)
            win = win_map.get(item["source"], {})
            total = int(win.get("total_15m") or 0)
            err = int(win.get("err_15m") or 0)
            item["error_ratio_15m"] = (err / total) if total > 0 else 0.0
            last_ok = _parse_iso(item.get("last_ok_at"))
            item["stale_seconds"] = None if not last_ok else max(0, int((now - last_ok).total_seconds()))
            item["state"] = "GREEN"
            if item["error_ratio_15m"] > 0.4:
                item["state"] = "RED"
            if item["source"] == "btc.market_discovery" and (item["stale_seconds"] or 999) > 120:
                item["state"] = "RED"
            if int(item.get("consecutive_errors") or 0) >= 3:
                item["state"] = "RED"
            out_sources.append(item)

        reasons = []
        state = "GREEN"
        for s in out_sources:
            if s["state"] == "RED":
                state = "RED"
                reasons.append(f"{s['source']} unhealthy")
        if not reasons:
            reasons = ["all sources healthy"]

        return {"state": state, "reasons": reasons, "sources": out_sources,
                "checked_at": utc_now_iso(), "window_minutes": window_minutes}

    # ─── KV / Profile ──────────────────────
    def set_kv(self, key: str, value: str):
        with self._lock:
            self._conn.execute("""
                INSERT INTO runtime_kv (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, (key, value, utc_now_iso()))
            self._conn.commit()

    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM runtime_kv WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set_active_profile(self, profile: str):
        self.set_kv("active_profile", profile)

    def get_active_profile(self, default: str = "brief_default") -> str:
        return self.get_kv("active_profile", default) or default

    # ─── Whale Registry ────────────────────
    def list_whales(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, address, label, enabled, created_at, updated_at FROM whale_wallets ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_enabled_whale_addresses(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT address FROM whale_wallets WHERE enabled=1 ORDER BY id").fetchall()
            return [str(r["address"]) for r in rows]

    def upsert_whale(self, address: str, label: str = "", enabled: bool = True) -> int:
        addr = (address or "").strip().lower()
        if not addr:
            raise ValueError("address_required")
        now = utc_now_iso()
        with self._lock:
            self._conn.execute("""
                INSERT INTO whale_wallets (address, label, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    label=excluded.label, enabled=excluded.enabled, updated_at=excluded.updated_at
            """, (addr, (label or "").strip(), 1 if enabled else 0, now, now))
            row = self._conn.execute("SELECT id FROM whale_wallets WHERE address=?", (addr,)).fetchone()
            self._conn.commit()
        return int(row["id"])

    def patch_whale(self, whale_id: int, *, address=None, label=None, enabled=None) -> bool:
        updates, params = [], []
        if address is not None:
            updates.append("address=?"); params.append(address.strip().lower())
        if label is not None:
            updates.append("label=?"); params.append(label.strip())
        if enabled is not None:
            updates.append("enabled=?"); params.append(1 if enabled else 0)
        if not updates:
            return False
        updates.append("updated_at=?"); params.append(utc_now_iso())
        params.append(int(whale_id))
        with self._lock:
            cur = self._conn.execute(f"UPDATE whale_wallets SET {', '.join(updates)} WHERE id=?", params)
            self._conn.commit()
            return cur.rowcount > 0

    def delete_whale(self, whale_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM whale_wallets WHERE id=?", (int(whale_id),))
            self._conn.commit()
            return cur.rowcount > 0

    def import_whales_from_env_if_empty(self, addresses: list[str]) -> int:
        if (self.get_kv("whales_env_bootstrapped", "0") or "0") == "1":
            return 0
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM whale_wallets").fetchone()
            if int(row["c"] or 0) > 0:
                self.set_kv("whales_env_bootstrapped", "1")
                return 0
        imported = 0
        for idx, raw in enumerate(addresses):
            addr = (raw or "").strip().lower()
            if not addr:
                continue
            self.upsert_whale(addr, label=f"env_{idx+1}", enabled=True)
            imported += 1
        self.set_kv("whales_env_bootstrapped", "1")
        return imported
