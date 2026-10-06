"""Research defaults. Never loads wallet credentials or .env."""


class Config:
    DRY_RUN = True
    ALLOW_PUBLIC_DATA = False
    GAMMA_HOST = "https://gamma-api.polymarket.com"
    CLOB_HOST = "https://clob.polymarket.com"
    INITIAL_BANKROLL = 100.0
    MAX_POSITION_PCT = 0.02
    MAX_COIN_EXPOSURE_PCT = 0.10
    MAX_CONCURRENT = 6
    MAX_DAILY_TRADES = 20
    MAX_DAILY_LOSS_PCT = 0.05
    STOP_LOSS_PCT = 0.30
    PAUSE_ON_LOSS_PCT = 0.30
    PAUSE_ON_CONSECUTIVE_LOSSES = 3
    KELLY_FRACTION = 0.20
    MIN_EDGE = 0.02
    MIN_CONFIDENCE = 0.70
    SCAN_INTERVAL = 10
    MAX_RESOLVE_DAYS = 14
    MIN_LIQUIDITY = 500
    SCAN_BATCH_SIZE = 100
    SCAN_PAGES = 1
    CRYPTO_VOLATILITY_WINDOW = 30
    CRYPTO_COINS = ["BTC", "ETH", "SOL"]
    ARB_MIN_GAP = 0.01
    ARB_MAX_OUTCOMES = 50


# Historical signal interfaces, without private runtime settings or database IO.
# Values are experiment parameters, not validated trading recommendations.
def max_daily_trades(): return Config.MAX_DAILY_TRADES
def max_concurrent(): return Config.MAX_CONCURRENT
def max_position_pct(): return Config.MAX_POSITION_PCT
def kelly_fraction(): return Config.KELLY_FRACTION
def stop_loss_pct(): return Config.STOP_LOSS_PCT
def scan_interval(): return Config.SCAN_INTERVAL
def min_edge(): return Config.MIN_EDGE
def crypto_entry_min(): return 0.90
def crypto_entry_max(): return 0.98
def crypto_fill_slip_max(): return 0.03
def crypto_trend_threshold(): return 0.005
def crypto_cap_base_pct(): return 0.02
def crypto_cap_step_pct(): return 0.002
def crypto_cap_max_pct(): return 0.02
def crypto_cap_min_usd(): return 0.0
def crypto_consecutive_losses_stop(): return 3
def crypto_window_seconds(): return 45
def sport_nonspread_max(): return 0.95
def sport_spread_max(): return 0.97
def ep_cost_usd(): return 1.0
def momentum_cost_usd(): return 1.0


def crypto_cap_milestone_pct(bankroll):
    """No minimum-notional override of the percentage limit."""
    return Config.MAX_POSITION_PCT
