<!-- Language switch -->
**English** · [中文](README.zh-CN.md)

# 📚 Literature Monitor Bot

An **agent-deployed**, hands-off literature radar. It scans **bioRxiv** (twice a day),
**a configurable set of journals** via NCBI (weekly), and optional **non-PubMed journals**
via OAI-PMH, runs every paper through a 3-stage filter funnel, and pushes a concise,
summarized digest card to a **Discord webhook** (Telegram supported too). You can then ask
follow-up questions right in a chat channel — expand an entry, deep-read the full PDF,
or pull a specific figure.

> 📡 **We ship a documented scanning strategy for each source** — bioRxiv API,
> PubMed-by-ISSN, OAI-PMH for non-PubMed journals, and an RSS pattern — with the gotchas
> and hardening each one needed. See **[docs/feed-sources.md](docs/feed-sources.md)**.

### Where it lives: an always-on machine → your phone

This is designed to run on a computer that **never powers off** — an **HPC login node, a
lab workstation, or a NAS**. That box quietly runs the scheduled scans around the clock,
and the only thing that reaches *you* is a tidy push to **Discord or Telegram**. The point
is to turn a machine you already keep running into an **AI-curated feed**: instead of
checking bioRxiv and a dozen journal tables of contents yourself, the always-on host does
the reading and the filtering, and delivers just the handful of papers worth your
attention straight to the app on your phone — where you can also ask it to go deeper.

The reference setup runs on an **HPC via `cron → sbatch`**, so the scans keep working
whether or not you have a session open. The core pipeline is plain Python + the `claude`
CLI, so on a workstation or NAS you can drive `run_biorxiv.sh` / `run_journals.sh` straight
from `cron` (or a `systemd` timer) and skip the SLURM layer entirely.

---

## ⭐ What makes this different: it's *agentic*

