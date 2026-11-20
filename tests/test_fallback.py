"""Offline test of the --fallback-model handling (no LLM is called):
 - the flag is on the command line, and absent when FALLBACK_MODEL == ""
 - main model refuses, backup answers in the same call (exit 0): the result is
   returned, fallback_used recorded, judge_batch tags records with judge_model
 - a normal main-model answer gets no tag
 - push_discord.build_embed shows the note only when judge_model is set"""
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
import push_discord  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"
INIT = {"type": "system", "subtype": "init", "session_id": SID, "model": "claude-opus-5-5"}
REFUSED = {"type": "assistant", "session_id": SID,
           "message": {"model": "claude-opus-5-5", "stop_reason": "refusal",
                       "content": [{"type": "thinking", "thinking": ""}]}}
ARR = ('[{"i":0,"score":4,"summary_zh":"甲","commentary_zh":"乙"},'
       '{"i":1,"score":1,"summary_zh":"丙","commentary_zh":"丁"}]')
RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": ARR,
          "session_id": SID}


def answer(model):
    return {"type": "assistant", "session_id": SID,
            "message": {"model": model, "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": ARR}]}}


def J(*evs):
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evs)


seen_cmds = []


def fake(stdout):
    def run(cmd, *a, **k):
        seen_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout, "")
    return run


llm_judge.TRANSCRIPT_ROOT = Path(tempfile.mkdtemp())
llm_judge.FALLBACK_MODEL = "sonnet"
recs = [{"doi": f"10.0/{i}", "title": f"paper {i}", "abstract": "a"} for i in range(2)]
fails = 0


def check(name, ok, info=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name:26s} {info}")


# 1. the backup model answered
llm_judge.subprocess.run = fake(J(INIT, REFUSED, answer("claude-sonnet-5-5"), RESULT))
out = llm_judge.judge_batch(recs, 1)
check("flag on command line", seen_cmds[-1][-2:] == ["--fallback-model", "sonnet"],
      str(seen_cmds[-1][-2:]))
check("fallback_used recorded", llm_judge.LAST_CALL.get("fallback_used") == "claude-sonnet-5-5")
check("records tagged", [o.get("judge_model") for o in out] == ["claude-sonnet-5-5"] * 2,
      str([(o.get("score"), o.get("judge_model")) for o in out]))

# 2. normal main-model answer -> no tag
llm_judge.subprocess.run = fake(J(INIT, answer("claude-opus-5-5"), RESULT))
out = llm_judge.judge_batch(recs, 1)
check("main-model answer untagged", all("judge_model" not in o for o in out)
      and "fallback_used" not in llm_judge.LAST_CALL)

# 3. FALLBACK_MODEL disabled -> no flag
llm_judge.FALLBACK_MODEL = ""
llm_judge.call_claude("x")
check("disabled -> no flag", "--fallback-model" not in seen_cmds[-1], str(seen_cmds[-1]))

# 4. card note
p = {"title": "t", "score": 4, "summary_zh": "s", "category": "genomics"}


def score_field(paper):
    return [f for f in push_discord.build_embed(paper, 1, 1)["fields"]
            if f["name"] == "评分"][0]["value"]


v0 = score_field(p)
v1 = score_field({**p, "judge_model": "claude-sonnet-5-5"})
check("card without judge_model", v0 == "⭐⭐⭐⭐ (4/5)", repr(v0))
check("card with judge_model", v1 == "⭐⭐⭐⭐ (4/5) · Sonnet 备用模型打分", repr(v1))
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
