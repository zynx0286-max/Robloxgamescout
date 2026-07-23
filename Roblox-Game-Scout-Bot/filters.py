def passes_filters(game, settings):
    """Return True if a game meets the user's filter settings."""
    if game["visits"] < settings["minimum_visits"]:
        return False

    if game["playing"] < settings["minimum_players"]:
        return False

    if game.get("growth", 0) < settings["minimum_growth"]:
        return False

    return True
