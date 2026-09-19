---
name: literature-monitor-bot
description: >
  Deploy an automated bioRxiv + journal literature monitor that pushes
  Chinese-summarized digests to Discord (or Telegram), filtered through a
  3-stage funnel (free category+keyword pre-filter → batched LLM title triage →
  batched LLM abstract judge). Runs unattended on an HPC via cron→sbatch; answers
  deep follow-ups (full-PDF analysis, figure retrieval) in a project-bound chat.
  Use when someone wants to (a) stand up a personal literature-watching bot on a
  new HPC account, (b) deploy this bot for a colleague, (c) add a source/journal
  (incl. non-PubMed journals via OAI-PMH), (d) debug a run that silently pushed
  "0 papers", or (e) deep-read a single paper / pull a figure. Triggers: "lit-bot",
  "literature bot", "bioRxiv monitor", "auto literature digest", "deploy lit-bot
  for colleague", "push 0 papers but there are papers", "deep read this preprint".
metadata:
  type: reference
---

# Literature monitor bot — automated bioRxiv + journal digest to Discord

A hands-off literature radar. It scans **bioRxiv** (twice daily), **a configurable set of
journals** via NCBI (weekly), and optional **non-PubMed journals** via OAI-PMH, filters each
paper through a 3-stage funnel, and pushes a Chinese-summarized digest card to a
**Discord webhook** (Telegram supported too). Follow-up questions ("展开第 3 条",
"深度追问", "给我看 Fig.1") are answered by a project-bound chat bot that reads the
digest from disk, fetches the full PDF, and can render figures.

Templates live in `templates/`: a shared `scripts/` pipeline plus one folder per channel —
**`discord/`** (default) and **`telegram/`** — each with its pusher, run/cron wrappers, and
a `SETUP.md`. Copy `scripts/` + one channel folder, fill the placeholders, wire cron.
**No secrets ship in the templates** (webhook/token live only in gitignored config files
you create).

## Architecture — the 3-stage funnel

```
Source (bioRxiv API / NCBI E-utils / OAI-PMH)
  │  fetch + dedup (cache/seen.sqlite by doi+version)
  ▼
Stage 1  category whitelist + keyword pre-filter        ← FREE, no LLM
  │
  ▼
Stage 2  title triage: ALL titles in ONE claude -p call → yes/maybe/no
  │        (yes+maybe pass; ~3K tokens for ~50 titles)
  ▼
Stage 3  abstract judge: 8 papers/claude -p call → score 0-5 + 中文摘要 + 点评
  │        (relevant = score ≥ 3)
  ▼
Push     score≥3 → Discord webhook cards (+ archive digests/, write latest_digest.json)
```

Token budget ≈ **10-25K per run** (vs ~300K without the funnel — ~15× cheaper).

**Two channels of communication, on purpose:**
- **Outbound digest = incoming webhook** — a plain HTTPS POST from cron. No daemon,
  no gateway, no token refresh; the scheduled push works even with no live session.
- **Follow-up Q&A = a project-bound chat bot** — reads `state/latest_digest.json`
  from disk (not chat history), so it works even if the push landed in a different
  channel. The webhook doesn't need to be two-way.

Cron submits `sbatch` to a compute node; the LLM calls (`claude -p`) run there.
Closing your laptop / signing out never affects the scheduled runs.

## Components

**`templates/scripts/` — shared pipeline (channel-agnostic):**

| File | Role |
|---|---|
| `fetch_biorxiv.py` | bioRxiv details API → category whitelist → keyword filter → candidate JSONL. `--days N` or `--from/--to YYYY-MM-DD`. Robust retry. |
| `fetch_journals.py` | NCBI E-utilities (esearch/efetch by ISSN) → drop corrections/editorials → **abstract backfill (Crossref/EuropePMC)** → keyword filter. |
| `fetch_vita.py` | OAI-PMH harvester for a journal NOT in PubMed (example: Vita). Same candidate schema. |
| `triage_titles.py` | Stage 2 — one batched `claude -p` call, yes/maybe/no per title. |
| `llm_judge.py` | Stage 3 — batched `claude -p` (8/call), score 0-5 + 中文摘要 + 点评. |
| `deep_analyze.py` | Single-paper deep read: JATS → cookie-warmed PDF → Firecrawl → abstract; full-text to `claude -p`; 6-section 中文 review. `--doi/--url/--pdf`. |
| `get_figure.py` | Find a figure by caption + render the page (`pdftoppm`, not pdfimages) → PNG to attach. |
| `jc_prep.py` | Journal-club prep: full PDF + per-figure walkthrough + slide outline. |

