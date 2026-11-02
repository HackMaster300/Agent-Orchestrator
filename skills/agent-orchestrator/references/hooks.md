# Hooks (optional hardening)

The prose rules in SKILL.md depend on the orchestrator model following them. Hooks add enforcement that does not depend on anyone's memory.

## writeset_guard (included)

`scripts/hooks/writeset_guard.py` is a `PreToolUse` hook. In a worktree prepared by `orch.py bootstrap` (marked by an untracked `.orch-task` file) it blocks `Edit`, `Write`, `MultiEdit` and `NotebookEdit` calls that target:
- a file outside the task's `write_set`, or
- a path outside the agent's own worktree (this stops a writer from editing your main checkout), except files under the run directory, where results are written.

Everywhere else it does nothing, so it is safe to register globally. Any internal error fails open.

Register it in `.claude/settings.json` (project) or `~/.claude/settings.json` (user):
```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /ABSOLUTE/PATH/TO/agent-orchestrator/scripts/hooks/writeset_guard.py"
          }
        ]
      }
    ]
  }
}
```
Use the absolute path to wherever the skill is installed (for example `~/.claude/skills/agent-orchestrator/...`). Do not rely on `$CLAUDE_PROJECT_DIR`: inside a worktree it may point at the worktree rather than your project.

Behavior: exit code 2 blocks the tool call and feeds stderr back to the agent. Test it by hand:
```bash
echo '{"cwd":"<worktree>","tool_name":"Write","tool_input":{"file_path":"<worktree>/forbidden/x"}}' \
  | python3 scripts/hooks/writeset_guard.py; echo "exit=$?"
```

## Limits

- It does not see shell redirections (`echo x > file`, `sed -i`). `orch.py integrate` runs `check-writeset` on every branch before merging, which catches those afterwards.
- It is tested against simulated payloads (`scripts/selftest.py`), not yet against a live Claude Code session. Run experiment E6 in `verify-assumptions.md`.

## Not included, and why

A `SubagentStop` hook that records completion automatically looks attractive, but the hook payload does not carry our task id, so it could not update the right task reliably. Completion is recorded by the orchestrator from the agent's return message. `orch.py stale` covers agents that died silently.
