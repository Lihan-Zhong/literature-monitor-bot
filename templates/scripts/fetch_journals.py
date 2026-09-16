#!/usr/bin/env python3
"""
Fetch recent papers from a configured set of high-impact journals via NCBI
E-utilities, dedupe against the SQLite cache, apply the same keyword
pre-filter as fetch_biorxiv.py, emit candidate JSONL on stdout.

Journals come from config/journals.json (the journals in config/journals.json — Cell, Nature,
Science, NBT, Nat Methods, Cell Genomics, Nat Commun, etc.). Each is queried
by ISSN and EDAT (entrez date) range.

Output schema matches fetch_biorxiv.py so downstream stages (triage_titles,
llm_judge, push_telegram) work unchanged.
"""
import argparse
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
KEYWORDS_FILE = ROOT / "config" / "keywords.txt"
JOURNALS_FILE = ROOT / "config" / "journals.json"
CACHE_DB = ROOT / "cache" / "seen.sqlite"
STATE_DIR = ROOT / "state"
CONTACT = "you@example.com"
USER_AGENT = f"lit-bot/1.0 (mailto:{CONTACT})"

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
CROSSREF = "https://api.crossref.org/works/"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# PublicationTypes that are noise for a research digest (feed-audit mode 5):
# corrections, editorials, news, comments, retractions, etc.
DROP_PUBTYPES = {
    "published erratum", "erratum", "correction", "corrigendum",
    "retraction of publication", "retracted publication", "comment",
    "editorial", "news", "newspaper article", "biography", "portrait",
    "interview", "historical article", "congress", "expression of concern",
}
# Some publishers (Nature) tag corrections only as "Journal Article", so also
# catch them by title prefix.
CORRECTION_TITLE_RE = re.compile(
    r"^\s*(author correction|publisher correction|correction|corrigendum|"
    r"erratum|retraction|editorial expression of concern)\b\s*[:.]", re.I)
# retmax: raised 200→500 so a busy week at a high-volume journal (Nature,
# Nature Communications) is not silently truncated (feed-audit mode: retmax cap).
ESEARCH_RETMAX = 500


def load_keywords(path: Path) -> list[str]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.lower())
    return out


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
    return con


def http_get(url: str, params: dict, timeout: int = 90) -> bytes:
    """GET with robust retries. Catches OSError too (covers socket.timeout read
    timeouts — the failure class that can silently sink a fetch if only
    (HTTPError, URLError) are caught). 5 attempts, capped backoff."""
    full = url + "?" + urlencode(params)
    last_err = None
    for attempt in range(1, 6):
        try:
            req = Request(full, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=timeout) as r:
                return r.read()
        except (HTTPError, URLError, OSError) as e:
            last_err = e
            wait = min(5 * attempt, 30)
            print(f"[fetch_journals] {url} attempt {attempt}/5 failed: "
                  f"{type(e).__name__}: {str(e)[:100]}; "
                  f"{'retry in %ds' % wait if attempt < 5 else 'giving up'}",
                  file=sys.stderr)
            if attempt < 5:
                time.sleep(wait)
    raise RuntimeError(f"NCBI request failed after 5 attempts: {url} "
                       f"({type(last_err).__name__}: {last_err})")


def keyword_match(text: str, keywords: list[str]) -> list[str]:
    text_norm = re.sub(r"[\s\-_]+", " ", text.lower())
    matched = []
    for kw in keywords:
        kw_norm = re.sub(r"[\s\-_]+", " ", kw.lower())
        pattern = r"(?<![A-Za-z0-9])" + re.escape(kw_norm) + r"(?![A-Za-z0-9])"
        if re.search(pattern, text_norm):
            matched.append(kw)
    return matched


def esearch_pmids(issn: str, frm: str, to: str) -> list[str]:
    """Find PMIDs published in [from, to] for a given journal ISSN.
    Date format YYYY/MM/DD."""
    term = f'"{issn}"[ISSN] AND "{frm}"[EDAT] : "{to}"[EDAT]'
    data = http_get(ESEARCH, {
        "db": "pubmed", "term": term,
        "retmode": "json", "retmax": str(ESEARCH_RETMAX),
    })
    j = json.loads(data)
    res = j.get("esearchresult", {})
    ids = list(res.get("idlist", []))
    # If the true count exceeds what we pulled, we truncated — surface it.
    try:
        total = int(res.get("count", len(ids)))
        if total > len(ids):
            print(f"[fetch_journals] WARNING: {issn} has {total} results but "
                  f"only pulled {len(ids)} (raise ESEARCH_RETMAX)", file=sys.stderr)
    except (TypeError, ValueError):
        pass
    return ids


