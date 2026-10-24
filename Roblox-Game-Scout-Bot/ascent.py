"""
Ascent Radar — early-breakout detection between scan cycles.

The scheduled scanner only *discovers* games every SCAN_INTERVAL minutes
(and its `alerted_games` ledger makes each announced game a one-time
event), while the tracker only watches games someone has chosen to
follow. That leaves a gap: a game that is about to explode — one scan
cycle saw it at 40 CCU, the next discovery pass is 30-45 minutes away —
has nobody measuring its climb *between* passes.

This module closes it:

1. One indexed pass over the bot's own 24-hour `game_history` snapshot
   window finds games whose newest CCU reading is out of line with their
   recent average AND shows a net rise over the window — the "ascent"
   signature. Local reads only: the radar costs no extra Roblox requests.
2. Post state lives in the `ascent_state` table (created additively by
   database.create_database), so the first sighting posts a NEW ASCENT
   alert and each later sighting past the cooldown posts an upgrade that
   names the new CCU level. A game whose discovery alert already posted
   always escalates instead of re-announcing (its dust caught by the
   one-alert-per-game rule is exactly what the radar now reports).
3. Anti-spam: per-game cooldown + a per-cycle cap keep the channel
   readable even in a wild market. Timestamps are UTC-consistent with
   SQLite's datetime('now'/'now', '+N ...') comparisons (both use the
   same clock), so tests can inject offsets with datetime('now', ...).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config import (
    ASCENT_CHECK_INTERVAL_MINUTES,
    ASCENT_COOLDOWN_MINUTES,
    ASCENT_MAX_ALERTS_PER_CYCLE,
    ASCENT_MIN_CCU,
    ASCENT_MIN_WINDOW_RISE_RATIO,
    ASCENT_SPIKE_RATIO,
    DATABASE_PATH,
)

logger = logging.getLogger("ascent")


def _connect(path):
    """Open the shared SQLite file under the sqlite_conn contract."""
    import sqlite_conn

    conn = sqlite3.connect(path)
    sqlite_conn.tune(conn)
    return conn

# How much history the radar considers. 24 h matches the velocity window's
# freshness bar while staying cheap on indexed reads; older climbs are the
# tracker's job, not a fresh-spike signal.
_RADAR_WINDOW_HOURS = 24

# Kind lifetimes, for clarity of reading in /diag checks.
FIRST_FIRE_KIND = "first"
ESCALATE_KIND = "escalate"


def _parse_utc(ts: Optional[str]) -> Optional[datetime]:
    """Parse a SQLite CURRENT_TIMESTAMP-style string as UTC.

    Returns None when unreadable (callers treat that as "unknown").
    """
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        try:
            return datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S.%f").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None


def find_ascent_candidates(
    db_path: Optional[str] = None,
    now: Optional[datetime] = None,
    return_diagnostics: bool = False,
) -> list[dict]:
    """Games whose newest CCU is climbing out of their recent average.

    Two conditions must hold at once for a candidate:
    * ASCENT_SPIKE_RATIO — the newest reading is out of line with the
      average of the game's *other* readings in the window (it is a
      discontinuity, not the natural top of a steady climb);
    * ASCENT_MIN_WINDOW_RISE_RATIO — the newest reading is also at least
      that much larger than the lowest reading in the window (the game
      has actually risen, not just wobbled).

    Freshness is part of the definition: the newest reading must be
    younger than ASCENT_CHECK_INTERVAL_MINUTES + 15, otherwise the game
    simply has not been scanned lately — ascents are detected from fresh
    data, never replayed from stale snapshots.

    Pure function over a database path (default: the live database) and
    an optional `now` for tests. Returns a list of dicts with:

        game_id, game_name, latest_players, latest_visits,
        baseline_players (avg of the other readings), spike_ratio,
        rise_ratio, n_snapshots, was_alerted, age_minutes

    With return_diagnostics=True, returns (candidates, coverage) where
    coverage explains *why* in-window games did not fire — counts per
    skip reason plus the totals — so a silent radar can be told apart
    from a broken one on the same tick.
    """
    db_path = db_path or DATABASE_PATH
    now = now or datetime.now(timezone.utc)

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT game_id, players, visits, timestamp
            FROM game_history
            WHERE timestamp >= datetime('now', ?)
            ORDER BY game_id ASC, timestamp ASC
            """,
            (f"-{_RADAR_WINDOW_HOURS} hours",),
        ).fetchall()
    finally:
        conn.close()

    # Group per game and evaluate the newest reading against the rest.
    per_game: dict[int, list[tuple[str, int, int]]] = {}
    for game_id, players, visits, ts in rows:
        players = players or 0
        bucket = per_game.setdefault(int(game_id), [])
        if len(bucket) < 200:  # process bound for pathological histories
            bucket.append((ts or "", int(players), int(visits or 0)))

    coverage = {
        "games_in_window": len(per_game),
        "snapshots_in_window": sum(len(v) for v in per_game.values()),
        "skipped_too_few_snapshots": 0,
        "skipped_below_min_ccu": 0,
        "skipped_baseline_zero": 0,
        "skipped_ratio_below_bar": 0,
        "skipped_stale_snapshot": 0,
    }

    candidates: list[dict] = []
    for game_id, snaps in per_game.items():
        skips = [k for k in coverage if k.startswith("skipped_")]
        if len(snaps) < 3 or snaps[-1][1] < ASCENT_MIN_CCU:
            coverage[
                "skipped_too_few_snapshots"
                if len(snaps) < 3
                else "skipped_below_min_ccu"
            ] += 1
            continue
        *_, (latest_ts, latest_players, latest_visits) = snaps

        baseline_players = [p for _, p, _ in snaps[:-1]]
        baseline = sum(baseline_players) / len(baseline_players)
        if baseline <= 0:
            # Everything before the newest reading was 0: a from-zero jump
            # is real, but our labels cannot express a spike *ratio**, and
            # 0-CCU games are below any alert bar anyway.
            coverage["skipped_baseline_zero"] += 1
            continue
        min_players = min(baseline_players)

        spike_ratio = latest_players / baseline
        rise_ratio = latest_players / min_players if min_players > 0 else None
        if rise_ratio is None or (
            spike_ratio < ASCENT_SPIKE_RATIO
            or rise_ratio < ASCENT_MIN_WINDOW_RISE_RATIO
        ):
            coverage["skipped_ratio_below_bar"] += 1
            continue

        age_minutes: Optional[float] = None
        parsed = _parse_utc(latest_ts)
        if parsed is not None:
            age_minutes = (now - parsed).total_seconds() / 60.0
        if age_minutes is None or age_minutes > (
            ASCENT_CHECK_INTERVAL_MINUTES + 15
        ):
            coverage["skipped_stale_snapshot"] += 1
            continue

        candidates.append(
            {
                "game_id": game_id,
                "game_name": None,  # resolved by resolve_names()
                "latest_players": latest_players,
                "latest_visits": latest_visits,
                "baseline_players": round(baseline, 1),
                "spike_ratio": round(spike_ratio, 2),
                "rise_ratio": round(rise_ratio, 2),
                "n_snapshots": len(snaps),
                "was_alerted": None,  # resolved by resolve_names()
                "age_minutes": round(age_minutes, 1),
            }
        )

    candidates.sort(key=lambda c: c["spike_ratio"], reverse=True)
    if return_diagnostics:
        return candidates, coverage
    return candidates


