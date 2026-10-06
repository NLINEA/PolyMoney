# Research notes

## Win rate and expected value

A share bought for $0.95 earns $0.05 if it pays $1 and loses $0.95 if it pays $0.
Ignoring fees, this payoff needs a win probability above 95% to have positive
expected value. Three wins and one loss therefore yield $0.15 − $0.95 = −$0.80.
The included 75% example is synthetic and illustrates the payoff structure.

Entry price is not independent evidence of the true probability. The historical
Crypto Snipe score `(1 − price) × 0.5` and sports score `(1 − price) × 0.6` are
heuristics, not trained or calibrated probability estimates. Feeding a score
into fractional Kelly does not validate that score.

## Execution assumptions

The original dry executor contained a logic-validation path that treated limit
orders as filled, sometimes using a more favorable last/mid price. It did not
require available counterparty depth. Comments in that source also referred to
an older book-checking path; the active fill branch is what matters.

The public matcher sorts asks for buys and bids for sells, observes the limit,
consumes supplied depth and distinguishes full, partial and absent fills. FOK
orders with insufficient eligible depth consume nothing. FAK cancels unfilled
remainder in the simulation. Resting GTC orders are outside its scope.

Snapshot matching still cannot model future liquidity, queue priority, latency,
minimum order sizes, tick sizes or exchange rejection. Fee amounts in the fixture
are explicit dollar assumptions rather than a current exchange fee model.

## Timing and resolution

Historical functions use a field named `hours_to_resolve`, calculated from Gamma
`endDate`. That naming is preserved for source continuity. Treat it as a metadata
horizon: it does not independently verify the outcome or payout availability.
The [official Gamma market schema](https://docs.polymarket.com/api-reference/markets/list-markets)
exposes `endDate`, `closed` and `umaResolutionStatus` as separate fields.

The optional resolution helper now requires `closed`, an explicit `resolved`
status and exact `[1, 0]` or `[0, 1]` prices. The offline example instead supplies
the outcome directly. Neither the public edition nor these checks redeem assets.

## Remaining research questions

Time-of-day, rate and trend filters were selected during exploratory research.
They need evaluation on held-out observations and realistic executions. Some
legacy upstream-data helpers retain stale-value fallbacks or fail-open gates;
the signal modules are therefore inspectable hypotheses, not a robust market
decision service. The ATR helper in Crypto Snipe is dormant in its signal path.

The risk ledger restores positions and cash, and tracks realized P&L. Open
positions are valued at cost, not marked to market. The public replay settles
each supplied signal before the next, so it does not evaluate correlated open
positions or validate the original portfolio's results.
