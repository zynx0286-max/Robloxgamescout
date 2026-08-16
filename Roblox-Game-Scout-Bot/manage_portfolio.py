"""Manage which games appear on your public portfolio.

Examples:
    python manage_portfolio.py add 994732206 --name "HEAD TAP" --role "QA Tester"
    python manage_portfolio.py list
    python manage_portfolio.py hide 994732206
    python manage_portfolio.py remove 994732206
"""

import argparse

from portfolio_db import (
    add_portfolio_game,
    get_portfolio_game,
    list_portfolio_games,
    remove_portfolio_game,
    update_portfolio_game,
)


def cmd_add(args):
    add_portfolio_game(
        game_id=args.game_id,
        display_name=args.name,
        role=args.role,
        description=args.description,
        project_url=args.project_url,
        roblox_url=args.roblox_url,
        discord_url=args.discord_url,
        visible=not args.hidden,
        sort_order=args.sort_order,
    )
    print(f"added/updated portfolio game {args.game_id} ({args.name})")


def cmd_list(_args):
    games = list_portfolio_games(visible_only=False)
    if not games:
        print("portfolio is empty")
        return
    for g in games:
        flag = "visible" if g["visible"] else "hidden "
        print(f"  [{flag}] {g['sort_order']:>3} {g['game_id']:>12}  {g['display_name']}")


def cmd_show(args):
    game = get_portfolio_game(args.game_id)
    if game is None:
        print(f"game {args.game_id} is not on the portfolio")
        return
    for key, value in game.items():
        print(f"  {key}: {value}")


def cmd_update(args):
    fields = {
        "display_name": args.name,
        "role": args.role,
        "description": args.description,
        "project_url": args.project_url,
        "roblox_url": args.roblox_url,
        "discord_url": args.discord_url,
        "visible": args.visible,
        "sort_order": args.sort_order,
    }
    changed = update_portfolio_game(args.game_id, **{k: v for k, v in fields.items() if v is not None})
    print("updated" if changed else f"game {args.game_id} not found / nothing to change")


def cmd_hide(args):
    print("updated" if update_portfolio_game(args.game_id, visible=False) else "not found")


def cmd_remove(args):
    remove_portfolio_game(args.game_id)
    print(f"removed {args.game_id}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="add or upsert a game on the portfolio")
    p_add.add_argument("game_id", type=int)
    p_add.add_argument("--name", required=True, help="display name")
    p_add.add_argument("--role", default="")
    p_add.add_argument("--description", default="")
    p_add.add_argument("--project-url", default="")
    p_add.add_argument("--roblox-url", default="")
    p_add.add_argument("--discord-url", default="")
    p_add.add_argument("--sort-order", type=int, default=0)
    p_add.add_argument("--hidden", action="store_true", help="add hidden")
    p_add.set_defaults(func=cmd_add)

    sub.add_parser("list", help="list all portfolio games").set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show one game's portfolio entry")
    p_show.add_argument("game_id", type=int)
    p_show.set_defaults(func=cmd_show)

    p_upd = sub.add_parser("update", help="update portfolio fields")
    p_upd.add_argument("game_id", type=int)
    p_upd.add_argument("--name")
    p_upd.add_argument("--role")
    p_upd.add_argument("--description")
    p_upd.add_argument("--project-url")
    p_upd.add_argument("--roblox-url")
    p_upd.add_argument("--discord-url")
    p_upd.add_argument("--visible", type=lambda v: v.lower() in ("1", "true", "yes"))
    p_upd.add_argument("--sort-order", type=int)
    p_upd.set_defaults(func=cmd_update)

    p_hide = sub.add_parser("hide", help="hide a game (keep data)")
    p_hide.add_argument("game_id", type=int)
    p_hide.set_defaults(func=cmd_hide)

    p_rm = sub.add_parser("remove", help="remove a game and its snapshots")
    p_rm.add_argument("game_id", type=int)
    p_rm.set_defaults(func=cmd_remove)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