def resolve_names(db_path: str, candidates: list[dict]) -> None:
    """Attach game_name and was_alerted to every candidate, in place.

    Names come from alerted_games (the scanner's record) when present,
    else watched_games (the tracker's) — enabling embeds to name things
    without a network call or a Roblox lookup.

    Set-based: one connection and at most TWO queries serve the whole
    candidate list (one indexed lookup into alerted_games, plus a
    watched_games fallback only for ids the alert ledger has never
    seen), instead of 1-2 queries per candidate inside the minute tick.
    """
    if not candidates:
        return
    ids = [c["game_id"] for c in candidates]
    conn = _connect(db_path)
    try:
        q = ", ".join("?" for _ in ids)
        alerted_map: dict = {
            row[0]: row[1]
            for row in conn.execute(
                f"SELECT game_id, game_name FROM alerted_games "
                f"WHERE game_id IN ({q})",
                ids,
            ).fetchall()
        }
        # Fallback set: ids with no alerted row OR with a NULL/empty name
        # (the per-game path consulted watched_games in both cases).
        missing = [gid for gid in ids if not alerted_map.get(gid)]
        watched_map: dict = {}
        if missing:
            wq = ", ".join("?" for _ in missing)
            for gid, name, _added in conn.execute(
                f"SELECT game_id, game_name, date_added FROM watched_games "
                f"WHERE game_id IN ({wq}) AND game_name IS NOT NULL "
                f"ORDER BY date_added DESC",
                missing,
            ).fetchall():
                # Rows come newest-first; the first hit per id wins.
                watched_map.setdefault(gid, name)
    finally:
        conn.close()

    for c in candidates:
        gid = c["game_id"]
        c["was_alerted"] = gid in alerted_map
        alerted = alerted_map.get(gid)
        if alerted:
            c["game_name"] = alerted
            continue
        watched = watched_map.get(gid)
        c["game_name"] = watched if watched else f"Game {gid}"


