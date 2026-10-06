import copy
from datetime import datetime, timedelta, timezone
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

from core.config import Config
from core.executor import Executor
from core.market_scanner import _parse_market
from core.public_data import get
from core.resolver import check_market_resolved
from core.risk import RiskManager
from core.state_store import StateStore
from research.replay import DEFAULT_FIXTURE, replay


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.ex = Executor()

    def test_empty_book_does_not_fill(self):
        result = self.ex.place_limit_order("demo", "BUY", .95, 1)
        self.assertFalse(result["filled"])
        self.assertEqual(result["filled_size"], 0)

    def test_limit_and_partial_depth(self):
        self.ex.set_book("demo", {"asks": [{"price": .96, "size": 10}, {"price": .94, "size": .5}]})
        result = self.ex.place_limit_order("demo", "BUY", .95, 1)
        self.assertTrue(result["partial"])
        self.assertAlmostEqual(result["value"], .47)
        self.assertEqual(result["filled_size"], .5)

    def test_depth_cannot_be_spent_twice(self):
        self.ex.set_book("demo", {"asks": [{"price": .9, "size": 1}]})
        self.assertTrue(self.ex.place_limit_order("demo", "BUY", .95, 1)["filled"])
        self.assertEqual(self.ex.place_limit_order("demo", "BUY", .95, 1)["filled_size"], 0)

    def test_fok_failure_keeps_depth_for_next_order(self):
        self.ex.set_book("demo", {"asks": [{"price": .9, "size": .5}]})
        self.assertEqual(self.ex.place_limit_order("demo", "BUY", .95, 1, "FOK")["filled_size"], 0)
        self.assertTrue(self.ex.place_limit_order("demo", "BUY", .95, .5)["filled"])

    def test_sell_uses_bids_and_weighted_price(self):
        self.ex.set_book("demo", {"bids": [{"price": .91, "size": .5}, {"price": .93, "size": .5}]})
        result = self.ex.place_limit_order("demo", "SELL", .9, 1)
        self.assertAlmostEqual(result["fill_price"], .92)

    def test_invalid_and_resting_orders_rejected(self):
        for price, size in [(math.nan, 1), (.95, -1), (1.1, 1), (.95, math.inf)]:
            with self.assertRaises(ValueError):
                self.ex.place_limit_order("demo", "BUY", price, size)
        with self.assertRaises(ValueError):
            self.ex.place_limit_order("demo", "BUY", .95, 1, "GTC")


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore()
        self.risk = RiskManager(self.store)

    def tearDown(self):
        self.store.close()

    def open(self, **changes):
        args = dict(market_id="demo", token_id="demo-yes", side="YES", shares=1,
                    entry_price=.95, cost=.95, engine="research")
        args.update(changes)
        return self.risk.open_position(**args)

    def test_position_percentage_cap_enforced_on_open(self):
        self.assertEqual(self.risk.can_trade(2.01), (False, "position_size_limit"))
        self.assertIsNone(self.open(shares=3, cost=2.85))

    def test_invalid_size_and_understated_cost_rejected(self):
        for value in [-1, 0, math.nan, math.inf]:
            self.assertFalse(self.risk.can_trade(value)[0])
        self.assertIsNone(self.open(cost=.1))
        self.assertIsNone(self.open(shares=math.nan))

    def test_daily_loss_boundary(self):
        self.risk.daily_pnl = -5.0
        self.assertEqual(self.risk.can_trade(1), (False, "daily_loss_limit"))

    def test_thirty_percent_drawdown_means_seventy_remaining(self):
        self.risk.cash = 70
        self.assertEqual(self.risk.can_trade(1), (False, "global_stop_loss"))
        self.assertTrue(self.risk.is_stopped)

    def test_invalid_payout_and_idempotent_settlement(self):
        p = self.open()
        with self.assertRaises(ValueError):
            self.risk.close_position(p.position_id, 2)
        self.assertAlmostEqual(self.risk.close_position(p.position_id, 1), .05)
        self.assertIsNone(self.risk.close_position(p.position_id, 1))
        self.assertAlmostEqual(self.risk.bankroll, 100.05)

    def test_restore_open_position_and_cash(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "research.db")
            store = StateStore(path)
            risk = RiskManager(store)
            p = risk.open_position(market_id="demo", token_id="demo", side="NO", shares=1,
                                  entry_price=.95, cost=.95, engine="research")
            store.close()
            reopened = StateStore(path)
            try:
                restored = RiskManager(reopened)
                self.assertEqual(restored.positions[0].position_id, p.position_id)
                self.assertAlmostEqual(restored.cash, 99.05)
                restored.close_position(p.position_id, 0)
                self.assertAlmostEqual(restored.bankroll, 99.05)
            finally:
                reopened.close()


