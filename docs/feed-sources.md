# Data sources & scanning strategies

Different literature sources expose their contents in very different ways, and each
has its own failure modes. Naively hitting them "just works" in a demo and then
silently rots your feed in production. This document records the fetch strategy and
the **hardening each source actually needed** — most of it learned the hard way — so
you can trust the scanner and add new sources safely.

Every fetcher, regardless of source, emits the **same candidate JSONL** so the rest of
the pipeline (triage → judge → push) never has to care where a paper came from.

---

## The universal contract: candidate JSONL

Each fetcher writes one JSON object per line to stdout. Downstream stages read this and
nothing else. The minimum useful record:

```jsonc
{
  "doi": "10.1101/2026.01.02.123456",   // dedup key (with "version"); "pmid:NNN" if no DOI
  "version": "1",                         // bioRxiv preprints revise; "1" for published papers
  "title": "…",
  "abstract": "…",                        // the LLM stages need this; backfill if missing
  "authors": "Last, F.; …",
  "date": "2026-01-02",
  "category": "Genomics",                 // bioRxiv subject / journal name / source tag
  "biorxiv_url": "https://doi.org/…",     // link fields (names kept for back-compat)
  "pdf_url": "…",
  "html_url": "…",
  "matched_keywords": ["single cell", …]  // which Stage-1 keywords hit (for debugging)
}
```

**Dedup is universal and keyed on `(doi, version)`** in `cache/seen.sqlite`. A source with
no DOI (some OAI records, PubMed-ahead-of-DOI) falls back to `pmid:<id>` or a stable
identifier. Getting the key right is what makes overlapping backfills safe and stops
duplicate pushes.

---

## Cross-cutting hardening (applies to every source)

These bit us on more than one source, so they belong here, once:

1. **Retry, and catch the *right* exceptions.** A single `except (HTTPError, URLError)`
   is the classic too-narrow catch — it misses the two errors that actually take feeds
   down:
   - **`socket.timeout` on read** is an **`OSError`**, *not* a `URLError`.
   - **A non-JSON error page** (a 5xx HTML body where you expected JSON) raises
     **`ValueError`** (`json.JSONDecodeError` subclasses it), *not* an HTTP error.

   So the retry wrapper catches `(HTTPError, URLError, OSError, ValueError)`, retries ~5×
   with capped backoff, and only raises once every attempt fails. See `fetch_page`
   (`fetch_biorxiv.py`), `http_get` (`fetch_journals.py`), `oai_get` (`fetch_vita.py`).

2. **Fail loud, never fake-empty.** If a fetch ultimately fails, the wrapper must push a
   ⚠️ banner and **not** cache anything — a crashed fetch that silently emits 0 candidates
   is indistinguishable from a genuinely quiet day, which is the single most dangerous bug
   for a literature bot. (See the *Operational resilience* section of `SKILL.md`.)

3. **Keyword pre-filter is recall-first.** Stage 1 keyword matching is a cheap gate before
   any LLM call — keep it **broad**. Precision is the LLM's job in Stages 2–3. Matching is
   word-boundary aware (`[\s\-_]` normalized), so `chip-seq` matches `chip seq` / `chip_seq`
   but not `microchip sequencing`.

4. **Dedup *before* spending on enrichment.** Any per-item API call (abstract backfill,
   full-text) goes **after** the cache check, so you never pay to enrich a paper you've
   already seen.

---

## Source 1 — bioRxiv (details API)

**Endpoint:** `https://api.biorxiv.org/details/{server}/{from}/{to}/{cursor}`
(`server` = `biorxiv` or `medrxiv`), paginated by `cursor` in the URL path.

**Strategy:** page through a date window, keep only whitelisted `category` values
(`config/categories.txt`), then keyword pre-filter. Preprints revise, so `version` is part
of the dedup key — a **v2 of an already-pushed v1 re-pushes once** (an update, not a dup).

**Gotchas & optimizations learned the hard way:**

- **The details API 504s on *big* date windows.** A month-long backfill (`--days 33`) hits
  a 504-storm and can crawl for hours; a 2–4 day window returns in seconds. So the daily
  cron uses a small `--days 2` window, and **backfills use `--from/--to YYYY-MM-DD` in
  small chunks**, not one giant range.