def _load_state(db_path: str, game_ids: list[int]) -> dict[int, dict]:
    """Read ascent_state rows as {game_id: {last_kind, last_alert_ts}}."""
    if not game_ids:
        return {}
    conn = _connect(db_path)
    try:
        q = ", ".join("?" for _ in game_ids)
        rows = conn.execute(
            f"SELECT game_id, last_kind, last_alert_ts FROM ascent_state "
            f"WHERE game_id IN ({q})",
            game_ids,
        ).fetchall()
    finally:
        conn.close()
    return {
        gid: {"last_kind": kind, "last_alert_ts": ts}
        for gid, kind, ts in rows
    }


def should_post_ascent(
    candidate: dict,
    state: dict[int, dict],
    now: Optional[datetime] = None,
) -> bool:
    """Anti-spam decision for one candidate.

    True when the game has never been posted, or when the last post is
    at least ASCENT_COOLDOWN_MINUTES old. A normaliser _parse_utc that
    fails means "unknown age" — post, and let the DB stamp overwrite it.
    """
    now = now or datetime.now(timezone.utc)
    entry = state.get(candidate["game_id"])
    if not entry or not entry.get("last_alert_ts"):
        return True
    last_ts = _parse_utc(entry["last_alert_ts"])
    if last_ts is None:
        return True
    return (now - last_ts).total_seconds() / 60.0 >= ASCENT_COOLDOWN_MINUTES


def classify_kind(candidate: dict) -> str:
    """first (new sighting) vs escalate (the scanner already announced
    this game — the ascent radar then reports the climb itself)."""
    return ESCALATE_KIND if candidate.get("was_alerted") else FIRST_FIRE_KIND


