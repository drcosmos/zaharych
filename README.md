# Zaharych — Autonomous AI Coach Daemon

An autonomous AI assistant that lives in Telegram: it pings you on a schedule,
answers like a person with memory, and runs entirely on your own machine.

Built as a practical experiment in **token-efficient agent design**:
scheduled messages cost **zero** LLM tokens (they're templates), and the
"living brain" activates only when the user actually writes.

## Architecture

```
Telegram ←→ daemon.py (long-polling + scheduler) ←→ pi --print --session-id zaharych-tg
                                                       ↑ cwd = ~/Documents/ai
                                              (AGENTS.md = persona, memory/ = long-term memory)
```

- **Persona & memory** — the LLM runtime (`pi`) loads `AGENTS.md` (persona,
  behavior rules) and a `memory/` folder (structured long-term notes) from the
  working directory. The agent is defined by plain Markdown files, not code.
- **Persistent session** — a named session (`zaharych-tg`) keeps conversation
  context between messages, isolated from other interactive sessions.
- **Token economy** — scheduled pings are pre-written templates (0 tokens).
  The LLM is invoked only for user messages and the morning brief.

## Quick start

1. **Create a bot**: talk to [@BotFather](https://t.me/BotFather) → `/newbot`
   → pick a name. You'll get a token like `123456:AA...`.
2. **Secrets**:
   ```bash
   cp .env.example .env
   nano .env   # paste your TG_BOT_TOKEN, optionally set ZAHARYCH_TZ
   ```
   `.env` and `chat.id` are git-ignored — secrets stay local.
3. **Dependencies** (virtualenv):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. **Run**:
   ```bash
   python3 src/daemon.py
   ```
   For 24/7 operation use `src/keeper.sh` — a tiny supervisor loop that
   restarts the daemon if it dies. Stop it with:
   `touch /tmp/zaharych.stop && pkill -f daemon.py`
5. **Pair**: send the bot any message — it stores your `chat.id` and from
   then on talks only to you.

## Daily schedule

| Time | Message | Brain |
|------|---------|-------|
| 07:00 | Morning brief (goals + health routine) | LLM |
| 12:00 | Midday check-in | template |
| 21:30 | "Lights out" reminder | template |
| You write | Live reply with full memory | LLM |

## Roadmap (phase 2+)

- Google Calendar: read the day's meetings into the morning brief
- Position alerts: crypto watchlist checks
- Task tracker: "you promised — where's the result?"
- systemd unit for boot autostart

## Logs

`logs/daemon.log` — everything the daemon does. If the bot goes quiet,
look there first.
