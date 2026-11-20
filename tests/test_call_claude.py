"""Offline test of llm_judge.call_claude's failure classification (no LLM is
called): subprocess.run is faked with stream-json shaped like the real CLI
output. The init event lists a skill whose name contains "quota" and every
run has a rate_limit_event, as in real runs, so a raw substring search would
mislabel every failure as quota."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # repo: templates/*, deployed project: scripts/
for _d in ("templates/scripts", "templates/discord", "scripts"):
    if (ROOT / _d).is_dir():
        sys.path.insert(0, str(ROOT / _d))
import llm_judge  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"
INIT = {"type": "system", "subtype": "init", "session_id": SID,
        "skills": ["disk-quota-check", "web-search"], "tools": []}
RL_OK = {"type": "rate_limit_event", "session_id": SID,
         "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}}
RL_REJ = {"type": "rate_limit_event", "session_id": SID,
          "rate_limit_info": {"status": "rejected", "rateLimitType": "seven_day"}}
API_ERR = ("API Error: Opus 5.5's safeguards flagged this session "
           "(https://www.anthropic.com/legal/aup). You may be seeing this for the "
           "first time: ...")
SYN = {"type": "assistant", "session_id": SID,
       "message": {"model": "<synthetic>", "role": "assistant", "stop_reason": "refusal",
                   "stop_details": {"type": "refusal", "category": "bio"},
                   "content": [{"type": "text", "text": API_ERR}]}}
PARTIAL = ('[{"i":0,"score":2,"summary_zh":"甲","commentary_zh":"乙"},'
           '{"i":1,"score":4,"summary_zh":"本文研究了')
CUT = {"type": "assistant", "session_id": SID,
       "message": {"role": "assistant", "stop_reason": "refusal",
                   "content": [{"type": "text", "text": PARTIAL}]}}
OKTXT = '[{"i":0,"score":1,"summary_zh":"a","commentary_zh":"b"}]'
OKMSG = {"type": "assistant", "session_id": SID,
         "message": {"content": [{"type": "text", "text": OKTXT}]}}
TX_REFUSAL = [  # transcript rows of a call refused twice while thinking
    {"type": "assistant", "message": {"model": "claude-opus-5-5", "stop_reason": "refusal",
                                      "content": [{"type": "thinking", "thinking": ""}]}},
    {"type": "system", "subtype": "model_refusal_no_fallback", "apiRefusalCategory": "bio"},
]


def result(text, is_error):
    return {"type": "result", "subtype": "success", "is_error": is_error,
            "result": text, "session_id": SID}


def J(*evs):
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evs)


def tx_root_with_refusal():
    root = Path(tempfile.mkdtemp())
    (root / "proj").mkdir()
    (root / "proj" / f"{SID}.jsonl").write_text(J(*TX_REFUSAL))
    return root


CASES = [  # name, rc, stdout, stderr, transcript root, expect, substring
    ("refusal_stream", 1, J(INIT, RL_OK, SYN, result(API_ERR, True)), "", None,
     "ClassifierRefusal", "exit 1 [classifier refusal: bio]: API Error"),
    ("refusal_text_only", 1, J(INIT, RL_OK, result(API_ERR, True)), "", None,
     "ClassifierRefusal", "exit 1 [classifier refusal]: API Error"),
    ("refusal_transcript", 1, J(INIT, RL_OK), "", "refusal",
     "ClassifierRefusal", "exit 1 [classifier refusal: bio]"),
    ("quota_result", 1, J(INIT, RL_REJ, result("Claude AI usage limit reached|1795392000", True)),
     "", None, "QuotaExhausted", "exit 1 [quota signal]: Claude AI usage limit"),
    ("quota_rl_only", 1, J(INIT, RL_REJ), "", None,
     "QuotaExhausted", "exit 1 [quota signal]: rate_limit rejected (seven_day)"),
    ("hard_empty", 1, "", "", None, "QuotaExhausted", "exit 1 [hard failure]: (empty output)"),
    ("init_only", 1, J(INIT, RL_OK), "", None, "QuotaExhausted", "exit 1 [hard failure]"),
    ("oauth_stderr", 1, "", "OAuth session expired", None,
     "QuotaExhausted", "[hard failure]: OAuth session expired"),
    ("plain_stdout", 1, "Credit balance is too low\n", "", None,
     "QuotaExhausted", "[hard failure]: Credit balance is too low"),
    ("ok", 0, J(INIT, RL_OK, OKMSG, result(OKTXT, False)), "", None, "ok", OKTXT),
    ("ok_text_mode", 0, OKTXT + "\n", "", None, "ok", OKTXT),
    ("rc0_is_error_refusal", 0, J(INIT, result(API_ERR, True)), "", None,
     "ClassifierRefusal", "result is_error [classifier refusal]"),
    ("rc0_is_error_other", 0, J(INIT, result("Overloaded", True)), "", None,
     "QuotaExhausted", "result is_error [hard failure]: Overloaded"),
]

fails = 0
for name, rc, out, err, tx, expect, sub in CASES:
    llm_judge.TRANSCRIPT_ROOT = tx_root_with_refusal() if tx else Path(tempfile.mkdtemp())
    llm_judge.subprocess.run = (lambda *a, _rc=rc, _o=out, _e=err, **k:
                                subprocess.CompletedProcess(a, _rc, _o, _e))
    try:
        got, msg = "ok", llm_judge.call_claude("prompt")
    except Exception as e:  # noqa: BLE001
        got, msg = type(e).__name__, str(e)
    good = got == expect and sub in msg
    fails += not good
    print(f"{'PASS' if good else 'FAIL'} {name:21s} -> {got}: {msg[:90]!r}")

# A refusal AFTER part of the array was written -> judge_batch raises
# PartialReply (reuse #0, suspect #1).
llm_judge.TRANSCRIPT_ROOT = Path(tempfile.mkdtemp())
llm_judge.subprocess.run = (lambda *a, **k: subprocess.CompletedProcess(
    a, 1, J(INIT, RL_OK, CUT, SYN, result(API_ERR, True)), ""))
recs = [{"doi": f"10.0/{i}", "title": f"paper {i}", "abstract": "a"} for i in range(4)]
try:
    llm_judge.judge_batch(recs, 1)
    got = "no exception"
except llm_judge.PartialReply as pr:
    got = f"PartialReply items={len(pr.items)} cut_at={pr.cut_at}"
except Exception as e:  # noqa: BLE001
    got = f"{type(e).__name__}: {e}"
good = got == "PartialReply items=1 cut_at=1"
fails += not good
print(f"{'PASS' if good else 'FAIL'} {'refusal_after_partial':21s} -> {got}")
# Exit 0, but the model was stopped at the start and then only apologised:
# nothing reusable -> judge_batch raises ClassifierRefusal (not ValueError),
# so the rescue rung can fire for it.
STOP = {"type": "assistant", "session_id": SID,
        "message": {"role": "assistant", "stop_reason": "refusal",
                    "content": [{"type": "text", "text": '[{"'}]}}
SORRY = ("My previous answer was cut off by a safety check, so I won't "
         "regenerate it.")
SORRY_MSG = {"type": "assistant", "session_id": SID,
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": SORRY}]}}
llm_judge.subprocess.run = (lambda *a, **k: subprocess.CompletedProcess(
    a, 0, J(INIT, RL_OK, STOP, SORRY_MSG, result(SORRY, False)), ""))
try:
    llm_judge.judge_batch(recs, 1)
    got = "no exception"
except Exception as e:  # noqa: BLE001
    got = type(e).__name__
good = got == "ClassifierRefusal"
fails += not good
print(f"{'PASS' if good else 'FAIL'} {'apology_after_stop':21s} -> {got}")
llm_judge.subprocess.run = (lambda *a, **k: subprocess.CompletedProcess(
    a, 1, J(INIT, RL_OK, CUT, SYN, result(API_ERR, True)), ""))
# A refused score-only call -> ClassifierRefusal propagates (bisection handles it).
try:
    llm_judge.judge_batch(recs, 1, score_only=True)
    got = "no exception"
except Exception as e:  # noqa: BLE001
    got = type(e).__name__
good = got == "ClassifierRefusal"
fails += not good
print(f"{'PASS' if good else 'FAIL'} {'score_only_refused':21s} -> {got}")
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
