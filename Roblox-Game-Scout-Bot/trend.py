def calculate_trend_score(game, growth):
    score = 0

    # Growth speed
    if growth >= 200:
        score += 40
    elif growth >= 100:
        score += 30
    elif growth >= 50:
        score += 20

    # Current players
    if game["playing"] >= 5000:
        score += 25
    elif game["playing"] >= 1000:
        score += 15

    # Engagement (favorites-to-visits ratio)
    if game["favorites"] > 0 and game["visits"] > 0:
        ratio = game["favorites"] / game["visits"]
        if ratio >= 0.05:
            score += 20

    return min(score, 100)
