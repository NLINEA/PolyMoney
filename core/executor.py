"""Offline immediate-order simulation. No live execution adapter.

Books are snapshots, not forecasts of executable liquidity. Matching consumes
depth in this executor; queue position, latency and fees are absent.
"""
import copy
import math
from itertools import count


class Executor:
    def __init__(self):
        self._books = {}
        self._orders = {}
        self._ids = count(1)

    def set_book(self, token_id, book):
        clean = {"bids": [], "asks": []}
        for side in clean:
            for level in book.get(side, []):
                price, size = float(level["price"]), float(level["size"])
                if not (math.isfinite(price) and 0 < price < 1
                        and math.isfinite(size) and size >= 0):
                    raise ValueError("Invalid book level")
                if size:
                    clean[side].append({"price": price, "size": size})
        clean["asks"].sort(key=lambda x: x["price"])
        clean["bids"].sort(key=lambda x: x["price"], reverse=True)
        self._books[token_id] = copy.deepcopy(clean)

    def place_limit_order(self, token_id, side, price, size, order_type="FAK"):
        side, order_type = side.upper(), order_type.upper()
        if side not in {"BUY", "SELL"} or order_type not in {"FAK", "FOK"}:
            raise ValueError("Replay supports BUY/SELL and immediate FAK/FOK orders")
        if not (math.isfinite(price) and 0 < price < 1
                and math.isfinite(size) and size > 0):
            raise ValueError("Invalid order price or size")
        levels = self._books.get(token_id, {}).get("asks" if side == "BUY" else "bids", [])
        eligible = [level for level in levels
                    if (level["price"] <= price if side == "BUY" else level["price"] >= price)]
        oid = f"sim-{next(self._ids)}"
        filled_size, value = 0.0, 0.0
        if order_type != "FOK" or sum(x["size"] for x in eligible) + 1e-10 >= size:
            for level in eligible:
                take = min(size - filled_size, level["size"])
                filled_size += take
                value += take * level["price"]
                level["size"] -= take
                if filled_size + 1e-10 >= size:
                    break
        result = {
            "accepted": True, "dry_run": True, "order_id": oid,
            "filled": filled_size + 1e-10 >= size,
            "partial": 0 < filled_size < size - 1e-10,
            "filled_size": filled_size, "value": value,
            "fill_price": value / filled_size if filled_size else None,
            "remaining_size": max(0.0, size - filled_size),
        }
        self._orders[oid] = result
        return result

    def check_fill(self, order_id, timeout_sec=0):
        return bool(self._orders.get(order_id, {}).get("filled", False))
