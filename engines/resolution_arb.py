"""Historical sports price/time heuristics.\n\nThe name resolution_arb is retained for source continuity; this is directional\nexposure, not a locked-in arbitrage. Edge and implied_prob are hand-built scores,\nnot externally calibrated probabilities. No performance claim is implied."""
import logging
import math
from dataclasses import dataclass
from typing import Optional
from core.config import Config

logger = logging.getLogger("engine.resolution_arb")


@dataclass
class ArbSignal:
    market_id: str
    question: str
    side: str
    token_id: str
    market_price: float
    implied_prob: float
    edge: float
    hours_to_resolve: float
    sport: str
    strategy: str  # "resolution_arb" | "momentum" | "spread"


def find_resolution_arbs(markets: list) -> list[ArbSignal]:
    """Find sports trading opportunities."""
    signals = []

    MAJOR_LEAGUE_KEYWORDS = [
        "serie a", "la liga", "premier league", "bundesliga", "ligue 1",
        "inter", "milan", "juventus", "napoli", "roma", "lazio", "atalanta", "fiorentina",
        "barcelona", "real madrid", "atletico", "sevilla", "villarreal", "osasuna", "girona",
        "arsenal", "liverpool", "manchester", "chelsea", "tottenham", "newcastle", "aston villa",
        "bayern", "dortmund", "leverkusen", "leipzig",
        "psg", "marseille", "monaco", "lyon", "lille",
        # Other top european
        "ajax", "psv", "feyenoord", "eredivisie",
        "benfica", "porto", "sporting",
        "galatasaray", "fenerbahce", "besiktas",
        "atlético", "atleti",
        "celtic", "rangers",
        # UCL / Europa
        "champions league", "europa league", "conference league",
        # NBA
        "celtics", "lakers", "warriors", "nuggets", "bucks", "76ers", "suns", "bulls",
        "knicks", "nets", "heat", "cavaliers", "pacers", "hawks", "raptors", "mavericks",
        "clippers", "kings", "rockets", "thunder", "timberwolves", "grizzlies", "pelicans",
        "spurs", "wizards", "pistons", "hornets", "magic", "blazers", "jazz",
        # NHL
        "bruins", "rangers", "maple leafs", "canadiens", "oilers", "flames", "canucks",
        "penguins", "capitals", "lightning", "panthers", "hurricanes", "avalanche",
        "stars", "blues", "wild", "predators", "red wings", "blackhawks", "flyers",
        "devils", "islanders", "senators", "jets", "kraken", "sharks", "ducks", "coyotes",
        # NFL (all 32)
        "chiefs", "eagles", "cowboys", "49ers", "bills", "ravens", "dolphins", "bengals",
        "packers", "lions", "vikings", "bears", "seahawks", "rams", "cardinals", "commanders",
        "steelers", "browns", "texans", "colts", "jaguars", "titans", "chargers", "raiders",
        "saints", "buccaneers", "falcons", "broncos",
        # MLB
        "yankees", "mets", "dodgers", "red sox", "cubs", "white sox",
        "giants", "padres", "braves", "phillies", "astros", "orioles", "twins",
        "rays", "brewers", "mariners", "rangers", "angels", "tigers", "reds",
        "pirates", "rockies", "marlins", "nationals", "royals", "guardians",
        "athletics", "diamondbacks", "blue jays",
        # MLS top
        "inter miami", "la galaxy", "lafc",
    ]
    # Tier 2: expanded keywords — tighter entry (96¢+ only)
    EXPANDED_KEYWORDS = [
        # Serie A (remaining squads)
        "torino", "bologna", "parma", "genoa", "cagliari", "udinese", "verona",
        "lecce", "como", "monza", "empoli", "venezia",
        # La Liga (remaining)
        "real sociedad", "betis", "athletic bilbao", "celta", "mallorca",
        "getafe", "valencia", "espanyol", "vallecano", "alaves",
        # Premier League (remaining)
        "brighton", "bournemouth", "brentford", "fulham", "west ham",
        "crystal palace", "wolves", "everton", "nottingham forest",
        "ipswich", "leicester", "southampton",
        # Bundesliga (remaining)
        "frankfurt", "stuttgart", "freiburg", "wolfsburg", "augsburg",
        "hoffenheim", "mainz", "union berlin", "heidenheim",
        # Ligue 1 (remaining)
        "nice", "lens", "rennes", "strasbourg", "toulouse", "nantes",
    ]
    BAN_KEYWORDS = [
        "up or down", "bitcoin", "ethereum", "solana", "btc", "eth ", "xrp",
        "counter-strike", "valorant", "dota", "lol:", "league of legends",
        "cs2", "cs:", "overwatch", "rainbow six", "rocket league",
        "game handicap", "map handicap", "map winner", "(bo3)", "(bo5)",
        "esports", "e-sports",
        # Season-long / non-game markets (not single game resolution)
        "make the", "playoff", "win the title", "win the cup",
        "win the premier", "win the serie", "win the la liga",
        "win the bundesliga", "win the ligue", "win the nba",
        "win the stanley", "win the world series", "win the super bowl",
        "win the championship", "win the finals",
        "relegated", "promotion", "promoted",
        "mvp", "award", "season", "division winner", "conference winner",
    ]
    sports = []
    for m in markets:
        if not all(math.isfinite(v) for v in (m.hours_to_resolve, m.yes_price, m.no_price, m.liquidity)):
            continue
        if m.hours_to_resolve <= 0 or not (0 < m.yes_price < 1 and 0 < m.no_price < 1):
            continue
        if m.hours_to_resolve >= 12 or m.liquidity <= 3000 or m.category == "crypto_price":
            continue
        q = (m.question or "").lower()
        if any(kw in q for kw in BAN_KEYWORDS):
            continue
        if any(kw in q for kw in MAJOR_LEAGUE_KEYWORDS):
            m._tier = 1
            sports.append(m)
        elif any(kw in q for kw in EXPANDED_KEYWORDS):
            m._tier = 2  # expanded: tighter entry (96¢+)
            sports.append(m)

    for m in sports:
        # Strategy 1: Resolution arb
        sig = _resolution_arb(m)
        if sig:
            signals.append(sig)
            continue

        # Strategy 2: Spread capture on liquid uncertain markets
        sig = _spread_capture(m)
        if sig:
            signals.append(sig)

    if signals:
        # Sort by SPEED first (fastest settle), then edge
        # Fast turnover > high edge for compounding
        signals.sort(key=lambda s: (s.hours_to_resolve, -s.edge))
        logger.info(f"Found {len(signals)} signals")
    return signals


