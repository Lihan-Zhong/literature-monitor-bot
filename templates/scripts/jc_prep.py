#!/usr/bin/env python3
"""
Journal-Club preparation: read a PDF in full, ask Claude for a detailed
algorithm walkthrough + per-figure interpretation + presentation outline,
render PDF pages as figure attachments, push everything to Telegram.

Usage:
    venv/bin/python3 jc_prep.py --pdf <path> [--pages "2-9"]

This is the deeper sibling of deep_analyze.py. Token cost ~30-40K.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
RESEARCH_FOCUS_FILE = ROOT / "config" / "research_focus.txt"
DIGEST_DIR = ROOT / "digests"
FIG_DIR = ROOT / "figures"


def load_research_focus() -> str:
    """Injected into the JC prompt; configured in config/research_focus.txt
    (see SKILL.md). Nothing about the focus is hard-coded here."""
    try:
        lines = [l for l in RESEARCH_FOCUS_FILE.read_text(encoding="utf-8").splitlines()
                 if not l.strip().startswith("#")]
        focus = "\n".join(lines).strip()
    except Exception:
        focus = ""
    return focus or ("（研究方向未配置——请填写 config/research_focus.txt，"
                     "或用部署 agent 端到端生成，见 SKILL.md）")
TG_ENV = ROOT / "config" / "telegram.env"

# Reuse helpers from deep_analyze (md→html, send, etc.)
sys.path.insert(0, str(ROOT / "scripts"))
from deep_analyze import (  # noqa
    extract_pdf_text, md_to_tg_html, html_escape, load_tg_creds,
    tg_send, chunk_text, archive_md, TG_LIMIT, TG_API,
)


JC_PROMPT_TEMPLATE = """**关键执行规则（务必遵守）：**
- 你不是 agent，不调用工具/MCP。
- 不写"已发送/已推送/已镜像 Telegram"等元话语。
- 你的回复内容就是最终讲稿材料，会被 Python 转发到 Telegram。
- 不知道的写「文中未明确提及」，不要编造。

---

你是文献分析助手，帮研究者准备 **Journal Club** 报告。基于下面这篇论文的**全文**（已从 PDF 提取），写一份**详细的中文讲解材料**——读完之后能上台讲 30-40 分钟。

**研究者关注的方向（决定讲解的侧重）：**
{research_focus}

**输出结构（严格按这 7 段，markdown 大标题用 `##`）：**

## 一句话核心
（≤30 字）

## 背景与动机
- 该领域现有方法的核心局限
- 这篇论文要解决什么问题，为什么这个问题重要

## 方法学详解（核心算法 / 流程逐步走读）
按时间顺序讲清楚整个 pipeline，包括：
- 实验：样本处理化学、library prep、测序 / 检测参数
- 分析：核心算法逐步走读（最详细！）、关键参数 / 阈值 / 假设
- 与该领域现有相关方法的具体技术差异（化学、平台、分辨率、通量）
- 方法学优势 + 潜在局限

## 关键实验与发现（按 Figure 顺序）
按论文的 Figure 顺序逐图讲：每张图展示什么、结论是什么、对论点的支持；也提一下补充材料里最关键的 1-2 个。

## 与你研究方向的关联
- 可立即借鉴的具体技术点（化学、分析框架、可移植算法部件）
- 是否能应用到你的研究系统 / 问题，或与之互补

## 可讨论 / 质疑的点（JC 听众可能挑刺的方向）
- 实验设计的潜在问题
- 数据分析的可重复性、统计严谨性
- 推论是否过强 / 是否有 confound

## 讲稿建议
- slide 顺序建议（约 12-15 张），每张主要内容一行
- 重点强调哪几张 figure（必备 + 可选）
- 听众可能问的 3-5 个问题及如何答

---

**JSON 规则不适用**（这次输出纯 markdown）。**中文里若用引号请用「」，不要用 ASCII 双引号**。整体目标 2500-4000 字。

