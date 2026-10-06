"""Risk manager — position sizing, limits, portfolio tracking."""
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from threading import RLock
from typing import Optional
from core.config import Config, max_daily_trades, max_concurrent, max_position_pct, kelly_fraction, stop_loss_pct

logger = logging.getLogger("risk")


@dataclass
class Position:
    position_id: str
    market_id: str
    token_id: str
    side: str               # YES / NO / ARB
    shares: float
    entry_price: float
    cost: float
    engine: str             # crypto_edge / multi_arb
    question: str = ""
    model_prob: float = 0.0
    edge: float = 0.0
    expected_payout: Optional[float] = None
    settle_at: Optional[datetime] = None
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class RiskManager:
    def __init__(self, store=None):
        self.store = store
        self._lock = RLock()
        self.cash = Config.INITIAL_BANKROLL
        self.positions: list[Position] = []
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.total_pnl = 0.0
        self.trade_date = date.today()
        self.is_stopped = False
        self._next_id = 1
        self._day_start_equity = Config.INITIAL_BANKROLL
        self._restore()

    # ── State persistence ──────────────────────────────────────────

    def _restore(self):
        if not self.store:
            return
        with self._lock:
            state = self.store.load_account_state()
            if state:
                self.cash = float(state.get("cash", self.cash))
                self.daily_trades = int(state.get("daily_trades", 0))
                self.daily_pnl = float(state.get("daily_pnl", 0))
                self.total_pnl = float(state.get("total_pnl", 0))
                self.is_stopped = bool(state.get("is_stopped", False))
                self._next_id = int(state.get("next_position_id", 1))
                self._day_start_equity = float(state.get("day_start_equity", Config.INITIAL_BANKROLL))
                td = state.get("trade_date")
                if td:
                    try:
                        self.trade_date = date.fromisoformat(td)
                    except ValueError:
                        pass
            open_pos = self.store.load_open_positions()
            for row in open_pos:
                settle_at = None
                sa_raw = row.get("settle_at")
                if sa_raw:
                    try:
                        settle_at = datetime.fromisoformat(sa_raw)
                        if settle_at.tzinfo is None:
                            settle_at = settle_at.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                ep = row.get("expected_payout")
                expected_payout = float(ep) if ep is not None else None

                self.positions.append(Position(
                    position_id=row["position_id"],
                    market_id=row.get("market", ""),
                    token_id=row.get("token_id", ""),
                    side=row.get("side", ""),
                    shares=float(row.get("shares", 0)),
                    entry_price=float(row.get("entry_price", 0)),
                    cost=float(row.get("cost", 0)),
                    engine=row.get("engine", ""),
                    question=row.get("question", ""),
                    model_prob=float(row.get("model_prob", 0) or 0),
                    edge=float(row.get("edge", 0) or 0),
                    expected_payout=expected_payout,
                    settle_at=settle_at,
                ))

    def _persist(self):
        if not self.store:
            return
        self.store.save_account_state({
            "cash": self.cash,
            "daily_trades": self.daily_trades,
            "daily_pnl": self.daily_pnl,
            "trade_date": self.trade_date.isoformat(),
            "total_pnl": self.total_pnl,
            "is_stopped": self.is_stopped,
            "next_position_id": self._next_id,
            "day_start_equity": self._day_start_equity,
        })

    # ── Day reset ──────────────────────────────────────────────────

    def _reset_daily(self):
        if date.today() != self.trade_date:
            # Fire daily summary BEFORE reset so it captures yesterday's numbers.
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self.trade_date = date.today()
            self._day_start_equity = self.bankroll
            self._persist()

    # ── Properties ─────────────────────────────────────────────────

    @property
    def bankroll(self) -> float:
        with self._lock:
            return self.cash + sum(p.cost for p in self.positions)

    @property
    def locked(self) -> float:
        with self._lock:
            return sum(p.cost for p in self.positions)

    # ── Kelly sizing ───────────────────────────────────────────────

    def kelly_size(self, edge: float, market_price: float) -> float:
        """
        Calculate position size using fractional Kelly criterion.

        Kelly fraction f* = (p*b - q) / b
        where p = win prob, q = 1-p, b = odds (net payout per $1 risked)

        For a binary market at price `market_price`:
        - Cost per share = market_price
        - Payout if win = $1
        - Net payout per $1 = (1 / market_price) - 1
        - Win probability (our estimate) = market_price + edge
        """
        if not (math.isfinite(edge) and math.isfinite(market_price)):
            return 0.0
        if edge <= 0 or market_price <= 0 or market_price >= 1:
            return 0.0

        p = min(0.99, market_price + edge)  # our estimated win prob
        q = 1.0 - p
        b = (1.0 / market_price) - 1.0      # net payout per $1

        if b <= 0:
            return 0.0

        full_kelly = (p * b - q) / b
        if full_kelly <= 0:
            return 0.0

        fraction = full_kelly * kelly_fraction()
        bankroll = self.bankroll
        max_bet = bankroll * max_position_pct()

        size_usd = min(fraction * bankroll, max_bet)
        return max(0.0, round(size_usd, 2))

    # ── Trade evaluation ───────────────────────────────────────────

    def _check_limits(self, size_usd: float) -> tuple:
        """Internal limit check — caller must hold self._lock."""
        if not math.isfinite(size_usd) or size_usd <= 0:
            return False, "invalid_size"
        if size_usd > self.bankroll * max_position_pct() + 1e-9:
            return False, "position_size_limit"
        self._reset_daily()
        if self.is_stopped:
            return False, "global_stopped"
        if self.bankroll <= Config.INITIAL_BANKROLL * (1 - stop_loss_pct()):
            self.is_stopped = True
            self._persist()
            return False, "global_stop_loss"
        # 20% pause threshold
        if self.bankroll < Config.INITIAL_BANKROLL * (1 - Config.PAUSE_ON_LOSS_PCT):
            self.is_stopped = True
            self._persist()
            return False, "pause_loss_threshold"
        if self.daily_pnl <= -(self._day_start_equity * Config.MAX_DAILY_LOSS_PCT):
            return False, "daily_loss_limit"

    def can_trade(self, size_usd: float) -> tuple:
        """Check if a trade of size_usd is allowed. Returns (ok, reason)."""
        with self._lock:
            result = self._check_limits(size_usd)
            if result:
                return result
            if self.daily_trades >= max_daily_trades():
                return False, "daily_trade_limit"
            if len(self.positions) >= max_concurrent():
                return False, "max_positions"
            # Retain a 10% cash buffer in the research ledger.
            min_buffer = self.bankroll * 0.10
            available = max(0, self.cash - min_buffer)
            if size_usd > available:
                return False, f"insufficient_cash (need ${size_usd:.2f}, available=${available:.2f}, buffer=${min_buffer:.2f})"
            return True, "ok"

    # ── Position management ────────────────────────────────────────

    def open_position(self, *, market_id: str, token_id: str, side: str,
                      shares: float, entry_price: float, cost: float,
                      engine: str, question: str = "",
                      model_prob: float = 0.0, edge: float = 0.0,
                      expected_payout: Optional[float] = None,
                      settle_at: Optional[datetime] = None) -> Optional[Position]:
        if not (math.isfinite(shares) and shares > 0
                and math.isfinite(entry_price) and 0 < entry_price < 1
                and math.isfinite(cost) and cost > 0
                and cost + 1e-9 >= shares * entry_price
                and side in {"YES", "NO"}):
            return None
        with self._lock:
            result = self._check_limits(cost)
            if result:
                return None
            if self.daily_trades >= max_daily_trades():
                return None
            if len(self.positions) >= max_concurrent():
                return None
            min_buffer = self.bankroll * 0.10
            if cost > max(0, self.cash - min_buffer):
                return None

            pos = Position(
                position_id=f"pos-{self._next_id}",
                market_id=market_id,
                token_id=token_id,
                side=side,
                shares=shares,
                entry_price=entry_price,
                cost=cost,
                engine=engine,
                question=question,
                model_prob=model_prob,
                edge=edge,
                expected_payout=expected_payout,
                settle_at=settle_at,
            )
            self._next_id += 1
            self.cash -= cost
            self.positions.append(pos)
            self.daily_trades += 1

            if self.store:
                self.store.upsert_open_position({
                    "position_id": pos.position_id,
                    "token_id": pos.token_id,
                    "market": pos.market_id,
                    "side": pos.side,
                    "shares": pos.shares,
                    "entry_price": pos.entry_price,
                    "cost": pos.cost,
                    "engine": pos.engine,
                    "expected_payout": pos.expected_payout,
                    "settle_at": pos.settle_at.isoformat() if pos.settle_at else None,
                    "opened_at": pos.opened_at.isoformat(),
                    "question": pos.question,
                    "model_prob": pos.model_prob,
                    "edge": pos.edge,
                })
                self.store.record_bankroll_snapshot(
                    bankroll=self.bankroll, cash=self.cash,
                    locked=self.locked, total_pnl=self.total_pnl,
                    daily_pnl=self.daily_pnl, positions_count=len(self.positions),
                )
            self._persist()
            logger.info(f"OPEN [{engine}] {pos.position_id} cost=${cost:.2f} edge={edge:.1%}")
            return pos

    def close_position(self, position_id: str, payout: float, engine: str = "") -> Optional[float]:
        with self._lock:
            pos = next((p for p in self.positions if p.position_id == position_id), None)
            if not pos:
                return None
            if not math.isfinite(payout) or payout < 0 or payout > pos.shares + 1e-9:
                raise ValueError("Invalid binary-market payout")
            pnl = payout - pos.cost
            self.cash += payout
            self.positions = [p for p in self.positions if p.position_id != position_id]
            self.daily_pnl += pnl
            self.total_pnl += pnl

            if self.store:
                self.store.close_position(
                    position_id=position_id, payout=payout, pnl=pnl,
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
                self.store.record_bankroll_snapshot(
                    bankroll=self.bankroll, cash=self.cash,
                    locked=self.locked, total_pnl=self.total_pnl,
                    daily_pnl=self.daily_pnl, positions_count=len(self.positions),
                )
            self._persist()
            logger.info(f"CLOSE [{engine or pos.engine}] {position_id} pnl=${pnl:+.2f}")
            return pnl

    def settle_due_positions(self) -> list[dict]:
        """Auto-settle positions past their settle_at time."""
        now = datetime.now(timezone.utc)
        with self._lock:
            due = [p for p in self.positions
                   if p.settle_at and p.expected_payout is not None and p.settle_at <= now]
        settled = []
        for p in due:
            pnl = self.close_position(p.position_id, float(p.expected_payout), p.engine)
            settled.append({
                "position_id": p.position_id,
                "engine": p.engine,
                "pnl": pnl,
                "payout": p.expected_payout,
            })
        return settled

    # ── Status ─────────────────────────────────────────────────────

    def get_status(self) -> dict:
        with self._lock:
            return {
                "bankroll": round(self.bankroll, 2),
                "cash": round(self.cash, 2),
                "locked": round(self.locked, 2),
                "total_pnl": round(self.total_pnl, 2),
                "daily_pnl": round(self.daily_pnl, 2),
                "daily_trades": self.daily_trades,
                "positions_count": len(self.positions),
                "is_stopped": self.is_stopped,
                "positions": [
                    {
                        "id": p.position_id,
                        "market": p.market_id,
                        "question": p.question[:60],
                        "side": p.side,
                        "engine": p.engine,
                        "cost": round(p.cost, 2),
                        "edge": round(p.edge, 4),
                        "model_prob": round(p.model_prob, 4),
                        "entry_price": round(p.entry_price, 4),
                        "settle_at": p.settle_at.isoformat() if p.settle_at else None,
                    }
                    for p in self.positions
                ],
            }
