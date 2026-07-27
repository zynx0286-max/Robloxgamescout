import time
from data_collector import collect_games
from roblox_api import get_game_info
from database import save_game_snapshot
from growth import get_growth
from trend import calculate_trend_score
from filters import passes_filters
from developer_intelligence import calculate_developer_score


DEBUG_LOG = "scan_debug.log"


def _log(message):
    """Append a debug line to the scan log file."""
    with open(DEBUG_LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def calculate_score(game):
    """Legacy flat score (0-100). Still used by /scoretest."""
    score = 0

    # Player activity
    if game["playing"] >= 1000:
        score += 25
    elif game["playing"] >= 100:
        score += 10

    # Visits
    if game["visits"] >= 1000000:
        score += 25
    elif game["visits"] >= 100000:
        score += 10

    # Favorites
    if game["favorites"] >= 50000:
        score += 20
    elif game["favorites"] >= 10000:
        score += 10

    # Growth trend
    growth = game.get("growth", 0)

    if growth >= 100:
        score += 30
    elif growth >= 50:
        score += 15

    return score


def _new_release_points(game):
    """Return (points, label, reason) for the New Release component."""
    stamp = game.get("created") or game.get("first_seen")
    if not stamp:
        return 0, "🆕 New release", "Release date unknown"

    try:
        from datetime import datetime, timezone

        iso = str(stamp).replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 0, "🆕 New release", "Release date unresolved"

    if age_days <= 30:
        return 15, "🆕 New release", f"🔥 Hot launch ({age_days}d old)"
    if age_days <= 90:
        return 10, "🆕 New release", f"📅 Recent release ({age_days}d)"
    if age_days <= 180:
        return 5, "🆕 New release", f"📅 Modestly aged ({age_days}d)"
    return 0, "🆕 New release", f"⏳ Mature game ({age_days}d)"


def _developer_history_points(game):
    """Pull creator history from Roblox via developer_intelligence and score it."""
    pts, label, reason = calculate_developer_score(game)
    return pts, label, reason


def calculate_scout_score(game):
    """Scout Score 2.0 — composable breakdown summing to 0-100."""
    breakdown = []

    # 📈 Growth (0-30)
    g = game.get("growth", 0)
    if g >= 100:
        growth_pts = 30
        growth_text = "🚀 Triple-digit growth"
    elif g >= 50:
        growth_pts = 25
        growth_text = "🚀 Strong growth"
    elif g >= 25:
        growth_pts = 20
        growth_text = "📈 Solid growth"
    elif g >= 10:
        growth_pts = 10
        growth_text = "📈 Mild growth"
    else:
        growth_pts = 0
        growth_text = "📊 Slow growth"
    breakdown.append(("📈 Growth", growth_pts, growth_text))

    # 👥 Player momentum (0-25)
    p = game.get("playing", 0)
    if p >= 10000:
        m_pts, m_text = 25, "🏟 Stadium-scale activity"
    elif p >= 5000:
        m_pts, m_text = 20, "🎯 Heavy momentum"
    elif p >= 1000:
        m_pts, m_text = 15, "🎯 Solid activity"
    elif p >= 100:
        m_pts, m_text = 8, "👥 Modest activity"
    else:
        m_pts, m_text = 0, "⏳ Quiet room"
    breakdown.append(("👥 Player momentum", m_pts, f"{m_text} ({p:,} CCU)"))

    # 🆕 New release bonus (0-15)
    nr_pts, nr_label, nr_text = _new_release_points(game)
    breakdown.append((nr_label, nr_pts, nr_text))

    # ❤️ Like ratio (0-10)
    visits = game.get("visits", 0) or 0
    favs = game.get("favorites", 0) or 0
    if visits > 0 and favs > 0:
        ratio = favs / visits
        if ratio >= 0.05:
            like_pts, like_text = 10, "❤️ Very high favor rate"
        elif ratio >= 0.02:
            like_pts, like_text = 7, "❤️ High favor rate"
        elif ratio >= 0.005:
            like_pts, like_text = 4, "❤️ Decent favor rate"
        else:
            like_pts, like_text = 0, "❤️ Low favor rate"
    else:
        like_pts, like_text = 0, "❤️ Favorites not yet tracked"
    breakdown.append(("❤️ Like ratio", like_pts, like_text))

    # 👨‍💻 Developer history (0-20 reserve — currently 0 until we add metrics)
    dev_pts, dev_label, dev_text = _developer_history_points(game)
    breakdown.append((dev_label, dev_pts, dev_text))

    total = sum(pts for _, pts, _ in breakdown)
    return {"total": min(total, 100), "breakdown": breakdown, "verdict": _score_verdict(total)}


def _score_verdict(total):
    """Verdict label based on total score."""
    if total >= 80:
        return "🐐 Elite opportunity"
    if total >= 60:
        return "🔥 Strong opportunity"
    if total >= 40:
        return "📈 Worth watching"
    if total >= 20:
        return "👀 Mild signal"
    return "⚪ Low signal"


def rank_games(games):
    """Attach both legacy score and Scout Score 2.0, then sort by Scout total."""
    for game in games:
        game["score"] = calculate_score(game)
        game["scout_score"] = calculate_scout_score(game)

    games.sort(
        key=lambda x: x["scout_score"]["total"],
        reverse=True,
    )

    return games


def scan_games(user_settings=None):
    """Scan games from all sources, filter, save snapshots, and rank."""
    _log("scan_games started")

    if user_settings is None:
        user_settings = {
            "minimum_visits": 0,
            "minimum_players": 0,
            "minimum_growth": 0,
        }

    _log(f"settings: {user_settings}")

    found_games = []

    games = collect_games()
    _log(f"collected {len(games)} games: {games}")

    for game in games:
        _log(f"processing game id={game.get('id')}")
        info = get_game_info(game["id"])
        _log(f"get_game_info result: {info is not None}")

        if info is None:
            continue

        info["source"] = game.get("source", "Unknown")
        save_game_snapshot(info)
        _log(f"saved snapshot for {info['name']}")

        growth = get_growth(info["id"])
        info["growth"] = growth
        info["trend_score"] = calculate_trend_score(info, growth)
        _log(f"growth={growth}, trend_score={info['trend_score']}")

        if passes_filters(info, user_settings):
            _log(f"passes filters, adding {info['name']}")
            found_games.append(info)
        else:
            _log(
                f"filtered out: visits={info['visits']}, "
                f"players={info['playing']}, growth={info['growth']}"
            )

    _log(f"found {len(found_games)} games before ranking")
    ranked = rank_games(found_games)
    _log(f"returning {len(ranked)} ranked games")
    return ranked
