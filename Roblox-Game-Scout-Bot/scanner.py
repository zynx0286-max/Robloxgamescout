def calculate_score(game):
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

    return score


def rank_games(games):
    for game in games:
        game["score"] = calculate_score(game)

    games.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return games
