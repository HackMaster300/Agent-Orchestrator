#!/usr/bin/env python3
"""PreToolUse hook: stop a writer agent from editing outside its write-set.

Active only inside a worktree prepared by `orch.py bootstrap` (it contains a
.orch-task marker). Everywhere else it does nothing, so it is safe to register
globally. Blocks by exiting with code 2 (stderr is shown to the agent).

Covers Edit/Write/MultiEdit/NotebookEdit. It cannot see shell redirections;
`orch.py check-writeset` / `integrate` catch those after the fact.
Any internal error fails open (exit 0) so a bug here never stops real work.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def main():
    data = json.load(sys.stdin)
    if data.get("tool_name") not in EDIT_TOOLS:
        return 0
    ti = data.get("tool_input") or {}
    target = ti.get("file_path") or ti.get("notebook_path")
    cwd = data.get("cwd") or os.getcwd()
    if not target:
        return 0
    top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True).stdout.strip()
    marker = os.path.join(top, ".orch-task") if top else ""
    if not marker or not os.path.exists(marker):
        return 0  # not an orchestrated worktree
    info = json.load(open(marker))
    from orch import effective_write_set, in_write_set, load  # noqa: E402
    plan = load(info["run_dir"])
    task = {t["id"]: t for t in plan["tasks"]}[info["task"]]

    target = os.path.realpath(target if os.path.isabs(target) else os.path.join(cwd, target))
    top_real = os.path.realpath(top)
    run_real = os.path.realpath(info["run_dir"])
    if target == run_real or target.startswith(run_real + os.sep):
        return 0  # results files etc. live in the run dir
    if not (target == top_real or target.startswith(top_real + os.sep)):
        print(f"[orchestrator] BLOCKED: {target} is outside your worktree ({top_real}). "
              f"Edit only files inside your own worktree.", file=sys.stderr)
        return 2
    rel = os.path.relpath(target, top_real)
    if in_write_set(rel, effective_write_set(task)):
        return 0
    print(f"[orchestrator] BLOCKED: {rel} is outside the write-set of task {task['id']} "
          f"{effective_write_set(task)}. Do not edit it. If it must change, stop and report it "
          f"under NEW_TASKS.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail open
        print(f"[orchestrator] guard error ignored: {e}", file=sys.stderr)
        sys.exit(0)
