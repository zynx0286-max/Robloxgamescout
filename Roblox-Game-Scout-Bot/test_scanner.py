from scanner import rank_games


games = [
    {
        "name": "Simulator A",
        "playing": 5000,
        "visits": 5000000,
        "favorites": 100000
    },
    {
        "name": "Tycoon B",
        "playing": 200,
        "visits": 50000,
        "favorites": 5000
    }
]


results = rank_games(games)


for game in results:
    print(
        game["name"],
        game["score"]
    )
