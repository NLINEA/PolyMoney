# PolyMoney

**Research into Polymarket trading automation — by NLINEA.**

PolyMoney explores how market discovery, short-horizon signals, position sizing,
execution assumptions and persistent state fit together. This public edition is
derived from the original research code, with a runnable offline replay and a
local dashboard for inspecting the workflow.

The default example makes a simple point: **three wins at 95¢ and one full-stake
loss produce a 75% win rate and a negative return.** All included trade inputs are
synthetic. They are not an export of an account or a backtest of the strategies.

![PolyMoney — Prediction market research](docs/polymoney-intro.png)

## Try the research replay

Requires Python 3.11 or newer.

```sh
git clone https://github.com/NLINEA/PolyMoney.git
cd PolyMoney
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python polymoney.py replay
python server.py
```

Open **http://127.0.0.1:8000**. Replay and dashboard use in-memory SQLite, need no
credentials and make no external API calls. On Windows, activate the environment
with `.venv\Scripts\activate`. Use `python server.py --port 8001` if necessary.

The example starts with $100 of synthetic equity and ends at $99.20: three gains
of $0.05, one loss of $0.95, an unfilled request and a rejected oversized request.
The dashboard identifies the data as synthetic throughout.

## What is included

| Component | Purpose |
| --- | --- |
| `core/market_scanner.py` | Normalize public market metadata and map binary outcomes to tokens |
| `engines/` | Selected original Crypto Snipe, Early Panic, BTC Momentum and sports signal hypotheses |
| `core/risk.py` | Fractional Kelly sizing, position limits, loss limits and accounting |
| `core/state_store.py` | SQLite account/position recovery, timeline and source health |
| `core/executor.py` | New offline FAK/FOK matcher with supplied depth and partial fills |
| `research/replay.py` | Replay supplied example signals and declared outcomes through the ledger |
| `server.py`, `static/` | Read-only local dashboard, using the original visual palette |

See [source provenance](docs/SOURCE_PROVENANCE.md) for what was preserved,
adapted and omitted. The replay runner does **not** call every strategy: it
isolates execution and accounting using declared inputs. The retained signal
functions are available for inspection and separately controlled experiments.

## Optional public market scan

```sh
python polymoney.py scan --pages 1
```

This explicitly enables credential-free GET requests to the public Gamma API
and returns historical sports heuristic candidates. It does not create orders
or inspect a wallet. API changes, access restrictions and rate limits can affect
the result; an empty scan is not evidence that no opportunities exist.

The `resolution_arb` name comes from the original code. Its price/time heuristic
has directional exposure, and its `edge` and `implied_prob` fields are hand-built
scores. They do not establish a calibrated probability or locked-in arbitrage.
Other signal modules may use public Binance/CoinGecko data when explicitly
enabled; no market workers start with the dashboard.

## Research boundaries

The original dry run could mark an order filled immediately at its limit or a
favorable last/mid price. That assumption cannot establish an executable return.
The published matcher consumes supplied book depth instead, but still omits
latency, queue priority and changing liquidity. Fees must be supplied in the
fixture; zero fees in the example are an assumption.

Outcome declarations bypass disputes and settlement delays. Historical time,
trend and panic thresholds remain hypotheses requiring independent validation.
The risk ledger values open positions at cost rather than market value; its
drawdown checks therefore measure that ledger, not a live marked portfolio.

This edition contains no wallet signing, live order submission, redemption,
private database connections, proxy bypass setup or outbound notifications.
It ignores private runtime environment variables and does not load `.env`.
Changing `DRY_RUN` cannot add a live execution adapter.
The dashboard and API documentation use local resources, reject external Host
headers and send no referrer information. See [the privacy boundaries](SECURITY.md).

Read the [research notes](docs/RESEARCH_NOTES.md) and
[architecture](docs/ARCHITECTURE.md) before interpreting the examples.

## Run the checks

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -v
python tools/check_public_tree.py
```

Tests cover book depth, partial fills, limit prices, FOK behavior, risk limits,
SQLite recovery, repeat settlement, synthetic replay, offline operation,
outcome mapping and the dashboard API. CI runs them on Python 3.11, 3.12 and 3.14.
`requirements.lock.txt` records the tested dependency snapshot.

## Related record

[PolyMoney: Research into Polymarket Trading Automation](https://nlinea.io/records/polymoney-polymarket-automation/)

Copyright © NLINEA. This repository publishes research source for inspection.
No open-source license grant is included. Dependencies retain their own licenses.
