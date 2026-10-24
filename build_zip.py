"""Rebuild GameScout-wispbyte-flat.zip (flat layout: files at zip root).

Includes everything in Roblox-Game-Scout-Bot/ that belongs to a deploy:
all root .py modules (plus tests/), the shipped non-py files (.env.example,
ARCHITECTURE.md, CHANGELOG.md, DEPLOY.md, CODE_OF_CONDUCT.md,
CONTRIBUTING.md, SECURITY.md, LICENSE, README.md, requirements.txt).

EXCLUDES: the local sqlite db, .env (secrets!), .gitignore, caches,
__pycache__, .venv, .git, .pytest_cache, .github, deploy/, run_bot.bat,
nonexistent-path.db, and *.log files. Verifies byte-equality of every
zipped entry against disk and runs in-zip wire checks (new modules
present, docs updated, version bumped, no secrets).
"""
import hashlib
import os
import re
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(ROOT, "Roblox-Game-Scout-Bot")
OUT = os.path.join(ROOT, "GameScout-wispbyte-flat.zip")

EXCLUDE_FILES = {
    ".env", ".gitignore", "scoutbot.db", "scan_debug.log",
    "run_bot.bat", "nonexistent-path.db",
    "GameScout-wispbyte-flat.zip",
}
EXCLUDE_DIRS = {
    "__pycache__", ".venv", ".git", ".pytest_cache", ".github",
    "node_modules", "deploy",
}

entries = {}
for root, dirs, files in os.walk(BOT):
    dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
    for f in sorted(files):
        if f in EXCLUDE_FILES or f.endswith(".log") or f.endswith(".pyc"):
            continue
        full = os.path.join(root, f)
        arc = os.path.relpath(full, BOT).replace("\\", "/")
        entries[arc] = full

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for arc in sorted(entries):
        z.write(entries[arc], arc)

# --- Verify ---
z = zipfile.ZipFile(OUT)
names = z.namelist()
assert sorted(names) == sorted(entries), "zip/member mismatch!"
mismatched = [
    arc for arc in names
    if hashlib.sha256(z.read(arc)).hexdigest()
    != hashlib.sha256(open(entries[arc], "rb").read()).hexdigest()
]
print("entries:", len(names))
print("byte-MISMATCH:", mismatched if mismatched else "NONE")
bad = [n for n in names if n == ".env" or n.endswith((".db", ".log"))]
print("secret/db/log entries:", bad if bad else "NONE")
token_re = re.compile(r"DISCORD_TOKEN\s*=\s*['\"]?[A-Za-z0-9_\-.]{20,}")
leak = [n for n in names if token_re.search(z.read(n).decode("utf-8", "ignore"))]
print("hardcoded-token leak:", leak if leak else "NONE")
for member in ("ledger.py", "tests/test_ledger.py", "scorecard.py"):
    assert member in names, f"missing from zip: {member}"
print("new members present: ledger.py, tests/test_ledger.py, scorecard.py")

checks = [
    ("CHANGELOG.md", "1.10.0 — Ledger"),
    ("ARCHITECTURE.md", "| Ledger | `ledger.py`"),
    ("ARCHITECTURE.md", "LEDGER_WEEKDAY"),
    ("DEPLOY.md", "Weekly Ledger: the bot grading its own calls"),
    ("DEPLOY.md", "`LEDGER_ENABLED` / `LEDGER_WEEKDAY` / `LEDGER_HOUR_UTC`"),
    ("config.py", "LEDGER_WEEKDAY"),
    ("scheduler.py", "weekly_ledger"),
    ("ledger.py", "grade_ai_call"),
    ("ledger.py", "weekly_ledger_body"),
]
fails = [
    (f, needle) for f, needle in checks
    if needle not in z.read(f).decode("utf-8", "ignore")
]
if fails:
    raise SystemExit(f"in-zip doc/wire checks FAILED: {fails}")
print("In-zip doc/wire checks: ALL PASS (%d)" % len(checks))
m = re.search(
    r'BOT_VERSION = "([^"]+)"', z.read("config.py").decode("utf-8", "ignore")
)
print("in-zip BOT_VERSION:", m.group(1))
