#!/usr/bin/env python3
"""
Read judged-relevant papers (JSONL) on stdin, push a digest to Telegram and
archive a Markdown digest to digests/YYYY-MM-DD.md.

Always sends at least one Telegram message — even when stdin is empty — so the
user knows the cron run executed.

Bot token: read from config/telegram.env (line 'TELEGRAM_BOT_TOKEN=...').
Chat id:   from --chat-id (default YOUR_TELEGRAM_CHAT_ID).
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DIGEST_DIR = ROOT / "digests"
CACHE_DB = ROOT / "cache" / "seen.sqlite"
# Bot token for this project, read from config/telegram.env (see telegram.env.example).
TG_ENV = ROOT / "config" / "telegram.env"
TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_LIMIT = 4000  # Telegram allows 4096; leave margin for safety.


def load_token() -> str:
    if not TG_ENV.exists():
        raise RuntimeError(f"missing {TG_ENV}; set TELEGRAM_BOT_TOKEN there")
    for line in TG_ENV.read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"TELEGRAM_BOT_TOKEN not found in {TG_ENV}")


def load_default_chat_id():
    if not TG_ENV.exists():
        return None
    for line in TG_ENV.read_text().splitlines():
        if line.startswith("TELEGRAM_CHAT_ID="):
            return line.split("=", 1)[1].strip()
    return None


def tg_call(token: str, method: str, payload: dict) -> dict:
    url = TG_API.format(token=token, method=method)
    data = json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except (HTTPError, URLError) as e:
            print(f"[push_telegram] {method} attempt {attempt+1} failed: {e}",
                  file=sys.stderr)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"telegram {method} failed after 3 attempts")


def send_message(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> dict:
    return tg_call(token, "sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    })


def chunk_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split on paragraph boundaries first, then hard-split if needed."""
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
    # Hard split anything still too long.
    final = []
    for p in parts:
        while len(p) > limit:
            final.append(p[:limit])
            p = p[limit:]
        final.append(p)
    return final


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _normalize_name(name: str) -> str:
    """Normalize a name for fuzzy matching: lowercase, drop punctuation."""
    n = name.lower().replace(",", " ").replace(".", " ").replace("-", " ")
    return " ".join(t for t in n.split() if t)


def _names_match(a: str, b: str) -> bool:
    """Two names likely refer to the same person if they share at least one
    token of length ≥2 (handles 2-letter Chinese surnames like Pu/Lu/He/Wu
    AND fuzzy surname matching like 'Gang Pei' vs 'Pei, G'). Single-letter
    initials are ignored to avoid spurious matches."""
    ta = {t for t in _normalize_name(a).split() if len(t) >= 2}
    tb = {t for t in _normalize_name(b).split() if len(t) >= 2}
    return bool(ta & tb)


def _format_authors_with_corresponding(authors: str, corresponding: str) -> str:
    """Compact author list: first 8 names, then '...', then a tail that always
    contains the corresponding-author group (CNS papers commonly have 4-8
    co-corresponding authors clustered at the end).

    `corresponding` may be a single name or a '; '-joined list of names."""
    if not authors:
        return corresponding or ""
    parts = [a.strip() for a in authors.split(";") if a.strip()]
    n = len(parts)
    corr_list = [c.strip() for c in (corresponding or "").split(";") if c.strip()]

    if n <= 12 and not corr_list:
        return "; ".join(parts)
    if n <= 12:
        # Short list: keep all, but if a corresponding name isn't already
        # represented (fuzzy match), append it.
        out = list(parts)
        for c in corr_list:
            if not any(_names_match(c, t) for t in parts):
                out.append(c)
        return "; ".join(out)

    head = parts[:8]
    # Tail strategy: walk the original list from the end, collect at least 2
    # names AND every name that fuzzy-matches a corresponding entry. This
    # captures the multi-PI corresponding cluster CNS papers love.
    tail_keep = set()
    # Always keep absolute last 2.
    for nm in parts[-2:]:
        tail_keep.add(nm)
    # Keep any author whose surname matches a corresponding entry.
    for nm in parts:
        if any(_names_match(nm, c) for c in corr_list):
            tail_keep.add(nm)
    # Re-order tail by their original position in `parts`.
    tail = [nm for nm in parts if nm in tail_keep]

    # If a corresponding name has NO author-list match (rare), splice it in
    # before the last author so the user still sees the explicit corr name.
    for c in corr_list:
        if not any(_names_match(c, t) for t in tail):
            tail = tail[:-1] + [c] + tail[-1:]

    # Cap tail to a reasonable length — if it ballooned we still want the
    # message readable. 6 is plenty for even mega-collab papers.
    if len(tail) > 6:
        tail = tail[-6:]

    return "; ".join(head) + "; ... ; " + "; ".join(tail)


