#!/usr/bin/env python3
"""
Deep-analyze a single bioRxiv paper: fetch JATS-XML full text, send to
`claude -p --tools ""` for a structured Chinese deep review, push the
review to Telegram via Bot API, archive to digests/.

Usage:
    deep_analyze.py --doi <DOI>
    deep_analyze.py --url <bioRxiv URL>      # parses DOI from URL

For v2 production, this is invoked by the cron pipeline whenever llm_judge
gives a bioRxiv paper score=5.

Token cost (approximate, per paper):
    input  ~9-14K (JATS body trimmed to ~30K chars)
    output ~2-3K
    total  ~11-17K
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DIGEST_DIR = ROOT / "digests"
TG_ENV = ROOT / "config" / "telegram.env"
RESEARCH_FOCUS_FILE = ROOT / "config" / "research_focus.txt"
USER_AGENT = "lit-bot/1.0 (you@example.com)"


def load_research_focus() -> str:
    """Injected into the deep-read prompt; configured in config/research_focus.txt
    (see SKILL.md). Nothing about the focus is hard-coded here."""
    try:
        lines = [l for l in RESEARCH_FOCUS_FILE.read_text(encoding="utf-8").splitlines()
                 if not l.strip().startswith("#")]
        focus = "\n".join(lines).strip()
    except Exception:
        focus = ""
    return focus or ("（研究方向未配置——请填写 config/research_focus.txt，"
                     "或用部署 agent 端到端生成，见 SKILL.md）")
VENV_PY = ROOT / "venv" / "bin" / "python3"  # has pypdf installed

# --- bioRxiv full-text fetch. Order of attempts:
# --- JATS-XML -> cookie-warmed PDF -> Firecrawl (optional) -> abstract-only.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PAPERS_DIR = ROOT / "cache" / "papers"           # downloaded PDFs, named by DOI
MIN_FULLTEXT_CHARS = 1500                         # below this = not real full text
PDF_RETRY_WAIT = 62                               # honor Cloudflare ~1-min window
# Poppler's pdftotext. Set LIT_PDFTOTEXT to an absolute path, or leave the
# placeholder — the code falls back to `pdftotext` on $PATH (see _pdftotext_poppler).
POPPLER_PDFTOTEXT = os.environ.get("LIT_PDFTOTEXT", "/PATH/TO/pdftotext")
# OPTIONAL Firecrawl fallback (a separate python + a `scrape` helper script).
# Set both env vars to enable it; unset → this stage is silently skipped.
FIRECRAWL_PY = Path(os.environ.get("LIT_FIRECRAWL_PY", "/nonexistent/firecrawl-python"))
FIRECRAWL_HELPER = Path(os.environ.get("LIT_FIRECRAWL_HELPER", "/nonexistent/fc.py"))

DOI_RE = re.compile(r"(10\.\d{4,9}/[^v\s/?#]+)")


def _truncate(text: str, max_chars: int) -> str:
    if len(text) > max_chars:
        print(f"[deep] body {len(text)} chars → truncating to {max_chars}", file=sys.stderr)
        return text[:max_chars] + "\n\n[... truncated ...]"
    return text


def parse_doi(s: str) -> str:
    """Extract DOI from a raw DOI string or a bioRxiv URL."""
    m = DOI_RE.search(s)
    if not m:
        raise ValueError(f"could not parse DOI from {s!r}")
    return m.group(1)


def http_get(url: str, timeout: int = 60) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.read()
        except (HTTPError, URLError) as e:
            print(f"[deep] {url} attempt {attempt+1} failed: {e}", file=sys.stderr)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET failed: {url}")


def fetch_metadata(doi: str) -> dict:
    """bioRxiv details API returns metadata including the JATS-XML URL."""
    url = f"https://api.biorxiv.org/details/biorxiv/{doi}"
    data = json.loads(http_get(url))
    coll = data.get("collection") or []
    if not coll:
        raise RuntimeError(f"no record returned for DOI {doi}")
    # Take the highest version.
    coll.sort(key=lambda r: int(r.get("version", "1")), reverse=True)
    return coll[0]


def fetch_jats_xml(jats_url: str) -> bytes:
    return http_get(jats_url, timeout=120)


def _pdftotext_poppler(pdf_path: Path) -> str:
    """Fallback PDF→text via poppler's pdftotext, used when pypdf is absent or
    yields nothing. Lets deep_analyze run under a base python without pypdf too."""
    for cand in (POPPLER_PDFTOTEXT, "pdftotext"):
        if cand != "pdftotext" and not os.path.exists(cand):
            continue
        try:
            res = subprocess.run([cand, "-q", str(pdf_path), "-"],
                                 capture_output=True, text=True, timeout=180)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout
        except Exception as e:
            print(f"[deep] poppler {cand} failed: {e}", file=sys.stderr)
    return ""


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract plain text from a PDF. Prefers pypdf (project venv); falls back to
    poppler pdftotext so the script also works without pypdf installed."""
    raw = ""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(pdf_path))
        parts = []
        for i, page in enumerate(reader.pages):
            try:
                t = page.extract_text() or ""
            except Exception as e:
                print(f"[deep] pdf page {i} extract failed: {e}", file=sys.stderr)
                continue
            if t.strip():
                parts.append(t)
        raw = "\n\n".join(parts)
    except ImportError:
        print("[deep] pypdf unavailable; using poppler pdftotext", file=sys.stderr)
        raw = _pdftotext_poppler(pdf_path)
    if not raw.strip():
        raw = _pdftotext_poppler(pdf_path)
    # Drop common journal-PDF cruft.
    raw = re.sub(r"^Downloaded from .*$", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def fetch_biorxiv_pdf(doi: str, version: str, tries: int = 6) -> Path | None:
    """Download a bioRxiv preprint PDF through Cloudflare, robustly: warm a
    __cf_bm cookie on a cheap endpoint, send a browser UA + Referer, verify the
    %PDF magic bytes, and on
    429 honor `Retry-After` (~1 min) instead of exponential backoff. Caches to
    cache/papers/<doi>vN.pdf. Returns the path, or None if it never got through."""
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    out = PAPERS_DIR / (re.sub(r"[/]", "-", doi) + f"v{version}.pdf")
    if out.exists() and out.stat().st_size > 20000:
        print(f"[deep] using cached PDF {out.name}", file=sys.stderr)
        return out
    pdf_url = f"https://www.biorxiv.org/content/{doi}v{version}.full.pdf"
    referer = f"https://www.biorxiv.org/content/{doi}v{version}"
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    # 1) warm the __cf_bm cookie
    try:
        opener.open(urllib.request.Request("https://www.biorxiv.org/",
                    headers={"User-Agent": BROWSER_UA}), timeout=30).read(2000)
    except Exception as e:
        print(f"[deep] cookie warm failed (continuing): {e}", file=sys.stderr)
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/pdf,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }
    for i in range(tries):
        wait = PDF_RETRY_WAIT
        try:
            with opener.open(urllib.request.Request(pdf_url, headers=headers), timeout=90) as r:
                data = r.read()
            if data[:5] == b"%PDF-":
                out.write_bytes(data)
                print(f"[deep] PDF fetched: {len(data)} bytes → {out.name} "
                      f"(attempt {i+1})", file=sys.stderr)
                return out
            print(f"[deep] attempt {i+1}: non-PDF body ({len(data)} bytes, "
                  f"throttled); waiting {wait}s", file=sys.stderr)
        except urllib.error.HTTPError as e:
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra and str(ra).strip().isdigit():
                wait = int(ra)
            print(f"[deep] attempt {i+1}: HTTP {e.code}; retry-after={ra}; "
                  f"waiting {wait}s", file=sys.stderr)
        except Exception as e:
            wait = 10
            print(f"[deep] attempt {i+1}: {type(e).__name__}: {e}; waiting {wait}s",
                  file=sys.stderr)
        if i < tries - 1:
            time.sleep(min(wait, 90) + 1)
    print("[deep] PDF fetch gave up", file=sys.stderr)
    return None


