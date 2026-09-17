#!/usr/bin/env python3
"""
Push a literature digest to Discord via an incoming webhook.

Drop-in sibling of push_telegram.py: reads judged-relevant papers as JSONL on
stdin, takes the same --source/--scanned/... arguments the run wrappers pass,
archives the same digests/YYYY-MM-DD.md, and updates the same cache/seen.sqlite.
The ONLY things that change vs the Telegram pusher:

  * transport  -> Discord webhook (HTTPS POST, 204 = success)
  * formatting -> Discord embeds (no HTML; <b>/<a href> would render literally)
  * a state/latest_digest.json + digests/runs/<ts>.json are written so the
    Discord follow-up bot ("展开第 3 条") can answer from disk.

Webhook URL resolution order:
  1. LIT_DISCORD_WEBHOOK
  2. state/discord_webhook.txt (first non-comment line)

Nothing is sent unless a webhook is configured, so it is safe to wire into cron
before enabling it: exit 3, having sent nothing.

Usage:
  push_discord.py < judged.jsonl          send latest digest (reads stdin)
  push_discord.py --dry-run < judged.jsonl render payloads to stdout, send nothing
  push_discord.py --healthcheck            verify the webhook without posting
  push_discord.py --text "hi"              send one plain message
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

# Compact author formatter lives in the shared scripts/ folder so both channel
# pushers render author lists identically and can't drift (first 8 + '...' + a
# tail that always contains the corresponding authors).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from authorfmt import _format_authors_with_corresponding  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DIGEST_DIR = ROOT / "digests"
RUNS_DIR = DIGEST_DIR / "runs"
STATE_DIR = ROOT / "state"
CACHE_DB = ROOT / "cache" / "seen.sqlite"

USER_AGENT = "lit-bot (+local literature digest, 0.1)"
MAX_CONTENT = 2000
MAX_EMBED_DESC = 4096
MAX_FIELD_VALUE = 1024
MAX_EMBEDS_PER_MESSAGE = 10
# gold / orange / blue by score; grey fallback.
SCORE_COLORS = {5: 0xE8B923, 4: 0xE8734A, 3: 0x4A90D9}
DEFAULT_COLOR = 0x8899A6


class DiscordNotConfigured(Exception):
    pass


# --------------------------------------------------------------------------- #
# webhook plumbing (the webhook contract below)
# --------------------------------------------------------------------------- #
def resolve_webhook() -> str:
    url = os.environ.get("LIT_DISCORD_WEBHOOK", "").strip()
    if url:
        return url
    path = STATE_DIR / "discord_webhook.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    raise DiscordNotConfigured(
        "no webhook configured (set LIT_DISCORD_WEBHOOK or state/discord_webhook.txt)"
    )


def post(webhook: str, payload: dict[str, Any], *, attempt: int = 0) -> None:
    request = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code == 429 and attempt < 3:
            retry_after = 1.0
            try:
                retry_after = float(json.loads(body).get("retry_after", 1.0))
            except Exception:
                pass
            time.sleep(min(retry_after, 10.0) + 0.2)
            return post(webhook, payload, attempt=attempt + 1)
        raise RuntimeError(f"discord webhook HTTP {exc.code}: {body}") from exc


def healthcheck(webhook: str) -> int:
    """GET the webhook to confirm identity + which channel it targets. Posts nothing."""
    result: dict[str, Any] = {"configured": bool(webhook)}
    if not webhook:
        result["error"] = "no webhook configured"
        write_health(ok=False, extra=result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3
    request = urllib.request.Request(webhook, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            info = json.loads(response.read())
        result.update(
            {
                "ok": True,
                "webhook_name": info.get("name"),
                "channel_id": info.get("channel_id"),
                "guild_id": info.get("guild_id"),
            }
        )
    except Exception as exc:
        result.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    write_health(ok=bool(result.get("ok")), extra=result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def write_health(*, ok: bool, extra: dict[str, Any] | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "push_ok": ok,
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if k not in payload})
    (STATE_DIR / "discord_health.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def clip(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_score(value: Any) -> int:
    try:
        return max(0, min(5, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_header(source: str, now_str: str, window, footer: str,
                  quota_hit: bool, n_hits: int) -> str:
    lines = [f"## 📚 {source} · {now_str}"]
    if quota_hit:
        lines.append("⚠️ **本次运行 Claude 调用失败（额度耗尽或临时错误）**，"
                     "未判完的下次 cron 自动续跑（本次未推的论文不会入库、下次会重扫）")
    lines.append(f"窗口: {window[0]} → {window[1]}")
    lines.append(f"命中 {n_hits} 篇相关文献" if n_hits else "今日无相关文献")
    lines.append(footer)
    if n_hits:
        # Push feed and Q&A live in separate channels; point the user at the
        # ask channel, not "本频道". Follow-up works by index OR title keyword
        # (answered from state/latest_digest.json, so no need to be in this feed).
        hint = os.environ.get(
            "LIT_DISCORD_ASK_HINT",
            "追问去问答频道说「展开第 3 条」或「深度追问 <标题关键词>」。",
        )
        lines.append("\n" + hint)
    return "\n".join(lines)


def build_embed(p: dict, idx: int, total: int) -> dict[str, Any]:
    score = parse_score(p.get("score"))
    url = p.get("biorxiv_url") or p.get("html_url") or ""
    pdf = p.get("pdf_url") or ""
    authors = _format_authors_with_corresponding(
        (p.get("authors") or "").strip(), (p.get("corresponding") or "").strip()
    )
    embed: dict[str, Any] = {
        "title": clip(f"[{idx}/{total}] {p.get('title', '(no title)').strip()}", 256),
        "color": SCORE_COLORS.get(score, DEFAULT_COLOR),
        "description": clip(p.get("summary_zh", "").strip(), MAX_EMBED_DESC),
        "footer": {"text": clip(f"{p.get('category', '')} · 追问：展开第 {idx} 条", 2048)},
        "fields": [],
    }
    if url:
        embed["url"] = url
    if authors:
        embed["fields"].append({"name": "作者", "value": clip(authors, MAX_FIELD_VALUE)})
    embed["fields"].append(
        {"name": "评分", "value": f"{'⭐' * score if score else '未评分'} ({score}/5)", "inline": True}
    )
    if p.get("category"):
        embed["fields"].append({"name": "类别", "value": clip(p["category"], 256), "inline": True})
    if p.get("commentary_zh", "").strip():
        embed["fields"].append(
            {"name": "💡 点评", "value": clip(p["commentary_zh"].strip(), MAX_FIELD_VALUE)}
        )
    matched = ", ".join(p.get("matched_keywords", []) or [])
    if matched:
        embed["fields"].append({"name": "🔍 命中关键词", "value": clip(matched, MAX_FIELD_VALUE)})
    links = []
    if url:
        links.append(f"[bioRxiv 页面]({url})")
    if pdf:
        links.append(f"[PDF]({pdf})")
    if links:
        embed["fields"].append({"name": "链接", "value": clip("  ·  ".join(links), MAX_FIELD_VALUE)})
    return embed


def build_payloads(header: str, papers: list[dict]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = [{"content": clip(header, MAX_CONTENT)}]
    if not papers:
        return payloads
    # Default: one paper = one message (own embed), so each card can be
    # reacted/replied to individually. Set LIT_DISCORD_BATCH=1 to pack up to
    # 10 embeds per message instead.
    batch = os.environ.get("LIT_DISCORD_BATCH", "").lower() in {"1", "true", "yes"}
    embeds = [build_embed(p, i, len(papers)) for i, p in enumerate(papers, 1)]
    chunk = MAX_EMBEDS_PER_MESSAGE if batch else 1
    for start in range(0, len(embeds), chunk):
        payloads.append({"embeds": embeds[start : start + chunk]})
    return payloads


# --------------------------------------------------------------------------- #
# archive / cache / follow-up state (parity with push_telegram.py)
# --------------------------------------------------------------------------- #
def write_digest_md(papers: list[dict], scanned: int, source: str, window) -> Path:
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGEST_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.md"
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
            f"- **命中关键词**: {', '.join(p.get('matched_keywords',[]) or [])}",
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


def write_followup_state(papers: list[dict], source: str, window, counts: dict) -> None:
    """Persist the digest so the Discord follow-up bot can answer '展开第 N 条'
    from disk. Also archive a per-run copy, because a same-day second run would
    otherwise overwrite the first (learned the hard way in production)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for i, p in enumerate(papers, 1):
        items.append({
            "index": i,
            "title": p.get("title", ""),
            "url": p.get("biorxiv_url") or p.get("html_url") or "",
            "pdf_url": p.get("pdf_url", ""),
            "doi": p.get("doi", ""),
            "version": str(p.get("version", "1")),
            "category": p.get("category", ""),
            "score": parse_score(p.get("score")),
            "authors": p.get("authors", ""),
            "corresponding": p.get("corresponding", ""),
            "summary_zh": p.get("summary_zh", ""),
            "comment_zh": p.get("commentary_zh", ""),
            "abstract": p.get("abstract", ""),
            "matched_keywords": p.get("matched_keywords", []) or [],
        })
    digest = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().astimezone().isoformat(),
        "source": source,
        "window": list(window),
        "counts": counts,
        "items": items,
    }
    blob = json.dumps(digest, ensure_ascii=False, indent=2) + "\n"
    (STATE_DIR / "latest_digest.json").write_text(blob, encoding="utf-8")
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    (RUNS_DIR / f"{stamp}.json").write_text(blob, encoding="utf-8")