def format_paper(p: dict, idx: int) -> str:
    title = html_escape(p.get("title", "(no title)").strip())
    raw_authors = p.get("authors", "").strip()
    corresponding = p.get("corresponding", "").strip()
    authors_compact = _format_authors_with_corresponding(raw_authors, corresponding)
    authors = html_escape(authors_compact)
    cat = html_escape(p.get("category", ""))
    score = p.get("score", 0)
    summary = html_escape(p.get("summary_zh", "").strip())
    commentary = html_escape(p.get("commentary_zh", "").strip())
    url = p.get("biorxiv_url") or p.get("html_url") or ""
    pdf = p.get("pdf_url") or ""
    matched = ", ".join(p.get("matched_keywords", []))
    star = "⭐" * min(int(score), 5)

    parts = [
        f"<b>[{idx}] {title}</b>",
        f"<i>{authors}</i>" if authors else None,
        f"📂 {cat}  |  评分: {star} ({score}/5)" if cat else f"评分: {star} ({score}/5)",
        f"📝 <b>摘要</b>: {summary}" if summary else None,
        f"💡 <b>点评</b>: {commentary}" if commentary else None,
        f"🔍 命中关键词: <code>{html_escape(matched)}</code>" if matched else None,
        f"🔗 <a href=\"{url}\">bioRxiv 页面</a>  |  <a href=\"{pdf}\">PDF</a>" if url else None,
    ]
    return "\n".join(p for p in parts if p)


