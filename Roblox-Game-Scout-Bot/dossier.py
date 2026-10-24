"""
Dossier — everything the bot knows about one game, on demand.

`/dossier <game_id>` and `@bot <game_id>` in the AI channel assemble a
complete briefing: live Roblox state, the bot's accumulated snapshot
history (window growth + velocity), chart presence (rank + cycle-over-
cycle movement), tracker state, alert ledger status, Ascent Radar state,
stored AI verdict, and a deterministic verdict the user can always trust —
the optional AI narrative rides ON TOP, never instead.

Design constraints, enforced by tests:

* The narrative renderer (`pure_llm_style_report`) is PURE: deterministic
  from the data alone, no LLM required. The verdict comes from a stable,
  explainable formula grounded in measurements, never free-form text.
* The real orchestrator (`build_dossier_view`) calls ONE Roblox request
  (`get_game_info`) and everything else is local. Fail-soft: any local
  selector may contribute an empty section on error, and a Roblox failure
  is still answered from local data with an explicit "state may be stale"
  note.
* The report is a fixed-order section list joined into one string, so the
  command path can render it as-is (with a compact view) or the AI
  channel path can route the same text through its narrative.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Optional

from config import DATABASE_PATH
from history import growth_summary
from velocity import compute_velocity
from chart_rank import chart_score_text, latest_positions

logger = logging.getLogger("dossier")


def _connect(path):
    """Open the shared SQLite file under the sqlite_conn contract."""
    import sqlite_conn

    conn = sqlite3.connect(path)
    sqlite_conn.tune(conn)
    return conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(value, fallback: str = "unknown") -> str:
    """Human formatting for a number-or-None."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return fallback
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.1f}"


def _fmt_pct(value, fallback: str = "unknown") -> str:
    """Human formatting for percentage-or-None."""
    if value is None:
        return fallback
    try:
        return f"{float(value):+.1f}%"
    except (TypeError, ValueError):
        return fallback


def compute_window_growth(game_id: int, db_path: Optional[str] = None) -> dict:
    """Re-purposed growth_summary with an explicit name for the dossier.

    Returns {} (not zeros) when there's insufficient history, so callers
    can distinguish "no data" from "flat".
    """
    return growth_summary(int(game_id))


def compute_window_velocity(game_id: int, db_path: Optional[str] = None) -> dict:
    """Velocity over the 72-hour window. Returns {} when insufficent."""
    try:
        v = compute_velocity(int(game_id))
        return v if v.get("snapshot_count", 0) >= 2 else {}
    except Exception:
        return {}