class ReplayTests(unittest.TestCase):
    def test_high_win_rate_can_lose_money(self):
        result = replay()
        self.assertEqual(result["win_rate"], .75)
        self.assertEqual(result["pnl"], -.8)
        self.assertEqual(result["bankroll"], 99.2)
        self.assertEqual([e["status"] for e in result["events"]][-2:], ["unfilled", "risk_rejected"])

    def test_partial_fill_and_fee_use_actual_cost(self):
        data = json.loads(DEFAULT_FIXTURE.read_text())
        trade = data["trades"][0]
        trade["book"]["asks"][0]["size"] = .5
        trade["fee_usd"] = .01
        data["trades"] = [trade]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.json"
            path.write_text(json.dumps(data))
            result = replay(path)
        self.assertTrue(result["events"][0]["partial_fill"])
        self.assertAlmostEqual(result["pnl"], .015)

    def test_replay_cannot_append_to_an_account_store(self):
        store = StateStore()
        try:
            RiskManager(store)._persist()
            with self.assertRaises(ValueError):
                replay(store=store)
        finally:
            store.close()

    def test_offline_does_not_open_a_network_connection(self):
        with patch("socket.socket.connect", side_effect=AssertionError("unexpected network")):
            self.assertEqual(replay()["data_kind"], "synthetic")

    def test_private_runtime_environment_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            unwanted = Path(folder) / "account.db"
            env = dict(os.environ, DRY_RUN="false", PRIVATE_KEY="test-only-placeholder",
                       STATE_DB_PATH=str(unwanted), SERVER_PORT="8889", ALLOW_PUBLIC_DATA="true")
            run = subprocess.run([sys.executable, "polymoney.py", "replay"],
                                 capture_output=True, text=True, check=True, env=env)
            self.assertEqual(json.loads(run.stdout)["mode"], "offline_replay")
            self.assertFalse(unwanted.exists())


class DataTests(unittest.TestCase):
    def tearDown(self):
        Config.ALLOW_PUBLIC_DATA = False

    def market(self):
        return {"id": "demo", "question": "Will the Lakers win?", "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]', "outcomePrices": '["0.94", "0.06"]',
                "endDate": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "bestBid": .93, "bestAsk": .95, "liquidity": "5000"}

    def test_malformed_prices_and_nonbinary_outcomes_are_skipped(self):
        for changes in [{"outcomePrices": "broken"}, {"outcomePrices": '["NaN", ".1"]'},
                        {"outcomes": '["Alice", "Bob"]'}, {"liquidity": "NaN"}]:
            raw = self.market(); raw.update(changes)
            self.assertIsNone(_parse_market(raw, datetime.now(timezone.utc), None))

    def test_reversed_outcome_token_mapping(self):
        raw = self.market(); raw.update(outcomes='["No", "Yes"]', outcomePrices='[".06", ".94"]',
                                         clobTokenIds='["no-token", "yes-token"]')
        market = _parse_market(raw, datetime.now(timezone.utc), None)
        self.assertEqual(market.yes_token_id, "yes-token")
        self.assertEqual(market.yes_price, .94)

    def test_closed_is_not_a_final_resolution(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"closed": True, "outcomePrices": '[".95", ".05"]'}
        with patch("core.resolver.requests.get", return_value=response):
            self.assertIsNone(check_market_resolved("demo"))
        response.json.return_value = {"closed": True, "umaResolutionStatus": "resolved", "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]'}
        with patch("core.resolver.requests.get", return_value=response):
            self.assertEqual(check_market_resolved("demo")["outcome"], "YES")

    def test_reversed_resolution_outcomes(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"closed": True, "umaResolutionStatus": "resolved",
            "outcomes": '["No", "Yes"]', "outcomePrices": '["1", "0"]'}
        with patch("core.resolver.requests.get", return_value=response):
            self.assertEqual(check_market_resolved("demo")["outcome"], "NO")

    def test_network_disabled_and_bad_endpoints(self):
        with self.assertRaises(RuntimeError):
            get("https://gamma-api.polymarket.com/markets")
        Config.ALLOW_PUBLIC_DATA = True
        for url in ["http://gamma-api.polymarket.com/markets", "https://example.com/", "https://user:pass@gamma-api.polymarket.com/markets"]:
            with self.assertRaises(ValueError):
                get(url)

    def test_redirects_rejected_and_proxy_environment_ignored(self):
        Config.ALLOW_PUBLIC_DATA = True
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value.status_code = 302
        with patch("core.public_data._requests.Session", return_value=session):
            with self.assertRaises(RuntimeError):
                get("https://gamma-api.polymarket.com/markets")
        self.assertFalse(session.trust_env)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_crypto_signal_and_consecutive_loss_stop(self):
        from engines import crypto_snipe as engine
        engine.reset_crypto_snipe()
        market = SimpleNamespace(market_id="demo", question="Bitcoin Up or Down", yes_price=.95,
            no_price=.05, yes_token_id="demo-yes", no_token_id="demo-no", hours_to_resolve=30/3600)
        try:
            with patch.object(engine, "_side_allowed", return_value=True):
                self.assertEqual(engine.find_crypto_snipe_signals([market])[0].side, "YES")
                for _ in range(3): engine.record_crypto_loss()
                self.assertEqual(engine.find_crypto_snipe_signals([market]), [])
        finally:
            engine.reset_crypto_snipe()

    def test_retained_modules_import_without_background_workers(self):
        for name in ["engines.early_panic_btc", "engines.btc_momentum", "engines.crypto_ws"]:
            importlib.import_module(name)


class DashboardTests(unittest.TestCase):
    def test_read_only_synthetic_dashboard_api(self):
        from fastapi.testclient import TestClient
        from server import app
        with patch("socket.socket.connect", side_effect=AssertionError("unexpected network")):
            with TestClient(app) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/static/dashboard.js").status_code, 200)
                self.assertEqual(client.get("/api/demo").json()["pnl"], -.8)
                self.assertFalse(client.get("/api/health").json()["live_execution"])
                self.assertEqual(client.post("/api/demo").status_code, 405)
                self.assertEqual(client.get("/api/settings/set?key=mode&value=live").status_code, 404)


if __name__ == "__main__":
    unittest.main()