def write_digest_md(papers: list[dict], scanned: int, source: str,
                    window: tuple[str, str]) -> Path:
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = DIGEST_DIR / f"{today}.md"
    stamp = datetime.now().strftime("%H:%M")

    lines = [
        f"\n\n## {source} — 推送 @ {stamp}",
        f"窗口: {window[0]} → {window[1]} | 扫描 {scanned} 篇 | 命中 {len(papers)} 篇",
        "",
    ]
    for i, p in enumerate(papers, 1):
        lines.extend([
            f"### [{i}] {p.get('title', '').strip()}",
            f"- **DOI**: `{p.get('doi','')}` (v{p.get('version','1')})",
            f"- **作者**: {p.get('authors', '')}",
            f"- **类别**: {p.get('category', '')}",
            f"- **评分**: {'⭐' * int(p.get('score',0))} ({p.get('score',0)}/5)",
            f"- **命中关键词**: {', '.join(p.get('matched_keywords',[]))}",
            f"- **链接**: [bioRxiv]({p.get('biorxiv_url','')}) · [PDF]({p.get('pdf_url','')})",
            "",
            f"**📝 中文摘要**: {p.get('summary_zh','')}",
            "",
            f"**💡 点评**: {p.get('commentary_zh','')}",
            "",
            f"<details><summary>原文摘要</summary>\n\n{p.get('abstract','')}\n\n</details>",
            "",
            "---",
            "",
        ])
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def update_cache(papers: list[dict]):
    """Mark every paper we processed as seen (relevant or not the script doesn't care here)."""
    if not papers:
        return
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(CACHE_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            doi TEXT NOT NULL, version TEXT NOT NULL, seen_at TEXT NOT NULL,
            relevant INTEGER, score REAL,
            PRIMARY KEY (doi, version)
        )
    """)
    now = datetime.utcnow().isoformat(timespec="seconds")
    for p in papers:
        con.execute(
            "INSERT OR REPLACE INTO seen (doi, version, seen_at, relevant, score) "
            "VALUES (?, ?, ?, ?, ?)",
            (p.get("doi", ""), str(p.get("version", "1")), now,
             1 if p.get("relevant") else 0, float(p.get("score", 0))),
        )
    con.commit()
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", default=load_default_chat_id() or "YOUR_TELEGRAM_CHAT_ID")
    ap.add_argument("--source", default="bioRxiv",
                    help="Source label shown in the message header")
    ap.add_argument("--scanned", type=int, default=0,
                    help="Total papers scanned (post category whitelist)")
    ap.add_argument("--keyword-pass", type=int, default=0,
                    help="Candidates after keyword pre-filter")
    ap.add_argument("--triage-pass", type=int, default=0,
                    help="Candidates passing Stage-2 title triage (yes+maybe)")
    ap.add_argument("--quota-hit", type=int, default=0,
                    help="1 if any stage hit Claude quota; show banner")
    ap.add_argument("--window-from", default="")
    ap.add_argument("--window-to", default="")
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--notice", default="",
                    help="Prepend a ⚠️ notice line to the header (fetch/coverage warning)")
    ap.add_argument("--text",
                    help="Send this one raw message and exit (used for failure alerts)")
    ap.add_argument("--no-archive", action="store_true",
                    help="Skip writing digests/YYYY-MM-DD.md")
    ap.add_argument("--no-send", action="store_true",
                    help="Print message to stdout instead of sending (dry run)")
    args = ap.parse_args()

    # Alert mode: send a single raw message (failure banners) and exit.
    if args.text:
        send_message(load_token(), args.chat_id, args.text)
        print(f"[push_telegram] sent alert to chat {args.chat_id}", file=sys.stderr)
        return

    all_papers = []  # everything we successfully judged (for cache)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            all_papers.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[push_telegram] skipping bad line: {line[:120]!r}", file=sys.stderr)
    # Pushable subset = relevant && score>=min, sorted high-to-low.
    papers = [p for p in all_papers if int(p.get("score", 0)) >= args.min_score]
    papers.sort(key=lambda p: -int(p.get("score", 0)))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    window = (args.window_from or "?", args.window_to or "?")

    # Filter-strength footer line (always shown).
    filter_footer = (f"📊 过滤强度: 类别白名单留 {args.scanned} → "
                     f"关键词留 {args.keyword_pass} → "
                     f"标题 triage 留 {args.triage_pass} → "
                     f"摘要判读得 {len(all_papers)} → "
                     f"score≥{args.min_score} 留 {len(papers)}")
    quota_banner = "\n⚠️ <b>本次运行 Claude quota 中途耗尽</b>，未判完的下次 cron 自动续跑\n" \
                   if args.quota_hit else ""
    notice_banner = f"\n⚠️ {html_escape(args.notice)}\n" if args.notice else ""

    if not papers:
        header = (f"📭 <b>{args.source}</b> {now_str}{quota_banner}{notice_banner}\n"
                  f"窗口: {window[0]} → {window[1]}\n"
                  f"今日无相关文献\n{filter_footer}")
        msgs = [header]
    else:
        header = (f"📚 <b>{args.source}</b> {now_str}{quota_banner}{notice_banner}\n"
                  f"窗口: {window[0]} → {window[1]}\n"
                  f"命中 {len(papers)} 篇相关文献\n{filter_footer}")
        msgs = [header]
        for i, p in enumerate(papers, 1):
            msgs.extend(chunk_text(format_paper(p, i)))

    if not args.no_archive:
        digest = write_digest_md(papers, args.scanned, args.source, window)
        print(f"[push_telegram] archived to {digest}", file=sys.stderr)

    # Mark every judged paper (relevant or not) as seen, so we don't re-judge
    # them. Quota-skipped ones never reach this script, so they stay un-cached
    # and will be retried on the next cron tick.
    update_cache(all_papers)

    if args.no_send:
        print("\n\n---\n\n".join(msgs))
        return

    token = load_token()
    for m in msgs:
        send_message(token, args.chat_id, m)
        time.sleep(0.5)
    print(f"[push_telegram] sent {len(msgs)} message(s) to chat {args.chat_id}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