Most literature-alert tools make **you** hand-write a brittle list of keywords and hope
for the best. This one is built to be **deployed by a coding agent** (e.g.
[Claude Code](https://claude.com/claude-code)), and that changes the whole setup step.

> **The research focus ships completely blank on purpose.**
> Nothing about any research topic is hard-coded anywhere in the code — every LLM prompt
> reads your focus from a config file at runtime.

**The recommended way to deploy is to just talk to the agent.** Instead of editing config
files by hand, you:

1. **Describe your research** to the agent in a paragraph or two — the methods/tech you
   care about, the biological systems or questions, what you'd consider a hit vs. noise.
2. **Give it 3–5 "must-catch" example papers** — ones you would definitely want flagged.
3. **Let the agent write the config for you** — it drafts:
   - `config/research_focus.txt` — the focus statement injected into every LLM prompt,
   - `config/keywords.txt` — the free Stage-1 pre-filter (kept broad for recall),
   - `config/journals.json` + `config/categories.txt` — which journals and bioRxiv
     categories to watch.
4. **Iterate with the agent** — it dry-runs the funnel on recent papers, you look at what
   passed and what didn't, and it tunes the focus/keywords until precision and recall look
   right **for your field** — not someone else's.

The result is a monitor tuned to *your* research by a conversation, not by you memorizing
a config schema. That agent-in-the-loop tuning is the core feature.

See **[SKILL.md](SKILL.md)** for the full step-by-step deployment guide (it doubles as a
[Claude Code Skill](https://claude.com/claude-code) — drop the folder into
`~/.claude/skills/` and the agent can deploy the whole thing for you).

---

## How it works — the 3-stage funnel

```
Source (bioRxiv API / NCBI E-utils / OAI-PMH)
  │  fetch + dedup (cache/seen.sqlite by doi+version)
  ▼
Stage 1  category whitelist + keyword pre-filter          ← FREE, no LLM
  │
  ▼
Stage 2  title triage: ALL titles in ONE LLM call → yes/maybe/no
  │        (yes + maybe pass on)
  ▼
Stage 3  abstract judge: 8 papers / LLM call → score 0-5 + summary + commentary
  │        (relevant = score ≥ 3)
  ▼
Push     score ≥ 3 → Discord cards (+ archive + write state/latest_digest.json)
```

The funnel is the whole point: two cheap stages throw out the ~95% that don't matter, so
only a handful of papers ever cost real LLM tokens. Typical budget is **~10–25K tokens
per run** — roughly **15× cheaper** than judging every abstract directly.

### Two channels, on purpose

- **Outbound digest = a Discord incoming webhook.** A plain HTTPS POST from cron — no
  daemon, no gateway, no token to refresh. The scheduled push works with no live session.
- **Follow-up Q&A = a project-bound chat bot.** It reads the digest from disk, not from
  chat history, so it works even if the push landed in a different channel and the webhook
  never needs to be two-way.

---

## 💬 The follow-up / Q&A system

Once a digest lands, the digest itself is just the start. A project-bound chat bot answers
follow-ups by reading `state/latest_digest.json` (and per-run archives under
`digests/runs/`) from disk — so it works without re-fetching or re-scanning:

- **Expand an entry** — "expand #3" / "展开第 3 条" — pulls up the full summary and metadata
  for that paper from the last digest. No LLM call, no refetch. Lookup works by index **or**
  by a title substring.
- **Deep read** — "deep-dive this one" / "深度追问" — runs `deep_analyze.py` on the paper:
  it fetches the **full text** (JATS → cookie-warmed PDF → Firecrawl → abstract fallback),
  feeds the whole paper to the LLM, and returns a multi-section review. Full-PDF, not just
  the abstract.
- **Show me a figure** — "show me Fig. 1" / "给我看 Fig.1" — runs `get_figure.py`: it finds
  the figure by its caption, renders the **whole page** (so multi-panel figures stay
  intact), and posts the image back into the chat.
- **Journal-club prep** — `jc_prep.py` turns a PDF into a full walkthrough: per-figure
  interpretation, a method deep-dive, and a slide outline.

All of these are meant to be driven from the chat channel — you read the digest, then just
ask. The bot does the fetching, rendering, and summarizing.

The Q&A bot is a project-bound Claude Code bot, set up per channel — see
**[templates/discord/SETUP.md](templates/discord/SETUP.md)**
(→ [claude-code-discord-multibot](https://github.com/Lihan-Zhong/claude-code-discord-multibot))
or **[templates/telegram/SETUP.md](templates/telegram/SETUP.md)**
(→ [claude-code-telegram-multibot](https://github.com/Lihan-Zhong/claude-code-telegram-multibot)).

---

## Quick start

The short version (full detail in **[SKILL.md](SKILL.md)**):

1. **Copy** the shared `templates/scripts/` plus one channel folder — `discord/` (default)
   or `telegram/` — into a project root you own, and fill the obvious placeholders (paths,
   SLURM partition/account, a contact email for the NCBI/Crossref polite pool).
2. **Add your Discord webhook** to `state/discord_webhook.txt` (chmod 600). Verify with
   `discord/push_discord.py --healthcheck` (it GETs the webhook without posting).
3. **Set your research focus** — copy the `.example` config files and, ideally,
   **have the agent write them for you** (see the *agentic* section above).
4. **Wire cron** — `cron_submit_*.sh` submits an `sbatch` job to a compute node.
5. **Smoke-test each stage** by hand before trusting cron:
   ```bash
   scripts/fetch_biorxiv.py --days 2 | tee cand.jsonl
   scripts/triage_titles.py --keep yes,maybe < cand.jsonl | tee tri.jsonl
   scripts/llm_judge.py --batch 8 --keep-all < tri.jsonl | tee jud.jsonl
   discord/push_discord.py --dry-run < jud.jsonl        # render, send nothing
   ```

---

## Reliability: failures are *loud*, never fake-empty

The most dangerous failure for a literature bot is one that looks like a quiet day. If a
run pushes **"0 papers today"**, that could mean either:

1. **Genuine quiet** — the LLM ran, judged, nothing scored ≥ 3, or
2. **A silent failure** — a stage crashed, produced 0 candidates, and shipped a fake-empty
   digest that looks exactly like (1).

Every stage failure in this repo is made **loud** instead: it pushes a ⚠️ banner rather
than a fake-empty digest, **does not cache** the un-pushed papers (they're retried on the
next run — nothing is lost), and logs a distinct marker you can grep. The handled cases —
quota exhaustion, transient overload, expired auth, flaky-API timeouts, stuck SLURM jobs —
are documented in the *Operational resilience* section of [SKILL.md](SKILL.md). They were
all found the hard way in production.

---

## Requirements

- **HPC with SLURM** and a queue you have priority on (a lab condo / allocation). The job
  itself is tiny — 1 CPU, 2 GB, 1 hour.
- **Python 3.9+** — the core pipeline uses only the standard library. `pypdf` + **poppler**
  (`pdftoppm` / `pdftotext`) are needed only for the deep-read / figure / JC tools.
- **The `claude` CLI** ([Claude Code](https://claude.com/claude-code)) on `PATH` — this is
  the LLM engine. Calls use `claude -p --tools ""` with prompts on **stdin**.
- **A Discord incoming webhook** (primary channel). Telegram bot token optional.
- Optional: [Firecrawl](https://github.com/firecrawl/firecrawl) as a deep-follow-up fallback.

---

## 🔒 Security

**No secrets are in this repo.** The webhook URL, bot tokens, and chat IDs live only in
config files **you create** at deploy time — all of which are covered by
[`.gitignore`](.gitignore). The shipped `*.example` files contain placeholders only.

- A **Discord webhook URL is a credential** — anyone with it can post to your channel.
  Never commit it.
- `.pyc` files embed the absolute source path of the machine they were built on — the
  `.gitignore` excludes `__pycache__/` and `*.pyc` so your paths don't leak into history.

---

## 📖 Documentation

- **[SKILL.md](SKILL.md)** — the full step-by-step deployment guide (also a Claude Code Skill).
- **[docs/feed-sources.md](docs/feed-sources.md)** — data sources & scanning strategies: the
  gotchas and hardening for bioRxiv / PubMed-by-ISSN / OAI-PMH / RSS, and how to add a new source.

## License

[MIT](LICENSE) © 2026 Lihan Zhong
