"""Offline test of llm_judge.py's failed-batch recovery (no LLM is called).

call_claude is replaced by a fake that also fills llm_judge.LAST_CALL the way
the real stream-json parser does. 4 papers, true scores 2, 4, 1, 3
(min_score 3). Each scenario runs in its own process; with no argument this
file runs them all and compares calls / scores / exit code with EXPECTED.

  ok          : nothing fails                                  -> 1 call
  stopped     : full batch answered with an apology, no partial -> score-only ladder
  hitfail     : as stopped + the full call on the hits fails   -> hits keep score-only
  input       : any call containing paper 2 fails              -> bisect, paper 2 ERR
  missing     : full batch omits index 1                       -> SO1 + FULL1
  quota       : score-only recovery hits quota                 -> exit 42
  cut         : classifier stop while writing #1 (partial in the stream texts)
                -> reuse #0, suspect #1 score-only, redo #2,#3 in ONE call
  cut_tx      : same, but the partial is only in the session transcript
  cut_twice   : the redo call is cut again while writing its #0 (= paper 2)
  cut_suspect : as cut, and the suspect's score-only call fails too -> ERR
  refuse      : CLI gives up with a classifier refusal on any call with paper 2
  refuse_once : only the first call is refused
  refuse_cut  : refused after #0 was written -> partial reuse, as in cut
"""
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # repo: templates/*, deployed project: scripts/
for _d in ("templates/scripts", "templates/discord", "scripts"):
    if (ROOT / _d).is_dir():
        sys.path.insert(0, str(ROOT / _d))

EXPECTED = {  # scenario: (exit code, calls, emitted scores)
    "ok": (0, ["FULL4"], ["2", "4", "1", "3"]),
    "stopped": (0, ["FULL4", "SO4", "FULL2"], ["2(so)", "4", "1(so)", "3"]),
    "hitfail": (0, ["FULL4", "SO4", "FULL2"], ["2(so)", "4(so)", "1(so)", "3(so)"]),
    "input": (0, ["FULL4", "SO4", "SO2", "SO2", "SO1", "SO1", "FULL2"],
              ["2(so)", "4", "ERR", "3"]),
    "missing": (0, ["FULL4", "SO1", "FULL1"], ["2", "4", "1", "3"]),
    "quota": (42, ["FULL4", "SO4"], []),
    "cut": (0, ["FULL4", "FULL2", "SO1"], ["2", "4(so)", "1", "3"]),
    "cut_tx": (0, ["FULL4", "FULL2", "SO1"], ["2", "4(so)", "1", "3"]),
    "cut_twice": (0, ["FULL4", "FULL2", "FULL1", "SO1", "SO1"], ["2", "4(so)", "1(so)", "3"]),
    "cut_suspect": (0, ["FULL4", "FULL2", "SO1"], ["2", "ERR", "1", "3"]),
    "refuse": (0, ["FULL4", "SO4", "SO2", "SO2", "SO1", "SO1", "FULL2"],
               ["2(so)", "4", "ERR", "3"]),
    "refuse_once": (0, ["FULL4", "SO4", "FULL2"], ["2(so)", "4", "1(so)", "3"]),
    "refuse_cut": (0, ["FULL4", "FULL2", "SO1"], ["2", "4(so)", "1", "3"]),
}