PDF FULL TEXT (已提取，可能含 OCR 噪音):
{body}
"""


def render_pages_to_png(pdf_path: Path, page_indices: list, out_dir: Path,
                        dpi: int = 200) -> list:
    """Use pypdfium2 to rasterize specific PDF pages (0-indexed) to PNGs.
    Returns list of (page_idx_1based, png_path)."""
    import pypdfium2 as pdfium  # type: ignore
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(pdf_path))
    results = []
    # pypdfium2 uses 1pt = 1/72 inch; scale = dpi/72.
    scale = dpi / 72.0
    for idx in page_indices:
        if idx < 0 or idx >= len(pdf):
            continue
        page = pdf[idx]
        bitmap = page.render(scale=scale)
        pil = bitmap.to_pil()
        out_path = out_dir / f"{pdf_path.stem[:50]}-p{idx+1:02d}.png"
        pil.save(str(out_path), "PNG", optimize=True)
        results.append((idx + 1, out_path))
    return results


def tg_send_photo(token: str, chat: str, photo_path: Path, caption: str = "") -> dict:
    """Telegram sendPhoto via curl (rolls multipart correctly; hand-rolled
    urllib multipart can choke on binary boundaries)."""
    url = TG_API.format(token=token, method="sendPhoto")
    cmd = [
        "curl", "-s", "-X", "POST", url,
        "-F", f"chat_id={chat}",
        "-F", f"photo=@{photo_path}",
    ]
    if caption:
        cmd += ["-F", f"caption={caption[:1024]}", "-F", "parse_mode=HTML"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"curl failed: {res.stderr[:300]}")
    return json.loads(res.stdout)


def parse_page_range(spec: str, n_pages: int) -> list:
    """'2-9' → [1..8]; '2,5,7' → [1,4,6]; '2-5,8' → [1,2,3,4,7]. 1-indexed input,
    0-indexed output."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a) - 1, int(b)))
        else:
            out.append(int(part) - 1)
    # clamp & dedupe
    out = sorted({p for p in out if 0 <= p < n_pages})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", default="1-9",
                    help="1-indexed PDF page range for figure rendering "
                         "(default '1-9' — typical Nature article main body)")
    ap.add_argument("--dpi", type=int, default=180)
    ap.add_argument("--max-chars", type=int, default=0,
                    help="0 = no truncation (full PDF text to LLM)")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)

    print(f"[jc] pdf: {pdf_path.name}", file=sys.stderr)
    print(f"[jc] reading text...", file=sys.stderr)
    full_text = extract_pdf_text(pdf_path)
    print(f"[jc] text: {len(full_text)} chars (~{len(full_text)//4} tokens)",
          file=sys.stderr)
    body = full_text
    if args.max_chars and len(body) > args.max_chars:
        body = body[:args.max_chars] + "\n\n[...truncated...]"
        print(f"[jc] truncated to {args.max_chars}", file=sys.stderr)

    prompt = JC_PROMPT_TEMPLATE.format(research_focus=load_research_focus(), body=body)
    print(f"[jc] prompt: {len(prompt)} chars (~{len(prompt)//4} tokens)",
          file=sys.stderr)
    t0 = time.time()
    res = subprocess.run(
        ["claude", "-p", "--tools", ""],
        input=prompt, capture_output=True, text=True, timeout=600,
    )
    if res.returncode != 0:
        raise RuntimeError(f"claude exit {res.returncode}: {res.stderr[:500]}")
    deep_text = res.stdout
    dt = time.time() - t0
    print(f"[jc] LLM: {dt:.1f}s, output {len(deep_text)} chars", file=sys.stderr)

    # Render PDF pages as PNGs.
    import pypdfium2 as pdfium  # type: ignore
    pdf = pdfium.PdfDocument(str(pdf_path))
    n_pages = len(pdf)
    page_indices = parse_page_range(args.pages, n_pages)
    print(f"[jc] rendering {len(page_indices)} pages at {args.dpi}dpi to PNG...",
          file=sys.stderr)
    out_dir = FIG_DIR / pdf_path.stem[:60]
    rendered = render_pages_to_png(pdf_path, page_indices, out_dir, dpi=args.dpi)
    print(f"[jc] rendered {len(rendered)} PNGs to {out_dir}", file=sys.stderr)

    # Archive markdown.
    meta = {"title": pdf_path.stem, "doi": f"pdf:{pdf_path.name}", "version": "1"}
    arch = archive_md(meta, deep_text)
    print(f"[jc] archived to {arch}", file=sys.stderr)

    if args.no_push:
        print(deep_text)
        return

    # Push: header + body chunks (HTML) + figure photos.
    token, chat = load_tg_creds()
    header = (f"📚 <b>Journal Club 讲稿</b>\n"
              f"<b>{html_escape(pdf_path.stem)}</b>")
    tg_send(token, chat, header)
    body_html = md_to_tg_html(deep_text)
    for chunk in chunk_text(body_html):
        tg_send(token, chat, chunk)
        time.sleep(0.3)

    # Push figure photos one at a time with captions.
    for page_num, png_path in rendered:
        try:
            tg_send_photo(token, chat, png_path,
                          caption=f"📄 Page {page_num}: {html_escape(pdf_path.stem)[:80]}")
            time.sleep(0.5)
        except Exception as e:
            print(f"[jc] sendPhoto page {page_num} failed: {e}", file=sys.stderr)
    print(f"[jc] done", file=sys.stderr)


if __name__ == "__main__":
    main()
