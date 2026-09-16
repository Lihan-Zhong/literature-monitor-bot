#!/usr/bin/env python3
"""
Fetch Vita articles via the journal's OAI-PMH endpoint and emit candidate JSONL
(same schema as fetch_journals.py) for the shared triage → judge → push pipeline.

Why a separate fetcher: Vita (China-led life-sciences journal, ISSN 2097-7468,
DOI prefix 10.15302, launched 2026) is NOT in PubMed or Europe PMC yet, so the
NCBI-by-ISSN scanner (fetch_journals.py) can't see it. Its platform DOES expose
OAI-PMH with Dublin Core INCLUDING the abstract (`dc:description`) — cleaner and
more reliable than scraping the site.

The journal is small, and OAI `datestamp` is the record LOAD date (not the
article pub date), so date-range harvesting is unreliable for "new since". We
therefore harvest ALL records each run and let the existing SQLite cache dedup
(by DOI) skip everything already seen. Keyword pre-filter matches fetch_journals.

When Vita eventually gets PubMed/MEDLINE-indexed, drop this and just add its
ISSN to config/journals.json — then it flows through the normal scanner.
"""
from __future__ import annotations

import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_journals as fj  # reuse load_keywords / keyword_match / init_cache / paths

OAI_BASE = "https://www.vita-journal.com/vita/oai"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DOI_RE = re.compile(r"10\.15302/[^\s\"'<>]+")
MAX_PAGES = 50  # resumptionToken safety cap


def oai_get(params: dict) -> str:
    url = OAI_BASE + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": UA})
    for attempt in range(1, 6):
        try:
            with urlopen(req, timeout=90) as r:
                return r.read().decode("utf-8", "replace")
        except (HTTPError, URLError, OSError) as e:  # OSError covers socket.timeout
            wait = min(5 * attempt, 30)
            print(f"[fetch_vita] OAI attempt {attempt}/5 failed: "
                  f"{type(e).__name__}: {str(e)[:100]}", file=sys.stderr)
            if attempt < 5:
                time.sleep(wait)
    raise RuntimeError("Vita OAI request failed after 5 attempts")


def harvest_records() -> list[str]:
    """ListRecords (oai_dc) with resumptionToken pagination → raw <record> XML."""
    records: list[str] = []
    params = {"verb": "ListRecords", "metadataPrefix": "oai_dc"}
    for _ in range(MAX_PAGES):
        xml = oai_get(params)
        err = re.search(r'<error[^>]*code="([^"]+)"', xml)
        if err:
            if err.group(1) != "noRecordsMatch":
                print(f"[fetch_vita] OAI error: {err.group(1)}", file=sys.stderr)
            break
        records += re.findall(r"<record>.*?</record>", xml, re.S)
        m = re.search(r"<resumptionToken[^>]*>([^<]+)</resumptionToken>", xml)
        if not m or not m.group(1).strip():
            break
        params = {"verb": "ListRecords", "resumptionToken": m.group(1).strip()}
        time.sleep(0.5)
    return records


def _field(rec: str, tag: str) -> list[str]:
    return re.findall(rf"<dc:{tag}[^>]*>(.*?)</dc:{tag}>", rec, re.S)


def _clean(s: str) -> str:
    """Vita's dc fields carry XML-escaped HTML (e.g. '&lt;p&gt;'), so unescape
    FIRST, then strip tags, then unescape any remaining entities."""
    s = unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_record(rec: str) -> dict:
    titles = _field(rec, "title")
    descs = _field(rec, "description")
    dates = _field(rec, "date")
    doi = ""
    for ident in _field(rec, "identifier"):
        m = DOI_RE.search(ident)
        if m:
            doi = m.group(0)
            break
    return {
        "doi": doi,
        "title": _clean(titles[0]) if titles else "",
        # some records carry multiple dc:description; the longest is the abstract
        "abstract": _clean(max(descs, key=len)) if descs else "",
        "date": (_clean(dates[0])[:10] if dates else ""),
        "authors": "; ".join(_clean(a) for a in _field(rec, "creator")),
    }


def main() -> int:
    keywords = fj.load_keywords(fj.KEYWORDS_FILE)
    try:
        recs = harvest_records()
    except Exception as e:
        # Non-fatal for the weekly run: log and emit nothing.
        print(f"[fetch_vita] harvest failed ({e}); emitting no candidates",
              file=sys.stderr)
        return 0
    print(f"[fetch_vita] harvested {len(recs)} OAI records", file=sys.stderr)

    con = fj.init_cache(fj.CACHE_DB)
    n_seen = n_nodoi = n_noabs = n_emit = 0
    for rec in recs:
        p = parse_record(rec)
        if not p["doi"]:
            n_nodoi += 1
            continue
        if con.execute("SELECT 1 FROM seen WHERE doi=? AND version=?",
                       (p["doi"], "1")).fetchone():
            n_seen += 1
            continue
        if not p["abstract"]:
            n_noabs += 1  # commentary/highlight with no abstract → title-only match
        matched = fj.keyword_match(f"{p['title']}\n{p['abstract']}", keywords)
        if not matched:
            continue
        url = f"https://doi.org/{p['doi']}"
        print(json.dumps({
            "doi": p["doi"],
            "version": "1",
            "title": p["title"],
            "authors": p["authors"],
            "corresponding": "",
            "corresponding_institution": "",
            "abstract": p["abstract"],
            "date": p["date"],
            "category": "Vita",
            "biorxiv_url": url,       # field name reused for downstream compat
            "pdf_url": url,
            "html_url": url,
            "matched_keywords": matched,
            "_journal": "Vita",
        }, ensure_ascii=False))
        n_emit += 1

    print(f"[fetch_vita] {n_emit} candidates (skipped {n_seen} seen, "
          f"{n_nodoi} no-DOI, {n_noabs} without abstract) source=Vita OAI-PMH",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
