# Discord channel — setup

This folder is the **Discord implementation**: the pusher plus the run / SLURM / cron
wrappers wired to Discord. The shared pipeline (fetch → triage → judge, plus the
deep-read / figure / JC tools) lives in [`../scripts/`](../scripts/) and is identical for
both channels.

```
discord/
├── push_discord.py         # render digest → POST to the incoming webhook
├── run_biorxiv.sh          # fetch → triage → judge → push_discord   (bioRxiv)
├── run_journals.sh         # same, for the weekly journal scan
├── run_biorxiv.sbatch      # SLURM wrapper → run_biorxiv.sh
├── run_journals.sbatch     # SLURM wrapper → run_journals.sh
├── cron_submit_biorxiv.sh  # what cron calls → sbatch run_biorxiv.sbatch
└── cron_submit_journals.sh # what cron calls → sbatch run_journals.sbatch
```

## Two directions of communication

| Direction | Mechanism | Set up here? |
|---|---|---|
| **Outbound digest** (bot → you) | Discord **incoming webhook** | ✅ yes, below |
| **Inbound Q&A** (you → bot: "expand #3", "deep-dive", "show me Fig.1") | a **project-bound Discord bot** | ↗ see the bot repo below |

The two are independent: the webhook is a one-way POST, and the Q&A bot answers from
`state/latest_digest.json` on disk — it doesn't read the webhook feed.

## 1. Outbound: the incoming webhook (required for the digest)

In Discord: **target channel → Edit Channel → Integrations → Webhooks → New Webhook →
Copy URL**, then:

```bash
umask 077
printf '%s\n' 'https://discord.com/api/webhooks/XXXX/YYYY' > "$PROJECT_ROOT/state/discord_webhook.txt"
chmod 600 "$PROJECT_ROOT/state/discord_webhook.txt"

# verify WITHOUT posting, then preview a card, then it's ready:
python3 discord/push_discord.py --healthcheck            # GET → {name, channel_id, guild_id}
python3 discord/push_discord.py --dry-run < some_judged.jsonl
```

`push_discord.py` also resolves the webhook from `$LIT_DISCORD_WEBHOOK` if set. The URL is
a **credential** — anyone with it can post to your channel. It stays in `state/`, which is
git-ignored. Never commit it.

**One-paper-per-message** is the default (each card is individually reply-/react-able); set
`LIT_DISCORD_BATCH=1` to pack up to 10 embeds per message instead.

## 2. Inbound: the Q&A bot (optional but recommended)

The follow-up features ("expand #N", "deep-dive", "show me Fig.1") are driven by a
project-bound Discord bot that runs Claude Code against this project directory. Set it up
with the companion repo:

**→ https://github.com/Lihan-Zhong/claude-code-discord-multibot**

Point that bot at this `$PROJECT_ROOT` so it can read `state/latest_digest.json` and run
`scripts/deep_analyze.py` / `scripts/get_figure.py` on demand. Use a **separate** channel
from the webhook feed (the digest channel stays push-only).

## 3. Wire cron

```crontab
0 8  * * *  $PROJECT_ROOT/discord/cron_submit_biorxiv.sh   # bioRxiv AM
0 20 * * *  $PROJECT_ROOT/discord/cron_submit_biorxiv.sh   # bioRxiv PM
0 20 * * 6  $PROJECT_ROOT/discord/cron_submit_journals.sh  # journals, Saturdays
```

Remember to run the placeholder substitution (paths, SLURM partition/account) over
`discord/*.sh discord/*.sbatch` — see the main [SKILL.md](../../SKILL.md) step 2.
