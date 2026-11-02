#!/usr/bin/env python3
"""Run-state manager for the agent-orchestrator skill (stdlib only).

Run lifecycle
  init <run_dir> --goal TEXT [--mode checkpoint|auto] [--test-cmd CMD]
  validate <run_dir>
  next <run_dir>                      tasks that can be launched now
  set <run_dir> <id> <status> [--summary T] [--branch B]
  status <run_dir>
  stale <run_dir> [--minutes N]       running tasks that look dead
Infrastructure errors (rate/usage limits, proxy, network)
  error <run_dir> <id> --kind transient|rate_limit|task [--note T] [--wait-sec N]
  policy <run_dir> [--mode M] [--transient-wait N] [--limit-wait N] ...   show/change
  resume <run_dir>                    clear a halt after the user fixed the cause
Writer support
  bootstrap <run_dir> <id>            run INSIDE a writer's worktree (step 0)
  check-writeset <run_dir> <id>       did the writer stay inside its write-set?
  integrate <run_dir> [ids...]        merge finished writers into the integration branch
  merge-order <run_dir>               dry run of the merge order
  land <run_dir>                      summary + how to land the result
  cleanup <run_dir> [--all]           remove merged branches/worktrees
Plan editing (re-validates; refuses invalid plans unless --force)
  add <run_dir> --id ID --kind K --title T [--deps a,b] [--write-set g1,g2] [--model M]
  update <run_dir> ID [--deps ..] [--write-set ..] [--model ..] [--title ..]
  remove <run_dir> ID [--rewire]

The orchestrator is the single writer of plan.json.
"""
import argparse
import datetime
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile

KINDS = {"read", "synth", "write", "review"}
STATUSES = {"pending", "running", "waiting", "done", "failed", "blocked"}
DEFAULT_POLICY = {
    "transient_wait_sec": 30,     # first wait after a proxy/network/5xx error
    "backoff_factor": 2,          # each further transient error waits this much longer
    "max_wait_sec": 600,          # cap for one transient wait
    "limit_wait_sec": 900,        # wait after a rate/usage limit (unless a reset time is known)
    "max_infra_retries": 5,       # per task; does NOT consume the 2-failure task budget
    "max_total_wait_min": 180,    # total time the run may spend waiting
    "on_exhausted": "pause",      # pause | ask | skip
}
DEFAULT_PARALLEL = {"read": 6, "synth": 1, "write": 3, "review": 2}
MARKER = ".orch-task"


# ---------------------------------------------------------------- helpers
def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def run(cmd, cwd=None, shell=False):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=shell)


def git(args, cwd=None):
    return run(["git"] + args, cwd=cwd)


def die(msg):
    sys.exit(f"error: {msg}")


def plan_path(run_dir):
    return os.path.join(run_dir, "plan.json")


def load(run_dir):
    try:
        with open(plan_path(run_dir)) as f:
            return json.load(f)
    except FileNotFoundError:
        die(f"{plan_path(run_dir)} not found (run init first)")
    except json.JSONDecodeError as e:
        die(f"plan.json is not valid JSON: {e}")


