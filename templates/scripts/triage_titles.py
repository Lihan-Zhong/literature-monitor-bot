#!/usr/bin/env python3
"""
Stage 2: title-only triage. Reads candidate JSONL on stdin, sends ALL titles
in ONE batched `claude -p` call, asks the LLM to label each as
'yes' / 'maybe' / 'no'. Emits to stdout the candidate records (unchanged)
that are NOT 'no' — i.e. yes + maybe pass through to Stage 3.

Single LLM call regardless of candidate count. Token cost ~= 500 (system) +
30 tokens/title + 10 tokens/output-per-title. For 50 titles that's ~3K tokens
total — about 50× cheaper than per-paper full-abstract judging.

Quota detection: same exit-42 convention as llm_judge.py.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESEARCH_FOCUS_FILE = ROOT / "config" / "research_focus.txt"


def load_research_focus() -> str:
    """The researcher's focus, injected into every LLM prompt. Configure it in
    config/research_focus.txt (see SKILL.md) — nothing about the focus is
    hard-coded, so this pipeline works for any field."""
    try:
        lines = [l for l in RESEARCH_FOCUS_FILE.read_text(encoding="utf-8").splitlines()
                 if not l.strip().startswith("#")]
        focus = "\n".join(lines).strip()
    except Exception:
        focus = ""
    return focus or ("（研究方向未配置——请填写 config/research_focus.txt，"
                     "或用部署 agent 端到端生成，见 SKILL.md）")


SYSTEM_PROMPT = """你是文献快速分流助手。下面给你一组论文标题，每篇标号 #N。

研究者关注的方向：
__RESEARCH_FOCUS__

仅看标题，给每篇打一个标签：
- yes：标题已强烈暗示击中研究方向
- maybe：标题语焉不详但有可能，需进一步看摘要
- no：标题已确定无关

宁可 maybe 也别漏。

输出严格 JSON 数组，每个元素 2 字段：
{"i": <编号>, "label": "yes" | "maybe" | "no"}

不要任何 markdown / 解释 / 字符串里的 ASCII 双引号。""".replace("__RESEARCH_FOCUS__", load_research_focus())

QUOTA_SIGNATURES = (
    "rate_limit", "rate-limit", "ratelimit",
    "quota", "usage limit",
    "reached your usage", "session limit",
    "5-hour", "session window",
    "429",
)


def call_claude(prompt: str, timeout: int = 300) -> str:
    # stdin for prompt (hides from ps/top); --tools "" disables all tool use
    # so the LLM cannot autonomously call MCP / Bash / Telegram / etc.
    try:
        res = subprocess.run(
            ["claude", "-p", "--tools", ""],
            input=prompt,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # A timeout is a transient failure, not a quiet day. Bail to exit 42 so
        # the wrapper shows the ⚠️ banner and retries on the next cron tick.
        print(f"__QUOTA_EXHAUSTED__ [timeout after {timeout}s]", file=sys.stderr)
        sys.exit(42)
    if res.returncode != 0:
        # ANY hard claude failure (non-zero exit) must NOT silently produce 0
        # passed-through titles — that ships a fake-empty digest that looks
        # exactly like a genuinely quiet day. The weekly-limit case in
        # particular exits 1 with EMPTY stderr and matches no QUOTA_SIGNATURE
        # (a real silent failure seen in production), so returncode != 0 — not the
        # signature list — is the reliable trigger. Treat every hard failure as
        # quota/transient → exit 42, and let the next cron retry.
        msg = (res.stderr + res.stdout).lower()
        detail = (res.stderr.strip() or res.stdout.strip() or "(empty output)")[:300]
        kind = "quota signal" if any(s in msg for s in QUOTA_SIGNATURES) else "hard failure"
        print(f"__QUOTA_EXHAUSTED__ [{kind}] claude exit {res.returncode}: {detail!r}",
              file=sys.stderr)
        sys.exit(42)
    return res.stdout


def extract_json_array(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON array found: {text[:300]!r}")
    return json.loads(m.group(0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=200,
                    help="Hard cap on titles per call (split into chunks if more)")
    ap.add_argument("--keep", default="yes,maybe",
                    help="Comma-separated labels to pass through (default yes,maybe)")
    ap.add_argument("--emit-labels", action="store_true",
                    help="Add a 'triage_label' field to passed-through records")
    args = ap.parse_args()
    keep_labels = {s.strip().lower() for s in args.keep.split(",")}

    candidates = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            candidates.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[triage] bad input line skipped: {line[:120]!r}", file=sys.stderr)
    if not candidates:
        print("[triage] no candidates on stdin; nothing to do", file=sys.stderr)
        return

    print(f"[triage] {len(candidates)} candidates → batching titles in chunks "
          f"of {args.max}", file=sys.stderr)

    labels: dict[int, str] = {}
    for chunk_start in range(0, len(candidates), args.max):
        chunk = candidates[chunk_start:chunk_start + args.max]
        title_block = "\n".join(
            f"#{chunk_start + i}: {r.get('title', '').strip()[:300]}"
            for i, r in enumerate(chunk)
        )
        prompt = SYSTEM_PROMPT + "\n\n## 标题列表：\n\n" + title_block + \
                 f"\n\n## 输出：\n请输出长度恰好为 {len(chunk)} 的 JSON 数组。"
        raw = call_claude(prompt)
        try:
            arr = extract_json_array(raw)
        except Exception as e:
            print(f"[triage] chunk {chunk_start} parse failed ({e}); "
                  f"defaulting to 'maybe' for all", file=sys.stderr)
            for j, r in enumerate(chunk):
                labels[chunk_start + j] = "maybe"
            continue
        for item in arr:
            try:
                idx = int(item.get("i", -1))
                lab = str(item.get("label", "maybe")).strip().lower()
                if lab not in {"yes", "maybe", "no"}:
                    lab = "maybe"
                labels[idx] = lab
            except (ValueError, TypeError):
                continue
        # Anything missing in the response → default 'maybe'.
        for j in range(len(chunk)):
            labels.setdefault(chunk_start + j, "maybe")

    counts = {"yes": 0, "maybe": 0, "no": 0}
    n_passed = 0
    for i, rec in enumerate(candidates):
        lab = labels.get(i, "maybe")
        counts[lab] = counts.get(lab, 0) + 1
        if lab in keep_labels:
            if args.emit_labels:
                rec = {**rec, "triage_label": lab}
            print(json.dumps(rec, ensure_ascii=False))
            n_passed += 1

    print(f"[triage] labels: yes={counts['yes']} maybe={counts['maybe']} "
          f"no={counts['no']} | passed_through={n_passed}", file=sys.stderr)


if __name__ == "__main__":
    main()
