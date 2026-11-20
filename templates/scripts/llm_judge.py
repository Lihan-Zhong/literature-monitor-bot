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

Safety-classifier refusals are NOT quota: call_claude raises
ClassifierRefusal and the batch goes through the failed-batch recovery
ladder (reuse a cut-off reply, score-only retry, bisection).
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


class PartialReply(Exception):
    """The reply was cut off mid-array (e.g. a safety classifier stopped it).
    `items` = the complete JSON objects written before the cut,
    `cut_at` = the "i" being written when it stopped (the suspect paper)."""

    def __init__(self, items: list, cut_at, cause: Exception):
        super().__init__(f"partial reply: {len(items)} complete item(s), "
                         f"cut at #{cut_at} ({type(cause).__name__})")
        self.items, self.cut_at = items, cut_at


class ClassifierRefusal(Exception):
    """claude -p was stopped by a safety classifier and gave up. Seen in
    production: the model was stopped while still thinking, the CLI retried
    once ("continuing once with that noted"), was stopped again, and exited
    1 with "API Error: ... safeguards flagged this session" (category "bio")
    on ordinary biology abstracts, i.e. a false positive. Counted as quota,
    it would skip the recovery ladder and report a false quota failure.
    Not quota: the caller treats it as a failed batch."""


# Texts the CLI uses when a classifier refuses (checked only on failed calls).
REFUSAL_MARKERS = (
    "safeguards flagged", "safeguards stopped",
    "stopped by a safety classifier", "anthropic.com/legal/aup",
)

# Backup model for a turn the main model (ANTHROPIC_MODEL) refuses or cannot
# serve: the CLI re-answers inside the same call, so no extra call of ours
# (a refused call with none configured logs "model_refusal_no_fallback").
# Cards the backup model scored are labelled (judge_model). Set
# LIT_JUDGE_FALLBACK_MODEL="" to disable.
FALLBACK_MODEL = os.environ.get("LIT_JUDGE_FALLBACK_MODEL", "sonnet")


# Diagnostics of the most recent call_claude(): session id, every assistant
# text in order, the final result. When the final text is not the JSON array
# (classifier stop -> the CLI asks the model to continue -> it declines), the
# cut-off array is in an EARLIER assistant message (stream-json) and in the
# session transcript (stop_reason "refusal").
LAST_CALL: dict = {}
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"