**`templates/discord/` and `templates/telegram/` — one folder per channel** (pick one at
deploy). Each holds the channel's pusher plus identical run/SLURM/cron wrappers wired to
it, and a channel `SETUP.md`. See [feed-sources doc](docs/feed-sources.md) for sources.

| File | Role |
|---|---|
| `push_discord.py` / `push_telegram.py` | Render digest cards → Discord webhook / Telegram Bot API; archive + write `state/latest_digest.json`. Both take `--source/--scanned/--keyword-pass/--triage-pass/--quota-hit/--window-from/-to/--notice/--text`; `--dry-run`+`--healthcheck` (Discord), `--no-send` (Telegram). |
| `run_biorxiv.sh` / `run_journals.sh` | Wrappers: fetch → triage → judge → push, with quota/fetch-failure handling. |
| `run_*.sbatch` | SLURM submission wrappers. |
| `cron_submit_*.sh` | What cron literally calls; sets PATH then `sbatch`. |
| `SETUP.md` | Channel-specific setup + link to the two-way Q&A bot repo. |

## Prerequisites

1. **HPC with SLURM** and a queue you have **priority** on (a lab condo/allocation).
2. **Python 3.9+** — core pipeline uses only the stdlib. `pypdf` + **poppler**
   (`pdftoppm`/`pdftotext`) are needed only for `deep_analyze`/`get_figure`/`jc_prep`.
