#!/usr/bin/env python3
"""
Fetch new bioRxiv preprints, dedupe against cache, apply keyword pre-filter,
emit candidate papers as JSONL on stdout.

Usage:
    fetch_biorxiv.py [--days N] [--server biorxiv|medrxiv]

Output: one JSON object per line with fields:
    doi, version, title, authors, abstract, date, category,
    biorxiv_url, pdf_url, html_url, matched_keywords
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
KEYWORDS_FILE = ROOT / "config" / "keywords.txt"
CATEGORIES_FILE = ROOT / "config" / "categories.txt"
CACHE_DB = ROOT / "cache" / "seen.sqlite"
USER_AGENT = "lit-bot/1.0 (you@example.com)"


def load_lines(path: Path) -> list[str]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.lower())
    return out


# Backwards-compat alias.
load_keywords = load_lines


def init_cache(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            doi      TEXT NOT NULL,
            version  TEXT NOT NULL,
            seen_at  TEXT NOT NULL,
            relevant INTEGER,
            score    REAL,
            PRIMARY KEY (doi, version)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_doi ON seen(doi)")
    con.commit()
    return con


def fetch_page(server: str, frm: str, to: str, cursor: int, max_retries: int = 5) -> dict:
    """Fetch one page from api.biorxiv.org with robust retries. The API
    intermittently returns 504 Gateway Timeout, times out on read, or serves a
    non-JSON error page (JSONDecodeError) — ALL transient. Retry with backoff and
    only raise once every attempt fails. (Catching OSError covers socket.timeout;
    ValueError covers json.JSONDecodeError.)"""
    url = f"https://api.biorxiv.org/details/{server}/{frm}/{to}/{cursor}"
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=90) as r:
                raw = r.read()
            return json.loads(raw)
        except (HTTPError, URLError, OSError, ValueError) as e:
            last_err = e
            wait = min(8 * attempt, 40)
            print(f"[fetch_biorxiv] page cursor={cursor} attempt {attempt}/{max_retries} "
                  f"failed: {type(e).__name__}: {str(e)[:120]}; "
                  f"{'retry in %ds' % wait if attempt < max_retries else 'giving up'}",
                  file=sys.stderr)
            if attempt < max_retries:
                time.sleep(wait)
    raise RuntimeError(
        f"biorxiv API fetch failed after {max_retries} attempts (cursor={cursor}): "
        f"{type(last_err).__name__}: {last_err}")


def fetch_all(server: str, frm: str, to: str) -> list[dict]:
    papers = []
    cursor = 0
    while True:
        data = fetch_page(server, frm, to, cursor)  # retries internally; raises if all fail
        msg = data.get("messages", [{}])[0]
        batch = data.get("collection", [])
        papers.extend(batch)
        total = int(msg.get("total", 0))
        cursor += len(batch)
        if cursor >= total or not batch:
            break
        time.sleep(0.3)
    return papers


def keyword_match(text: str, keywords: list[str]) -> list[str]:
    """Word-boundary match (case-insensitive). Hyphens / underscores between
    word chars are treated as spaces so 'ChIP-seq' matches 'ChIP seq' and
    'ChIP_seq' but does NOT match 'microchip sequencing' (no boundary on left)."""
    text_norm = re.sub(r"[\s\-_]+", " ", text.lower())
    matched = []
    for kw in keywords:
        kw_norm = re.sub(r"[\s\-_]+", " ", kw.lower())
        # \b only matches at word-char ↔ non-word-char transitions, so
        # "chip seq" inside "microchip sequencing" won't match.
        pattern = r"(?<![A-Za-z0-9])" + re.escape(kw_norm) + r"(?![A-Za-z0-9])"
        if re.search(pattern, text_norm):
            matched.append(kw)
    return matched


def latest_versions(papers: list[dict]) -> list[dict]:
    """Keep only the highest-version record for each DOI."""
    by_doi: dict[str, dict] = {}
    for p in papers:
        doi = p.get("doi", "")
        if not doi:
            continue
        v = int(p.get("version", "1"))
        if doi not in by_doi or v > int(by_doi[doi].get("version", "1")):
            by_doi[doi] = p
    return list(by_doi.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2,
                    help="Lookback window in days (default 2; cache handles dedup)")
    ap.add_argument("--from", dest="frm", default="",
                    help="explicit start date YYYY-MM-DD (overrides --days). Use "
                         "SMALL windows for backfills — the details API 504s on "
                         "big date ranges; a few-day window is fast and reliable.")
    ap.add_argument("--to", dest="to", default="",
                    help="explicit end date YYYY-MM-DD (default: today)")
    ap.add_argument("--server", default="biorxiv", choices=["biorxiv", "medrxiv"])
    args = ap.parse_args()

    keywords = load_lines(KEYWORDS_FILE)
    categories = set(load_lines(CATEGORIES_FILE)) if CATEGORIES_FILE.exists() else set()
    if not keywords:
        print("[fetch_biorxiv] WARNING: no keywords loaded", file=sys.stderr)
    if not categories:
        print("[fetch_biorxiv] WARNING: no category whitelist; allowing all",
              file=sys.stderr)

    today = date.today()
    frm = args.frm or (today - timedelta(days=args.days)).isoformat()
    to = args.to or today.isoformat()
    print(f"[fetch_biorxiv] querying {args.server} {frm} -> {to}", file=sys.stderr)

    try:
        papers = fetch_all(args.server, frm, to)
    except Exception as e:
        # Total fetch failure (API down after all retries). Exit 17 with a
        # distinct marker so run_biorxiv.sh flags it as a FETCH failure and
        # pushes a ⚠️ banner instead of a fake-empty "no papers" digest.
        print(f"__FETCH_FAILED__ {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        sys.exit(17)
    print(f"[fetch_biorxiv] received {len(papers)} records", file=sys.stderr)

    papers = latest_versions(papers)
    print(f"[fetch_biorxiv] after version dedup: {len(papers)} unique DOIs",
          file=sys.stderr)

    # Stage 1a: category whitelist (free, no LLM).
    if categories:
        before = len(papers)
        papers = [p for p in papers
                  if p.get("category", "").strip().lower() in categories]
        print(f"[fetch_biorxiv] after category whitelist: "
              f"{len(papers)} (dropped {before - len(papers)})", file=sys.stderr)

    con = init_cache(CACHE_DB)
    new_candidates = []
    n_already_seen = 0
    for p in papers:
        doi = p.get("doi", "")
        version = str(p.get("version", "1"))
        title = p.get("title", "")
        abstract = p.get("abstract", "")
        cur = con.execute("SELECT 1 FROM seen WHERE doi=? AND version=?",
                          (doi, version)).fetchone()
        if cur:
            n_already_seen += 1
            continue
        matched = keyword_match(f"{title}\n{abstract}", keywords)
        if not matched:
            continue
        biorxiv_url = f"https://www.biorxiv.org/content/{doi}v{version}"
        pdf_url = biorxiv_url + ".full.pdf"
        html_url = biorxiv_url + ".full"
        new_candidates.append({
            "doi": doi,
            "version": version,
            "title": title,
            "authors": p.get("authors", ""),
            "corresponding": p.get("author_corresponding", "").strip(),
            "corresponding_institution": p.get("author_corresponding_institution", "").strip(),
            "abstract": abstract,
            "date": p.get("date", ""),
            "category": p.get("category", ""),
            "biorxiv_url": biorxiv_url,
            "pdf_url": pdf_url,
            "html_url": html_url,
            "matched_keywords": matched,
        })

    print(f"[fetch_biorxiv] {len(new_candidates)} candidates after keyword pre-filter "
          f"(skipped {n_already_seen} already-seen)", file=sys.stderr)

    for c in new_candidates:
        print(json.dumps(c, ensure_ascii=False))

    # Stash scanned counts for the digest header
    stats = {"scanned": len(papers), "candidates": len(new_candidates),
             "already_seen": n_already_seen,
             "from": frm, "to": to, "server": args.server}
    print(f"[fetch_biorxiv] stats={json.dumps(stats)}", file=sys.stderr)


if __name__ == "__main__":
    main()