def _resolution_arb(m) -> Optional[ArbSignal]:
    """Select high-price, short-horizon candidates using historical thresholds.\n\n    Proximity to endDate does not confirm an outcome or eliminate tail risk.\n    Price-based scores require separate probability and execution validation.\n    """
    if m.hours_to_resolve > 12.0:
        return None  # markets resolving in <12h

    tier = getattr(m, '_tier', 1)
    is_spread = "spread" in (m.question or "").lower()
    if is_spread:
        min_price = 0.95
    elif tier == 2:
        min_price = 0.96   # Expanded teams: tighter entry
    elif m.hours_to_resolve < 1:
        min_price = 0.90   # <1h: game nearly over
    elif m.hours_to_resolve < 6:
        min_price = 0.92   # 1-6h: game in progress
    else:
        min_price = 0.95   # 6-12h: pregame

    try:
        from core.config import sport_spread_max, sport_nonspread_max
        max_price = sport_spread_max() if is_spread else sport_nonspread_max()
    except Exception:
        max_price = 0.97 if is_spread else 0.95  # fallback: non-Spread 0.97 zone is poison
    if min_price <= m.yes_price <= max_price:
        edge = (1.0 - m.yes_price) * 0.6
        min_edge = 0.01
        if edge < min_edge:
            return None
        return ArbSignal(
            market_id=m.market_id,
            question=m.question,
            side="YES",
            token_id=m.yes_token_id,
            market_price=m.yes_price,
            implied_prob=min(0.98, m.yes_price + edge),
            edge=edge,
            hours_to_resolve=m.hours_to_resolve,
            sport="sports",
            strategy="resolution_arb",
        )

    # Check for near-certain NO (YES is very low = NO is high)
    max_yes_for_no = 1.0 - min_price  # mirror the YES threshold
    min_yes_for_no = 1.0 - max_price  # mirror the ceiling: no_price <= max_price
    if min_yes_for_no <= m.yes_price <= max_yes_for_no:
        no_price = m.no_price
        edge = (1.0 - no_price) * 0.6
        min_edge = 0.01
        if edge < min_edge:
            return None
        return ArbSignal(
            market_id=m.market_id,
            question=m.question,
            side="NO",
            token_id=m.no_token_id,
            market_price=no_price,
            implied_prob=min(0.98, no_price + edge),
            edge=edge,
            hours_to_resolve=m.hours_to_resolve,
            sport="sports",
            strategy="resolution_arb",
        )

    return None


def _spread_capture(m) -> Optional[ArbSignal]:
    """
    Spread capture: place limit orders inside the spread on liquid markets.

    For markets with spread > 2% and high liquidity, we can place
    a limit order between bid and ask. If filled, we earn the spread.

    Only on markets resolving in 1-6h with prices in 30-70% range
    (the most liquid and actively traded range).
    """
    if m.hours_to_resolve < 0.5 or m.hours_to_resolve > 6:
        return None
    if m.liquidity < 100000:
        return None
    if not (0.30 <= m.yes_price <= 0.70):
        return None
    if m.spread < 0.02:
        return None  # spread too tight, no room

    # Place bid at midpoint — if filled, we bought below fair value
    # Edge = half the spread (conservative estimate)
    edge = m.spread * 0.3  # claim 30% of spread as edge
    try:
        from core.config import min_edge as _min_edge
        _me = _min_edge()
    except Exception:
        _me = Config.MIN_EDGE
    if edge < _me:
        return None

    # Favor the side closer to 50% (more liquid)
    if m.yes_price <= 0.50:
        return ArbSignal(
            market_id=m.market_id,
            question=m.question,
            side="YES",
            token_id=m.yes_token_id,
            market_price=m.yes_price,
            implied_prob=m.yes_price + edge,
            edge=edge,
            hours_to_resolve=m.hours_to_resolve,
            sport="sports",
            strategy="spread",
        )
    else:
        return ArbSignal(
            market_id=m.market_id,
            question=m.question,
            side="NO",
            token_id=m.no_token_id,
            market_price=m.no_price,
            implied_prob=m.no_price + edge,
            edge=edge,
            hours_to_resolve=m.hours_to_resolve,
            sport="sports",
            strategy="spread",
        )