def fetch_firecrawl_text(url: str) -> str:
    """Last-resort web-body fetch via Firecrawl (OPTIONAL — needs a Firecrawl
    python + a `scrape` helper; set LIT_FIRECRAWL_PY / LIT_FIRECRAWL_HELPER to
    enable it, otherwise it's skipped gracefully). Note: for bioRxiv a shared IP is
    often rate-limited the same as a direct hit, so this is a fallback *after* the
    PDF path, not before it."""
    if not FIRECRAWL_PY.exists() or not FIRECRAWL_HELPER.exists():
        return ""
    out_dir = ROOT / "cache" / "firecrawl"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", url)[:120] or "page"
    out_path = out_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe}.md"
    try:
        res = subprocess.run(
            [str(FIRECRAWL_PY), str(FIRECRAWL_HELPER), "scrape", url, "-o", str(out_path)],
            capture_output=True, text=True, timeout=150, check=False,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except Exception as e:
        print(f"[deep] firecrawl failed: {e}", file=sys.stderr)
        return ""
    if res.returncode != 0 or not out_path.exists():
        print(f"[deep] firecrawl no output (rc={res.returncode})", file=sys.stderr)
        return ""
    text = out_path.read_text(encoding="utf-8", errors="replace").strip()
    return text if len(text) >= 200 else ""


def fetch_fulltext(doi: str, version: str, jats_url: str, meta: dict,
                   max_chars: int) -> tuple:
    """Robust bioRxiv full-text chain. Returns (sections, body, source_label)."""
    base = {
        "title": meta.get("title", ""),
        "authors": meta.get("authors", ""),
        "abstract": meta.get("abstract", ""),
        "body": "",
    }
    # 1) JATS-XML — present for processed preprints, absent for brand-new ones.
    if jats_url:
        try:
            sec = extract_sections(fetch_jats_xml(jats_url))
            for k in ("title", "authors", "abstract"):
                if sec.get(k):
                    base[k] = sec[k]
            if len(sec.get("body", "")) >= MIN_FULLTEXT_CHARS:
                print(f"[deep] JATS full text: {len(sec['body'])} chars", file=sys.stderr)
                return sec, _truncate(sec["body"], max_chars), "bioRxiv 全文 (JATS)"
            print("[deep] JATS body too short; trying PDF", file=sys.stderr)
        except Exception as e:
            print(f"[deep] JATS failed ({e}); trying PDF", file=sys.stderr)

    # 2) Cookie-warmed PDF — the reliable path for fresh preprints.
    pdf = fetch_biorxiv_pdf(doi, version)
    if pdf:
        text = extract_pdf_text(pdf)
        if len(text) >= MIN_FULLTEXT_CHARS:
            print(f"[deep] PDF full text: {len(text)} chars", file=sys.stderr)
            sec = dict(base, body=text)
            return sec, _truncate(text, max_chars), "bioRxiv PDF 全文"
        print("[deep] PDF text too short; trying Firecrawl", file=sys.stderr)

    # 3) Firecrawl scrape of the full-text landing page.
    fc = fetch_firecrawl_text(f"https://www.biorxiv.org/content/{doi}v{version}.full")
    if len(fc) >= MIN_FULLTEXT_CHARS:
        print(f"[deep] Firecrawl text: {len(fc)} chars", file=sys.stderr)
        sec = dict(base, body=fc)
        return sec, _truncate(fc, max_chars), "bioRxiv 网页正文 (Firecrawl)"

    # 4) Abstract-only.
    print("[deep] all full-text paths failed; abstract-only", file=sys.stderr)
    return base, "[全文获取失败：JATS / PDF / Firecrawl 均未取得正文，本次仅基于摘要分析]", "bioRxiv 摘要"


def extract_sections(xml_bytes: bytes) -> dict:
    """Pull out the chapters we want from a bioRxiv JATS-XML document.
    Returns dict with keys: title, authors, abstract, body."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise RuntimeError(f"JATS XML parse failed: {e}")

    # Title.
    title_el = root.find(".//title-group/article-title")
    title = "".join(title_el.itertext()).strip() if title_el is not None else ""

    # Authors.
    authors = []
    for contrib in root.findall(".//contrib-group/contrib[@contrib-type='author']"):
        sur = contrib.findtext(".//surname") or ""
        giv = contrib.findtext(".//given-names") or ""
        if sur:
            initials = "".join(p[0] for p in giv.split() if p) if giv else ""
            authors.append(f"{sur}, {initials}".strip().rstrip(",").strip())
    authors_str = "; ".join(authors[:30])

    # Abstract.
    abstract_parts = []
    for ab in root.findall(".//abstract"):
        for p in ab.iter():
            tag = p.tag.split("}")[-1]
            if tag == "p" or tag == "title":
                txt = "".join(p.itertext()).strip()
                if txt:
                    abstract_parts.append(txt)
    abstract = "\n\n".join(abstract_parts)

    # Body sections.
    body_chunks = []
    body = root.find(".//body")
    if body is not None:
        for sec in body.findall(".//sec"):
            heading_el = sec.find("title")
            heading = "".join(heading_el.itertext()).strip() if heading_el is not None else ""
            paras = []
            for p in sec.findall("p"):
                paras.append("".join(p.itertext()).strip())
            section_text = "\n\n".join(paras).strip()
            if section_text:
                body_chunks.append(f"## {heading}\n\n{section_text}" if heading
                                   else section_text)
    body_text = "\n\n".join(body_chunks)

    return {"title": title, "authors": authors_str,
            "abstract": abstract, "body": body_text}


PROMPT_TEMPLATE = """**关键执行规则（务必遵守）：**
- 你不是一个 agent，你**不需要也不能**调用任何工具或 MCP 服务。
- 你**不要**回复"已发送"、"已推送"、"已镜像到 Telegram"、"已完成"等元话语。
- 你的回复内容**就是**最终输出本身，会被 Python 脚本捕获后转发。请把完整的中文深度分析作为正文输出。
- 如果你不知道某些方法学细节，照实写「摘要未提及」，**不要**编造、不要做"我去查证一下"之类承诺。

---

你是高级文献分析助手，给研究者做深度文献评论。

**研究者关注的方向：**
{research_focus}

**待深度分析的论文（bioRxiv 全文）：**

TITLE: {title}

AUTHORS: {authors}

ABSTRACT:
{abstract}

FULL TEXT (sections from JATS XML, may be truncated to {max_chars} chars):

{body}

---

**请输出中文深度分析，结构化的 6 块（纯 markdown，不要套代码块）：**

## 一句话核心
（≤30 字概括论文做了什么）

## 方法学创新
（描述具体技术、和现有方法的区别、关键 trick；必要时点出测序平台/化学/分析流程的差异）

## 关键生物学发现
（最有价值的 2-3 个结论 + 数据量级；如果有反直觉或数量惊人的数字一定提到）

## 与研究方向的关联
（明确说明这篇与上面「研究者关注的方向」的关系：命中哪一面、在方法/生物学上有何共鸣或距离；无关就直说，不要硬凑）

## 值不值得精读 + 为什么
（明确表态：必读 / 值得读 / 浏览即可，并给出依据）

## 可借鉴 / 可合作的 2-3 个具体点
（研究者可立刻动手 borrow 的方法学细节、分析思路、或潜在合作方向）

**JSON 字符串规则在中文里如需引号用「」，不要用 ASCII 双引号。
**长度限制：整个回复不要超过 1500 字（中文字符），重点突出，不要凑字数。"""


def call_claude_dump(prompt: str, timeout: int = 300) -> tuple:
    """Run `claude -p --tools ""` with prompt on stdin. Return (stdout, stderr)."""
    res = subprocess.run(
        ["claude", "-p", "--tools", ""],
        input=prompt, capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0:
        raise RuntimeError(f"claude exit {res.returncode}: {res.stderr.strip()[:500]}")
    return res.stdout, res.stderr


# --- Telegram push -------------------------------------------------------
TG_LIMIT = 4000
TG_API = "https://api.telegram.org/bot{token}/sendMessage"


def load_tg_creds() -> tuple:
    token = chat = ""
    for line in TG_ENV.read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.split("=", 1)[1].strip()
        elif line.startswith("TELEGRAM_CHAT_ID="):
            chat = line.split("=", 1)[1].strip()
    if not (token and chat):
        raise RuntimeError("missing bot token or chat id in config/telegram.env")
    return token, chat


def chunk_text(text: str, limit: int = TG_LIMIT) -> list:
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > limit:
            if buf:
                parts.append(buf)
            buf = para
        else:
            buf = (buf + "\n\n" + para) if buf else para
    if buf:
        parts.append(buf)
    final = []
    for p in parts:
        while len(p) > limit:
            final.append(p[:limit])
            p = p[limit:]
        final.append(p)
    return final


def tg_send(token: str, chat: str, text: str) -> dict:
    payload = json.dumps({
        "chat_id": chat, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": False,
    }).encode()
    req = Request(TG_API.format(token=token), data=payload,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_tg_html(md: str) -> str:
    """Lightweight markdown → Telegram HTML transform. Telegram only supports
    a small subset of HTML tags (b, i, u, s, code, pre, a) so we don't try
    to map every markdown construct — just headings, bold, italic, code,
    links, and bullets."""
    # Normalize.
    text = md.replace("\r\n", "\n").replace("\r", "\n")

    # Escape ALL HTML special chars first, then re-introduce our tags.
    text = html_escape(text)

    # `inline code` → <code>...</code>  (do this BEFORE bold so backticks
    # don't get eaten by the * matchers).
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)

    # Headings ## → bold line + blank line. Also strip any trailing `:` markers.
    def heading_repl(m):
        level, content = m.group(1), m.group(2).strip().rstrip(":：")
        return f"<b>{content}</b>"
    text = re.sub(r"^(#{1,6})\s+(.+)$", heading_repl, text, flags=re.MULTILINE)

    # **bold** → <b>...</b>
    text = re.sub(r"\*\*([^\n*]+)\*\*", r"<b>\1</b>", text)
    # *italic* (single asterisk, not part of **) → <i>...</i>
    text = re.sub(r"(?<![\w*])\*([^\n*]+)\*(?!\*)", r"<i>\1</i>", text)

    # Markdown links [text](url) → Telegram link.
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)

    # Bullets: lines starting with -  /  *  /  ·  → •
    text = re.sub(r"^\s*[-*·]\s+", "• ", text, flags=re.MULTILINE)

    # Numbered lists: keep "1. Foo" but ensure spacing (already fine).
    # Collapse any 3+ blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def push_to_telegram(meta: dict, deep_text: str, source_label: str = "bioRxiv 摘要"):
    """Push deep analysis to Telegram. Body is converted from markdown to
    Telegram HTML so headings/bold/links render natively on the user's device.
    (digests/ archive keeps the raw markdown.)"""
    token, chat = load_tg_creds()
    title = html_escape(meta.get("title", "(no title)").strip())
    doi = meta.get("doi", "")
    url = (f"https://www.biorxiv.org/content/{doi}v{meta.get('version', '1')}"
           if doi and doi.startswith(("10.1101/", "10.64898/"))
           else (f"https://doi.org/{doi}" if doi else ""))
    link_html = f'🔗 <a href="{url}">原文链接</a>' if url else ""
    header = (f"📖 <b>深度分析 ({source_label})</b>\n"
              f"<b>{title}</b>\n{link_html}").strip()

    # Convert markdown body to Telegram-friendly HTML.
    body_html = md_to_tg_html(deep_text)

    tg_send(token, chat, header)
    for chunk in chunk_text(body_html):
        tg_send(token, chat, chunk)
        time.sleep(0.3)


# --- Archive --------------------------------------------------------------
def archive_md(meta: dict, deep_text: str) -> Path:
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    safe_doi = re.sub(r"[/]", "_", meta.get("doi", "unknown"))
    path = DIGEST_DIR / f"{today}-deep-{safe_doi}.md"
    title = meta.get("title", "")
    doi = meta.get("doi", "")
    version = meta.get("version", "1")
    biorxiv_url = f"https://www.biorxiv.org/content/{doi}v{version}"
    text = f"""# 深度分析: {title}

- **DOI**: `{doi}` v{version}
- **生成时间**: {datetime.now().isoformat(timespec='seconds')}
- **链接**: [bioRxiv]({biorxiv_url})

---

{deep_text}
"""
    path.write_text(text, encoding="utf-8")
    return path


# --- Main -----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doi", help="bioRxiv DOI (use --pdf for full text from a local PDF)")
    ap.add_argument("--url", help="bioRxiv URL")
    ap.add_argument("--pdf", help="Path to a local PDF to deep-analyze")
    ap.add_argument("--title", help="Title to use when --pdf has no metadata")
    ap.add_argument("--max-chars", type=int, default=200000,
                    help="Safety cap on body chars fed to LLM (default 200000 ≈ "
                         "~55K tokens; covers a full preprint uncut). Lower it "
                         "only to save tokens for batched/automated use.")
    ap.add_argument("--no-push", action="store_true",
                    help="Print to stdout instead of pushing to Telegram")
    args = ap.parse_args()

    if not (args.doi or args.url or args.pdf):
        ap.error("must provide one of --doi / --url / --pdf")

    body = ""
    source_label = "bioRxiv 摘要"
    sections = {"title": "", "authors": "", "abstract": "", "body": ""}
    meta = {}

    if args.pdf:
        pdf_path = Path(args.pdf).expanduser().resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")
        print(f"[deep] reading PDF: {pdf_path}", file=sys.stderr)
        text = extract_pdf_text(pdf_path)
        print(f"[deep] PDF text: {len(text)} chars (~{len(text)//4} tokens)",
              file=sys.stderr)
        if len(text) > args.max_chars:
            print(f"[deep] truncating to {args.max_chars} chars", file=sys.stderr)
            text = text[:args.max_chars] + "\n\n[... truncated ...]"
        body = text
        source_label = "用户上传 PDF 全文"
        # Build minimal meta from filename / args.
        title = args.title or pdf_path.stem
        sections = {"title": title, "authors": "", "abstract": "",
                    "body": text}
        meta = {"title": title, "doi": f"pdf:{pdf_path.name}", "version": "1"}
    else:
        raw = args.doi or args.url
        doi = parse_doi(raw)
        print(f"[deep] DOI = {doi}", file=sys.stderr)

        # 1. Metadata (api.biorxiv.org is NOT throttled — safe to hit).
        meta = fetch_metadata(doi)
        version = str(meta.get("version", "1"))
        jats_url = meta.get("jatsxml", "")
        print(f"[deep] title: {meta.get('title','')[:120]}", file=sys.stderr)
        print(f"[deep] version={version} jats={'yes' if jats_url else 'none'}",
              file=sys.stderr)

        # 2. Full text — JATS → cookie-warmed PDF → Firecrawl → abstract.
        sections, body, source_label = fetch_fulltext(
            doi, version, jats_url, meta, args.max_chars)
    print(f"[deep] sending {len(body)} chars body to LLM (source: {source_label})",
          file=sys.stderr)

    # 3. LLM deep synthesis.
    prompt = PROMPT_TEMPLATE.format(
        research_focus=load_research_focus(),
        title=sections["title"] or meta.get("title", ""),
        authors=sections["authors"] or meta.get("authors", ""),
        abstract=sections["abstract"] or meta.get("abstract", ""),
        body=body, max_chars=args.max_chars,
    )
    print(f"[deep] full prompt size: {len(prompt)} chars (~{len(prompt)//4} tokens)",
          file=sys.stderr)
    t0 = time.time()
    deep_text, _stderr = call_claude_dump(prompt)
    dt = time.time() - t0
    print(f"[deep] LLM call took {dt:.1f}s, output {len(deep_text)} chars",
          file=sys.stderr)

    # 4. Archive + push.
    arch = archive_md(meta, deep_text)
    print(f"[deep] archived to {arch}", file=sys.stderr)
    if args.no_push:
        print(deep_text)
    else:
        push_to_telegram(meta, deep_text, source_label=source_label)
        print(f"[deep] pushed to Telegram", file=sys.stderr)


if __name__ == "__main__":
    main()
