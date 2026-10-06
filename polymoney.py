"""Public research entrypoint: offline replay or explicit read-only scanning."""
import argparse
from dataclasses import asdict
import json

from core.config import Config
from research.replay import DEFAULT_FIXTURE, replay


def main():
    parser = argparse.ArgumentParser(description="PolyMoney research tools (no live execution)")
    commands = parser.add_subparsers(dest="command", required=True)
    offline = commands.add_parser("replay", help="Replay synthetic books and declared outcomes offline")
    offline.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    scan = commands.add_parser("scan", help="Explicitly fetch public markets; creates no orders")
    scan.add_argument("--pages", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args()
    if args.command == "replay":
        result = replay(args.fixture)
    else:
        from core.market_scanner import scan_markets
        from engines.resolution_arb import find_resolution_arbs
        Config.ALLOW_PUBLIC_DATA = True
        try:
            markets = scan_markets(max_pages=args.pages)
            signals = find_resolution_arbs(markets)
            result = {"mode": "public_read_only", "markets": len(markets),
                "signals": [asdict(s) for s in signals],
                "note": "Heuristic candidates, not calibrated probabilities or verified arbitrage."}
        finally:
            Config.ALLOW_PUBLIC_DATA = False
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
