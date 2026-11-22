"""Offline test of triage_titles.py failure handling (no LLM is called):
subprocess.run is faked. A safety-classifier refusal must NOT exit 42 (false
额度耗尽 banner, run stops at Stage 2, likely again next cron): the refused
chunk's titles pass through as 'maybe' so the judge scores them, with no
retry call. Quota / auth failures keep the exit-42 path."""
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # repo: templates/*, deployed project: scripts/
for _d in ("templates/scripts", "templates/discord", "scripts"):
    if (ROOT / _d).is_dir():
        sys.path.insert(0, str(ROOT / _d))
import triage_titles  # noqa: E402

API_ERR = ("API Error: Opus 5.5's safeguards flagged this session "
           "(https://www.anthropic.com/legal/aup). You may be seeing this for the "
           "first time: ...")
WEEKLY = "You've hit your weekly limit · resets 1am (America/New_York)"
OAUTH = "Failed to authenticate: OAuth session expired and could not be refreshed"
RECS = [{"doi": f"10.0000/t{i}", "title": f"title {i}"} for i in range(3)]


def run(replies, argv=()):
    """replies: list of (returncode, stdout, stderr), one per claude call.
    Returns (exit code, passed-through dois, stderr text, number of calls)."""
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        rc, out, err = replies[len(calls) - 1]
        return subprocess.CompletedProcess(cmd, rc, out, err)

    triage_titles.subprocess.run = fake
    sys.argv = ["triage_titles.py", "--keep", "yes,maybe", *argv]
    sys.stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in RECS))
    out, err, code = io.StringIO(), io.StringIO(), 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            triage_titles.main()
        except SystemExit as e:
            code = e.code or 0
    dois = [json.loads(l)["doi"] for l in out.getvalue().splitlines() if l.strip()]
    return code, dois, err.getvalue(), len(calls)


ALL = [r["doi"] for r in RECS]
LABELS = '[{"i":0,"label":"yes"},{"i":1,"label":"no"},{"i":2,"label":"maybe"}]'
CASES = [
    # name, replies, argv, expected (exit, dois, calls), must-have stderr text
    ("refusal_stdout", [(1, API_ERR, "")], (), (0, ALL, 1), "classifier refusal"),
    ("refusal_stderr", [(1, "", API_ERR)], (), (0, ALL, 1), "classifier refusal"),
    ("weekly_limit", [(1, WEEKLY, "")], (), (42, [], 1), "__QUOTA_EXHAUSTED__"),
    ("oauth", [(1, "", OAUTH)], (), (42, [], 1), "__QUOTA_EXHAUSTED__"),
    ("ok_labels", [(0, LABELS, "")], (), (0, [ALL[0], ALL[2]], 1), "passed_through=2"),
    ("apology_exit0", [(0, "I'm sorry, I can't help with that.", "")], (),
     (0, ALL, 1), "parse failed"),
    # 2 chunks: the first is refused (no retry), the second is labelled.
    ("refused_chunk_of_two", [(1, API_ERR, ""), (0, '[{"i":2,"label":"no"}]', "")],
     ("--max", "2"), (0, ALL[:2], 2), "classifier refusal"),
]

fails = 0
real_stdin = sys.stdin
for name, replies, argv, want, must in CASES:
    code, dois, err, n = run(replies, argv)
    got = (code, dois, n)
    good = got == want and must in err
    if name.startswith("refus"):  # a refusal never prints the quota marker
        good = good and "__QUOTA_EXHAUSTED__" not in err
    fails += not good
    print(f"{'PASS' if good else 'FAIL'} {name:21s} -> exit {code}, "
          f"passed {len(dois)}, calls {n}" + ("" if good else f" (want {want}; stderr {err[-200:]!r})"))
sys.stdin = real_stdin
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
