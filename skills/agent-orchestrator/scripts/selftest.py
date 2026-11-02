#!/usr/bin/env python3
"""Self-test for orch.py and the write-set guard. No agents needed.

Simulates writers with real git worktrees (started from `main`, like Claude
Code's isolation does) and checks: bootstrap, write-set enforcement, wave
integration, conflict handling, test-failure revert, plan editing, the hook.
Usage: python selftest.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.join(HERE, "orch.py")
GUARD = os.path.join(HERE, "hooks", "writeset_guard.py")
fails = []


def sh(cmd, cwd=None, inp=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, input=inp, shell=isinstance(cmd, str))


def orch(*a, cwd=None):
    return sh([sys.executable, ORCH, *a], cwd=cwd)


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"\n     {detail}"))
    if not cond:
        fails.append(name)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(text)


def writer(repo, wts, run, tid, files, msg=None):
    """Simulate a Claude Code worktree agent: starts from main, bootstraps, edits, commits."""
    wt = os.path.join(wts, tid)
    sh(["git", "worktree", "add", "-b", f"agent/{tid}", wt, "main"], cwd=repo)
    b = orch("bootstrap", run, tid, cwd=wt)
    for f, content in files.items():
        write(os.path.join(wt, f), content)
    sh("git add -A && git commit -qm " + (msg or tid), cwd=wt)
    return wt, b


def main():
    tmp = tempfile.mkdtemp()
    repo, wts = os.path.join(tmp, "repo"), os.path.join(tmp, "wts")
    os.makedirs(repo)
    sh("git init -q -b main && git config user.email t@t && git config user.name t", cwd=repo)
    write(os.path.join(repo, "README.md"), "x\n")
    write(os.path.join(repo, "src/shared/cfg.txt"), "line=1\n")
    sh("git add -A && git commit -qm init", cwd=repo)
    run = os.path.join(repo, ".claude/runs/t1")

    r = orch("init", run, "--goal", "selftest", "--test-cmd", "test ! -f BAD", cwd=repo)
    check("init creates integration branch+worktree", r.returncode == 0 and os.path.isdir(run + "/integration"), r.stderr + r.stdout)

    # plan building via add (with validation)
    for a in (["--id", "r1", "--kind", "read", "--title", "scan"],
              ["--id", "s1", "--kind", "synth", "--title", "gate", "--deps", "r1"],
              ["--id", "w1", "--kind", "write", "--title", "auth", "--deps", "s1", "--write-set", "src/auth/**"],
              ["--id", "w2", "--kind", "write", "--title", "api", "--deps", "s1", "--write-set", "src/api/**"],
              ["--id", "w3", "--kind", "write", "--title", "docs", "--deps", "s1", "--write-set", "docs/**"],
              ["--id", "w4", "--kind", "write", "--title", "tests", "--deps", "w1", "--write-set", "tests/**"]):
        r = orch("add", run, *a)
        check(f"add {a[1]}", r.returncode == 0, r.stdout + r.stderr)
    r = orch("add", run, "--id", "wX", "--kind", "write", "--title", "bad", "--deps", "s1", "--write-set", "src/**")
    check("add rejects overlapping concurrent writer", r.returncode == 1 and "overlap" in r.stdout, r.stdout)
    r = orch("remove", run, "s1")
    check("remove refuses when dependents exist", r.returncode != 0)
    r = orch("add", run, "--id", "v1", "--kind", "review", "--title", "review", "--deps", "w1,w2,w3,w4")
    check("validate ok", orch("validate", run).returncode == 0)

    # research + gate
    orch("set", run, "r1", "running"); orch("set", run, "r1", "done", "--summary", "ok")
    orch("set", run, "s1", "running"); orch("set", run, "s1", "done")
    nxt = orch("next", run).stdout
    check("wave 1 = w1,w2,w3 (w4 waits for merge)", all(x in nxt for x in ("w1", "w2", "w3")) and "w4\t" not in nxt, nxt)
    for t in ("w1", "w2", "w3"):
        orch("set", run, t, "running")

    # writers (wave 1)
    wt1, b1 = writer(repo, wts, run, "w1", {"src/auth/a.py": "auth\n"})
    check("bootstrap ok", b1.returncode == 0, b1.stdout + b1.stderr)
    wt2, _ = writer(repo, wts, run, "w2", {"src/api/b.py": "api\n", "docs/stolen.md": "oops\n"})  # violation
    wt3, _ = writer(repo, wts, run, "w3", {"docs/guide.md": "guide\n"})
    for t in ("w1", "w2", "w3"):
        orch("set", run, t, "done", "--branch", f"agent/{t}")
    r = orch("check-writeset", run, "w2")
    check("check-writeset flags violation", r.returncode == 1 and "docs/stolen.md" in r.stdout, r.stdout)
    check("check-writeset passes clean writer", orch("check-writeset", run, "w1").returncode == 0)

    nxt = orch("next", run).stdout
    check("next waits for integration", "not yet merged" in nxt, nxt)
    r = orch("integrate", run)
    check("integrate: w2 rejected, others merged", r.returncode == 1 and "[w2] REJECTED" in r.stdout
          and "[w1] merged" in r.stdout and "[w3] merged" in r.stdout, r.stdout)
    tasks = {t["id"]: t for t in json.load(open(run + "/plan.json"))["tasks"]}
    check("merged flags recorded", tasks["w1"].get("merged") and tasks["w3"].get("merged") and not tasks["w2"].get("merged"))
    check("main tree untouched", not os.path.exists(os.path.join(repo, "src/auth/a.py")))

    # wave 2: w4 starts from integration tip, must see w1's work
    nxt = orch("next", run).stdout
    check("w4 unlocked after w1 merged", "w4\t" in nxt, nxt)
    orch("set", run, "w4", "running")
    wt4, b4 = writer(repo, wts, run, "w4", {"tests/test_a.py": "t\n"})
    check("wave-2 writer sees wave-1 code (bootstrap reset)", os.path.exists(os.path.join(wt4, "src/auth/a.py")), b4.stdout + b4.stderr)

    # guard hook
    def guard(cwd, tool, path):
        return sh([sys.executable, GUARD], inp=json.dumps({"cwd": cwd, "tool_name": tool, "tool_input": {"file_path": path}}))
    check("hook allows in-write-set edit", guard(wt4, "Write", os.path.join(wt4, "tests/new.py")).returncode == 0)
    g = guard(wt4, "Edit", os.path.join(wt4, "src/auth/a.py"))
    check("hook blocks out-of-write-set edit", g.returncode == 2 and "write-set" in g.stderr, g.stderr)
    g = guard(wt4, "Write", os.path.join(repo, "tests/escape.py"))
    check("hook blocks edits in the main checkout", g.returncode == 2, g.stderr)
    check("hook allows run-dir files", guard(wt4, "Write", run + "/results/w4.md").returncode == 0)
    check("hook inert outside orchestrated worktrees", guard(repo, "Write", os.path.join(repo, "README.md")).returncode == 0)
    check("hook ignores non-edit tools", guard(wt4, "Read", "/etc/passwd").returncode == 0)

    # test-failure revert and conflict (force-overlapping writers)
    orch("set", run, "w4", "done", "--branch", "agent/w4")
    r = orch("integrate", run, "w4")
    check("integrate w4", r.returncode == 0, r.stdout)
    for a in (["--id", "w6", "--kind", "write", "--title", "cfg-a", "--deps", "s1", "--write-set", "src/shared/**"],
              ["--id", "w7", "--kind", "write", "--title", "cfg-b", "--deps", "s1", "--write-set", "src/shared/**"],
              ["--id", "w8", "--kind", "write", "--title", "breaks-tests", "--deps", "s1", "--write-set", "BAD"]):
        orch("add", run, *a, "--force")
    for t in ("w6", "w7", "w8"):
        orch("set", run, t, "running")
    writer(repo, wts, run, "w6", {"src/shared/cfg.txt": "line=6\n"})
    writer(repo, wts, run, "w7", {"src/shared/cfg.txt": "line=7\n"})
    writer(repo, wts, run, "w8", {"BAD": "x\n"})
    for t in ("w6", "w7", "w8"):
        orch("set", run, t, "done", "--branch", f"agent/{t}")
    tip_before = sh("git rev-parse orch/t1", cwd=repo).stdout.strip()
    r = orch("integrate", run)
    check("conflict detected and aborted", "[w7] CONFLICT" in r.stdout and "[w6] merged" in r.stdout, r.stdout)
    check("failing tests revert the merge", "[w8] TESTS FAILED" in r.stdout, r.stdout)
    tip_after = sh("git rev-parse orch/t1", cwd=repo).stdout.strip()
    check("integration branch advanced only for good merges", tip_after != tip_before)
    check("BAD file not in integration", not os.path.exists(run + "/integration/BAD"))

    # infrastructure errors: backoff, global pause, exhaustion, resume
    run2 = os.path.join(repo, ".claude/runs/t2")
    r = orch("init", run2, "--goal", "infra", "--transient-wait", "0", "--max-retries", "2",
             "--limit-wait", "3600", "--max-total-wait", "600", "--on-exhausted", "pause", cwd=repo)
    check("init accepts retry-policy flags", r.returncode == 0, r.stdout + r.stderr)
    orch("add", run2, "--id", "r1", "--kind", "read", "--title", "scan")
    orch("add", run2, "--id", "r2", "--kind", "read", "--title", "scan2")
    orch("set", run2, "r1", "running")
    r = orch("error", run2, "r1", "--kind", "transient", "--note", "proxy 502")
    p2 = lambda: json.load(open(run2 + "/plan.json"))
    t1 = {t["id"]: t for t in p2()["tasks"]}["r1"]
    check("transient error -> waiting, attempts untouched", t1["status"] == "waiting" and t1.get("attempts", 0) == 0 and t1["infra_retries"] == 1, r.stdout)
    check("due waiting task is relaunchable", "r1\t" in orch("next", run2).stdout)
    orch("set", run2, "r1", "running")
    orch("error", run2, "r1", "--kind", "transient", "--note", "ECONNRESET")
    orch("set", run2, "r1", "running")
    r = orch("error", run2, "r1", "--kind", "transient", "--note", "timeout")
    check("budget exhausted -> run halted", "EXHAUSTED (pause)" in r.stdout and p2()["halted"], r.stdout)
    check("next reports HALTED", "HALTED" in orch("next", run2).stdout)
    r = orch("resume", run2)
    check("resume clears halt and re-queues", p2()["halted"] is None and "1 task" in r.stdout, r.stdout)
    orch("set", run2, "r1", "running")
    r = orch("error", run2, "r1", "--kind", "rate_limit", "--note", "usage limit")
    n = orch("next", run2).stdout
    check("rate limit pauses the WHOLE run (even r2)", "PAUSED" in n and "r2\t" not in n, n)
    orch("resume", run2)
    orch("set", run2, "r2", "running")
    orch("error", run2, "r2", "--kind", "task", "--note", "bad output")
    t2 = {t["id"]: t for t in p2()["tasks"]}["r2"]
    check("task failure counts as an attempt", t2["status"] == "failed" and t2["attempts"] == 1)
    r = orch("policy", run2, "--mode", "auto", "--limit-wait", "120")
    check("policy command changes mode and values", "mode: auto" in r.stdout and "limit_wait_sec: 120" in r.stdout, r.stdout)
    check("status shows retry policy", "retry policy" in orch("status", run2).stdout)

    # misc commands
    check("status runs", orch("status", run).returncode == 0)
    check("stale runs", orch("stale", run, "--minutes", "0").returncode == 0)
    check("land runs", orch("land", run).returncode == 0)
    r = orch("cleanup", run, "--all")
    check("cleanup removes merged branches", r.returncode == 0 and "removed agent/w1" in r.stdout, r.stdout + r.stderr)
    check("unmerged branches are kept", "agent/w2" in sh("git branch", cwd=repo).stdout)

    print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
