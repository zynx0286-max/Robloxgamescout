---
name: Discord duplicate bot processes
description: Two `python main.py` PIDs sharing one Discord token race the interaction ACK; the loser shows up to users as "application did not respond".
---

If a Discord bot starts crashing disk-resident (e.g., you restart but forget to kill the prior one, or the dev container leaves a stale process behind), **two `python main.py` processes will share the same `DISCORD_TOKEN`**. Both reconnect to the gateway, both register the same slash commands, both eagerly answer the same interaction.

Discord's gateway routes each InteractionCreate event to **one** of the connected sessions for ACK. Whichever bot is sitting in `setup_hook` / settling into `readiness` at that moment loses the race: the user sees the canonical 3-second timeout error "**This application did not respond**" even though the bot *did* handle the command a second later on the other process.

**Why:** discord.py is single-process per token. Two processes look healthy to the gateway but only one of them wins each Interaction. The losing process's response is silently dropped by the gateway because the ACK already went to the other one.

**How to apply:**
- Before restarting in dev, run `pkill -9 -f 'python main.py'` (or the user's shell equivalent) and confirm `pgrep -af 'python.*main\.py'` returns a *single* PID.
- The "application did not respond" symptom is **not** a code bug per se — it is the dup-process race. Always check `pgrep` first.
- Run the bot in one place (one Shell tab, one tmux pane, one systemd unit). Don't run it in both a debug pane and the user's preview pane simultaneously.
