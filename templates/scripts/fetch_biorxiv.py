#!/usr/bin/env python3
"""
Fetch new bioRxiv preprints, dedupe against cache, apply keyword pre-filter,
emit candidate papers as JSONL on stdout.

Source = **Crossref** (bioRxiv's DOI registrar). The old api.biorxiv.org JSON
API broke in bioRxiv's 2026-09 site redesign — it now returns HTTP 200 with an
empty body for every query — and bioRxiv moved new preprints from DOI prefix
10.1101 to **10.64898**. Crossref exposes DOI, title, abstract, category
(group-title), posted date and authors for the new prefix, and is far more
reliable, so we query it instead. The record shape below is unchanged, so the
downstream triage/judge/push stages are untouched.

Usage:
    fetch_biorxiv.py [--days N] [--from YYYY-MM-DD --to YYYY-MM-DD]

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
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
KEYWORDS_FILE = ROOT / "config" / "keywords.txt"
CATEGORIES_FILE = ROOT / "config" / "categories.txt"
CACHE_DB = ROOT / "cache" / "seen.sqlite"

CONTACT_EMAIL = "you@example.com"                   # Crossref polite-pool contact
USER_AGENT = f"lit-bot/1.0 (mailto:{CONTACT_EMAIL})"

# bioRxiv's DOI prefix since the 2026-09 migration (older preprints used 10.1101,
# which no longer receives new posts). Crossref prefix 10.64898 also carries
# medRxiv preprints, but the category whitelist (config/categories.txt) keeps only
# the bioRxiv subjects, so mixing is harmless.
BIORXIV_PREFIX = "10.64898"
CROSSREF_WORKS = "https://api.crossref.org/prefixes/{prefix}/works"


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


# ---------------------------------------------------------------------------
# Crossref source
# ---------------------------------------------------------------------------
def _strip_jats(text: str) -> str:
    """Crossref abstracts are JATS-XML wrapped (<jats:title>Abstract</jats:title>
    <jats:p>…</jats:p>). Strip tags, unescape entities, drop a leading 'Abstract'."""
    if not text:
        return ""
    text = re.sub(r"</?jats:[^>]*>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"^\s*abstract\b[:.]?\s*", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _fmt_author(a: dict) -> str:
    """Crossref author {family, given} → 'Family, G.' (matches the old API's
    author-string style so the downstream author formatter is unchanged)."""
    fam = (a.get("family") or "").strip()
    giv = (a.get("given") or "").strip()
    if not fam:
        return (a.get("name") or "").strip()
    initials = "".join(p[0] for p in re.split(r"[\s\-]+", giv) if p)
    return (f"{fam}, {initials}".strip().rstrip(",")).strip()


def _crossref_get(prefix: str, frm: str, to: str, cursor: str,
                  max_retries: int = 5) -> dict:
    """One Crossref page with robust retries (transient HTTP/network/JSON errors).
    OSError covers socket.timeout; ValueError covers json.JSONDecodeError."""
    params = {
        "filter": f"from-posted-date:{frm},until-posted-date:{to}",
        "rows": "1000",
        "cursor": cursor,
        "select": "DOI,title,abstract,group-title,posted,author",
        "mailto": CONTACT_EMAIL,
    }
    url = CROSSREF_WORKS.format(prefix=prefix) + "?" + urlencode(params)
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        except (HTTPError, URLError, OSError, ValueError) as e:
            last_err = e
            wait = min(8 * attempt, 40)
            print(f"[fetch_biorxiv] Crossref attempt {attempt}/{max_retries} failed: "
                  f"{type(e).__name__}: {str(e)[:120]}; "
                  f"{'retry in %ds' % wait if attempt < max_retries else 'giving up'}",
                  file=sys.stderr)
            if attempt < max_retries:
                time.sleep(wait)
    raise RuntimeError(
        f"Crossref fetch failed after {max_retries} attempts: "
        f"{type(last_err).__name__}: {last_err}")


def fetch_all(server: str, frm: str, to: str) -> list[dict]:
    """All bioRxiv preprints posted in [frm, to], from Crossref, as internal
    records (doi/version/title/abstract/date/category/authors) — same shape the
    old api.biorxiv.org path produced, so main() below is unchanged."""
    out: list[dict] = []
    cursor = "*"
    while True:
        data = _crossref_get(BIORXIV_PREFIX, frm, to, cursor)
        msg = data.get("message", {})
        items = msg.get("items", [])
        if not items:
            break
        for it in items:
            doi = (it.get("DOI") or "").strip().lower()
            if not doi:
                continue
            title = " ".join(t for t in (it.get("title") or []) if t).strip()
            dp = (it.get("posted") or {}).get("date-parts") or [[]]
            parts = dp[0] if dp and dp[0] else []
            if len(parts) >= 3:
                date_str = f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
            elif parts:
                date_str = "-".join(str(p) for p in parts)
            else:
                date_str = ""
            authors = "; ".join(
                _fmt_author(a) for a in (it.get("author") or [])
                if (a.get("family") or a.get("name")))
            out.append({
                "doi": doi,
                "version": "1",  # Crossref exposes the base DOI; versions rare
                "title": title,
                "abstract": _strip_jats(it.get("abstract") or ""),
                "date": date_str,
                "category": (it.get("group-title") or "").strip(),
                "authors": authors,
                "author_corresponding": "",
                "author_corresponding_institution": "",
            })
        cursor = msg.get("next-cursor")
        if not cursor:
            break
        time.sleep(0.5)  # polite to Crossref
    return out


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
                    help="explicit start date YYYY-MM-DD (overrides --days)")
    ap.add_argument("--to", dest="to", default="",
                    help="explicit end date YYYY-MM-DD (default: today)")
    ap.add_argument("--server", default="biorxiv", choices=["biorxiv", "medrxiv"],
                    help="label only; Crossref prefix 10.64898 covers bioRxiv "
                         "(medRxiv rows are dropped by the category whitelist)")
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
    # NB: keep this log line's "querying biorxiv <from> -> <to>" shape — run_biorxiv.sh
    # greps it to extract the window shown in the digest header.
    print(f"[fetch_biorxiv] querying biorxiv {frm} -> {to} (via Crossref {BIORXIV_PREFIX})",
          file=sys.stderr)

    try:
        papers = fetch_all(args.server, frm, to)
    except Exception as e:
        # Total fetch failure (Crossref down after all retries). Exit 17 with a
        # distinct marker so run_biorxiv.sh flags it as a FETCH failure and pushes
        # a ⚠️ banner (+ schedules the auto-retry) instead of a fake-empty digest.
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