def save(run_dir, plan):
    fd, tmp = tempfile.mkstemp(dir=run_dir, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(plan, f, indent=2)
        f.write("\n")
    os.replace(tmp, plan_path(run_dir))  # atomic


def policy_of(plan):
    return {**DEFAULT_POLICY, **plan.get("retry_policy", {})}


def parse_ts(x):
    return datetime.datetime.fromisoformat(x) if x else None


def in_future(x):
    d = parse_ts(x)
    return bool(d) and d > datetime.datetime.now(datetime.timezone.utc)


def by_id(plan):
    return {t["id"]: t for t in plan["tasks"]}


def csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def ancestors(tasks, tid):
    seen, stack = set(), list(tasks[tid].get("deps", []))
    while stack:
        d = stack.pop()
        if d in seen or d not in tasks:
            continue
        seen.add(d)
        stack.extend(tasks[d].get("deps", []))
    return seen


# ------------------------------------------------------- write-set matching
def glob_to_re(pattern):
    """Glob with ** support. A bare directory pattern also matches its contents."""
    p = pattern.rstrip("/")
    i, out = 0, ""
    while i < len(p):
        if p.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif p.startswith("**", i):
            out += ".*"
            i += 2
        elif p[i] == "*":
            out += "[^/]*"
            i += 1
        elif p[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    return re.compile("^" + out + "(?:/.*)?$")


def in_write_set(path, patterns):
    while path.startswith("./"):
        path = path[2:]
    return any(glob_to_re(p).match(path) for p in patterns)


def patterns_overlap(a, b):
    """Conservative heuristic used at planning time."""
    a, b = a.rstrip("/"), b.rstrip("/")
    if a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    base_a = a.split("*")[0].rstrip("/")
    base_b = b.split("*")[0].rstrip("/")
    for x, y in ((base_a, base_b), (base_b, base_a)):
        if x == "" or y == x or y.startswith(x + "/"):
            return True
    return False


def sets_overlap(sa, sb):
    return any(patterns_overlap(a, b) for a in sa for b in sb)


def concurrent(tasks, a, b):
    return b not in ancestors(tasks, a) and a not in ancestors(tasks, b)


# ------------------------------------------------------------- plan checks
def check_plan(plan, complete=True):
    """complete=False skips whole-plan rules (gate/review) so a plan can be built step by step."""
    errors, warnings = [], []
    tasks_list = plan.get("tasks", [])
    ids = [t.get("id") for t in tasks_list]
    if len(ids) != len(set(ids)):
        errors.append("duplicate task ids")
    tasks = by_id(plan)

    for t in tasks_list:
        tid = t.get("id", "?")
        if t.get("kind") not in KINDS:
            errors.append(f"{tid}: kind must be one of {sorted(KINDS)}")
        if t.get("status", "pending") not in STATUSES:
            errors.append(f"{tid}: bad status {t.get('status')}")
        for d in t.get("deps", []):
            if d not in tasks:
                errors.append(f"{tid}: unknown dependency '{d}'")
        if t.get("kind") == "write" and not t.get("write_set"):
            errors.append(f"{tid}: write task needs a non-empty write_set")
        if t.get("kind") != "write" and t.get("write_set"):
            errors.append(f"{tid}: only write tasks may declare a write_set")
        if not t.get("title"):
            warnings.append(f"{tid}: missing title")

    indeg = {i: 0 for i in tasks}
    for t in tasks_list:
        for d in t.get("deps", []):
            if d in tasks:
                indeg[t["id"]] += 1
    queue = [i for i, n in indeg.items() if n == 0]
    visited = 0
    while queue:
        n = queue.pop()
        visited += 1
        for t in tasks_list:
            if n in t.get("deps", []):
                indeg[t["id"]] -= 1
                if indeg[t["id"]] == 0:
                    queue.append(t["id"])
    if visited != len(tasks):
        errors.append("dependency cycle detected")

    if not errors:
        writers = [t for t in tasks_list if t.get("kind") == "write"]
        for i, a in enumerate(writers):
            for b in writers[i + 1:]:
                if concurrent(tasks, a["id"], b["id"]) and sets_overlap(a["write_set"], b["write_set"]):
                    errors.append(
                        f"write-set overlap between concurrent writers {a['id']} and {b['id']}: "
                        f"add a dependency or split the files"
                    )
        has_read = any(t.get("kind") == "read" for t in tasks_list)
        synths = {t["id"] for t in tasks_list if t.get("kind") == "synth"}
        if has_read and complete:
            if not synths:
                errors.append("research tasks exist but there is no synth task (the gate)")
            else:
                for w in writers:
                    if not (ancestors(tasks, w["id"]) & synths):
                        errors.append(f"{w['id']}: writer does not depend on a synth task")
        if complete and tasks_list and not any(t.get("kind") == "review" for t in tasks_list):
            warnings.append("no review task planned")
        if writers and not plan.get("base_sha"):
            warnings.append("base_sha unset: worktrees cannot be pinned")
        if complete and writers and not plan.get("test_cmd"):
            warnings.append("no test_cmd set: integration cannot run tests between merges")
    return errors, warnings


def print_check(errors, warnings):
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"ERROR: {e}")


# ------------------------------------------------------------------ init
POLICY_FLAGS = {  # cli attribute -> policy key
    "transient_wait": "transient_wait_sec", "limit_wait": "limit_wait_sec",
    "max_retries": "max_infra_retries", "max_total_wait": "max_total_wait_min",
    "max_wait": "max_wait_sec", "backoff": "backoff_factor", "on_exhausted": "on_exhausted",
}


def policy_from_args(args, base=None):
    pol = dict(base if base is not None else DEFAULT_POLICY)
    for attr, key in POLICY_FLAGS.items():
        v = getattr(args, attr, None)
        if v is not None:
            pol[key] = v
    return pol


def cmd_init(args):
    run_dir = os.path.abspath(args.run_dir)
    if os.path.exists(plan_path(run_dir)):
        die("plan.json already exists; refusing to overwrite")
    os.makedirs(run_dir, exist_ok=True)
    for sub in ("findings", "results", "briefs"):
        os.makedirs(os.path.join(run_dir, sub), exist_ok=True)
    sha = None
    r = git(["rev-parse", "HEAD"], cwd=run_dir)
    if r.returncode == 0:
        sha = r.stdout.strip()
    else:
        print("warning: not a git repo or no commits; base_sha unset (write lane needs git)")
    run_id = os.path.basename(run_dir.rstrip("/"))
    plan = {
        "goal": args.goal,
        "mode": args.mode,
        "run_id": run_id,
        "run_dir": run_dir,
        "base_sha": sha,
        "integration_branch": f"orch/{run_id}" if sha else None,
        "integration_dir": os.path.join(run_dir, "integration") if sha else None,
        "test_cmd": args.test_cmd,
        "retry_policy": policy_from_args(args),
        "waited_total_sec": 0,
        "paused_until": None,
        "halted": None,
        "max_parallel": dict(DEFAULT_PARALLEL),
        "tasks": [],
    }
    if sha:
        if git(["status", "--porcelain"], cwd=run_dir).stdout.strip():
            print("warning: working tree has uncommitted changes; agents will not see them")
        r = git(["branch", plan["integration_branch"], sha], cwd=run_dir)
        if r.returncode:
            die(f"cannot create branch {plan['integration_branch']}: {r.stderr.strip()}")
        r = git(["worktree", "add", plan["integration_dir"], plan["integration_branch"]], cwd=run_dir)
        if r.returncode:
            die(f"cannot create integration worktree: {r.stderr.strip()}")
    save(run_dir, plan)
    print(f"initialized {run_dir}\n  base_sha={sha}\n  integration={plan['integration_branch']} at {plan['integration_dir']}")


# -------------------------------------------------------------- validate
def cmd_validate(args):
    plan = load(args.run_dir)
    errors, warnings = check_plan(plan)
    print_check(errors, warnings)
    if errors:
        sys.exit(1)
    counts = {}
    for t in plan["tasks"]:
        counts[t["kind"]] = counts.get(t["kind"], 0) + 1
    print(f"plan OK: {len(plan['tasks'])} tasks {counts}")


# ------------------------------------------------------------------ next
def dep_satisfied(tasks, d):
    t = tasks[d]
    if t.get("status") != "done":
        return False
    # a writer's output only exists for others once merged into the integration branch
    return t["kind"] != "write" or t.get("merged", False)


def cmd_next(args):
    plan = load(args.run_dir)
    tasks = by_id(plan)
    if plan.get("halted"):
        print(f"HALTED: {plan['halted']}\nNothing will launch until the cause is fixed and you run `resume`.")
        return
    if in_future(plan.get("paused_until")):
        left = int((parse_ts(plan["paused_until"]) - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
        print(f"PAUSED until {plan['paused_until']} ({left}s left): infrastructure back-off in effect. "
              f"Wait, then call `next` again.")
        return
    limits = {**DEFAULT_PARALLEL, **plan.get("max_parallel", {})}
    running = [t for t in plan["tasks"] if t.get("status") == "running"]
    slots = dict(limits)
    for t in running:
        slots[t["kind"]] = slots.get(t["kind"], 0) - 1
    launch = []
    busy = [t["write_set"] for t in running if t["kind"] == "write"]
    for t in plan["tasks"]:
        st = t.get("status", "pending")
        if st not in ("pending", "waiting"):
            continue
        if st == "waiting" and in_future(t.get("next_retry_at")):
            continue
        if not all(dep_satisfied(tasks, d) for d in t.get("deps", [])):
            continue
        if slots.get(t["kind"], 0) <= 0:
            continue
        if t["kind"] == "write":
            if any(sets_overlap(t["write_set"], ws) for ws in busy):
                continue
            busy.append(t["write_set"])
        slots[t["kind"]] -= 1
        launch.append(t)
    if not launch:
        pending = [t for t in plan["tasks"] if t.get("status", "pending") in ("pending", "waiting")]
        awaiting = [t["id"] for t in plan["tasks"]
                    if t["kind"] == "write" and t.get("status") == "done" and not t.get("merged")]
        if awaiting and not running:
            print(f"nothing ready: finished writers not yet merged ({', '.join(awaiting)}); run `integrate`")
            return
        blocked = [t["id"] for t in pending
                   if any(tasks[d].get("status") in ("failed", "blocked") for d in t.get("deps", []))]
        if blocked and not running:
            print(f"BLOCKED by failed/blocked dependencies: {', '.join(blocked)}")
            return
        print("nothing ready" + (" (agents still running)" if running else ""))
        return
    for t in launch:
        extra = f" write_set={t['write_set']}" if t["kind"] == "write" else ""
        print(f"{t['id']}\t{t['kind']}\t{t.get('model', '-')}\t{t.get('title', '')}{extra}")
    if any(t["kind"] == "write" for t in launch):
        print("# for writers: run `set <id> running` first; it records the start_sha they must use")


# ------------------------------------------------------------------- set
def integration_tip(plan):
    if not plan.get("integration_branch"):
        return None
    r = git(["rev-parse", plan["integration_branch"]], cwd=plan["run_dir"])
    return r.stdout.strip() if r.returncode == 0 else None


def cmd_set(args):
    run_dir = os.path.abspath(args.run_dir)
    plan = load(run_dir)
    tasks = by_id(plan)
    if args.task_id not in tasks:
        die(f"unknown task '{args.task_id}'")
    if args.status not in STATUSES:
        die(f"status must be one of {sorted(STATUSES)}")
    t = tasks[args.task_id]
    t["status"] = args.status
    if args.status == "running":
        t.pop("next_retry_at", None)
        t["started_at"] = now()
        t["attempts"] = t.get("attempts", 0)
        if t["kind"] == "write":
            t["start_sha"] = integration_tip(plan) or plan.get("base_sha")
            print(f"start_sha={t['start_sha']}")
    elif args.status == "failed":
        t["attempts"] = t.get("attempts", 0) + 1
        if t["attempts"] >= 2:
            print(f"note: {t['id']} has failed {t['attempts']} times; stop retrying and escalate")
    elif args.status == "done":
        t["infra_retries"] = 0
        t.pop("last_error", None)
    elif args.status == "pending":
        for k in ("start_sha", "branch", "merged", "merge_note", "started_at", "next_retry_at", "infra_retries", "last_error"):
            t.pop(k, None)
    if args.summary:
        t["summary"] = args.summary
    if args.branch:
        t["branch"] = args.branch
    if args.status == "done" and t["kind"] == "write" and not t.get("branch"):
        print("warning: writer marked done without --branch; it cannot be merged")
    save(run_dir, plan)
    if args.status == "done" and t["kind"] == "read":
        with open(os.path.join(run_dir, "findings", "INDEX.md"), "a") as f:
            f.write(f"- {t['id']}: {t.get('title', '')} -> {t.get('output', 'findings/' + t['id'] + '.md')}"
                    f" | {t.get('summary', '')}\n")
    print(f"{t['id']} -> {t['status']}")


# ---------------------------------------------------------------- status
def cmd_status(args):
    plan = load(args.run_dir)
    print(f"goal: {plan.get('goal')}")
    print(f"mode: {plan.get('mode')}  base_sha: {plan.get('base_sha')}")
    tip = integration_tip(plan)
    if tip:
        print(f"integration: {plan['integration_branch']} @ {tip[:10]}")
    if plan.get("halted"):
        print(f"HALTED: {plan['halted']}")
    elif in_future(plan.get("paused_until")):
        print(f"PAUSED until {plan['paused_until']}")
    pol = policy_of(plan)
    print(f"retry policy: transient {pol['transient_wait_sec']}s x{pol['backoff_factor']} (max {pol['max_wait_sec']}s), "
          f"limit {pol['limit_wait_sec']}s, max {pol['max_infra_retries']} infra retries/task, "
          f"total wait cap {pol['max_total_wait_min']} min (used {int(plan.get('waited_total_sec', 0)/60)}), "
          f"on exhausted: {pol['on_exhausted']}")
    counts = {}
    for t in plan["tasks"]:
        s = t.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1
    print("totals: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) + "\n")
    print(f"{'ID':<7}{'KIND':<8}{'STATUS':<9}{'TRY':<4}{'MRG':<4}{'DEPS':<13}{'BRANCH/NOTE'}")
    for t in plan["tasks"]:
        note = t.get("merge_note") or t.get("branch") or (t.get("summary", "")[:48])
        if t.get("status") == "waiting":
            note = f"retry at {(t.get('next_retry_at') or '')[11:19]}Z | {t.get('last_error', '')[:28]}"
        mrg = "y" if t.get("merged") else ("-" if t["kind"] != "write" else "n")
        print(f"{t['id']:<7}{t['kind']:<8}{t.get('status', 'pending'):<9}{t.get('attempts', 0):<4}"
              f"{mrg:<4}{','.join(t.get('deps', [])) or '-':<13}{note}")


def cmd_stale(args):
    plan = load(args.run_dir)
    out = []
    for t in plan["tasks"]:
        if t.get("status") != "running" or not t.get("started_at"):
            continue
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(t["started_at"])).total_seconds() / 60
        if age < args.minutes:
            continue
        default_out = ("findings" if t["kind"] == "read" else "results") + f"/{t['id']}.md"
        outp = os.path.join(plan["run_dir"], t.get("output", default_out))
        out.append((t["id"], int(age), os.path.exists(outp)))
    if not out:
        print(f"no running task older than {args.minutes} min")
    for tid, age, has in out:
        hint = "output file exists: inspect it, may have finished" if has else "no output file: probably dead, relaunch"
        print(f"{tid}\trunning {age} min\t{hint}")


# ------------------------------------------------- infrastructure errors
def cmd_error(args):
    run_dir = os.path.abspath(args.run_dir)
    plan = load(run_dir)
    t = by_id(plan).get(args.task_id) or die(f"unknown task '{args.task_id}'")
    if args.kind == "task":  # the task itself failed: counts against its 2-attempt budget
        args.status, args.summary, args.branch = "failed", args.note, None
        return cmd_set(args)
    pol = policy_of(plan)
    t["infra_retries"] = t.get("infra_retries", 0) + 1
    t["last_error"] = f"{args.kind}: {args.note or ''}".strip()
    t.pop("started_at", None)
    if args.kind == "rate_limit":
        wait = args.wait_sec if args.wait_sec is not None else pol["limit_wait_sec"]
    else:
        wait = min(pol["transient_wait_sec"] * pol["backoff_factor"] ** (t["infra_retries"] - 1), pol["max_wait_sec"])
    total = plan.get("waited_total_sec", 0) + wait
    exhausted = t["infra_retries"] > pol["max_infra_retries"] or total > pol["max_total_wait_min"] * 60
    if exhausted:
        action = pol["on_exhausted"]
        t["status"] = "blocked"
        reason = (f"{t['id']}: retry budget exhausted after {t['infra_retries']} infra errors, "
                  f"{int(total/60)} min of waiting. Last error: {t['last_error']}")
        if action == "skip":
            print(f"EXHAUSTED (skip): {reason}\nTask marked blocked; continue with independent tasks and report it at the end.")
        else:
            plan["halted"] = reason
            print(f"EXHAUSTED ({action}): {reason}\nRun HALTED. Tell the user exactly what happened and "
                  f"{'ask whether to resume, change the policy, or stop' if action == 'ask' else 'how to resume (`resume`)'}.")
    else:
        t["status"] = "waiting"
        until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=wait)
        t["next_retry_at"] = until.isoformat(timespec="seconds")
        # limits and proxy outages affect every agent, so back off the whole run
        cur = parse_ts(plan.get("paused_until"))
        if not cur or until > cur:
            plan["paused_until"] = t["next_retry_at"]
        plan["waited_total_sec"] = total
        print(f"{t['id']}: {args.kind} error #{t['infra_retries']}; run paused for {wait}s "
              f"(until {t['next_retry_at']}). Task attempts untouched ({t.get('attempts', 0)}).")
    save(run_dir, plan)


def cmd_policy(args):
    run_dir = os.path.abspath(args.run_dir)
    plan = load(run_dir)
    changed = False
    if args.mode:
        plan["mode"] = args.mode
        changed = True
    new = policy_from_args(args, base=policy_of(plan))
    if new != policy_of(plan):
        plan["retry_policy"] = new
        changed = True
    if changed:
        save(run_dir, plan)
    print(f"mode: {plan['mode']}")
    for k, v in policy_of(plan).items():
        print(f"  {k}: {v}")


def cmd_resume(args):
    run_dir = os.path.abspath(args.run_dir)
    plan = load(run_dir)
    plan["halted"] = None
    plan["paused_until"] = None
    plan["waited_total_sec"] = 0
    n = 0
    for t in plan["tasks"]:
        if t.get("status") in ("blocked", "waiting") and t.get("last_error"):
            t["status"] = "pending"
            t["infra_retries"] = 0
            t.pop("next_retry_at", None)
            n += 1
    save(run_dir, plan)
    print(f"resumed; {n} task(s) back to pending")


# --------------------------------------------------- writer-side commands
def cmd_bootstrap(args):
    run_dir = os.path.abspath(args.run_dir)
    plan = load(run_dir)
    t = by_id(plan).get(args.task_id) or die(f"unknown task '{args.task_id}'")
    start = t.get("start_sha")
    if not start:
        die(f"{t['id']} has no start_sha; the orchestrator must run `set {t['id']} running` first")
    head = git(["rev-parse", "HEAD"]).stdout.strip()
    if head != start:
        r = git(["reset", "--hard", start])
        if r.returncode:
            die(f"cannot reset to {start}: {r.stderr.strip()}")
    top = git(["rev-parse", "--show-toplevel"]).stdout.strip()
    with open(os.path.join(top, MARKER), "w") as f:
        json.dump({"run_dir": run_dir, "task": t["id"]}, f)
    exclude = git(["rev-parse", "--git-path", "info/exclude"]).stdout.strip()
    exclude = exclude if os.path.isabs(exclude) else os.path.join(top, exclude)
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    existing = open(exclude).read() if os.path.exists(exclude) else ""
    if MARKER not in existing:
        with open(exclude, "a") as f:
            f.write(f"\n{MARKER}\n")
    br = git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    print(f"ready: task={t['id']} start_sha={start[:10]} branch={br}\nwrite_set={t['write_set']}")


def writeset_violations(plan, t):
    base = t.get("start_sha") or plan.get("base_sha")
    cwd = plan["integration_dir"] if plan.get("integration_dir") and os.path.isdir(plan["integration_dir"]) else plan["run_dir"]
    r = git(["diff", "--name-only", f"{base}..{t['branch']}"], cwd=cwd)
    if r.returncode:
        die(f"git diff failed for {t['id']}: {r.stderr.strip()}")
    files = [f for f in r.stdout.splitlines() if f]
    return files, [f for f in files if not in_write_set(f, t["write_set"])]


def cmd_check_writeset(args):
    plan = load(args.run_dir)
    t = by_id(plan).get(args.task_id) or die(f"unknown task '{args.task_id}'")
    if t["kind"] != "write" or not t.get("branch"):
        die("task is not a writer with a recorded branch")
    files, bad = writeset_violations(plan, t)
    print(f"{t['id']}: {len(files)} files changed")
    if bad:
        print("OUTSIDE write-set:\n  " + "\n  ".join(bad))
        sys.exit(1)
    print("within write-set")


# ------------------------------------------------------------- integrate
def merge_sequence(plan, only=None):
    tasks = by_id(plan)
    cand = [t for t in plan["tasks"] if t["kind"] == "write" and t.get("status") == "done"
            and not t.get("merged") and (not only or t["id"] in only)]
    ordered, seen = [], set()

    def visit(tid):
        if tid in seen or tid not in tasks:
            return
        seen.add(tid)
        for d in tasks[tid].get("deps", []):
            visit(d)
        if tasks[tid] in cand:
            ordered.append(tasks[tid])

    for t in cand:
        visit(t["id"])
    return ordered


def cmd_merge_order(args):
    plan = load(args.run_dir)
    seq = merge_sequence(plan)
    if not seq:
        print("no finished, unmerged writers")
    for i, t in enumerate(seq, 1):
        print(f"{i}. {t['id']}\t{t.get('branch', '(no branch)')}\t{t.get('title', '')}")


def cmd_integrate(args):
    run_dir = os.path.abspath(args.run_dir)
    plan = load(run_dir)
    idir = plan.get("integration_dir")
    if not idir or not os.path.isdir(idir):
        die("no integration worktree; was this run initialised inside a git repo?")
    seq = merge_sequence(plan, set(args.ids) if args.ids else None)
    if not seq:
        print("nothing to integrate")
        return
    problems = 0
    for t in seq:
        tid = t["id"]
        if not t.get("branch"):
            t["merge_note"] = "no branch recorded"
            print(f"[{tid}] SKIP: no branch recorded")
            problems += 1
            continue
        files, bad = writeset_violations(plan, t)
        if bad:
            t["merge_note"] = "write-set violation: " + ", ".join(bad[:3])
            print(f"[{tid}] REJECTED: touched files outside write-set:\n    " + "\n    ".join(bad))
            problems += 1
            continue
        pre = git(["rev-parse", "HEAD"], cwd=idir).stdout.strip()
        r = git(["merge", "--no-ff", "-m", f"orch: merge {tid} {t.get('title', '')}".strip(), t["branch"]], cwd=idir)
        if r.returncode:
            conflicts = git(["diff", "--name-only", "--diff-filter=U"], cwd=idir).stdout.split()
            git(["merge", "--abort"], cwd=idir)
            t["merge_note"] = "conflict: " + ", ".join(conflicts[:3]) if conflicts else "merge failed"
            print(f"[{tid}] CONFLICT in {conflicts or r.stderr.strip()}; merge aborted. "
                  f"The write-set was probably wrong. Resolve manually or re-scope.")
            problems += 1
            continue
        if plan.get("test_cmd"):
            tr = run(plan["test_cmd"], cwd=idir, shell=True)
            if tr.returncode:
                git(["reset", "--hard", pre], cwd=idir)
                tail = (tr.stdout + tr.stderr).strip().splitlines()[-8:]
                t["merge_note"] = "tests failed after merge (reverted)"
                print(f"[{tid}] TESTS FAILED, merge reverted. Last output:\n    " + "\n    ".join(tail))
                problems += 1
                continue
        t["merged"] = True
        t["merge_sha"] = git(["rev-parse", "HEAD"], cwd=idir).stdout.strip()
        t.pop("merge_note", None)
        print(f"[{tid}] merged ({len(files)} files) -> {t['merge_sha'][:10]}")
    save(run_dir, plan)
    print(f"integration tip: {integration_tip(plan)[:10]}")
    if problems:
        print(f"{problems} writer(s) not merged; see `status`")
        sys.exit(1)


# ------------------------------------------------------- land / cleanup
def cmd_land(args):
    plan = load(args.run_dir)
    unmerged = [t["id"] for t in plan["tasks"] if t["kind"] == "write" and not t.get("merged")]
    if unmerged:
        print(f"warning: writers not merged: {', '.join(unmerged)}")
    ib = plan["integration_branch"]
    print(git(["diff", "--stat", f"{plan['base_sha']}..{ib}"], cwd=plan["run_dir"]).stdout)
    print(f"Nothing has touched your working branch. To land the result:\n"
          f"  git merge {ib}        # or open a PR from {ib}\n"
          f"Then: python orch.py cleanup {plan['run_dir']} --all")


def worktrees(cwd):
    out, cur = [], {}
    for line in git(["worktree", "list", "--porcelain"], cwd=cwd).stdout.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
            out.append(cur)
        elif line.startswith("branch refs/heads/") and cur:
            cur["branch"] = line[len("branch refs/heads/"):]
    return out


def cmd_cleanup(args):
    plan = load(args.run_dir)
    cwd = plan["run_dir"]
    wts = {w.get("branch"): w["path"] for w in worktrees(cwd)}
    for t in plan["tasks"]:
        b = t.get("branch")
        if not (b and t.get("merged")):
            continue
        if b in wts:
            git(["worktree", "remove", "--force", wts[b]], cwd=cwd)
        r = git(["branch", "-D", b], cwd=cwd)
        print(f"{t['id']}: removed {b}" if r.returncode == 0 else f"{t['id']}: {r.stderr.strip()}")
    if args.all and plan.get("integration_dir") and os.path.isdir(plan["integration_dir"]):
        git(["worktree", "remove", "--force", plan["integration_dir"]], cwd=cwd)
        print(f"removed integration worktree (branch {plan['integration_branch']} kept until you delete it)")
    git(["worktree", "prune"], cwd=cwd)
    left = [t["id"] for t in plan["tasks"] if t.get("branch") and not t.get("merged")]
    if left:
        print(f"kept branches of unmerged writers: {', '.join(left)}")


# ------------------------------------------------------------ plan edits
def commit_edit(run_dir, plan, force):
    # while nothing has started the plan may be incomplete; once running, enforce every rule
    started = any(t.get("status", "pending") != "pending" for t in plan["tasks"])
    errors, warnings = check_plan(plan, complete=started)
    print_check(errors, warnings)
    if errors and not force:
        print("plan NOT changed (use --force to save anyway)")
        sys.exit(1)
    save(run_dir, plan)
    print("plan updated")


def cmd_add(args):
    run_dir = os.path.abspath(args.run_dir)
    plan = load(run_dir)
    if args.id in by_id(plan):
        die(f"task '{args.id}' already exists")
    t = {"id": args.id, "kind": args.kind, "title": args.title, "deps": csv(args.deps), "status": "pending"}
    if args.model:
        t["model"] = args.model
    if csv(args.write_set):
        t["write_set"] = csv(args.write_set)
    plan["tasks"].append(t)
    commit_edit(run_dir, plan, args.force)


def cmd_update(args):
    run_dir = os.path.abspath(args.run_dir)
    plan = load(run_dir)
    t = by_id(plan).get(args.id) or die(f"unknown task '{args.id}'")
    if t.get("status") in ("running", "done") and not args.force:
        die(f"{args.id} is {t['status']}; editing it is meaningless (use `set ... pending` to redo, or --force)")
    if args.deps is not None:
        t["deps"] = csv(args.deps)
    if args.write_set is not None:
        t["write_set"] = csv(args.write_set)
    if args.model:
        t["model"] = args.model
    if args.title:
        t["title"] = args.title
    commit_edit(run_dir, plan, args.force)


def cmd_remove(args):
    run_dir = os.path.abspath(args.run_dir)
    plan = load(run_dir)
    t = by_id(plan).get(args.id) or die(f"unknown task '{args.id}'")
    if t.get("status") in ("running", "done") and not args.force:
        die(f"{args.id} is {t['status']}; refusing to remove (use --force)")
    dependents = [x for x in plan["tasks"] if args.id in x.get("deps", [])]
    if dependents and not args.rewire:
        die(f"{', '.join(x['id'] for x in dependents)} depend on {args.id}; use --rewire to inherit its deps")
    for x in dependents:
        x["deps"] = [d for d in x["deps"] if d != args.id] + [d for d in t.get("deps", []) if d not in x["deps"]]
    plan["tasks"] = [x for x in plan["tasks"] if x["id"] != args.id]
    commit_edit(run_dir, plan, args.force)


# ------------------------------------------------------------------ main
def add_policy_flags(s):
    s.add_argument("--transient-wait", dest="transient_wait", type=int, help="seconds before 1st retry after proxy/network error")
    s.add_argument("--backoff", type=float, help="multiplier per further transient error")
    s.add_argument("--max-wait", dest="max_wait", type=int, help="cap (s) for one transient wait")
    s.add_argument("--limit-wait", dest="limit_wait", type=int, help="seconds to wait after a rate/usage limit")
    s.add_argument("--max-retries", dest="max_retries", type=int, help="infra retries per task")
    s.add_argument("--max-total-wait", dest="max_total_wait", type=int, help="total minutes the run may wait")
    s.add_argument("--on-exhausted", dest="on_exhausted", choices=["pause", "ask", "skip"])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def sp(name, fn, *positional):
        s = sub.add_parser(name)
        s.add_argument("run_dir")
        for a in positional:
            s.add_argument(a)
        s.set_defaults(fn=fn)
        return s

    s = sp("init", cmd_init)
    s.add_argument("--goal", required=True)
    s.add_argument("--mode", choices=["checkpoint", "auto"], default="checkpoint")
    s.add_argument("--test-cmd", dest="test_cmd")
    add_policy_flags(s)
    sp("validate", cmd_validate)
    sp("next", cmd_next)
    sp("status", cmd_status)
    sp("merge-order", cmd_merge_order)
    sp("land", cmd_land)
    s = sp("stale", cmd_stale)
    s.add_argument("--minutes", type=int, default=30)
    s = sp("set", cmd_set, "task_id", "status")
    s.add_argument("--summary")
    s.add_argument("--branch")
    s = sp("error", cmd_error, "task_id")
    s.add_argument("--kind", required=True, choices=["transient", "rate_limit", "task"])
    s.add_argument("--note")
    s.add_argument("--wait-sec", dest="wait_sec", type=int)
    s = sp("policy", cmd_policy)
    s.add_argument("--mode", choices=["checkpoint", "auto"])
    add_policy_flags(s)
    sp("resume", cmd_resume)
    sp("bootstrap", cmd_bootstrap, "task_id")
    sp("check-writeset", cmd_check_writeset, "task_id")
    s = sp("integrate", cmd_integrate)
    s.add_argument("ids", nargs="*")
    s = sp("cleanup", cmd_cleanup)
    s.add_argument("--all", action="store_true")
    s = sp("add", cmd_add)
    for a in ("--id", "--kind", "--title"):
        s.add_argument(a, required=True)
    s.add_argument("--deps")
    s.add_argument("--write-set", dest="write_set")
    s.add_argument("--model")
    s.add_argument("--force", action="store_true")
    s = sp("update", cmd_update, "id")
    s.add_argument("--deps")
    s.add_argument("--write-set", dest="write_set")
    s.add_argument("--model")
    s.add_argument("--title")
    s.add_argument("--force", action="store_true")
    s = sp("remove", cmd_remove, "id")
    s.add_argument("--rewire", action="store_true")
    s.add_argument("--force", action="store_true")
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
