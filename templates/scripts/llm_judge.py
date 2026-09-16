#!/usr/bin/env python3
"""
Batched LLM judge for bioRxiv candidate papers.

Reads candidate papers (JSONL) on stdin. Sends them to `claude -p` in batches
of N (default 8) per call to share the system prompt across many papers and
cut input-token usage by ~75%. Writes enriched JSONL on stdout (one record
per paper, with judge fields added).

Each input record gains:
    relevant     (bool)
    score        (0-5, int)
    summary_zh   (str, 2-3 sentences)
    commentary_zh(str, 1-2 sentences — explicitly names which axis hit)
    judge_error  (str, present only when judge call failed for that paper)

Quota detection: if `claude -p` returns a rate-limit / session-quota error,
the script stops, prints `__QUOTA_EXHAUSTED__` to stderr, and exits 42 so
the wrapper can re-trigger on the next cron tick.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESEARCH_FOCUS_FILE = ROOT / "config" / "research_focus.txt"


def load_research_focus() -> str:
    """Injected into the judge prompt; configured in config/research_focus.txt
    (see SKILL.md). Nothing about the focus is hard-coded here."""
    try:
        lines = [l for l in RESEARCH_FOCUS_FILE.read_text(encoding="utf-8").splitlines()
                 if not l.strip().startswith("#")]
        focus = "\n".join(lines).strip()
    except Exception:
        focus = ""
    return focus or ("（研究方向未配置——请填写 config/research_focus.txt，"
                     "或用部署 agent 端到端生成，见 SKILL.md）")


# ---------------------------------------------------------------------------
# Compact system prompt — ~400 tokens (vs ~2500 in the per-paper version).
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是文献筛选助手。评估给定的论文（来自 bioRxiv preprint 或正式发表期刊）摘要列表，对每篇打分。

**研究者关注的方向：**
__RESEARCH_FOCUS__

**评分规则（按与上述研究方向的相关度）：**
- 5 = 极强相关（方法与生物学两面都正中研究方向）
- 4 = 强相关（单一维度强命中：一个正相关的新方法，或一个正相关的生物学系统/问题）
- 3 = 弱相关（边缘命中研究方向的某一面）
- 2 = 外围（沾边但基本无关）
- 1 = 关键词假阳性
- 0 = 完全无关
relevant=true 当且仅当 score≥3。

**输出格式：** 严格 JSON 数组，每个元素 4 个字段：
- i: 输入论文编号（与输入对应）
- score: 整数 0-5
- summary_zh: 2-3 句中文摘要总结（写清楚做了什么+用了什么方法+得到什么结论）
- commentary_zh: 1-2 句中文点评，说明它与研究方向的关系（命中哪一面 / 为何不命中）及理由

**JSON 严格约束：**
- summary_zh / commentary_zh 内**不要用 ASCII 双引号**，用中文「」代替
- 字符串内不换行
- 不要尾逗号
- 仅输出 JSON 数组，前后不要加任何 markdown / 解释

输入论文将以 #N 开头编号。""".replace("__RESEARCH_FOCUS__", load_research_focus())

QUOTA_SIGNATURES = (
    "rate_limit", "rate-limit", "ratelimit",
    "quota", "usage limit",
    "reached your usage", "session limit",
    "5-hour", "session window",
    "429",
)


class QuotaExhausted(Exception):
    """Raised when claude -p reports quota / rate-limit / session-limit."""