def efetch_articles(pmids: list[str]) -> list[dict]:
    """Fetch metadata + abstract for a batch of PMIDs. Returns list of dicts
    with keys: pmid, doi, title, abstract, authors, journal, year, month, day."""
    if not pmids:
        return []
    out = []
    # NCBI recommends ≤200 PMIDs per efetch.
    for chunk_start in range(0, len(pmids), 200):
        chunk = pmids[chunk_start:chunk_start + 200]
        data = http_get(EFETCH, {
            "db": "pubmed", "id": ",".join(chunk),
            "rettype": "abstract", "retmode": "xml",
        }, timeout=120)
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            print(f"[fetch_journals] XML parse failed for chunk: {e}",
                  file=sys.stderr)
            continue
        for art in root.findall(".//PubmedArticle"):
            try:
                rec = parse_article(art)
                if rec:
                    out.append(rec)
            except Exception as e:
                print(f"[fetch_journals] failed to parse one article: {e}",
                      file=sys.stderr)
        time.sleep(0.4)  # NCBI courtesy
    return out


def parse_article(art: ET.Element) -> dict:
    pmid_el = art.find(".//PMID")
    pmid = pmid_el.text if pmid_el is not None else ""

    title_el = art.find(".//ArticleTitle")
    title = "".join(title_el.itertext()).strip() if title_el is not None else ""

    # Abstract may have multiple AbstractText sections.
    abstract_parts = []
    for ab in art.findall(".//Abstract/AbstractText"):
        label = ab.attrib.get("Label")
        body = "".join(ab.itertext()).strip()
        if label:
            abstract_parts.append(f"{label}: {body}")
        else:
            abstract_parts.append(body)
    abstract = "\n\n".join(abstract_parts)

    # DOI
    doi = ""
    for art_id in art.findall(".//ArticleId"):
        if art_id.attrib.get("IdType", "").lower() == "doi":
            doi = (art_id.text or "").strip()
            break

    # Publication types (used to drop corrections/editorials/news).
    pub_types = [
        (pt.text or "").strip().lower()
        for pt in art.findall(".//PublicationTypeList/PublicationType")
    ]

    # Journal title
    journal = ""
    j_el = art.find(".//Journal/Title")
    if j_el is not None:
        journal = j_el.text or ""

    # Pub date
    y = m = d = ""
    pubd = art.find(".//Journal/JournalIssue/PubDate")
    if pubd is not None:
        y_el = pubd.find("Year")
        m_el = pubd.find("Month")
        d_el = pubd.find("Day")
        if y_el is not None: y = y_el.text or ""
        if m_el is not None: m = m_el.text or ""
        if d_el is not None: d = d_el.text or ""

    # Authors → "LastName, F.; ..." and find ALL corresponding authors.
    # PubMed marks corresponding-author intent by including an email address
    # in their Affiliation text. Many CNS papers have 4-8 co-corresponding
    # authors all clustered at the END of the list, so we collect them all
    # (joined by '; ') and let the downstream formatter use that.
    authors = []
    corr_names = []
    corr_institutions = []
    last_full_name = ""
    last_full_inst = ""
    for au in art.findall(".//AuthorList/Author"):
        last = au.findtext("LastName") or ""
        fore = au.findtext("ForeName") or ""
        if not last:
            continue
        initials = "".join(p[0] for p in fore.split() if p) if fore else ""
        full_short = f"{last}, {initials}".strip().rstrip(",").strip()
        authors.append(full_short)
        full_long = (f"{fore} {last}".strip() if fore else last)
        last_full_name = full_long
        # Scan ALL affiliations for this author (some have 2-3).
        has_email = False
        primary_aff = ""
        for aff_el in au.findall(".//Affiliation"):
            aff_text = (aff_el.text or "").strip()
            if not primary_aff:
                primary_aff = aff_text
            if "@" in aff_text:
                has_email = True
                primary_aff = aff_text   # prefer the email-containing aff
        last_full_inst = primary_aff
        if has_email:
            corr_names.append(full_long)
            corr_institutions.append(primary_aff)

    # Fallback: no email in any affiliation (common for some non-Cell journals).
    # Use last author as conventional Nature/Cell senior corresponding.
    if not corr_names and last_full_name:
        corr_names.append(last_full_name)
        corr_institutions.append(last_full_inst)

    authors_str = "; ".join(authors)   # no cap — preserve corresponding tail
    corresponding = "; ".join(corr_names)
    corr_institution = corr_institutions[-1] if corr_institutions else ""

    return {
        "pmid": pmid,
        "doi": doi,
        "title": title,
        "abstract": abstract,
        "authors": authors_str,
        "corresponding": corresponding,
        "corresponding_institution": corr_institution,
        "journal": journal,
        "pub_types": pub_types,
        "year": y, "month": m, "day": d,
    }


