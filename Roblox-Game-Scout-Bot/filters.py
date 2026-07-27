def passes_filters(game, settings):
    """Return True if a game meets the user's filter settings."""
    if game["visits"] < settings.get("minimum_visits", 0):
        return False

    if game["playing"] < settings.get("minimum_players", 0):
        return False

    # Only enforce a growth floor when the user actually set one above 0.
    if settings.get("minimum_growth", 0) > 0 and game.get("growth", 0) < settings["minimum_growth"]:
        return False

    # Smart Filters: upper bounds. 0 (or missing) means no upper cap.
    max_players = settings.get("maximum_players", 0)
    if max_players and max_players > 0 and game["playing"] > max_players:
        return False

    max_visits = settings.get("maximum_visits", 0)
    if max_visits and max_visits > 0 and game["visits"] > max_visits:
        return False

    # Genre filter: "Any" or empty means no filter. Otherwise require a
    # case-insensitive keyword match in the game name (best-effort given
    # Roblox Discovery doesn't expose a genre field).
    genre = (settings.get("genre") or "Any").strip()
    if genre and genre.lower() != "any":
        needle = genre.lower()
        if needle not in game.get("name", "").lower():
            return False

    # Game age: enforced only when the game carries a `created` or
    # `first_seen` ISO timestamp. Otherwise pass.
    max_age = settings.get("max_age", 0)
    if max_age and max_age > 0:
        iso = game.get("created") or game.get("first_seen")
        if iso:
            try:
                from datetime import datetime, timezone

                stamp = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - stamp).days
                if age_days > max_age:
                    return False
            except Exception:
                pass

    return True
