def get_category(game):
    """Return a category label based on the game's trend score."""
    score = game.get("trend_score", 0)

    if score >= 80:
        return "🔥 Rising Star"
    elif score >= 50:
        return "📈 Growing"
    else:
        return "⚪ Normal"