3. **An agent CLI** on `PATH` — the LLM engine. Ships wired to **Claude Code**: every call
   is `claude -p --tools "" ` (NO `--bare`; that forces API-key mode and breaks
   OAuth/Team auth), prompts via **stdin**, never argv (so they don't leak in `ps`). A
   **Codex-based agent** works too — swap the `call_claude`/subprocess layer per
   [codex-chat-bridge](https://github.com/Lihan-Zhong/codex-chat-bridge).
4. **A Discord incoming webhook** (primary channel). Optionally a Telegram bot token.
5. Optional: Firecrawl (deep-follow-up fallback), a project chat bot for Q&A.

## Deployment for a new user / colleague

### 1. Copy templates into a project root
Copy the shared `scripts/` plus **one channel folder** — `discord` (default) or `telegram`:
```bash
PROJECT_ROOT=/PATH/TO/literatures        # pick a real path you own
CHANNEL=discord                          # or: telegram
mkdir -p "$PROJECT_ROOT"/{scripts,$CHANNEL,config,state,logs/slurm,digests,cache/tmp}
cp templates/scripts/*  "$PROJECT_ROOT/scripts/"
cp templates/$CHANNEL/* "$PROJECT_ROOT/$CHANNEL/"
cp templates/config/categories.txt templates/config/journals.json "$PROJECT_ROOT/config/"
cp templates/config/*.example "$PROJECT_ROOT/config/"   # research_focus / keywords / webhook / telegram examples
```
See `templates/$CHANNEL/SETUP.md` for the channel-specific push + Q&A-bot setup.

### 2. Fill the placeholders (paths + SLURM)
Every template uses obvious placeholders. Replace them for your environment:
```bash
cd "$PROJECT_ROOT"
# absolute paths (run/sbatch/cron wrappers live in the channel folder)
sed -i "s#/PATH/TO/literatures#$PROJECT_ROOT#g" "$CHANNEL"/*.sh "$CHANNEL"/*.sbatch
sed -i "s#/PATH/TO/miniconda3#$HOME/miniconda3#g" "$CHANNEL"/*.sh scripts/*.py   # your python/poppler base
# SLURM (in $CHANNEL/*.sbatch): a queue you have PRIORITY on
sed -i "s#YOUR_PARTITION#<your_partition>#; s#YOUR_ACCOUNT#<your_account>#" "$CHANNEL"/*.sbatch
# UA email (in scripts/fetch_*.py, scripts/deep_analyze.py): NCBI/Crossref polite-pool contact
sed -i "s#you@example.com#<your_email>#g" scripts/*.py
```
`$HOME/.local/bin/claude` and `python3` are assumed on PATH — adjust the `PY=` /
`CLAUDE=` lines in `$CHANNEL/run_*.sh` if yours differ.

### 3. Configure the channel (Discord webhook / Telegram token)
Full instructions are in `templates/$CHANNEL/SETUP.md`. For **Discord** (default):
```bash
# Discord → target channel → Edit Channel → Integrations → Webhooks → New → Copy URL
umask 077
printf '%s\n' 'https://discord.com/api/webhooks/XXXX/YYYY' > "$PROJECT_ROOT/state/discord_webhook.txt"
chmod 600 "$PROJECT_ROOT/state/discord_webhook.txt"
# verify WITHOUT posting, then preview cards, then send one for real:
discord/push_discord.py --healthcheck     # GET → {name, channel_id, guild_id}
discord/push_discord.py --dry-run < some_judged.jsonl
```
For **Telegram**: `cp config/telegram.env.example config/telegram.env`, paste the
BotFather token + chat id, `chmod 600`, then `telegram/push_telegram.py --no-send < …`.
The repo's [`.gitignore`](.gitignore) already excludes `state/discord_webhook.txt`,
`config/telegram.env`, `cache/`, `logs/`, `digests/`. **The webhook URL / bot token is a
credential — anyone with it can post as you. Never commit it.**

### 4. Define your research focus  ← THE key step
This is what the whole funnel filters for. **Nothing about the research topic is
hard-coded** — the scripts read it from two config files, so the same code works
for any field:
```bash
cp config/research_focus.example.txt config/research_focus.txt
cp config/keywords.example.txt       config/keywords.txt
```
- **`config/research_focus.txt`** — a few lines describing what you care about (a
  TECH/methods axis + a BIO/system axis works well). Injected verbatim into every
  LLM prompt (triage, judge, deep-read, JC prep).
- **`config/keywords.txt`** — the free Stage-1 pre-filter. Keep it BROAD (high
  recall); the LLM makes the precise call.
- `config/categories.txt` — bioRxiv category whitelist. `config/journals.json` —
  journals to scan (`{name, issn}`, PubMed ISSN).

**Recommended — let a Claude agent optimize both end-to-end.** Give it (a) a
paragraph about your research + (b) 3-5 must-catch example papers, and have it
draft `research_focus.txt` + `keywords.txt`, then dry-run
`fetch_biorxiv.py --days 7 | triage_titles.py --keep yes,maybe | llm_judge.py --keep-all`
on recent papers and tune both until precision/recall look right for your field.

Non-PubMed journal (OAI-PMH only, e.g. a new launch): point `fetch_vita.py`'s
`OAI_BASE` + DOI prefix at it (`<site>/oai?verb=Identify` finds the base); it's
already wired into `run_journals.sh`.

### 5. Wire cron
```crontab
0 8  * * *  $PROJECT_ROOT/discord/cron_submit_biorxiv.sh   # bioRxiv AM  (or telegram/)
0 20 * * *  $PROJECT_ROOT/discord/cron_submit_biorxiv.sh   # bioRxiv PM
0 20 * * 6  $PROJECT_ROOT/discord/cron_submit_journals.sh  # journals, Saturdays
```
`cron_submit_*.sh` sets PATH then `sbatch`es the `.sbatch` wrapper. Cron does NOT use
MCP — it submits to a compute node and pushes via HTTPS, independent of any session.

### 6. Smoke-test each stage before trusting cron
```bash
scripts/fetch_biorxiv.py --days 2 | tee /tmp/cand.jsonl        # Stage 1 (free)
scripts/triage_titles.py --keep yes,maybe < /tmp/cand.jsonl | tee /tmp/tri.jsonl  # Stage 2 (1 call)
scripts/llm_judge.py --batch 8 --keep-all < /tmp/tri.jsonl | tee /tmp/jud.jsonl   # Stage 3
discord/push_discord.py --dry-run < /tmp/jud.jsonl            # render, send nothing (telegram/push_telegram.py --no-send)
```

## Operational resilience — lessons learned (READ THIS)

These were all found the hard way in production. Every one produced a plausible-
looking result while silently degrading the feed.

### The dangerous class: a failure that looks like a quiet day
If a run pushes **"今日无相关文献" (0 papers)**, that can mean two very different things:
1. **Genuine quiet** — the LLM ran, judged papers, none scored ≥3. `quota_hit=0`, no ⚠️.
2. **A silent failure** — a stage crashed, produced 0 candidates, and shipped a
   fake-empty digest indistinguishable from (1).

**The fix (already in these templates): make every stage failure LOUD.**
- Push a **⚠️ banner** instead of a fake-empty digest.
- **Do not cache** the un-pushed papers (they're retried next cron — nothing lost).
- Log a distinct marker so you can grep the cause.

**How to tell them apart:** a real quiet day has `quota_hit=0` and NO ⚠️ banner; the
digest footer shows "摘要判读得 N" (N ≥ 0). A failure shows a ⚠️ banner.

### Failure taxonomy (all handled → ⚠️ banner, retried next cron)
| Symptom in `logs/<src>-<date>.log` | Cause | Fix already in templates |
|---|---|---|
| `claude exit 1`, empty stderr | weekly quota exhausted | triage/judge exit 42 on ANY non-zero claude exit (not just keyword match) → ⚠️ |
| `529 Overloaded` | transient Anthropic server overload | same exit-42 path; retries next cron |
| `OAuth session expired…` | stored OAuth expired | user runs `/login` to refresh, then re-run |
| `504 Gateway Timeout` / `socket.timeout` / `JSONDecodeError` + `candidates=0 scanned=0` | **bioRxiv API flaky** | `fetch_page` retries 5× (catches HTTPError/URLError/**OSError**/**ValueError** — the socket-timeout + JSON errors the naive catch missed); wrapper checks fetch exit code → ⚠️ banner |

### bioRxiv API 504s on BIG date windows — backfill in SMALL chunks
The details API times out on large ranges. A month-long backfill (e.g. `--days 33`)
can hit a 504-storm and crawl for hours; a 2-4 day window fetches in seconds. Use
targeted windows: `fetch_biorxiv.py --from YYYY-MM-DD --to YYYY-MM-DD` (wired as
`LIT_BIORXIV_FROM` / `LIT_BIORXIV_TO` in `run_biorxiv.sh`). The daily cron already
uses a small `--days 2` window and is fine.

### No duplicate pushes
Dedup is by `(doi, version)` in `cache/seen.sqlite`; already-pushed papers are
skipped ("skipped N already-seen"). Backfills with overlapping windows are safe. A
genuine **v2** of an already-pushed v1 re-pushes once (an update, not a dup).

### HPC scheduling — "submitted but no push" = stuck PENDING
If `logs/cron-*.log` shows "Submitted batch job N" but there's no run log and no
push, the job is stuck **PENDING** (partition saturated). Check `squeue -j N`.
Pick a partition you have **priority** on; the job is tiny (**1 CPU, 2 GB, 1 h**).
A SLURM association pins a single partition per account, so a queued job can't move
itself — instead set up **`config/partitions.txt`** and the cron submitter (and the
auto-retry) pick a partition with free capacity **at submit time**, falling back off a
saturated priority partition. See **[docs/hpc-partition-fallback.md](docs/hpc-partition-fallback.md)**.

### Cost / token discipline (non-negotiable)
- **Batch every LLM call**: title triage = 1 call for all titles; abstract judge =
  8 papers/call. This is where the 15× savings come from.
- `claude -p --tools ""` (never `--bare`); prompt via **stdin**.
- Only the ~3-8 papers that reach Stage 3 cost real tokens.

## Follow-up, deep analysis, figures
- **Ordinary follow-up** ("展开第 N 条") reads `state/latest_digest.json` — no refetch,
  no LLM. Support lookup by **title substring** too (the by-date digest is overwritten
  by the day's 2nd run; also archive per-run copies under `digests/runs/`).
- **Deep follow-up**: `deep_analyze.py --doi <DOI>` (or `--pdf` for a local file).
  bioRxiv full text: try JATS, then the **cookie-warmed PDF** (a fresh preprint has
  no full-text HTML — go straight to `<doi>vN.full.pdf`, honor `retry-after`, verify
  the `%PDF` magic), then Firecrawl. Feed full text to `claude -p`; 6-section 中文 review.
- **Figures**: `get_figure.py --doi <DOI> --fig 1` — locate the caption at line start
  (avoids in-text "…in Figure 1"), render the whole PAGE with `pdftoppm` (NOT
  pdfimages, which fragments multi-panel figures), attach the PNG. Handles Cell-Press
  PDFs (pypdf font bug) by falling back to poppler `pdftotext`.