def call_claude(prompt: str, timeout: int = 240) -> str:
    # Pass prompt via stdin so it doesn't show in `ps` / `top -c`.
    # `--tools ""` disables ALL tool use (no MCP, no Read/Edit/Bash) so the
    # LLM can ONLY respond with text — preventing it from autonomously
    # invoking the user's Telegram MCP or any other side-effect tool.
    # NOTE: no `--bare` here: it broke OAuth / setup-token auth in production.
    # stream-json (+ --verbose, required with -p) so a cut-off reply can be
    # recovered from the intermediate assistant messages; the returned value
    # is still the final result text, as before.
    LAST_CALL.clear()
    cmd = ["claude", "-p", "--tools", "", "--output-format", "stream-json", "--verbose"]
    if FALLBACK_MODEL:
        cmd += ["--fallback-model", FALLBACK_MODEL]
    try:
        res = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise QuotaExhausted(f"claude timeout after {timeout}s (transient)")

    # Parse the stream first, also for a failed call: the session id, the
    # assistant texts (a cut-off array may be among them) and any refusal
    # event are needed to tell a classifier refusal from quota.
    texts, final, saw_result, is_error = [], "", False, False
    refusal, limit_note, plain = None, "", []
    for line in res.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            if line.strip():
                plain.append(line.strip())
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("session_id"):
            LAST_CALL["session_id"] = ev["session_id"]
        cat = _refusal_category(ev)
        if cat is not None and not refusal:  # keep looking for a named category
            refusal = cat
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            LAST_CALL["main_model"] = ev.get("model") or ""
        if ev.get("type") == "assistant":
            msg = ev.get("message") or {}
            content = msg.get("content") or []
            text = "".join(c.get("text", "") for c in content
                           if isinstance(c, dict) and c.get("type") == "text")
            if text:
                texts.append(text)
                if msg.get("model") and msg.get("model") != "<synthetic>":
                    LAST_CALL["answer_model"] = msg["model"]
        elif ev.get("type") == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                limit_note = (f"rate_limit {info.get('status')} "
                              f"({info.get('rateLimitType', '?')})")
        elif ev.get("type") == "result":
            saw_result = True
            final = ev.get("result") or ""
            is_error = bool(ev.get("is_error"))
    LAST_CALL["assistant_texts"] = texts
    if not saw_result:  # not stream-json after all -> behave like the old text mode
        final = res.stdout
    LAST_CALL["final"] = final
    main_m, ans_m = LAST_CALL.get("main_model", ""), LAST_CALL.get("answer_model", "")
    if main_m and ans_m and ans_m != main_m:
        LAST_CALL["fallback_used"] = ans_m
        print(f"[llm_judge] fallback model {ans_m} answered "
              f"({main_m} refused or unavailable)", file=sys.stderr)
    if res.returncode == 0 and not is_error:
        return final

    # Failed call. Judge it only by what the CLI *said* (stderr, result text,
    # assistant texts, non-JSON lines, a non-"allowed" rate-limit status) —
    # never by the raw stream-json: its init event lists tool/skill names
    # (any of which may contain "quota") and every run has a
    # rate_limit_event, so a substring search there flags "quota" on ANY
    # failure.
    result_text = final.strip() if saw_result else ""
    said = "\n".join([res.stderr.strip(), result_text, *texts, *plain, limit_note])
    if refusal is None and any(m in said.lower() for m in REFUSAL_MARKERS):
        refusal = ""
    if refusal is None:
        refusal = _refusal_in_transcript(LAST_CALL.get("session_id"))
    detail = (res.stderr.strip() or result_text or (texts[-1].strip() if texts else "")
              or " | ".join(plain) or limit_note or "(empty output)")[:300]
    where = f"exit {res.returncode}" if res.returncode != 0 else "result is_error"
    if refusal is not None:
        raise ClassifierRefusal(f"claude {where} [classifier refusal"
                                f"{': ' + refusal if refusal else ''}]: {detail}")
    # ANY other hard claude failure → QuotaExhausted, so main() sets quota_hit and
    # exits 42 (⚠️ banner + retry next cron) instead of the failed-batch
    # recovery below, whose retries would all hit the same wall. The
    # weekly-limit case exits 1 with EMPTY stderr and matches no
    # QUOTA_SIGNATURE (a real silent failure seen in production), so
    # returncode != 0 — not the signature list — is the reliable trigger.
    kind = "quota signal" if any(s in said.lower() for s in QUOTA_SIGNATURES) else "hard failure"
    raise QuotaExhausted(f"claude {where} [{kind}]: {detail}")


def _refusal_category(ev):
    """For a stream-json event or a transcript row: the refusal category
    ("bio", ..., or "" when unnamed) if it records a safety-classifier
    refusal, else None. Shapes as seen in real CLI transcripts."""
    if not isinstance(ev, dict):
        return None
    if ev.get("type") == "system" and str(ev.get("subtype", "")).startswith("model_refusal"):
        return str(ev.get("apiRefusalCategory") or "")
    msg = ev.get("message")
    if (ev.get("type") == "assistant" and isinstance(msg, dict)
            and msg.get("stop_reason") == "refusal"):
        return str((msg.get("stop_details") or {}).get("category") or "")
    return None