def run_scenario(scenario):
    import llm_judge

    scores = {"paper 0": 2, "paper 1": 4, "paper 2": 1, "paper 3": 3}
    apology = ("My previous response was stopped, so the scoring output for this "
               "batch is incomplete and I won't regenerate it here.")
    log, ncalls = [], {"n": 0}
    tx_root = Path(tempfile.mkdtemp())
    llm_judge.TRANSCRIPT_ROOT = tx_root

    def item(i, t, so):
        it = {"i": i, "score": scores[t]}
        if not so:
            it.update(summary_zh="总结", commentary_zh="点评")
        return it

    def cut_reply(titles, cut_title):
        """Partial array: complete items before cut_title, then a cut-off one."""
        parts = []
        for i, t in enumerate(titles):
            if t == cut_title:
                parts.append('{"i":%d,"score":%d,"summary_zh":"本文研究了' % (i, scores[t]))
                break
            parts.append(json.dumps(item(i, t, False), ensure_ascii=False))
        return "[" + ",".join(parts)

    def fake_call(prompt, timeout=240):
        ncalls["n"] += 1
        llm_judge.LAST_CALL.clear()
        sid = f"sess{ncalls['n']}"
        llm_judge.LAST_CALL["session_id"] = sid
        titles = re.findall(r"TITLE: (.*)", prompt)
        so = "仅打分" in prompt
        log.append(("SO" if so else "FULL") + str(len(titles)))

        def reply(final, partial=None, tx=False):
            texts = []
            if partial and not tx:
                texts.append(partial)
            if partial and tx:
                d = tx_root / "proj"
                d.mkdir(exist_ok=True)
                row = {"type": "assistant", "message": {"stop_reason": "refusal",
                       "content": [{"type": "text", "text": partial}]}}
                (d / f"{sid}.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n")
            texts.append(final)
            llm_judge.LAST_CALL["assistant_texts"] = texts
            llm_judge.LAST_CALL["final"] = final
            return final

        def refuse(partial=None):  # CLI gave up: exit 1 after a classifier refusal
            llm_judge.LAST_CALL["assistant_texts"] = [partial] if partial else []
            llm_judge.LAST_CALL["final"] = "API Error: safeguards flagged this session"
            raise llm_judge.ClassifierRefusal(
                "claude exit 1 [classifier refusal: bio]: API Error: safeguards flagged")

        if scenario == "refuse" and "paper 2" in titles:
            refuse()
        if scenario == "refuse_once" and ncalls["n"] == 1:
            refuse()
        if scenario == "refuse_cut" and not so and "paper 1" in titles:
            refuse(cut_reply(titles, "paper 1"))
        if scenario == "input" and "paper 2" in titles:
            return reply(apology)
        if scenario.startswith("cut") and not so and "paper 1" in titles:
            return reply(apology, cut_reply(titles, "paper 1"), tx=(scenario == "cut_tx"))
        if scenario == "cut_twice" and not so and titles and titles[0] == "paper 2":
            return reply(apology, cut_reply(titles, "paper 2"))
        if scenario == "cut_suspect" and so and titles == ["paper 1"]:
            return reply(apology)
        if not so:
            if scenario in ("stopped", "hitfail", "quota") and len(titles) == 4:
                return reply(apology)
            if scenario == "hitfail" and len(titles) < 4:
                return reply(apology)
        if so and scenario == "quota":
            raise llm_judge.QuotaExhausted("claude exit 1 [quota signal]: usage limit")
        items = [item(i, t, so) for i, t in enumerate(titles)
                 if not (scenario == "missing" and not so and len(titles) == 4 and i == 1)]
        return reply(json.dumps(items, ensure_ascii=False))

    llm_judge.call_claude = fake_call
    recs = [{"doi": f"10.0/{i}", "title": f"paper {i}", "abstract": "a",
             "category": "genomics"} for i in range(4)]
    sys.stdin = io.StringIO("\n".join(json.dumps(r) for r in recs) + "\n")
    sys.argv = ["llm_judge.py", "--batch", "8", "--keep-all"]
    out, real_stdout, code = io.StringIO(), sys.stdout, 0
    sys.stdout = out
    try:
        llm_judge.main()
    except SystemExit as e:
        code = e.code or 0
    finally:
        sys.stdout = real_stdout
    rows = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    def tag(r):
        if "judge_error" in r:
            return "ERR"
        return f"{r['score']}{'(so)' if r.get('judge_mode') == 'score_only' else ''}"

    print(json.dumps({"exit": code, "calls": log, "emitted": [tag(r) for r in rows]}))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_scenario(sys.argv[1])
        sys.exit(0)
    fails = 0
    for name, (code, calls, emitted) in EXPECTED.items():
        res = subprocess.run([sys.executable, __file__, name],
                             capture_output=True, text=True)
        try:
            got = json.loads(res.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            got = {"error": res.stderr.strip()[-300:]}
        ok = got == {"exit": code, "calls": calls, "emitted": emitted}
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name:12s} calls={got.get('calls')} "
              f"emitted={got.get('emitted')} exit={got.get('exit')}"
              + ("" if ok else f"  expected calls={calls} emitted={emitted} exit={code}"))
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
