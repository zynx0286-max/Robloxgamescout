# Reads must not rewrite user settings

`get_user_filters()` originally "migrated" values on every read: if a user had
`minimum_rating = 0`, `require_discord = false`, or
`require_market_links = false`, it silently wrote the defaults back and
returned them. Combined with `update_user()` not persisting those three
columns at all, the settings shown in `/profile` were read-only decorations.

Rule of thumb: **read paths should never write user state.** One-time legacy
migrations belong in `create_database()` (schema level), not in getters. The
one intentional exception kept: migrating the old broad defaults
(100000 visits / 100 players) to the acquisition defaults, because those
represented a genuinely obsolete default pair, not a user choice.

Related trap found in the same sweep: `UPDATE ... WHERE discord_id = ?` on a
row that doesn't exist silently affects 0 rows — settings updates no-op
without error. Any update helper should `INSERT OR IGNORE` the row first.
