"""Single source of truth for alert priority tiers.

The bot classifies each alert (discovery scan or tracker explosion) into one
of three tiers: high, medium, or low. Each user's `alert_level` setting is
also one of three tiers — "All", "Medium+", or "High Only". When an alert
is posted, the watcher (or, for discovery broadcasts, every subscriber) is
mentioned only if `priority_rank >= setting_rank`.

Discovery broadcasts (scan_games output to #alerts) don't carry per-watcher
state, so the priority is rendered into the embed title for human scanning
only — no user mentions are added.
"""

# --- alert priority tiers (computed from signal strength) -----------------
HIGH = "high"
MEDIUM = "medium"
LOW = "low"

# --- user-facing threshold choices (stored in users.alert_level) ----------
SETTING_ALL = "all"           # ping on every alert
SETTING_MEDIUM_PLUS = "medium"  # ping on Medium and High
SETTING_HIGH_ONLY = "high"    # ping only on High

_VALID_SETTINGS = {SETTING_ALL, SETTING_MEDIUM_PLUS, SETTING_HIGH_ONLY}
_VALID_PRIORITIES = {HIGH, MEDIUM, LOW}

# Rank lets us compare tiers numerically without string gymnastics.
_PRIORITY_RANK = {HIGH: 3, MEDIUM: 2, LOW: 1}


def normalize_setting(value):
    """Coerce a user-supplied setting string to a canonical value.

    Accepts the documented names ("All" / "Medium+" / "High Only") and the
    lowercase codes ("all" / "medium" / "high"). Falls back to SETTING_ALL
    when the value is missing or unrecognized — that preserves the bot's
    pre-prioritization behavior for users who haven't touched /settings.
    """
    if value is None:
        return SETTING_ALL
    cleaned = str(value).strip().lower()

    # Friendly labels users might type in the slash command.
    aliases = {
        "all": SETTING_ALL,
        "every": SETTING_ALL,
        "everything": SETTING_ALL,
        "low+": SETTING_ALL,
        "medium+": SETTING_MEDIUM_PLUS,
        "medium": SETTING_MEDIUM_PLUS,
        "med+": SETTING_MEDIUM_PLUS,
        "med": SETTING_MEDIUM_PLUS,
        "high": SETTING_HIGH_ONLY,
        "high only": SETTING_HIGH_ONLY,
        "high+": SETTING_HIGH_ONLY,
    }
    if cleaned in _VALID_SETTINGS:
        return cleaned
    if cleaned in aliases:
        return aliases[cleaned]
    return SETTING_ALL


def setting_rank(setting):
    """Numeric rank for a user setting; higher = stricter."""
    return _PRIORITY_RANK[normalize_setting(setting)]


def should_mention(priority, setting):
    """Return True if the user wants a mention at this priority tier."""
    return _PRIORITY_RANK[priority] >= setting_rank(setting)


def priority_emoji(priority):
    return {
        HIGH: "🚨",
        MEDIUM: "📈",
        LOW: "👀",
    }.get(priority, "📊")


def priority_label(priority):
    return {
        HIGH: "BREAKOUT DETECTED",
        MEDIUM: "Growing Opportunity",
        LOW: "Small Growth",
    }.get(priority, "Activity")