def call_claude(prompt: str, timeout: int = 240) -> str:
    # Pass prompt via stdin so it doesn't show in `ps` / `top -c`.
    # `--tools ""` disables ALL tool use (no MCP, no Read/Edit/Bash) so the
    # LLM can ONLY respond with text — preventing it from autonomously
    # invoking the user's Telegram MCP or any other side-effect tool.
    try:
        res = subprocess.run(
            ["claude", "-p", "--tools", ""],
            input=prompt,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise QuotaExhausted(f"claude timeout after {timeout}s (transient)")
    if res.returncode != 0:
        # ANY hard claude failure → QuotaExhausted, so main() sets quota_hit and
        # exits 42 (⚠️ banner + retry next cron) instead of the generic-Exception
        # path that marks the batch 'errored' (score 0) and ships a fake-empty
        # digest. The weekly-limit case exits 1 with EMPTY stderr and matches no
        # QUOTA_SIGNATURE (a real silent failure seen in production), so
        # returncode != 0 — not the signature list — is the reliable trigger.
        msg = (res.stderr + res.stdout).lower()
        detail = (res.stderr.strip() or res.stdout.strip() or "(empty output)")[:300]
        kind = "quota signal" if any(s in msg for s in QUOTA_SIGNATURES) else "hard failure"
        raise QuotaExhausted(f"claude exit {res.returncode} [{kind}]: {detail}")
    return res.stdout


# ---------------------------------------------------------------------------
# JSON extraction with repair for unescaped CJK quotes
# ---------------------------------------------------------------------------
def _repair_inner_quotes(blob: str, keys=("summary_zh", "commentary_zh")) -> str:
    """LLM sometimes uses ASCII " inside Chinese strings. Toggle them to 「/」."""
    for key in keys:
        pattern = re.compile(rf'("{key}"\s*:\s*")(.*?)("\s*[,}}\]])', re.DOTALL)
        def fix(m):
            head, body, tail = m.group(1), m.group(2), m.group(3)
            cleaned, toggle = [], 0
            for ch in body:
                if ch == '"':
                    cleaned.append("「" if toggle % 2 == 0 else "」")
                    toggle += 1
                else:
                    cleaned.append(ch)
            return head + "".join(cleaned) + tail
        blob = pattern.sub(fix, blob)
    return blob


def extract_json_array(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Find the first [...] block (greedy across newlines).
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON array found: {text[:300]!r}")
    blob = m.group(0)
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return json.loads(_repair_inner_quotes(blob))


# ---------------------------------------------------------------------------
# Batch judge
# ---------------------------------------------------------------------------
def format_paper_block(idx: int, rec: dict) -> str:
    title = rec.get("title", "")[:1000]
    cat = rec.get("category", "")
    abstract = rec.get("abstract", "")[:3500]  # tighter than 5000 to save tokens
    return f"#{idx}\nTITLE: {title}\nCATEGORY: {cat}\nABSTRACT: {abstract}"


def judge_batch(records: list, batch_idx: int) -> list:
    """Judge a batch of papers in one LLM call. Returns enriched copies of
    the input records (same length, same order)."""
    if not records:
        return []
    blocks = [format_paper_block(i, r) for i, r in enumerate(records)]
    prompt = (
        SYSTEM_PROMPT
        + "\n\n## 待评估论文：\n\n"
        + "\n\n---\n\n".join(blocks)
        + f"\n\n## 输出：\n请输出长度恰好为 {len(records)} 的 JSON 数组，"
          f"i 字段对应上面的 #0..#{len(records)-1}。"
    )
    raw = call_claude(prompt)
    arr = extract_json_array(raw)
    by_i = {int(item.get("i", -1)): item for item in arr if isinstance(item, dict)}

    out = []
    for i, rec in enumerate(records):
        item = by_i.get(i)
        if item is None:
            out.append({**rec, "relevant": False, "score": 0,
                        "judge_error": f"missing index {i} in batch {batch_idx} response"})
            continue
        try:
            score = int(item.get("score", 0))
        except (ValueError, TypeError):
            score = 0
        out.append({
            **rec,
            "score": score,
            "relevant": score >= 3,
            "summary_zh": str(item.get("summary_zh", "")).strip(),
            "commentary_zh": str(item.get("commentary_zh", "")).strip(),
        })
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=8,
                    help="Papers per LLM call (default 8)")
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--keep-all", action="store_true",
                    help="Emit every judgement, including non-relevant")
    args = ap.parse_args()

    candidates = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            candidates.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[llm_judge] bad input line skipped: {line[:120]!r}", file=sys.stderr)
    print(f"[llm_judge] {len(candidates)} candidates, batch_size={args.batch}",
          file=sys.stderr)

    n_kept = n_errored = n_quota_skipped = n_judged = 0
    quota_hit = False
    batch_idx = 0
    for chunk_start in range(0, len(candidates), args.batch):
        chunk = candidates[chunk_start:chunk_start + args.batch]
        batch_idx += 1

        if quota_hit:
            n_quota_skipped += len(chunk)
            for r in chunk:
                print(f"[llm_judge] {r.get('doi','?')} SKIPPED (quota)",
                      file=sys.stderr)
            continue

        t0 = time.time()
        try:
            judged = judge_batch(chunk, batch_idx)
        except QuotaExhausted as e:
            quota_hit = True
            n_quota_skipped += len(chunk)
            print(f"[llm_judge] QUOTA EXHAUSTED on batch {batch_idx}: {e}",
                  file=sys.stderr)
            print("__QUOTA_EXHAUSTED__", file=sys.stderr)
            continue
        except Exception as e:
            # Whole batch failed (parsing, timeout, etc.); mark all in chunk
            # as judge_error and continue.
            print(f"[llm_judge] batch {batch_idx} FAILED ({type(e).__name__}: "
                  f"{str(e)[:200]}); marking {len(chunk)} as errored",
                  file=sys.stderr)
            for r in chunk:
                err_rec = {**r, "relevant": False, "score": 0,
                           "judge_error": f"batch_failure: {type(e).__name__}"}
                if args.keep_all:
                    print(json.dumps(err_rec, ensure_ascii=False))
                n_errored += 1
            continue

        dt = time.time() - t0
        for out in judged:
            n_judged += 1
            if "judge_error" in out:
                n_errored += 1
            if args.keep_all or (out.get("relevant") and out.get("score", 0) >= args.min_score):
                print(json.dumps(out, ensure_ascii=False))
                n_kept += 1
        scores = [str(o.get("score", "?")) for o in judged]
        print(f"[llm_judge] batch {batch_idx} ({len(chunk)} papers, {dt:.1f}s) "
              f"scores=[{','.join(scores)}]", file=sys.stderr)

    print(f"[llm_judge] candidates={len(candidates)} judged={n_judged} "
          f"kept={n_kept} errored={n_errored} quota_skipped={n_quota_skipped}",
          file=sys.stderr)
    if quota_hit:
        sys.exit(42)


if __name__ == "__main__":
    main()
