def analyze_game(game):
    """Return human-readable reasons why this game is an opportunity."""
    reasons = []

    score = game.get("score", 0)
    trend = game.get("trend_score", 0)
    growth = game.get("growth", 0)

    if trend >= 80:
        reasons.append("🔥 Strong trending activity")

    if growth >= 100:
        reasons.append("📈 Extremely fast player growth")
    elif growth >= 50:
        reasons.append("📈 Positive player growth")

    if game.get("source") in ("RobloxCharts", "Curated"):
        reason = "📈 Featured on Roblox Charts" if game.get("source") == "RobloxCharts" else "📈 Curated seed game"
        reasons.append(reason)

    if game["playing"] >= 5000:
        reasons.append("👥 High active player count")

    if not reasons:
        reasons.append("📊 Normal activity")

    return reasons


def get_opportunity_level(game):
    """Classify the game as a high opportunity, watchlist, or normal."""
    score = game.get("score", 0)
    trend = game.get("trend_score", 0)

    total = score + trend

    if total >= 150:
        return "🔥 HIGH OPPORTUNITY"
    elif total >= 90:
        return "📈 WATCHLIST"
    else:
        return "⚪ NORMAL"