def _refusal_in_transcript(sid):
    """Refusal category recorded in the session transcript of `sid` ("" if
    the refusal rows name none), or None when there is no refusal."""
    if not sid:
        return None
    found = None
    for path in TRANSCRIPT_ROOT.glob(f"*/{sid}.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                cat = _refusal_category(json.loads(line))
            except ValueError:
                continue
            if cat:
                return cat
            if cat is not None:
                found = ""
    return found


def _parse_partial_array(text: str):
    """(complete_items, cut_at) from a JSON array cut off part-way, or
    (None, None) when `text` does not look like one."""
    t = re.sub(r"^```(?:json)?\s*", "", (text or "").strip())
    start = t.find("[")
    if start < 0 or '"i"' not in t[start:]:
        return None, None
    dec, pos, items = json.JSONDecoder(), start + 1, []
    while True:
        while pos < len(t) and t[pos] in " \r\n\t,":
            pos += 1
        if pos >= len(t) or t[pos] == "]":
            break
        try:
            obj, pos = dec.raw_decode(t, pos)
        except ValueError:
            break
        if isinstance(obj, dict):
            items.append(obj)
    m = re.search(r'\{\s*"i"\s*:\s*(\d+)', t[pos:])
    return items, (int(m.group(1)) if m else None)


def find_partial_reply():
    """Look for the cut-off JSON array of the last call: first in the
    intermediate assistant messages, then in the session transcript
    (messages with stop_reason "refusal"). Returns (items, cut_at) or
    (None, None)."""
    final = LAST_CALL.get("final", "")
    candidates = [t for t in LAST_CALL.get("assistant_texts", []) if t != final]
    sid = LAST_CALL.get("session_id")
    if sid:
        for path in TRANSCRIPT_ROOT.glob(f"*/{sid}.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    msg = row.get("message") or {}
                    if row.get("type") == "assistant" and msg.get("stop_reason") == "refusal":
                        candidates.append("".join(
                            c.get("text", "") for c in msg.get("content") or []
                            if isinstance(c, dict) and c.get("type") == "text"))
            except (OSError, ValueError):
                continue
    best = (None, None)
    for text in candidates:
        items, cut_at = _parse_partial_array(text)
        if items is None or (not items and cut_at is None):
            continue
        if best[0] is None or len(items) > len(best[0]):
            best = (items, cut_at)
    return best


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


# Last-resort mode for a paper whose full judgement keeps failing. Seen in
# production: a safety classifier stopped the reply partway through one
# paper's Chinese summary, and the model then declined to regenerate. Asking
# for the score alone keeps the reply to a few tokens, which leaves far less
# output for a classifier to trip on.
SCORE_ONLY_SUFFIX = (
    "\n\n## 本次为「仅打分」模式（完整输出已失败，降级重试）：\n"
    "JSON 数组的每个元素只含 i 和 score 两个字段，"
    "不要 summary_zh / commentary_zh，不要任何其他文字。"
)


def judge_batch(records: list, batch_idx, score_only: bool = False) -> list:
    """Judge a batch of papers in one LLM call. Returns enriched copies of
    the input records (same length, same order). score_only=True asks for
    {"i","score"} only (summary/commentary come back empty)."""
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
    if score_only:
        prompt += SCORE_ONLY_SUFFIX
    try:
        raw = call_claude(prompt)
        arr = extract_json_array(raw)
    except (ValueError, json.JSONDecodeError, ClassifierRefusal) as e:
        # A refusal may come after part of the array was written: reuse it.
        if not score_only:  # score-only failures go to bisection instead
            items, cut_at = find_partial_reply()
            if items is not None:
                raise PartialReply(items, cut_at, e) from e
        raise
    out = enrich(records, arr, batch_idx, score_only)
    fb = LAST_CALL.get("fallback_used")
    if fb:  # scored by the backup model: say so on the card
        for o in out:
            if "judge_error" not in o:
                o["judge_model"] = fb
    return out


def enrich(records: list, arr: list, batch_idx, score_only: bool = False) -> list:
    """Map the model's JSON items onto the input records (same order);
    a record with no item gets judge_error 'missing index'."""
    by_i = {}
    for item in arr:
        if isinstance(item, dict):
            try:
                by_i[int(item.get("i", -1))] = item
            except (ValueError, TypeError):
                continue

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
        if score_only:
            out[-1]["judge_mode"] = "score_only"
    return out


# ---------------------------------------------------------------------------
# Recovery after a failed batch — token-frugal, NOT one call per paper
# (every `claude -p` call re-sends the whole prompt + CLI overhead, so
# per-paper retries multiply input tokens). Ladder:
#   1. ONE score-only call for all the papers the batch did not score;
#      bisect (halves, recursively) only if that call fails too;
#   2. ONE full call for just the papers that scored >= min_score, to get the
#      summary/commentary their digest card needs; if it fails they keep the
#      score-only result (the card says so). No further retries.
# QuotaExhausted propagates everywhere (caller stops, exits 42).
# ---------------------------------------------------------------------------
def score_only_isolate(recs: list, batch_idx) -> list:
    """Score-only judging of `recs` in one call; bisect on failure. A single
    paper that still fails comes back with judge_error. Same order as recs."""
    err = ""
    try:
        out = judge_batch(recs, batch_idx, score_only=True)
    except QuotaExhausted:
        raise
    except Exception as e:
        out, err = None, f"{type(e).__name__}: {str(e)[:120]}"
    if out is not None:
        bad = [k for k, o in enumerate(out) if "judge_error" in o]
        if not bad:
            return out
        if len(bad) < len(recs):  # re-do only the missing ones (strictly fewer)
            redo = score_only_isolate([recs[k] for k in bad], batch_idx)
            for k, o in zip(bad, redo):
                out[k] = o
            return out
        err = "reply had none of the indices"
    if len(recs) == 1:
        print(f"[llm_judge] score-only FAILED for {recs[0].get('doi', '?')} ({err})",
              file=sys.stderr)
        return [{**recs[0], "relevant": False, "score": 0,
                 "judge_error": f"unjudgeable: {err}"[:300]}]
    mid = len(recs) // 2
    print(f"[llm_judge] score-only call on {len(recs)} paper(s) FAILED ({err}); "
          f"bisecting {mid}+{len(recs) - mid}", file=sys.stderr)
    return (score_only_isolate(recs[:mid], batch_idx)
            + score_only_isolate(recs[mid:], batch_idx))


def resolve_partial(recs: list, pr: PartialReply, batch_idx, min_score: int) -> list:
    """Use what the cut-off reply already wrote, treat the paper it was
    writing when stopped as the suspect, and redo the rest in ONE call. The
    suspect gets one score-only call (its summary is what tripped the stop).
    Recursion always excludes the suspect, so it ends."""
    done = enrich(recs, pr.items, batch_idx)
    out = [None if "judge_error" in o else o for o in done]
    if pr.cut_at is not None and 0 <= pr.cut_at < len(recs) and out[pr.cut_at] is None:
        suspect = pr.cut_at
    else:
        suspect = next((k for k, o in enumerate(out) if o is None), None)
    rest = [k for k, o in enumerate(out) if o is None and k != suspect]
    print(f"[llm_judge] cut-off reply (batch {batch_idx}): reused "
          f"{sum(o is not None for o in out)}, suspect "
          f"{recs[suspect].get('doi', '?') if suspect is not None else '-'}, "
          f"redoing {len(rest)} in one call", file=sys.stderr)
    if rest:
        sub = [recs[k] for k in rest]
        try:
            res = judge_batch(sub, batch_idx)
        except QuotaExhausted:
            raise
        except PartialReply as pr2:
            res = resolve_partial(sub, pr2, batch_idx, min_score)
        except Exception as e:
            print(f"[llm_judge] redo call FAILED ({type(e).__name__}: {str(e)[:160]}); "
                  "falling back to score-only recovery", file=sys.stderr)
            res = recover_failed(sub, batch_idx, min_score)
        miss = [j for j, o in enumerate(res) if "judge_error" in o]
        if miss:
            again = recover_failed([sub[j] for j in miss], batch_idx, min_score)
            for j, o in zip(miss, again):
                res[j] = o
        for k, o in zip(rest, res):
            out[k] = o
    if suspect is not None:
        out[suspect] = score_only_isolate([recs[suspect]], batch_idx)[0]
    return out


def recover_failed(recs: list, batch_idx, min_score: int) -> list:
    """Score `recs` (papers a batch call failed to score) with the ladder
    above. Returns records in the same order; judge_error on any paper that
    could not be scored at all."""
    out = score_only_isolate(recs, batch_idx)
    print(f"[llm_judge] score-only recovery: scores="
          f"[{','.join('ERR' if 'judge_error' in o else str(o.get('score')) for o in out)}]",
          file=sys.stderr)
    hits = [k for k, o in enumerate(out)
            if "judge_error" not in o and o.get("score", 0) >= min_score]
    if not hits:
        return out
    try:
        full = judge_batch([recs[k] for k in hits], batch_idx)
    except QuotaExhausted:
        raise
    except Exception as e:
        print(f"[llm_judge] full re-judge of {len(hits)} hit(s) FAILED "
              f"({type(e).__name__}: {str(e)[:160]}); keeping score-only cards",
              file=sys.stderr)
        return out
    for k, o in zip(hits, full):
        if "judge_error" not in o:
            out[k] = o
    print(f"[llm_judge] full re-judge of {len(hits)} hit(s) OK", file=sys.stderr)
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
        except PartialReply as pr:
            # Reply cut off mid-array (classifier stop): reuse what was
            # written, isolate the suspect, redo the rest in one call.
            print(f"[llm_judge] batch {batch_idx} reply cut off ({pr})",
                  file=sys.stderr)
            try:
                judged = resolve_partial(chunk, pr, batch_idx, args.min_score)
            except QuotaExhausted as qe:
                quota_hit = True
                n_quota_skipped += len(chunk)
                print(f"[llm_judge] QUOTA EXHAUSTED during cut-off recovery "
                      f"(batch {batch_idx}): {qe}", file=sys.stderr)
                print("__QUOTA_EXHAUSTED__", file=sys.stderr)
                continue
        except Exception as e:
            # Whole batch failed (unparseable reply, ClassifierRefusal with
            # nothing written yet, etc.). Zero-scoring the chunk here would
            # let the pusher cache those papers as score 0 -> never judged
            # again (a real loss seen in production). Instead:
            # recover_failed() below.
            print(f"[llm_judge] batch {batch_idx} FAILED ({type(e).__name__}: "
                  f"{str(e)[:200]}); recovering with a score-only call",
                  file=sys.stderr)
            judged = [{**r, "relevant": False, "score": 0,
                       "judge_error": f"batch_failure: {type(e).__name__}"}
                      for r in chunk]

        # Recover every paper the batch call did not score (whole-batch
        # failure above, or an index missing from the reply) with the
        # token-frugal ladder. judge_batch keeps input order: judged[k] <-> chunk[k].
        # ("unjudgeable" = already went through recovery; don't pay for it twice)
        failed = [k for k, o in enumerate(judged) if "judge_error" in o
                  and not str(o["judge_error"]).startswith("unjudgeable")]
        drop = set()  # quota-skipped here: not emitted -> retried next cron
        if failed:
            try:
                recovered = recover_failed([chunk[k] for k in failed], batch_idx,
                                           args.min_score)
                for k, o in zip(failed, recovered):
                    judged[k] = o
            except QuotaExhausted as qe:
                quota_hit = True
                drop.update(failed)
                n_quota_skipped += len(failed)
                print(f"[llm_judge] QUOTA EXHAUSTED during recovery "
                      f"(batch {batch_idx}): {qe}", file=sys.stderr)
                print("__QUOTA_EXHAUSTED__", file=sys.stderr)
        judged = [o for k, o in enumerate(judged) if k not in drop]

        dt = time.time() - t0
        for out in judged:
            # judge_error records are still emitted with --keep-all so the
            # digest can name them; push_discord.py keeps them OUT of
            # seen.sqlite so the next cron judges them again.
            if "judge_error" in out:
                n_errored += 1
            else:
                n_judged += 1
            if args.keep_all or (out.get("relevant") and out.get("score", 0) >= args.min_score):
                print(json.dumps(out, ensure_ascii=False))
                n_kept += 1
        scores = ["ERR" if "judge_error" in o else str(o.get("score", "?"))
                  for o in judged]
        print(f"[llm_judge] batch {batch_idx} ({len(chunk)} papers, {dt:.1f}s) "
              f"scores=[{','.join(scores)}]", file=sys.stderr)

    print(f"[llm_judge] candidates={len(candidates)} judged={n_judged} "
          f"kept={n_kept} errored={n_errored} quota_skipped={n_quota_skipped}",
          file=sys.stderr)
    if quota_hit:
        sys.exit(42)


if __name__ == "__main__":
    main()