def mark_ascent_posted(
    db_path: str,
    entries: list[dict],
    now: Optional[datetime] = None,
) -> None:
    """Stamp ascent_state for alerts that were (or will be) posted.

    Stamping happens before the send, so the cooldown clock runs even if
    the channel is misconfigured or a send fails — otherwise a config
    mistake would queue up a burst of old ascents for the moment the
    channel comes back.
    """
    if not entries:
        return
    conn = _connect(db_path)
    try:
        for e in entries:
            conn.execute(
                """
                INSERT INTO ascent_state
                    (game_id, game_name, last_kind, last_ccu,
                     first_seen_ts, last_alert_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    game_name = COALESCE(ascent_state.game_name, excluded.game_name),
                    last_kind = excluded.last_kind,
                    last_ccu = excluded.last_ccu,
                    last_alert_ts = excluded.last_alert_ts
                """,
                (
                    e["game_id"],
                    e.get("game_name"),
                    e["kind"],
                    e.get("latest_players", 0),
                    e["_now_str"],
                    e["_now_str"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Diagnostics renderer (/radar)
# ---------------------------------------------------------------------------

def render_radar_status(
    candidates: list[dict], coverage: dict, max_lines: int = 8,
) -> str:
    """One page of radar diagnostics. Pure — data in, text out.

    Shows the coverage counters (why in-window games did not fire) and
    the current candidates highest-spike-first, so a cold channel can be
    told apart from a broken radar without reading server logs.
    """
    lines = ["🛰 **Ascent radar — current tick**"]

    skipped_total = sum(
        v for k, v in coverage.items() if k.startswith("skipped_")
    )
    lines.append(
        f"Window: **{coverage['games_in_window']}** games · "
        f"**{coverage['snapshots_in_window']}** snapshots · "
        f"{skipped_total} skipped ("
        f"short-history={coverage['skipped_too_few_snapshots']} "
        f"ccu={coverage['skipped_below_min_ccu']} "
        f"zero-base={coverage['skipped_baseline_zero']} "
        f"below-bar={coverage['skipped_ratio_below_bar']} "
        f"stale={coverage['skipped_stale_snapshot']})"
    )

    if not candidates:
        lines.append("_Radar quiet — no qualifying breakouts right now._")
        return "\n".join(lines)

    lines.append(f"**{len(candidates)} candidate(s), highest spike first:**")
    for c in candidates[:max_lines]:
        name = c.get("game_name") or f"Game {c['game_id']}"
        tag = "🔥 escalate" if c.get("was_alerted") else "🚀 first"
        lines.append(
            f"{tag} · **{name}** — {c['latest_players']:,} CCU "
            f"({c['spike_ratio']:g}× avg, {int(c['rise_ratio'] * 100):+d}% "
            f"in-window, {c['age_minutes']:g} min fresh)"
        )
    if len(candidates) > max_lines:
        lines.append(f"…and {len(candidates) - max_lines} more.")
    return "\n".join(lines)


def radar_status(db_path: Optional[str] = None) -> str:
    """Read local data and render /radar's text."""
    db_path = db_path or DATABASE_PATH
    try:
        candidates, coverage = find_ascent_candidates(
            db_path=db_path, return_diagnostics=True
        )
        resolve_names(db_path, candidates)
    except sqlite3.Error as exc:
        logger.error("Radar status read failed: %s", exc)
        return "🛰 _Radar status unavailable: the local store could not be read._"
    return render_radar_status(candidates, coverage)


# ---------------------------------------------------------------------------
# Loop body
# ---------------------------------------------------------------------------

def _fmt_utc_now(now: datetime) -> str:
    """Format a UTC datetime the way SQLite CURRENT_TIMESTAMP writes it."""
    return now.strftime("%Y-%m-%d %H:%M:%S")


def build_ascent_text(candidate: dict) -> str:
    """One-line channel body. Numbers, not verdicts: the embed-style
    detail lives in the tracker/scanner lanes; this is the "eyebrow"
    pinging the channel that something is moving *right now*.
    """
    kind = candidate.get("kind", FIRST_FIRE_KIND)
    name = candidate["game_name"]
    ccu = candidate["latest_players"]
    spike = candidate["spike_ratio"]
    rise_pct = int(candidate["rise_ratio"] * 100)

    if kind == ESCALATE_KIND:
        return (
            f"🔥 **Ascent continues** — **{name}** now at {ccu:,} CCU, "
            f"{spike:g}× its recent average ({rise_pct:+d}% in-window)."
        )
    return (
        f"🚀 **New ascent** — **{name}** jumped to {ccu:,} CCU: "
        f"{spike:g}× its recent average and {rise_pct:+d}% within the "
        f"window. Momentum is building *between* scan cycles."
    )


async def run_ascent_check(bot) -> int:
    """Minute tick: fetch candidates, resolve state, post, record. Fail-soft.

    Returns the number of alerts posted.
    """
    db_path = DATABASE_PATH

    try:
        candidates, coverage = find_ascent_candidates(
            db_path=db_path, return_diagnostics=True
        )
        if not candidates:
            logger.debug(
                "Ascent radar quiet this tick: %s",
                {k: v for k, v in coverage.items() if v},
            )
            return 0
        resolve_names(db_path, candidates)
        state = _load_state(db_path, [c["game_id"] for c in candidates])
    except sqlite3.Error as exc:
        logger.error("Ascent radar could not read the database: %s", exc)
        return 0

    now = datetime.now(timezone.utc)
    to_post: list[dict] = []
    for c in candidates:
        c["kind"] = classify_kind(c)
        if should_post_ascent(c, state, now=now):
            to_post.append(c)

    # Coverage log: fired + passed-over in one line, so operator /diag
    # greps can reconstruct why a tick did (or did not) post.
    logger.info(
        "Ascent radar tick: fired=%d qualified=%d games_in_window=%d "
        "skipped(min_snap=%d min_ccu=%d zero_baseline=%d ratio=%d stale=%d)",
        len(to_post), len(candidates), coverage["games_in_window"],
        coverage["skipped_too_few_snapshots"],
        coverage["skipped_below_min_ccu"],
        coverage["skipped_baseline_zero"],
        coverage["skipped_ratio_below_bar"],
        coverage["skipped_stale_snapshot"],
    )

    if not to_post:
        return 0

    to_post = to_post[:ASCENT_MAX_ALERTS_PER_CYCLE]
    for c in to_post:
        c["_now_str"] = _fmt_utc_now(now)

    alert_channel = None
    if bot is not None:
        from config import ALERT_CHANNEL_ID
        if ALERT_CHANNEL_ID:
            alert_channel = bot.get_channel(ALERT_CHANNEL_ID)

    # Stamp BEFORE sending; see mark_ascent_posted's docstring.
    try:
        mark_ascent_posted(db_path, to_post, now=now)
    except sqlite3.Error as exc:
        # The sheet has a bug — still attempt the send, but never crash
        # the minute tick over bookkeeping.
        logger.error("ascent_state update failed: %s", exc)

    if alert_channel is None:
        logger.warning(
            "Ascent radar had %d alert(s) but ALERT_CHANNEL_ID unresolved.",
            len(to_post),
        )
        return 0

    posted = 0
    for c in to_post:
        try:
            await alert_channel.send(build_ascent_text(c))
            posted += 1
            logger.info(
                "Ascent alert: %s (id=%s) kind=%s ccu=%d spike=%.2f",
                c["game_name"],
                c["game_id"],
                c["kind"],
                c["latest_players"],
                c["spike_ratio"],
            )
        except Exception as exc:
            logger.exception("Ascent post failed for %s: %s",
                             c["game_name"], exc)
    return posted
