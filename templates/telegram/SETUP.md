# Telegram channel — setup

This folder is the **Telegram implementation**: the pusher plus the run / SLURM / cron
wrappers wired to Telegram. It is a drop-in alternative to [`../discord/`](../discord/) —
the shared pipeline in [`../scripts/`](../scripts/) (fetch → triage → judge, plus the
deep-read / figure / JC tools) is identical for both channels. Use this if you'd rather
receive digests on Telegram than Discord.

```
telegram/
├── push_telegram.py        # render digest → Telegram Bot API sendMessage
├── run_biorxiv.sh          # fetch → triage → judge → push_telegram   (bioRxiv)
├── run_journals.sh         # same, for the weekly journal scan
├── run_biorxiv.sbatch      # SLURM wrapper → run_biorxiv.sh
├── run_journals.sbatch     # SLURM wrapper → run_journals.sh
├── cron_submit_biorxiv.sh  # what cron calls → sbatch run_biorxiv.sbatch
└── cron_submit_journals.sh # what cron calls → sbatch run_journals.sbatch
```

## Two directions of communication

| Direction | Mechanism | Set up here? |
|---|---|---|
| **Outbound digest** (bot → you) | Telegram **Bot API** (`sendMessage`) | ✅ yes, below |
| **Inbound Q&A** (you → bot: "expand #3", "deep-dive", "show me Fig.1") | a **project-bound Telegram bot** | ↗ see the bot repo below |

## 1. Outbound: the bot token (required for the digest)

Create a bot with **@BotFather** (`/newbot`) to get a token, and get your numeric chat id
(message the bot, then read `getUpdates`, or use **@userinfobot**). Then:

```bash
umask 077
cp config/telegram.env.example config/telegram.env
# edit config/telegram.env:
#   TELEGRAM_BOT_TOKEN=123456789:AA...           ← from BotFather
#   TELEGRAM_CHAT_ID=<your numeric chat id>
chmod 600 config/telegram.env

# preview WITHOUT sending, then it's ready:
python3 telegram/push_telegram.py --no-send < some_judged.jsonl
```

`push_telegram.py` reads the token + chat id from `config/telegram.env` (git-ignored). The
token is a **credential** — anyone with it controls your bot. Never commit it.

It supports the same flags as the Discord pusher (`--source`, `--scanned`,
`--keyword-pass`, `--triage-pass`, `--quota-hit`, `--window-from/-to`, `--notice`,
`--text`), so the run wrappers here are identical to the Discord ones except for the push
command — which is why both channels stay in sync.

## 2. Inbound: the Q&A bot (optional but recommended)

The follow-up features ("expand #N", "deep-dive", "show me Fig.1") are driven by a
project-bound Telegram bot that runs Claude Code against this project directory. Set it up
with the companion repo:

**→ https://github.com/Lihan-Zhong/claude-code-telegram-multibot**

Point that bot at this `$PROJECT_ROOT` so it can read `state/latest_digest.json` and run
`scripts/deep_analyze.py` / `scripts/get_figure.py` on demand. (The same bot token can
serve both the digest push and the Q&A, or use two bots — your call.)

## 3. Wire cron

```crontab
0 8  * * *  $PROJECT_ROOT/telegram/cron_submit_biorxiv.sh   # bioRxiv AM
0 20 * * *  $PROJECT_ROOT/telegram/cron_submit_biorxiv.sh   # bioRxiv PM
0 20 * * 6  $PROJECT_ROOT/telegram/cron_submit_journals.sh  # journals, Saturdays
```

Remember to run the placeholder substitution (paths, SLURM partition/account) over
`telegram/*.sh telegram/*.sbatch` — see the main [SKILL.md](../../SKILL.md) step 2.