def gather_ledger(db_path: Optional[str] = None, game_id: int = 0) -> dict:
    """Alert/watch/radar state for one game, all local.

    Keys:
        first_alerted   — timestamp from alerted_games, or None
        watchers        — number of human watchers in watched_games
        watched_since   — most recent date_added among watch rows
        ascent_first    — first_seen_ts from ascent_state, or None
        ascent_last     — last_alert_ts from ascent_state, or None
        ascent_kind     — last_kind from ascent_state, or None
    """
    db_path = db_path or DATABASE_PATH
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT first_alerted FROM alerted_games WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        first_alerted = row[0] if row else None

        row = conn.execute(
            "SELECT COUNT(*), MAX(date_added) FROM watched_games "
            "WHERE game_id = ? AND source != 'auto'",
            (game_id,),
        ).fetchone()
        watchers, watched_since = row if row else (0, None)

        row = conn.execute(
            "SELECT first_seen_ts, last_alert_ts, last_kind FROM ascent_state "
            "WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        if row:
            ascent_first, ascent_last, ascent_kind = row
        else:
            ascent_first = ascent_last = ascent_kind = None
    finally:
        conn.close()

    return {
        "first_alerted": first_alerted,
        "watchers": int(watchers or 0),
        "watched_since": watched_since,
        "ascent_first": ascent_first,
        "ascent_last": ascent_last,
        "ascent_kind": ascent_kind,
    }


def gather_stored_verdict(
    game_id: int, db_path: Optional[str] = None
) -> Optional[dict]:
    """The cached AI verdict, if any. None when the game has none."""
    db_path = db_path or DATABASE_PATH
    try:
        conn = _connect(db_path)
        row = conn.execute(
            "SELECT verdict, confidence, strengths, risks, recommendation "
            "FROM ai_analysis_cache WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        return {
            "verdict": row[0],
            "confidence": row[1],
            "strengths": row[2],
            "risks": row[3],
            "recommendation": row[4],
        }
    except sqlite3.Error as exc:
        logger.warning("AI cache read failed for %s: %s", game_id, exc)
        return None


# ---------------------------------------------------------------------------
# The deterministic scoreboard (the always-trustable core)
# ---------------------------------------------------------------------------

def compose_scoreboard(state: dict) -> str:
    """Fixed-order section list rendered from the assembled state dict.

    Keys the scorer reads:
        name, playing, visits, favorites (live numbers, optional),
        state_stale (bool — live fetch failed),
        growth (dict from growth_summary, or {}), velocity (dict or {}),
        chart_positions (list of (label, rank)), chart_text (str),
        ledger (dict from gather_ledger), verdict (dict or None),
        roots (dict — optional creator-insight block)

    Each section degrades gracefully to a quiet line. The order is fixed;
    the caller renders it directly or routes it through a narrative.
    """
    lines = []

    name = state.get("name") or "Unknown game"
    lines.append(f"📒 **Dossier — {name}**")

    # --- Live state -------------------------------------------------------
    if state.get("state_stale"):
        lines.append("")
        lines.append(
            "_⚠️ Live Roblox fetch failed — the numbers below may be **stale**; "
            "snapshot-based sections still reflect the bot's own history._"
        )
    if state.get("playing") is not None:
        lines.append("")
        lines.append("**Live (Roblox):**")
        lines.append(
            f"👥 { _fmt(state['playing']) } playing · "
            f"👀 { _fmt(state['visits']) } visits · "
            f"⭐ { _fmt(state['favorites']) } favorites"
        )
        if state.get("created_at"):
            lines.append(f"🗓 Created: {state['created_at'][:10]}")

    # --- Window growth ----------------------------------------------------
    growth = state.get("growth") or {}
    lines.append("")
    lines.append("**Snapshot history (bot's own):**")
    if growth:
        lines.append(
            f"📈 Window growth: CCU {_fmt_pct(growth.get('ccu_growth_pct'))}, "
            f"visits {_fmt_pct(growth.get('visits_growth_pct'))} "
            f"({growth.get('snapshot_count', 0)} snapshots over "
            f"{growth.get('history_days', 0)}d)"
        )
    else:
        lines.append("_Not enough snapshot history yet — growth becomes reportable after two scan cycles._")

    velocity = state.get("velocity") or {}
    if velocity:
        lines.append(
            f"🚀 Momentum: {velocity.get('ccu_trend', '?')} "
            f"({velocity.get('ccu_spike', 1.0):.2f}× rolling avg, "
            f"{velocity.get('snapshot_count', 0)} samples)"
        )

    # --- Chart presence ----------------------------------------------------
    positions = state.get("chart_positions") or []
    lines.append("")
    lines.append("**Charts:**")
    chart_text = state.get("chart_text")
    if positions:
        lines.append(", ".join(f"{label} #{rank}" for label, rank in positions))
        if chart_text:
            lines.append(chart_text)
    elif chart_text:
        lines.append(chart_text)
    else:
        lines.append("_Not on a Roblox chart in the retained window._")

    # --- Ledger ------------------------------------------------------------
    ledger = state.get("ledger") or {}
    lines.append("")
    lines.append("**Bot ledger:**")
    bits = []
    if ledger.get("first_alerted"):
        bits.append(f"first alerted {str(ledger['first_alerted'])[:16]}")
    if ledger.get("watchers"):
        bits.append(f"{ledger['watchers']} human watcher(s) since {str(ledger['watched_since'])[:16]}")
    if ledger.get("ascent_last"):
        kind_txt = (
            "escalated" if ledger.get("ascent_kind") == "escalate" else "first ascent"
        )
        bits.append(f"radar {kind_txt} {str(ledger['ascent_last'])[:16]}")
    lines.append(" · ".join(bits) if bits else "_Not alerted, watched, or radar-flagged yet._")

    # --- Stored AI verdict --------------------------------------------------
    verdict = state.get("verdict")
    lines.append("")
    lines.append("**AI verdict (cached):**")
    if verdict:
        lines.append(f"“{verdict.get('verdict') or '?'}”")
        conf = verdict.get("confidence")
        if conf is not None:
            lines.append(f"Confidence: {conf}%")
    else:
        lines.append("_Not analyzed yet — run /analyze for the full AI breakdown._")

    # --- Roots (creator insight) --------------------------------------------
    roots = state.get("roots") or {}
    if roots:
        lines.append("")
        lines.append("**Creator insight:**")
        for line in roots["lines"]:
            lines.append(line)

    return "\n".join(lines)


def pure_llm_style_report(state: dict) -> str:
    """Deterministic \"AI-flavored\" narrative — NOT an LLM call.

    The bot's verdict must be explainable and free to run 24/7, so the
    narrative is composed from the data with a fixed scoring rubric. The
    verdict formula is explained in ARCHITECTURE.md and verified in tests;
    nothing in this parser may call the network.

    Formula:
      momentum pts  : spike ratio >= 2.0 → 2; >= 1.3 → 1; else 0
      chart pts     : on a chart → 1
      growth pts    : window CCU growth >= 50% → 2; >= 15% → 1
      network pts   : first_alerted or watched → 1
      total 0..6    → verdict bands:
          5-6 🔥 Strong momentum — buy-side interest likely
          3-4 📈 Building — worth tracking closely
          1-2 🌱 Early signal — thin data, watch the next cycles
          0   ⚪ Flat — no measurable momentum in the bot's data
    """
    growth = (state.get("growth") or {})
    velocity = (state.get("velocity") or {})
    ledger = (state.get("ledger") or {})
    positions = (state.get("chart_positions") or [])

    spike = 0.0
    if velocity.get("ccu_spike"):
        try:
            spike = float(velocity["ccu_spike"])
        except (TypeError, ValueError):
            spike = 0.0

    momentum_pts = 2 if spike >= 2.0 else (1 if spike >= 1.3 else 0)
    chart_pts = 1 if positions else 0
    growth_pct = growth.get("ccu_growth_pct")
    growth_pts = (
        2 if growth_pct is not None and growth_pct >= 50
        else (1 if growth_pct is not None and growth_pct >= 15 else 0)
    )
    network_pts = 1 if (ledger.get("first_alerted") or ledger.get("watchers")) else 0

    total = momentum_pts + chart_pts + growth_pts + network_pts

    if total >= 5:
        headline = "🔥 **Strong momentum** — buy-side interest likely"
    elif total >= 3:
        headline = "📈 **Building** — worth tracking closely"
    elif total >= 1:
        headline = "🌱 **Early signal** — thin data, watch the next cycles"
    else:
        headline = "⚪ **Flat** — no measurable momentum in the bot's data"

    reasons = []
    if momentum_pts:
        reasons.append(f"momentum {spike:.2f}× avg")
    if chart_pts:
        reasons.append("on a Roblox chart")
    if growth_pts:
        reasons.append(f"window CCU {growth_pct:+.0f}%")
    if network_pts:
        reasons.append("bot has already alerted or someone watches it")

    name = state.get("name") or "This game"
    lines = [f"🔎 **Scout read on {name}:** {headline}"]
    if reasons:
        lines.append("Because: " + ", ".join(reasons) + ".")
    else:
        lines.append("The bot has no qualifying signals recorded for it yet.")

    stored = state.get("verdict")
    if stored and (stored.get("verdict") or "").strip():
        lines.append(
            f"🤖 Stored AI take: “{stored['verdict']}”"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator — one Roblox request, everything else local.
# ---------------------------------------------------------------------------

async def build_dossier_view(
    game_id: int,
    include_ai: bool = True,
) -> str:
    """The full /dossier text for one game. Fail-soft throughout.

    Raises nothing: a Roblox failure degrades to a stale-state note and
    still returns a full local report; a local selector failure shrinks
    that section only.
    """
    from roblox_api import get_game_info

    gid = int(game_id)

    # One network call: live state.
    live: Optional[dict] = None
    try:
        live = await get_game_info(str(gid))
    except Exception as exc:
        logger.warning("Dossier live fetch failed for %s: %s", gid, exc)

    state: dict = {
        "name": None,
        "playing": None,
        "visits": None,
        "favorites": None,
        "created_at": None,
        "state_stale": live is None,
    }
    if live:
        state["name"] = live.get("name")
        state["playing"] = live.get("playing")
        state["visits"] = live.get("visits")
        state["favorites"] = live.get("favorites")
        state["created_at"] = live.get("created")

    if not state["name"]:
        # Roblox returned no usable name (fetch failed or a shell payload
        # with empty name) — pull one from the bot's own records so the
        # report still has a real heading. alerted_games first (the
        # scanner's ledger), then watched_games.
        try:
            conn = _connect(DATABASE_PATH)
            row = conn.execute(
                "SELECT game_name FROM alerted_games WHERE game_id = ? "
                "AND game_name IS NOT NULL "
                "UNION ALL "
                "SELECT game_name FROM watched_games WHERE game_id = ? "
                "AND game_name IS NOT NULL LIMIT 1",
                (gid, gid),
            ).fetchone()
            conn.close()
            if row and row[0]:
                state["name"] = row[0]
        except sqlite3.Error:
            pass
    if not state["name"]:
        state["name"] = f"Game {gid}"

    try:
        state["growth"] = compute_window_growth(gid)
    except sqlite3.Error as exc:
        logger.warning("Dossier growth read failed: %s", exc)
        state["growth"] = {}

    try:
        state["velocity"] = compute_window_velocity(gid)
    except sqlite3.Error as exc:
        logger.warning("Dossier velocity read failed: %s", exc)
        state["velocity"] = {}

    try:
        positions = latest_positions(gid)
    except sqlite3.Error as exc:
        logger.warning("Dossier chart read failed: %s", exc)
        positions = []
    state["chart_positions"] = positions

    chart_probe = dict(state)
    chart_probe.setdefault("chart_rank", None)
    try:
        state["chart_text"] = chart_score_text(chart_probe)
    except Exception:
        state["chart_text"] = None

    try:
        state["ledger"] = gather_ledger(game_id=gid)
    except sqlite3.Error as exc:
        logger.warning("Dossier ledger read failed: %s", exc)
        state["ledger"] = {}

    state["verdict"] = gather_stored_verdict(gid) if include_ai else None

    try:
        state["roots"] = await _creator_insight(gid, live)
    except Exception as exc:
        logger.warning("Dossier creator insight failed: %s", exc)
        state["roots"] = None

    scoreboard = compose_scoreboard(state)
    narrative = pure_llm_style_report(state)

    return scoreboard + "\n\n---\n\n" + narrative


async def _creator_insight(gid: int, live: Optional[dict]) -> Optional[dict]:
    """Local-first creator insight for the dossier's roots block.

    Uses the developer_cache (written 24h-ttl by developer_intelligence)
    when the creator is known there; falls back to nothing (not a fetch)
    so the dossier never adds unbounded network cost.
    """
    if not live or not live.get("creator"):
        return None
    creator_name = live.get("creator")
    conn = _connect(DATABASE_PATH)
    try:
        row = conn.execute(
            "SELECT name, member_count, games_total, games_successful, dev_score "
            "FROM developer_cache WHERE name = ? LIMIT 1",
            (creator_name,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    name, members, total, successful, score = row
    lines = [f"👨‍💻 {name}"]
    if members:
        lines.append(f"Group with {members:,} members")
    if total:
        lines.append(
            f"{total} tracked game(s), {successful} with >= 1k CCU "
            f"(dev score {score}/20)"
        )
    return {"lines": lines}