def is_unwanted_type(rec: dict) -> bool:
    """True for corrections / editorials / news / comments — noise for a
    research digest (feed-audit mode 5). Reviews and research articles pass."""
    if CORRECTION_TITLE_RE.search(rec.get("title", "")):
        return True
    return bool(set(rec.get("pub_types") or []) & DROP_PUBTYPES)


def _strip_markup(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def backfill_abstract(doi: str, timeout: int = 20) -> str:
    """Fetch a missing abstract for a DOI: Crossref first, then Europe PMC.
    Free, no API key, keyed by DOI (feed-audit mode 6). Returns '' if neither
    has one — which is correct for editorials/news/corrections."""
    if not doi or doi.startswith("pmid:"):
        return ""
    # 1) Crossref (JATS markup in message.abstract; strip tags).
    try:
        req = Request(CROSSREF + quote(doi, safe=""), headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as r:
            ab = json.loads(r.read()).get("message", {}).get("abstract")
        if ab:
            ab = _strip_markup(ab)
            if len(ab) > 40:
                return ab
    except Exception:
        pass
    # 2) Europe PMC.
    try:
        url = EUROPEPMC + "?" + urlencode(
            {"query": f'DOI:"{doi}"', "format": "json", "resultType": "core"})
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read()).get("resultList", {}).get("result", [])
        if res:
            ab = res[0].get("abstractText")
            if ab:
                ab = _strip_markup(ab)
                if len(ab) > 40:
                    return ab
    except Exception:
        pass
    return ""


def update_coverage(counts: dict) -> str:
    """Track consecutive-empty runs per journal (feed-audit mode 7). Returns a
    warning string for journals empty ≥2 runs in a row (1 is too noisy), else ''."""
    path = STATE_DIR / "journal_coverage.json"
    prev_streaks = {}
    if path.exists():
        try:
            prev_streaks = json.loads(path.read_text()).get("consecutive_empty", {})
        except Exception:
            prev_streaks = {}
    streaks, warnings = {}, []
    for name, c in counts.items():
        streaks[name] = int(prev_streaks.get(name, 0)) + 1 if c == 0 else 0
        if streaks[name] >= 2:
            warnings.append(f"{name}（连续 {streaks[name]} 次 0 篇）")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"updated": date.today().isoformat(), "last_counts": counts,
         "consecutive_empty": streaks}, ensure_ascii=False, indent=2), encoding="utf-8")
    return "；".join(warnings)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8,
                    help="Lookback window in days (default 8 = past week + buffer)")
    args = ap.parse_args()

    keywords = load_keywords(KEYWORDS_FILE)
    if not keywords:
        print("[fetch_journals] WARNING: no keywords loaded", file=sys.stderr)

    journals_cfg = json.loads(JOURNALS_FILE.read_text())["journals"]
    today = date.today()
    frm_d = today - timedelta(days=args.days)
    to_str = today.strftime("%Y/%m/%d")
    frm_str = frm_d.strftime("%Y/%m/%d")
    print(f"[fetch_journals] window {frm_str} → {to_str}, "
          f"{len(journals_cfg)} journals", file=sys.stderr)

    # Stage 1a: collect PMIDs from all journals.
    all_pmids: list[str] = []
    pmid_to_journal: dict[str, str] = {}
    per_journal_counts: dict[str, int] = {}
    for j in journals_cfg:
        name = j["name"]
        issn = j["issn"]
        try:
            pmids = esearch_pmids(issn, frm_str, to_str)
        except Exception as e:
            print(f"[fetch_journals] {name} ({issn}) esearch failed: {e}",
                  file=sys.stderr)
            per_journal_counts[name] = -1  # -1 = fetch error (not a true 0)
            continue
        per_journal_counts[name] = len(pmids)
        print(f"[fetch_journals] {name}: {len(pmids)} PMIDs", file=sys.stderr)
        for pmid in pmids:
            if pmid not in pmid_to_journal:
                all_pmids.append(pmid)
                pmid_to_journal[pmid] = name
        time.sleep(0.4)
    print(f"[fetch_journals] total unique PMIDs: {len(all_pmids)}",
          file=sys.stderr)

    # Coverage tracking (feed-audit mode 7): warn on journals empty ≥2 runs.
    # -1 (fetch error) is excluded — it's already visible as an error above.
    cov_warn = update_coverage({k: v for k, v in per_journal_counts.items() if v >= 0})
    if cov_warn:
        print(f"[fetch_journals] coverage_warning={cov_warn}", file=sys.stderr)

    if not all_pmids:
        stats = {"scanned": 0, "candidates": 0, "from": frm_str, "to": to_str,
                 "source": "journals"}
        print(f"[fetch_journals] stats={json.dumps(stats)}", file=sys.stderr)
        return

    # Stage 1b: efetch metadata + abstracts.
    articles = efetch_articles(all_pmids)
    print(f"[fetch_journals] fetched {len(articles)} article records",
          file=sys.stderr)

    # Stage 1b-2: drop corrections / editorials / news (feed-audit mode 5).
    kept = [a for a in articles if not is_unwanted_type(a)]
    n_dropped_type = len(articles) - len(kept)
    print(f"[fetch_journals] dropped {n_dropped_type} non-research "
          f"(correction/editorial/news/…)", file=sys.stderr)

    # Stage 1c: dedup against cache (do this before backfill so we never spend
    # API calls on already-seen items).
    con = init_cache(CACHE_DB)
    fresh = []
    n_already_seen = 0
    for art in kept:
        key_doi = art["doi"] or f"pmid:{art['pmid']}"
        if con.execute("SELECT 1 FROM seen WHERE doi=? AND version=?",
                       (key_doi, "1")).fetchone():
            n_already_seen += 1
            continue
        art["_key_doi"] = key_doi
        fresh.append(art)

    # Stage 1c-2: backfill missing abstracts BEFORE the keyword filter, so a
    # relevant paper whose PubMed record has no abstract yet is matched on its
    # real content rather than its title alone (feed-audit mode 6). Crossref →
    # Europe PMC, free, keyed by DOI.
    n_backfilled = 0
    for art in fresh:
        if art["abstract"].strip():
            continue
        ab = backfill_abstract(art["doi"])
        if ab:
            art["abstract"] = ab
            art["_abstract_backfilled"] = True
            n_backfilled += 1
        time.sleep(0.15)  # polite to Crossref/EuropePMC
    print(f"[fetch_journals] backfilled {n_backfilled} missing abstracts",
          file=sys.stderr)

    # Stage 1d: keyword pre-filter.
    candidates = []
    for art in fresh:
        title, abstract = art["title"], art["abstract"]
        matched = keyword_match(f"{title}\n{abstract}", keywords)
        if not matched:
            continue
        url_main = (f"https://doi.org/{art['doi']}" if art["doi"]
                    else f"https://pubmed.ncbi.nlm.nih.gov/{art['pmid']}/")
        candidates.append({
            "doi": art["_key_doi"],
            "version": "1",
            "title": title,
            "authors": art["authors"],
            "corresponding": art.get("corresponding", ""),
            "corresponding_institution": art.get("corresponding_institution", ""),
            "abstract": abstract,
            "date": "-".join([p for p in (art["year"], art["month"], art["day"]) if p]),
            "category": pmid_to_journal.get(art["pmid"], art["journal"] or ""),
            "biorxiv_url": url_main,           # field name reused for downstream compat
            "pdf_url": url_main,
            "html_url": url_main,
            "matched_keywords": matched,
            "abstract_backfilled": bool(art.get("_abstract_backfilled")),
            "_pmid": art["pmid"],
            "_journal": pmid_to_journal.get(art["pmid"], art["journal"] or ""),
        })

    print(f"[fetch_journals] {len(candidates)} candidates after keyword pre-filter "
          f"(dropped {n_dropped_type} non-research, skipped {n_already_seen} seen, "
          f"backfilled {n_backfilled} abstracts)", file=sys.stderr)

    for c in candidates:
        print(json.dumps(c, ensure_ascii=False))

    stats = {"scanned": len(articles), "candidates": len(candidates),
             "already_seen": n_already_seen, "dropped_nonresearch": n_dropped_type,
             "backfilled": n_backfilled,
             "from": frm_str, "to": to_str, "source": "journals"}
    print(f"[fetch_journals] stats={json.dumps(stats)}", file=sys.stderr)


if __name__ == "__main__":
    main()
