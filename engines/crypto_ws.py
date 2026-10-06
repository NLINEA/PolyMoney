"""WebSocket client for real-time BTC up/down market prices.

Connects to Polymarket Market Channel for live order book data.
Updates a shared price cache that crypto_snipe reads from.
"""
import json
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("crypto_ws")

# Shared price cache: token_id -> {"bid": float, "ask": float, "last": float, "ts": float}
_prices: dict = {}
_lock = threading.Lock()
_ws_thread: Optional[threading.Thread] = None
_running = False
# token_id -> subscribe timestamp. Evicted when older than _TOKEN_TTL to keep sub list lean.
_subscribed_tokens: dict = {}
_TOKEN_TTL = 600  # 10 min: BTC 5min markets expire fast, keeping dead tokens causes server drops


def _price_from_dict(data: dict) -> Optional[float]:
    """Derive a numeric price from a {bid, ask, last, ts} record."""
    if not data:
        return None
    if data.get("last", 0) > 0:
        return data["last"]
    bid = data.get("bid", 0)
    ask = data.get("ask", 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return None


def get_live_price(token_id: str) -> Optional[float]:
    """Real-time price with cross-process fallback.
    1) Local _prices (fastest, in-process)
    2) Shared SQLite cache (populated by other bot's WS if local missed)
    3) None (caller falls back to CLOB REST midpoint)
    """
    with _lock:
        data = _prices.get(token_id)
        if data and time.time() - data.get("ts", 0) <= 30:
            price = _price_from_dict(data)
            if price is not None:
                return price

    # Local missing or stale — try shared cache (other bot may have fresher tick)
    try:
        from core.shared_cache import get as _cache_get
        shared = _cache_get(f"ws_tick_{token_id}")
        if shared:
            return _price_from_dict(shared)
    except Exception:
        pass
    return None


# Throttle shared-cache writes per token (avoid lock churn on noisy book updates).
_SHARED_WRITE_INTERVAL = 0.2  # 200ms per-token
_last_shared_write: dict = {}


def _publish_tick(token_id: str, data: dict):
    """Write tick to shared cache, throttled per token."""
    now = time.time()
    last = _last_shared_write.get(token_id, 0)
    if now - last < _SHARED_WRITE_INTERVAL:
        return
    _last_shared_write[token_id] = now
    try:
        from core.shared_cache import set as _cache_set
        _cache_set(f"ws_tick_{token_id}", data, 2.0)
    except Exception:
        pass


def subscribe_tokens(token_ids: list):
    """Refresh subscription timestamps for these tokens. Add if new."""
    now = time.time()
    for tk in token_ids:
        if tk:
            _subscribed_tokens[tk] = now


def _active_tokens() -> list:
    """Return non-expired subscribed tokens, evicting stale ones."""
    now = time.time()
    expired = [tk for tk, ts in _subscribed_tokens.items() if now - ts > _TOKEN_TTL]
    for tk in expired:
        _subscribed_tokens.pop(tk, None)
        _prices.pop(tk, None)
    if expired:
        logger.debug(f"Crypto WS: evicted {len(expired)} stale tokens, {len(_subscribed_tokens)} remain")
    return list(_subscribed_tokens.keys())


def start_ws():
    """Start WebSocket connection in background thread."""
    from core.config import Config
    if not Config.ALLOW_PUBLIC_DATA:
        raise RuntimeError("Public data access is disabled")
    global _ws_thread, _running
    if _running:
        return
    _running = True
    _ws_thread = threading.Thread(target=_ws_loop, name="crypto-ws", daemon=True)
    _ws_thread.start()
    logger.info("Crypto WebSocket started")


def stop_ws():
    """Stop WebSocket connection."""
    global _running
    _running = False


def _ws_loop():
    """Main WebSocket loop with reconnection."""
    while _running:
        try:
            _connect_and_listen()
        except Exception as e:
            logger.debug(f"Crypto WS error: {e}")
        if _running:
            time.sleep(3)  # reconnect delay


def _connect_and_listen():
    """Connect to Polymarket Market Channel and listen for updates."""
    from core.config import Config
    if not Config.ALLOW_PUBLIC_DATA:
        raise RuntimeError("Public data access is disabled")
    try:
        import websocket
    except ImportError:
        # Try websocket-client
        try:
            from websocket import WebSocketApp
        except ImportError:
            logger.warning("websocket-client not installed, using simple implementation")
            _simple_ws_loop()
            return

    ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def on_open(ws):
        tokens = _active_tokens()
        logger.info(f"Crypto WS connected, subscribing to {len(tokens)} active tokens")
        if tokens:
            msg = {
                "assets_ids": tokens,
                "type": "market",
                "initial_dump": True,
                "level": 2,
                "custom_feature_enabled": True,
            }
            ws.send(json.dumps(msg))

    def on_message(ws, message):
        if message == "PONG":
            return
        try:
            data = json.loads(message)
            _handle_message(data)
        except Exception:
            pass

    def on_error(ws, error):
        logger.debug(f"Crypto WS error: {error}")

    def on_close(ws, close_status_code, close_msg):
        logger.info(f"Crypto WS closed: code={close_status_code} msg={close_msg}")

    ws = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    # Application-level ping — Polymarket expects text "PING"/"PONG" heartbeat
    def app_ping_loop():
        while _running and ws.sock and ws.sock.connected:
            try:
                ws.send("PING")
            except Exception:
                break
            time.sleep(10)

    # Refresh subscription every 60s with current active tokens (prunes dead ones server-side
    # via re-subscribe and keeps the connection's scope aligned with the live market set).
    def resub_loop():
        time.sleep(60)
        while _running and ws.sock and ws.sock.connected:
            try:
                tokens = _active_tokens()
                if tokens:
                    ws.send(json.dumps({
                        "assets_ids": tokens,
                        "type": "market",
                        "initial_dump": False,
                        "level": 2,
                        "custom_feature_enabled": True,
                    }))
            except Exception:
                break
            time.sleep(60)

    threading.Thread(target=app_ping_loop, daemon=True).start()
    threading.Thread(target=resub_loop, daemon=True).start()

    # Native WS ping frames as second keepalive layer (detects half-open sockets).
    # If no PONG within ping_timeout, ws.run_forever returns and outer loop reconnects.
    # Never inherit a proxy or its credentials from the private runtime environment.
    ws.run_forever(ping_interval=20, ping_timeout=10, http_no_proxy=["*"])


def _simple_ws_loop():
    """Fallback: poll public metadata through the credential-free GET adapter."""
    from core.config import Config
    if not Config.ALLOW_PUBLIC_DATA:
        raise RuntimeError("Public data access is disabled")

    # Too complex for simple implementation, just poll Gamma API faster
    logger.warning("WebSocket fallback: polling Gamma API every 3s")
    while _running:
        try:
            from engines.crypto_scanner import scan_crypto_markets
            markets = scan_crypto_markets()
            with _lock:
                for m in markets:
                    _prices[m.yes_token_id] = {
                        "bid": m.yes_price,
                        "ask": m.yes_price,
                        "last": m.yes_price,
                        "ts": time.time(),
                    }
                    _prices[m.no_token_id] = {
                        "bid": m.no_price,
                        "ask": m.no_price,
                        "last": m.no_price,
                        "ts": time.time(),
                    }
        except Exception:
            pass
        time.sleep(3)


def _handle_message(data: dict):
    """Process WebSocket message and update price cache."""
    event_type = data.get("event_type", "")
    asset_id = data.get("asset_id", "")

    if not asset_id:
        return

    snapshot = None
    with _lock:
        if asset_id not in _prices:
            _prices[asset_id] = {"bid": 0, "ask": 0, "last": 0, "ts": 0}

        if event_type == "book":
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids:
                _prices[asset_id]["bid"] = float(bids[0]["price"])
            if asks:
                _prices[asset_id]["ask"] = float(asks[0]["price"])
            _prices[asset_id]["ts"] = time.time()

        elif event_type == "last_trade_price":
            price = float(data.get("price", 0))
            if price > 0:
                _prices[asset_id]["last"] = price
                _prices[asset_id]["ts"] = time.time()

        elif event_type == "price_change":
            # Delta update
            changes = data.get("changes", [])
            for change in changes:
                side = change.get("side", "")
                price = float(change.get("price", 0))
                if side == "BUY" and price > 0:
                    _prices[asset_id]["bid"] = price
                elif side == "SELL" and price > 0:
                    _prices[asset_id]["ask"] = price
            _prices[asset_id]["ts"] = time.time()

        # Snapshot copy for shared-cache write outside the lock
        if event_type in ("book", "last_trade_price", "price_change"):
            snapshot = dict(_prices[asset_id])

    if snapshot:
        _publish_tick(asset_id, snapshot)