- **Transient errors are the norm, not the exception.** 504 Gateway Timeout, read
  timeouts, and non-JSON error pages all occur regularly — hence the 5× retry catching
  `OSError`/`ValueError` (see cross-cutting #1). Before that retry existed, a single 504
  crashed the whole run into a fake-empty digest.
- **Full text for deep-reads is a separate problem.** A fresh preprint often has no
  full-text HTML; the reliable path is the cookie-warmed `…vN.full.pdf` (warm the
  `__cf_bm` cookie, browser UA, honor `Retry-After`, verify the `%PDF` magic). The JATS
  XML host is aggressively rate-limited from shared IPs. See `deep_analyze.py`.

---

## Source 2 — journals via NCBI E-utilities (PubMed by ISSN)

**Endpoints:** `esearch.fcgi` (ISSN + entrez-date range → PMIDs) then `efetch.fcgi`
(≤200 PMIDs/batch → XML with title, abstract, authors, publication types).
Journals configured in `config/journals.json` as `{name, issn}`.

**Gotchas & optimizations:**

- **`retmax` silently truncates.** `esearch` defaults to a small `retmax`; a busy week at a
  high-volume journal (Nature, Nature Communications) can exceed it and you'd never know.
  Fix: raise it (here `500`) **and** compare `esearchresult.count` to the number pulled —
  warn when `count > len(idlist)` so truncation is visible, not silent.
- **~35% of a journal feed is not research.** Corrections, errata, editorials, News &
  Views, comments, retractions. Drop them by PubMed `PublicationType` **and** by a
  title-prefix regex — because some publishers (Nature) tag an "Author Correction:" only
  as a plain "Journal Article", so the type list alone misses it. On a real window this
  dropped 143/411 items. Reviews and research articles pass.
- **Abstracts arrive late — backfill before filtering.** A paper can be PubMed-indexed
  *before* its abstract is attached. Since the keyword gate is pre-LLM, such a paper would
  be matched on its **title alone** and likely dropped. So for records still missing an
  abstract after dedup, backfill **Crossref → Europe PMC** by DOI (free, no key) *before*
  the keyword filter. In practice this fires rarely (the type filter already removed the
  abstract-less news/corrections), which is exactly right — it only rescues genuine research.
- **Coverage visibility.** Track a per-journal consecutive-empty streak in
  `state/journal_coverage.json`; a journal empty **≥ 2 runs in a row** raises a warning
  that surfaces as a ⚠️ line in the digest header. Threshold 2 (not 1) avoids quiet-week
  noise. This catches a wrong ISSN or a feed that quietly died — distinct from a fetch
  *error* (tracked as `-1`, already loud).
- **Corresponding-author extraction.** PubMed marks corresponding intent by an email in the
  `Affiliation` text. Many CNS papers have 4–8 co-corresponding authors clustered at the end
  of the list, so collect **all** email-bearing authors (not just the last), and fall back to
  the last author when no affiliation carries an email.

---

## Source 3 — non-PubMed journals via OAI-PMH

**When you need it:** a journal too new (or otherwise absent) for PubMed/Europe PMC, so the
ISSN scanner can't see it, and Crossref has the records but **no abstracts**. Many journal
platforms expose **OAI-PMH**, and its Dublin Core often includes the abstract in
`dc:description` — cleaner than scraping the site. `fetch_vita.py` is a working template.

**Endpoint:** `{base}/oai?verb=ListRecords&metadataPrefix=oai_dc`, paginated by
`resumptionToken`. Find the base with `{base}/oai?verb=Identify`.

**Gotchas & optimizations:**

- **`datestamp` is the record *load* date, not the article pub date**, so date-range
  harvesting ("new since X") is unreliable. For a small journal the robust pattern is
  **harvest ALL records every run and let the SQLite cache dedup by DOI** skip everything
  already seen. (Cap `resumptionToken` pages so a broken feed can't loop forever.)
- **`dc:description` is XML-escaped HTML.** It arrives as `&lt;p&gt;…&lt;/p&gt;`, so you must
  **unescape first, *then* strip tags, then unescape again** for any leftover entities.
  When a record has multiple `dc:description`, the **longest** is the abstract (the others
  are running titles / highlights).
- **Be non-fatal.** A single new source failing shouldn't sink the weekly run — log and
  emit nothing, so the rest of the journals still push. (`fetch_vita.py` returns 0 on any
  harvest error and is appended to the journals run with `|| true`.)
- **Retire it when possible.** Once the journal gets PubMed-indexed, delete the OAI fetcher
  and just add its ISSN to `config/journals.json` — it then flows through Source 2.

---

## Source 4 — RSS / Atom (a pattern, not yet shipped)

Many journals and preprint servers publish an RSS/Atom feed of recent items. It's the
easiest source to *start* with and the easiest to get subtly wrong:

- **Feeds are title + link, often with a thin or absent abstract.** Treat RSS as a source of
  DOIs/links, then **backfill the abstract by DOI** via Crossref → Europe PMC (reuse
  `backfill_abstract` from `fetch_journals.py`) before the keyword filter — same reasoning as
  Source 2.
- **Empty / entity-mangled titles.** Some feeds ship items with an empty `<title>` or with
  double-escaped entities. Guard against empty titles (don't let one poison a batch) and run
  the same unescape-then-strip cleaning as OAI.
- **Dedup by a stable key.** Use the DOI when the feed carries one; otherwise the entry
  `<guid>`/`<id>`. Feeds re-list the same item as it's updated, so without this you re-push.
- **Poll politely and expect flakiness.** Same 5× retry / `OSError`+`ValueError` catch as
  everywhere else; set a real `User-Agent` with a contact.

To add it: write `fetch_rss.py` that parses the feed, emits the candidate JSONL above, and
append it to `run_journals.sh` the way `fetch_vita.py` already is.

---

## Adding a new source — checklist

1. **Pick the cleanest structured endpoint** the source offers: API > OAI-PMH > RSS >
   scraping. Prefer one that includes abstracts.
2. **Write `fetch_<source>.py`** that emits the candidate JSONL contract above. Reuse
   `load_keywords` / `keyword_match` / `init_cache` from `fetch_journals.py`.
3. **Choose the dedup key**: real DOI if available, else a stable id as `pmid:`/`guid:`.
4. **Enrich after dedup**: backfill missing abstracts by DOI before the keyword gate.
5. **Harden the fetch**: 5× retry catching `(HTTPError, URLError, OSError, ValueError)`;
   be non-fatal so one bad source doesn't sink the run.
6. **Wire it in**: append its output to `$CAND` in `run_journals.sh` (`>> "$CAND" || true`).
   It now flows through the shared triage → judge → push with no other changes.
7. **Make quiet failure visible**: if the source can legitimately be empty, add it to the
   coverage tracker so a *persistently* empty source raises a warning.
