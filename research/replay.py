"""Replay declared synthetic outcomes through the original risk/state pipeline."""
import json
import math
from pathlib import Path

from core.config import Config
from core.executor import Executor
from core.resolver import calculate_payout
from core.risk import RiskManager
from core.state_store import StateStore

DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "synthetic_replay.json"


def replay(path=DEFAULT_FIXTURE, *, store=None):
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if fixture.get("data_kind") != "synthetic":
        raise ValueError("Replay requires an explicitly synthetic fixture")
    own_store = store is None
    store = store or StateStore(":memory:")
    if store.load_account_state() or store.load_open_positions():
        if own_store:
            store.close()
        raise ValueError("Replay requires a fresh research state store")
    risk = RiskManager(store=store)
    executor = Executor()
    events, curve = [], [Config.INITIAL_BANKROLL]
    try:
        for sample in fixture["trades"]:
            outcome, side = sample["outcome"], sample["side"]
            if outcome not in {"YES", "NO"} or side not in {"YES", "NO"}:
                raise ValueError("Only binary YES/NO outcomes are supported")
            fee = float(sample.get("fee_usd", 0))
            if not math.isfinite(fee) or fee < 0:
                raise ValueError("Invalid fee")
            executor.set_book(sample["token_id"], sample["book"])
            estimate = float(sample["limit_price"]) * float(sample["shares"]) + fee
            ok, reason = risk.can_trade(estimate)
            if not ok:
                event = {"market": sample["market_id"], "status": "risk_rejected", "reason": reason}
            else:
                order = executor.place_limit_order(sample["token_id"], "BUY",
                    float(sample["limit_price"]), float(sample["shares"]), sample.get("order_type", "FAK"))
                if not order["filled_size"]:
                    event = {"market": sample["market_id"], "status": "unfilled", "reason": "no_matching_depth"}
                else:
                    position = risk.open_position(
                        market_id=sample["market_id"], token_id=sample["token_id"], side=side,
                        shares=order["filled_size"], entry_price=order["fill_price"],
                        cost=order["value"] + fee, engine=sample["engine"], question=sample["question"],
                    )
                    if position is None:
                        raise RuntimeError("Replay risk state changed unexpectedly")
                    payout = calculate_payout(side, outcome, position.shares)
                    pnl = risk.close_position(position.position_id, payout)
                    event = {"market": sample["market_id"], "question": sample["question"],
                        "engine": sample["engine"], "side": side, "outcome": outcome,
                        "status": "settled", "shares": position.shares,
                        "entry_price": order["fill_price"], "cost": position.cost,
                        "fee_usd": fee, "payout": payout, "pnl": round(pnl, 6),
                        "partial_fill": order["partial"]}
            events.append(event)
            curve.append(round(risk.bankroll, 6))
            store.add_timeline_event(engine=sample["engine"], event_type=event["status"], payload=event)
        settled = [e for e in events if e["status"] == "settled"]
        wins = sum(e["pnl"] > 0 for e in settled)
        losses = sum(e["pnl"] < 0 for e in settled)
        return {
            "mode": "offline_replay", "data_kind": "synthetic", "title": fixture["title"],
            "description": fixture["description"], "initial_bankroll": Config.INITIAL_BANKROLL,
            "bankroll": round(risk.bankroll, 6), "pnl": round(risk.total_pnl, 6),
            "wins": wins, "losses": losses, "settled": len(settled),
            "win_rate": wins / len(settled) if settled else 0,
            "events": events, "curve": curve,
            "limits": {"max_position_pct": Config.MAX_POSITION_PCT,
                       "max_daily_loss_pct": Config.MAX_DAILY_LOSS_PCT},
            "limitations": ["Synthetic data; no historical performance claim.",
                "Snapshot depth omits latency, queue priority and subsequent liquidity changes.",
                "Fees are fixture inputs; default zero does not imply fee-free trading.",
                "Declared outcomes bypass disputes and settlement delays."],
        }
    finally:
        if own_store:
            store.close()