def update_cache(papers: list[dict]) -> None:
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


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="bioRxiv", help="Source label in the header")
    ap.add_argument("--scanned", type=int, default=0)
    ap.add_argument("--keyword-pass", type=int, default=0)
    ap.add_argument("--triage-pass", type=int, default=0)
    ap.add_argument("--quota-hit", type=int, default=0)
    ap.add_argument("--window-from", default="")
    ap.add_argument("--window-to", default="")
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--no-archive", action="store_true")
    ap.add_argument("--notice", default="",
                    help="extra ⚠️ line appended to the header (e.g. journal "
                         "coverage warning)")
    ap.add_argument("--dry-run", action="store_true",
                    help="render payloads to stdout, send nothing")
    ap.add_argument("--healthcheck", action="store_true",
                    help="GET the webhook to verify it; posts nothing")
    ap.add_argument("--text", help="send a single plain message and exit")
    args = ap.parse_args()

    # Resolve webhook. Diagnostics still run when nothing is configured.
    try:
        webhook = resolve_webhook()
    except DiscordNotConfigured as exc:
        if args.dry_run or args.healthcheck:
            webhook = ""
        else:
            print(f"discord push skipped: {exc}", file=sys.stderr)
            return 3

    if args.healthcheck:
        return healthcheck(webhook)

    if args.text is not None:
        if args.dry_run:
            print(json.dumps({"content": args.text}, ensure_ascii=False, indent=2))
            return 0
        post(webhook, {"content": clip(args.text, MAX_CONTENT)})
        print("discord text sent")
        return 0

    # Read judged papers (JSONL) from stdin — same contract as push_telegram.py.
    all_papers: list[dict] = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            all_papers.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[push_discord] skipping bad line: {line[:120]!r}", file=sys.stderr)

    papers = [p for p in all_papers if int(p.get("score", 0)) >= args.min_score]
    papers.sort(key=lambda p: -int(p.get("score", 0)))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    window = (args.window_from or "?", args.window_to or "?")
    footer = (
        f"📊 过滤强度: 类别白名单留 {args.scanned} → "
        f"关键词留 {args.keyword_pass} → 标题 triage 留 {args.triage_pass} → "
        f"摘要判读得 {len(all_papers)} → score≥{args.min_score} 留 {len(papers)}"
    )
    counts = {
        "scanned": args.scanned,
        "keyword_pass": args.keyword_pass,
        "triage_pass": args.triage_pass,
        "judged": len(all_papers),
        "selected": len(papers),
        "min_score": args.min_score,
        "quota_hit": bool(args.quota_hit),
    }
    header = render_header(args.source, now_str, window, footer,
                           bool(args.quota_hit), len(papers))
    if args.notice.strip():
        header += f"\n⚠️ {args.notice.strip()}"
    # Self-check line: on a genuinely empty result with NO failure signal
    # (no quota hit, no fetch/coverage notice), state explicitly that the run
    # was healthy — so a real quiet day is distinguishable at a glance from a
    # silent failure (which instead shows a ⚠️ banner above).
    degraded = bool(args.quota_hit) or bool(args.notice.strip())
    if len(papers) == 0 and not degraded:
        header += ("\n✅ 自检正常：抓取 / 筛选 / 判读均成功完成"
                   f"（扫描 {args.scanned} 篇 → 判读 {len(all_papers)} 篇 → 命中 0），"
                   "确为「真·无相关文献」，非故障。")
    payloads = build_payloads(header, papers)

    if args.dry_run:
        for payload in payloads:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"\n-- dry run: {len(payloads)} message(s), nothing sent --", file=sys.stderr)
        return 0

    # Archive + cache + follow-up state BEFORE the network send, so a webhook
    # hiccup never loses the record. (Cron marks nothing "pushed" off our exit
    # code, so ordering here is about durability, not correctness.)
    if not args.no_archive:
        digest = write_digest_md(papers, args.scanned, args.source, window)
        print(f"[push_discord] archived to {digest}", file=sys.stderr)
    write_followup_state(papers, args.source, window, counts)
    update_cache(all_papers)

    for index, payload in enumerate(payloads, start=1):
        post(webhook, payload)
        if index < len(payloads):
            time.sleep(float(os.environ.get("LIT_DISCORD_MESSAGE_DELAY", "0.6")))
    write_health(ok=True, extra={"messages": len(payloads), "selected": len(papers)})
    print(f"[push_discord] sent {len(payloads)} message(s); {len(papers)} paper(s)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
